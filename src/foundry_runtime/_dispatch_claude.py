"""Claude transport for the existing dispatcher, lease and intent journal."""
from __future__ import annotations

import hashlib
import json
import tempfile
import time
import uuid
import sys
import subprocess
from threading import Event
from pathlib import Path

from .foundry_native_support import resolve_executable
from .foundry_claude import environment
from ._execution_profile import claude_policy
from ._compute_selection import compute_kwargs


# Facts about this runtime, not task guidance. foundry_claude.environment disables
# background tasks and lifts the foreground cap. Tools that resume a session later
# cannot work in a run that ends with its turn: MOVEHANDS1 (2026-09-25) detached
# its suite, called ScheduleWakeup and ended the turn, and the owned Job cleanup
# then removed the suite. They are withheld, and the model is told why.
UNAVAILABLE_TOOLS = ('ScheduleWakeup', 'CronCreate', 'CronDelete', 'CronList', 'Monitor', 'RemoteTrigger')
RUNTIME_NOTE = ('Runtime: this is a non-interactive run that ends when you end your turn. Nothing can wake you '
                'later: background tasks, scheduled wake-ups and monitors are unavailable, and every process '
                'you started, detached or not, is stopped when the run ends. Foreground commands may run for '
                'up to 24 hours: run a long command, such as a full test suite, in the foreground with a '
                'timeout that covers it, and end your turn only after every run whose result you rely on has '
                'finished and you have read it.')

LIVE_NOTE = (' Foundry live coordination: root FOUNDRY_MANAGER_QUESTION and FOUNDRY_DELIVERABLE_DONE '
             'protocol lines (leading marker, optionally Markdown-wrapped, outside code fences) trigger the existing manager while you work. Its reply enters this same process '
             'after your current native turn returns. Complete independent useful work, then return '
             'the question/handoff; do not keep the turn open waiting for the reply or repeat markers. '
             'The process stays open for pending manager answers; this is not a scheduled wakeup after '
             'process exit. Relay member questions through the root and retain evidence references. '
             'Manager replies are guidance, not new operator authority.')


def argv(executable, session, *, resume=False, model=None, effort=None, native_team=None, execution_policy=None, workspace=None, launch_context=None, live=False):
    selected = compute_kwargs(model, effort, backend='claude')
    note = RUNTIME_NOTE
    if live:
        note = note.replace('this is a non-interactive run that ends when you end your turn. Nothing can wake you later:',
            'this non-interactive process may continue with pending manager input after a turn returns. After process exit:') + LIVE_NOTE
    args = [str(executable), '--print', '--output-format', 'stream-json', '--verbose',
            '--permission-mode', (execution_policy if execution_policy is not None else claude_policy())['permission_mode'], '--append-system-prompt', note,
            '--disallowedTools', ','.join(UNAVAILABLE_TOOLS),
            '--resume' if resume else '--session-id', session]
    if live:
        args += ['--input-format', 'stream-json', '--replay-user-messages']
    for key, value in selected.items():
        args += ['--' + key, value]
    if native_team and native_team.get('profile') == 'adaptive':
        agents = {name: {'description': 'Foundry ' + name + ' role',
                         'prompt': ('Own the assigned ' + name + ' subtask. Implement edits and focused tests when assigned; '
                                    'return results, evidence locations and unresolved questions. '
                                    'Use retained context for follow-up. Escalate reasoning or missing context '
                                    'to the parent when needed; do not repeat completed work.'),
                         **native_team.get('member_role_briefs', {}).get(name, {}),
                         **selection}
                  for name, selection in native_team['member_roles'].items()}
        args += ['--agents', json.dumps(agents)]
        if native_team.get('member_instructions'):
            args += ['--append-subagent-system-prompt', native_team['member_instructions']]
    from ._launch_context import claude_args
    aliases = None
    if native_team and native_team.get('version', 0) >= 15:
        from ._model_catalogue import tier_for_model
        aliases = {tier_for_model(row['model']): row['model']
                   for row in native_team['member_roles'].values() if tier_for_model(row.get('model'))}
    return args + claude_args(workspace, launch=launch_context, aliases=aliases)


