"""Operator inputs owned by the existing goal; native requests own delivery.

No process or scheduler is started here. Immutable inputs survive controller
recovery; status is projected from the native reservation/receipt, not an ack DB.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from concurrent.futures import ThreadPoolExecutor
import queue
import time
import re
from threading import Lock

_live = ContextVar('foundry_live_manager', default=None)


def protocol_markers(text):
    """Recognize leading protocol markers, excluding fenced examples."""
    markers, fence = set(), None
    for line in text.splitlines():
        stripped = line.strip()
        # Strip container prefixes before fence recognition as well as markers.
        leading = re.sub(r'^(?:>\s*|(?:[-+*]|[0-9]+[.)])\s+)+', '', stripped)
        delimiter = re.match(r'^(`{3,}|~{3,})(.*)$', leading)
        if fence is not None:
            if (delimiter and delimiter[1][0] == fence[0]
                    and len(delimiter[1]) >= len(fence) and not delimiter[2].strip()):
                fence = None
            continue
        if delimiter:
            token, info = delimiter.groups()
            # A backtick in an info string is inline code, not an opener.
            if token[0] != '`' or '`' not in info:
                fence = token
            continue
        leading = re.sub(r'^(?:\*{1,2}|_{1,2})', '', leading)
        match = re.match(r'^(FOUNDRY_MANAGER_QUESTION|FOUNDRY_DELIVERABLE_DONE)(?=\s|$|[^\w\s])', leading)
        if match:
            markers.add(match[1])
    return markers


@contextmanager
def live_scope(decide, *, capture=None, claude=False, undelivered=None):
    token = _live.set((decide, capture, claude, undelivered))
    try:
        yield
    finally:
        _live.reset(token)


class LiveEvents:
    """One serialized manager conversation, independent of worker supervision.

    Dispatch joins it before returning ownership of the goal manifest.
    """
    def __init__(self, decide=None, threshold=None):
        from ._context_signals import CompactionResidual
        inherited, self.capture, self.claude_enabled, self.undelivered = _live.get() or (None, None, False, None)
        self.decide = decide if decide is not None else inherited
        self.events = queue.Queue()
        self.event_lock = Lock()
        self.seen = set()
        self.queued_messages = set()
        self.pending_messages = set()
        self.delivering_messages = set()
        self.evidence_generation = 0
        self.observed_items = set()
        self.wait_states = {}
        self.executor = None
        self.pending = None
        self.pending_rows = []
        self.residual = CompactionResidual(threshold)

    def observe(self, event, root):
        if self.decide is None:
            return
        sample = self.residual.observe(event)
        if sample:
            # Residual belongs to the measured thread, never to a convenient
            # replacement member. Keep the existing episode/re-arm owner.
            role = 'root' if sample['member_id'] == root else 'member' if root else 'unknown'
            sample.update(measured_thread_id=sample['member_id'], thread_role=role,
                          context_pressure_high=sample['renewal_recommended'],
                          member_renewal_recommended=role == 'member' and sample['renewal_recommended'],
                          renewal_target=sample['member_id'] if role == 'member' and sample['renewal_recommended'] else None)
            sample['renewal_recommended'] = sample['member_renewal_recommended']
        if sample and sample['renewal_signal']:
            self.events.put({'kind': 'context_renewal', **sample})
        params = event.get('params') or {}
        item = params.get('item') or {}
        # Completed tools/member results or operator input may change the answer
        # even if the next question has identical text. Heartbeats, reasoning and
        # compaction alone do not. Unknown completed item types conservatively
        # count as evidence; this is not a whitelist of allowed task progress.
        if (event.get('method') == 'item/completed' and isinstance(item.get('id'), str)
                and item.get('type') not in ('agentMessage', 'reasoning', 'contextCompaction')):
            key = (params.get('threadId'), params.get('turnId'), item['id'])
            with self.event_lock:
                if key not in self.observed_items:
                    self.observed_items.add(key)
                    from ._context_signals import wait_result
                    waiting = (item.get('type') == 'collabAgentToolCall' and item.get('tool') == 'wait'
                               or item.get('type') == 'toolResult' and item.get('tool') == 'TaskOutput')
                    result = wait_result(item if item.get('type') == 'collabAgentToolCall'
                                         else item.get('result')) if waiting else None
                    scope = (params.get('threadId'), params.get('turnId'),
                             tuple(sorted(item.get('receiverThreadIds') or [])))
                    if result is None:
                        self.evidence_generation += 1
                    elif result['state'] == 'observed':
                        if self.wait_states.get(scope) != result['signature']:
                            self.wait_states[scope] = result['signature']
                            self.evidence_generation += 1
        if (event.get('method') != 'item/completed' or params.get('threadId') != root
                or item.get('type') != 'agentMessage' or not isinstance(item.get('id'), str)):
            return sample
        text = item.get('text') or ''
        # Explicit semantic markers prevent a steer -> acknowledgement -> paid
        # manager-call feedback loop. Other commentary remains in messages.jsonl.
        if not item.get('questions') and not protocol_markers(text):
            return sample
        key = (root, params.get('turnId'), item['id'])
        with self.event_lock:
            if key not in self.seen:
                self.seen.add(key)
                message_key = (root, params.get('turnId'), self.evidence_generation, text,
                               json.dumps(item.get('questions'), sort_keys=True, ensure_ascii=False))
                if (message_key in self.queued_messages or message_key in self.pending_messages
                        or message_key in self.delivering_messages):
                    return sample
                self.queued_messages.add(message_key)
                self.events.put({'kind': 'worker_message', 'thread_id': root,
                    'turn_id': params.get('turnId'), 'item_id': item['id'], 'text': text,
                    'evidence_generation': self.evidence_generation,
                    'questions': item.get('questions'), 'received_at': time.time()})
        return sample

    def drain(self, app):
        if self.pending is not None:
            if not self.pending.done():
                return
            self._settle(app)
        if app.outcome_ready.is_set():
            return
        with self.event_lock:
            rows = []
            while True:
                try:
                    rows.append(self.events.get_nowait())
                except queue.Empty:
                    break
            self.pending_messages, self.queued_messages = self.queued_messages, set()
        if not rows:
            return
        actionable = []
        for row in rows:
            member = self.residual.members.get(row.get('member_id'), {})
            current = member.get('last_input_tokens')
            threshold = self.residual.threshold
            if (row.get('kind') == 'context_renewal' and threshold and current is not None
                    and current <= threshold * self.residual.rearm_share):
                # A measured recovery supersedes this queued alert. Do not pay
                # a manager to request a handoff that is no longer indicated.
                member.pop('renewal_baseline', None)
                app._log(json.dumps({'event': 'foundry_live_event_superseded',
                    'member_id': row['member_id'], 'reason': 'measured_context_recovery',
                    'input_tokens': current}))
            else:
                actionable.append(row)
        rows = actionable
        if not rows:
            return
        if self.executor is None:
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='foundry-live-manager')
        self.pending_rows = rows
        self.pending_generation = self.evidence_generation
        self.pending = self.executor.submit(copy_context().run, self.decide, rows)

    def _settle(self, app, *, steer=True):
        pending, self.pending = self.pending, None
        try:
            reply = pending.result()
            if steer and reply and not app.outcome_ready.is_set():
                app.steer(reply, app.turn_id)
            elif reply:
                unchanged = self.pending_generation == self.evidence_generation and self.events.empty()
                retained = self.undelivered is not None and self.undelivered(unchanged)
                if not retained and self.capture is not None:
                    self.capture(self.pending_rows)
        except Exception as exc:
            # Native request/goal manifest retain recovery evidence. Do not kill
            # useful independent worker execution because a live answer failed.
            app._log('live manager delivery unresolved: ' + str(exc))
            if self.capture is not None:
                self.capture(self.pending_rows)
        finally:
            with self.event_lock:
                self.pending_messages.clear()
            self.pending_rows = []

    def close(self, app):
        # Never release goal ownership while a callback can still write its
        # manifest or receipts. No time-based model cancellation is implied.
        if self.executor is not None:
            self.executor.shutdown(wait=True)
        if self.pending is not None:
            self._settle(app, steer=False)
        rows = []
        while not self.events.empty():
            rows.append(self.events.get_nowait())
        if rows and self.capture is not None:
            self.capture(rows)  # Retain terminal-race questions without another paid call.


def retain_events(manifest, events):
    """The existing goal manifest owns unanswered live input, not a retry queue."""
    rows = manifest.setdefault('unanswered_live_events', [])
    # Older failed callbacks retained their input only in this diagnostic field.
    for row in [*(manifest.get('live_manager_error') or {}).get('events', []), *events]:
        if row not in rows:
            rows.append(row)
    return rows


def refusal_signature(event, workspace):
    """Use refusing gate/verdict and measured evidence, never judgement prose."""
    from ._expert_consultation import evidence_snapshot
    judgements = event.get('refused_judgements')
    if not judgements:
        return None
    rows = []
    for name, judgement in sorted(judgements.items()):
        evidence = judgement.get('evidence') or []
        refs = judgement.get('evidence_refs') or evidence
        snapshot = evidence_snapshot({'evidence_refs': refs}, workspace)
        if judgement.get('evidence_refs') and snapshot is None:
            return None  # Unknown named evidence is not proof that nothing changed.
        rows.append({'gate': name, 'verdict': judgement.get('status'), 'snapshot': snapshot})
    # Changed tracked source or a new commit is also evidence, even when a judge
    # repeats its old prose. This is the existing repository reader, no new store.
    from ._repository_observation import snapshot as repository_snapshot
    try:
        source = repository_snapshot(workspace)
    except (OSError, ValueError, RuntimeError):
        return None
    return hashlib.sha256(json.dumps({'objections': rows, 'source': source},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def carry_events(manifest, packet):
    rows = retain_events(manifest, [])
    packet = {key: value for key, value in packet.items() if key != 'unanswered_live_events'}
    return {**packet, 'unanswered_live_events': list(rows)} if rows else packet


def answer_events(manifest, packet, guidance):
    # The next consumed manager decision owns the new question, including a
    # different await_input. Never merge the previous refusal question into it.
    manifest.pop('assignment_refusal_question', None)
    if guidance.get('action') == 'await_input':
        return  # A commander ruling is still required.
    covered = packet.get('unanswered_live_events', [])
    manifest['unanswered_live_events'] = [row for row in manifest.get('unanswered_live_events', []) if row not in covered]
    error = manifest.get('live_manager_error')
    if error and all(row in covered for row in error.get('events', [])):
        manifest.pop('live_manager_error', None)


def _read(path):
    return json.loads(Path(path).read_bytes())


def input_root(folder, manifest):
    folder = Path(folder).resolve()
    if manifest.get('follow_up_goal'):
        return Path(manifest['follow_up_goal']).resolve(strict=True)
    parent = folder.parent / 'manifest.json'
    if parent.is_file():
        outer = _read(parent)
        if outer.get('mode') == 'auto-amend' and Path(outer.get('active_round', '')).resolve() == folder:
            return folder.parent  # Retained goals can predate follow_up_goal.
    return folder


def resolve(folder):
    root = Path(folder).resolve(strict=True)
    manifest = _read(root / 'manifest.json')
    owner = input_root(root, manifest)
    if owner != root:
        root = owner
        manifest = _read(root / 'manifest.json')
    from ._foundry_goal_auto import active_round
    _, active = active_round(root, manifest)
    return root, manifest, active


def _identity(root, manifest, text):
    from ._followup import identity
    run_id = identity(root, manifest)
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    message_id = hashlib.sha256((run_id + '\0' + digest).encode()).hexdigest()
    return {'message_id': message_id, 'run_id': run_id, 'text_sha256': digest}


def submit(folder, tag, text, adapter):
    root, manifest, active = resolve(folder)
    if active.get('tag') != tag:
        raise ValueError('manager intervention tag differs from the exact goal')
    adapter.steer_admission(text, tag, active['worktree'])
    expected_root = adapter.evidence(tag).resolve()
    if not root.is_relative_to(expected_root):
        raise ValueError('manager goal is outside the selected project task evidence')
    if not active.get('manager_state'):
        raise ValueError('this goal has no manager; use worker --steer for its active app-server turn')
    capability = active.get('intervention', {})
    if capability != {'schema_version': 1, 'root': str(root)}:
        raise ValueError('the recorded controller does not support queued manager input; '
                         'use its recorded recovery procedure at a stopped boundary with the updated runtime, '
                         'not desktop thread/resume')
    row = _identity(root, manifest, text)
    path = root / 'operator-inputs' / (row['message_id'] + '.json')
    if not path.exists():
        from ._foundry_goal_auto import outcome_path
        if outcome_path(root, manifest).exists():
            raise ValueError('goal has an outcome; use its explicit recovery/continuation with new instructions')
        from datetime import datetime, timezone
        payload = {**row, 'text': text, 'created_utc': datetime.now(timezone.utc).isoformat()}
        path.parent.mkdir(parents=True, exist_ok=True)
        # Publish complete bytes once. Concurrent identical submissions share an ID.
        fd, temporary = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                json.dump(payload, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            Path(temporary).unlink()
    observed = next(item for item in observe(root) if item['message_id'] == row['message_id'])
    return {**observed, 'goal': str(root), 'target': 'manager',
            'status_argv': [sys.executable, '-m', 'foundry_runtime', '--foundry-project',
                            'status', '--status-goal', str(root)],
            'note': 'Queued input is consumed at the next manager coordination boundary. '
                    'A native response is not proof that the team implemented the instruction.'}


def inputs(root):
    root = Path(root)
    manifest = _read(root / 'manifest.json')
    result = []
    for path in (root / 'operator-inputs').glob('*.json'):
        row = _read(path)
        expected = _identity(root, manifest, row['text'])
        if any(row.get(key) != value for key, value in expected.items()) or path.stem != row['message_id']:
            raise ValueError('operator input identity or bytes changed')
        result.append(row)
    return sorted(result, key=lambda row: (row['created_utc'], row['message_id']))


def deliveries(root):
    """Include prior manager transfers/retries without creating another receipt."""
    results = {}
    root = Path(root)
    paths = set()
    # Upgraded goals contain an automatic controller and its worker rounds.
    # Walk the same round-directory hierarchy, retaining old manager receipts
    # for deduplication without scanning unrelated task evidence.
    manifests, pending = [], [root / 'manifest.json']
    while pending:
        manifest_path = pending.pop()
        manifests.append(manifest_path)
        pending.extend(manifest_path.parent.glob('round-*/manifest.json'))
    for manifest_path in manifests:
        state = _read(manifest_path).get('manager_state') or {}
        if state.get('native_dir'):
            paths.update(Path(state['native_dir']).glob('call-*/reservation.json'))
        for row in (state.get('handoff') or {}).get('history', []):
            paths.add(Path(row['request']['path']).with_name('reservation.json'))
    visited = set()
    while paths:
        path = paths.pop()
        if path in visited:
            continue
        visited.add(path)
        if not path.parent.name.startswith('call-'):
            continue
        allocation = _read(path)
        if allocation.get('kind') != 'coordinate':
            continue
        stdin = path.with_name('stdin.txt')
        if not stdin.exists():
            continue  # Reservation precedes input materialization.
        raw = stdin.read_bytes()
        if hashlib.sha256(raw).hexdigest() != allocation['stdin_sha256']:
            raise ValueError('manager request bytes changed')
        packet = json.loads(raw.rsplit(b'\n\n', 1)[-1])['packet']
        for row in (packet.get('manager_handoff') or {}).get('history', []):
            paths.add(Path(row['request']['path']).with_name('reservation.json'))
        messages = packet.get('operator_message_ids', [])
        receipt = path.with_name('receipt.json')
        record = _read(receipt) if receipt.exists() else {}
        status = ('responded' if record.get('outcome') == 'success' else
                  'blocked' if record else 'admitted')
        for key in messages:
            row = {'status': status, 'request': str(stdin),
                   'receipt': str(receipt) if record else None}
            # A successful retry supersedes a prior quota/transport stop.
            if key not in results or status == 'responded' or results[key]['status'] == 'blocked':
                results[key] = row
    return results


def observe(root):
    rows = inputs(root)
    if not rows:
        return []
    admitted = deliveries(root)
    from ._foundry_goal_auto import outcome_path
    ended = outcome_path(root, _read(Path(root) / 'manifest.json')).exists()
    return [{key: row[key] for key in ('message_id', 'created_utc', 'text_sha256')} |
            admitted.get(row['message_id'], {'status': 'undelivered' if ended else 'queued'}) for row in rows]


def incorporate(root, packet):
    admitted = deliveries(root) if (Path(root) / 'operator-inputs').exists() else {}
    pending = [row for row in inputs(root) if row['message_id'] not in admitted]
    if not pending:
        return packet, False
    existing = set(packet.get('operator_message_ids', []))
    fresh = [row for row in pending if row['message_id'] not in existing]
    if not fresh:
        return packet, True
    message = '\n\n'.join('Operator update ' + row['message_id'] + ':\n' + row['text'] for row in fresh)
    answer = packet.get('operator_answer')
    return {**packet, 'operator_message_ids': packet.get('operator_message_ids', []) + [row['message_id'] for row in fresh],
            'operator_answer': (answer + '\n\n' if answer else '') + message}, True