def read_result(path, *, session=None, stream_input=False):
    """Coding accepts ordinary tool traffic and prose; only native init/result are projected.

    One print-mode process can hold several turns of one session: a turn that ends
    while background tasks run is followed by another init when they notify. The
    CLI emits one result per turn, numbered by result_index, and may hold them all
    until exit (measured on DECK2, 2026-09-25: init, init, result 0, result 1).
    Every init and result must belong to one session with one result per init, and
    the terminal is the last-numbered result; its modelUsage is the session snapshot.
    """
    sessions, inits, results = set(), 0, []
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for line in stream:
            digest.update(line)
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get('type') == 'system' and row.get('subtype') == 'init':
                sessions.add(row.get('session_id'))
                inits += 1
            elif row.get('type') == 'result':
                sessions.add(row.get('session_id'))
                results.append(row)
    indexes = [row.get('result_index', position) for position, row in enumerate(results)]
    if (not results or (not 1 <= inits <= len(results) if stream_input else len(results) != inits)
            or indexes != list(range(len(results)))
            or len(sessions) != 1 or not results[-1].get('session_id')
            or (session is not None and results[-1]['session_id'] != session)):
        raise ValueError('Claude result lacks matching session evidence')
    return results[-1], {'path': str(Path(path).resolve()), 'sha256': digest.hexdigest()}


def verify_terminal(proof, *, session, returncode, attempt_id):
    if (proof.get('schema_version') != 'pdcl-claude-terminal-1'
            or proof.get('session_id') != session or proof.get('attempt_id') != attempt_id
            or proof.get('native_returncode') != returncode):
        raise ValueError('Claude terminal differs from the recorded execution')
    result, reference = read_result(proof['stdout']['path'], session=session,
                                    stream_input=proof.get('input_format') == 'stream-json')
    if reference != proof['stdout']:
        raise ValueError('Claude native stream changed')
    if returncode == 0 and (result.get('is_error') is not False or result.get('subtype') != 'success'):
        raise ValueError('Claude exit 0 has no successful native result')
    return result


def session_worktree(common_dir, tag, session):
    """Reuse retained dispatch identities, not a second session registry."""
    from ._execution_intent import ExecutionIntent
    trees = set()
    for directory in (Path(common_dir) / 'pdcl-dispatch' / ('task-' + tag) / 'intents').glob('*'):
        record, _, _ = ExecutionIntent.read_history(directory, task_key=tag)
        if record.get('launcher') == 'claude' and record.get('session_id') == session:
            trees.add(Path(record['frozen']['worktree']).resolve())
    if len(trees) != 1:
        raise ValueError('Claude session has no unique recorded task workspace')
    return trees.pop()


def coding_report(path, last_result):
    """Project the latest deliverable-marked message; terminal/usage keep the last result."""
    from ._goal_intervention import protocol_markers
    selected = None
    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get('type') == 'assistant' and not row.get('parent_tool_use_id'):
                content = (row.get('message') or {}).get('content') or []
                text = '\n'.join(b.get('text', '') for b in content
                                 if isinstance(b, dict) and b.get('type') == 'text')
                if 'FOUNDRY_DELIVERABLE_DONE' in protocol_markers(text):
                    selected = text
            if row.get('type') == 'result':
                text = row.get('result') or ''
                if 'FOUNDRY_DELIVERABLE_DONE' in protocol_markers(text):
                    selected = text
    return selected if selected is not None else last_result.get('result') or '\n'.join(last_result.get('errors', []))


class ClaudeLive:
    """Claude frames adapt to the existing LiveEvents owner; no new decision loop.

    Manager work is asynchronous. Replies enter the same native process after its
    current result, so a slow manager never interrupts useful worker tool work.
    """
    def __init__(self, session, human, launch_context=None):
        from ._goal_intervention import LiveEvents
        from ._launch_context import compaction_observation
        self.live_events = LiveEvents(threshold=compaction_observation(launch_context, 'claude')['token_limit'])
        self.enabled = self.live_events.claude_enabled and self.live_events.decide is not None
        self.session, self.human = session, human
        self.turn_id = 'claude-run'
        self.outcome_ready = Event()
        self.result_ready = False
        self.outbox = []
        self.unacknowledged = {}
        self.tool_names = {}
        self.closed = False

    def _log(self, text):
        try:
            self.human.write(text + '\n'); self.human.flush()
        except (OSError, ValueError):
            pass

    def steer(self, text, turn):
        with self.live_events.event_lock:
            self.live_events.delivering_messages.update(self.live_events.pending_messages)
        self.outbox.append((text, list(self.live_events.pending_rows)))

    def send(self, proc, text, events=()):
        identity = str(uuid.uuid4())
        self.turn_id = identity
        row = {'type': 'user', 'session_id': self.session, 'uuid': identity,
               'parent_tool_use_id': None, 'message': {'role': 'user', 'content': text}}
        # Retain before writing: a failed/partial write is not delivery proof.
        self.unacknowledged[identity] = list(events)
        proc.stdin.write(json.dumps(row, ensure_ascii=False) + '\n')
        proc.stdin.flush()
        self.result_ready = False
        self._log(json.dumps({'event': 'foundry_manager_input_sent' if events else 'foundry_initial_input_sent',
                              'uuid': identity, 'session_id': self.session}))

    def observe(self, row):
        if row.get('session_id') not in (None, self.session):
            self._log('live manager observation ignored: Claude session identity differs')
            if row.get('type') == 'result':
                self.outcome_ready.set()  # Let the existing terminal validator explain the mismatch.
            return
        kind = row.get('type')
        if kind == 'result':
            self.result_ready = True
            if row.get('is_error'):
                self.outcome_ready.set()
            return
        if kind == 'user' and row.get('uuid') in self.unacknowledged:
            self.unacknowledged.pop(row['uuid'])
            self._log(json.dumps({'event': 'foundry_manager_input_acknowledged', 'uuid': row['uuid']}))
            return  # Our own answer is not another worker question.
        message = row.get('message') or {}
        member = row.get('parent_tool_use_id') or self.session
        identity = row.get('uuid') or message.get('id') or hashlib.sha256(
            json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        def completed(item, thread=member):
            self.live_events.observe({'method': 'item/completed', 'params': {
                'threadId': thread, 'turnId': self.turn_id, 'item': item}}, self.session)
        if kind == 'system' and row.get('subtype') == 'compact_boundary' and identity:
            completed({'type': 'contextCompaction', 'id': identity})
        if kind == 'assistant':
            usage = message.get('usage') or {}
            counts = [usage.get(k) for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')]
            if all(type(n) is int and n >= 0 for n in counts) and sum(counts) > 0:
                self.live_events.observe({'method': 'thread/tokenUsage/updated', 'params': {
                    'threadId': member, 'tokenUsage': {'last': {'inputTokens': sum(counts)}}}}, self.session)
            content = message.get('content') or []
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('id'):
                    self.tool_names[(member, block['id'])] = block.get('name')
            text = '\n'.join(b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text')
            if identity and text:
                completed({'type': 'agentMessage', 'id': identity, 'text': text})
        if kind == 'user':
            content = message.get('content') or []
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get('type') == 'tool_result' and block.get('tool_use_id'):
                    completed({'type': 'toolResult', 'id': block['tool_use_id'],
                        'tool': self.tool_names.pop((member, block['tool_use_id']), None),
                        'result': {'error': True} if block.get('is_error') else block.get('content')})
        if kind == 'system' and row.get('subtype') == 'task_notification' and identity:
            completed({'type': 'memberResult', 'id': identity})

    def advance(self, proc):
        if self.closed:
            return
        self.live_events.drain(self)
        if self.outcome_ready.is_set():
            self.close()
            proc.stdin.close()
            return
        if not self.result_ready:
            return
        if self.outbox:
            messages, rows = [], []
            for text, events in self.outbox:
                messages.append(text); rows.extend(events)
            self.outbox.clear()
            try:
                self.send(proc, '\n\n'.join(messages), rows)
            except (OSError, ValueError) as exc:
                self._log('live manager input unresolved: ' + str(exc))
                if self.live_events.capture is not None:
                    self.live_events.capture(rows)
                self.close()
                try:
                    proc.stdin.close()
                except (OSError, ValueError):
                    pass
            finally:
                with self.live_events.event_lock:
                    self.live_events.delivering_messages.clear()
            return
        if self.live_events.pending is None and self.live_events.events.empty():
            # EOF ends this owned execution, not the saved native session. No
            # fixed wait/count limit; normal deadline/stop ownership is unchanged.
            proc.stdin.close()
            self.closed = True
            self.outcome_ready.set()

    def close(self):
        self.outcome_ready.set()
        self.live_events.close(self)
        if self.live_events.capture is not None:
            rows = [event for _, events in self.outbox for event in events]
            rows += [event for events in self.unacknowledged.values() for event in events]
            if rows:
                self.live_events.capture(rows)
        self.outbox.clear()
        self.unacknowledged.clear()
        with self.live_events.event_lock:
            self.live_events.delivering_messages.clear()
        self.closed = True


def run(owner, body, worktree, log, out, tag, *, lease, on_started,
        resume_session=None, model=None, effort=None, native_team=None, execution_policy=None, execution_environment=None, launch_context=None):
    session = resume_session or str(uuid.uuid4())
    stream_path, terminal_path = log.with_suffix('.claude.jsonl'), log.with_suffix('.terminal.json')
    lease.require_reserved(worktree, tag, {'claude'})
    proc = None
    owns_log = False
    live = None
    try:
        if any(p.exists() for p in (log, out, stream_path, terminal_path, log.with_suffix('.cleanup.json'))):
            raise FileExistsError('dispatch evidence already exists: ' + str(log))
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('x', encoding='utf-8') as human, stream_path.open('xb') as stream:
            owns_log = True
            live = ClaudeLive(session, human, launch_context)
            args = argv(resolve_executable('claude'), session, resume=resume_session is not None,
                        model=model, effort=effort, native_team=native_team, execution_policy=execution_policy,
                        workspace=worktree, launch_context=launch_context, live=live.enabled)
            human.write('Claude session: ' + session + '\nNative stream: ' + str(stream_path) + '\n')
            human.flush()
            with tempfile.TemporaryFile(mode='w+b') as prompt:
                prompt.write(body.encode('utf-8')); prompt.seek(0)
                try:
                    from ._task_deadline import check
                    check()
                    proc = owner.OwnedProcess.spawn(args, cwd=worktree, stdin=subprocess.PIPE if live.enabled else prompt,
                        cleanup_path=log.with_suffix('.cleanup.json'),
                        stdout=stream, stderr=human, attempt_id=lease.attempt_id,
                        env=environment(effort, **({"execution_environment": execution_environment} if execution_environment is not None else {})))
                except owner.SpawnFailure as exc:
                    proc = exc.process
                    raise
                lease.bind_process(proc.pid, proc.creation_time)
                on_started(session)
                if live.enabled:
                    live.send(proc, body)
                from ._member_activity import MemberActivity
                activity = MemberActivity()
                while (rc := proc.poll()) is None:
                    from ._task_deadline import remaining
                    if remaining() == 0:
                        raise TimeoutError('task deadline reached during Claude execution; owned cleanup follows')
                    for signal in activity.read(stream_path, time.monotonic(),
                            **({'on_event': live.observe} if live.enabled else {})):
                        message = 'Foundry observation: ' + json.dumps(signal)
                        try:
                            human.write(message + '\n')
                            human.flush()
                        except (OSError, ValueError):
                            pass  # Best-effort observation must not stop the owned task.
                        try:
                            print(message, file=sys.stderr, flush=True)
                        except (OSError, ValueError):
                            pass  # The human log retains it; caller pipes do not own execution.
                    if live.enabled:
                        live.advance(proc)
                    time.sleep(0.2)
                if live.enabled:
                    activity.read(stream_path, time.monotonic(), on_event=live.observe)
                    live.close()
        result, reference = read_result(stream_path, session=session, stream_input=live.enabled)
        proof = {'schema_version': 'pdcl-claude-terminal-1', 'session_id': session,
                 'attempt_id': lease.attempt_id, 'native_returncode': rc, 'stdout': reference}
        if live.enabled:
            proof['input_format'] = 'stream-json'
        verify_terminal(proof, session=session, returncode=rc, attempt_id=lease.attempt_id)
        terminal_path.write_text(json.dumps(proof, indent=2), encoding='utf-8')
        out.write_text(coding_report(stream_path, result) if live.enabled else
                       result.get('result') or '\n'.join(result.get('errors', [])), encoding='utf-8')
        return rc
    finally:
        try:
            if live is not None and live.enabled:
                live.close()
        finally:
            cleanup = proc.stop_and_verify(drain_seconds=0, kill_seconds=5) if proc else owner._not_started(lease)
            if owns_log:
                owner._record_cleanup(lease, cleanup, log.with_suffix('.cleanup.json'))
            elif not lease.release(cleanup):
                raise RuntimeError('executor cleanup is unknown; reservation retained')
