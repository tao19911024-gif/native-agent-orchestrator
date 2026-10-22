"""Native manager turns; goal owns sequencing, worker execution and review verdicts."""
from pathlib import Path
import hashlib
import json
from time import sleep, time
from time import monotonic
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event, Timer
from . import _task_deadline as deadline

from ._compute_selection import compute_kwargs
from ._model_catalogue import BACKEND, model
from .foundry_native import NativeAnalyst
from .foundry_native_support import resolve_executable


LIVE_SOFT_WARNING_SECONDS = 120
LIVE_ORDINARY_TIMEOUT_SECONDS = 2 * 60 * 60
LIVE_EXPERT_TIMEOUT_SECONDS = 4 * 60 * 60
LIVE_TOTAL_TIMEOUT_SECONDS = 8 * 60 * 60
_live_policy = ContextVar('foundry_live_manager_policy', default=None)


@contextmanager
def live_budget(*, ordinary_seconds=LIVE_ORDINARY_TIMEOUT_SECONDS,
                expert_seconds=LIVE_EXPERT_TIMEOUT_SECONDS,
                total_seconds=LIVE_TOTAL_TIMEOUT_SECONDS,
                soft_warning_seconds=LIVE_SOFT_WARNING_SECONDS):
    """Bound live native calls while preserving their existing evidence owner."""
    values = (ordinary_seconds, expert_seconds, total_seconds, soft_warning_seconds)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0 for value in values):
        raise ValueError('live manager budgets must be positive numbers')
    policy = {'ordinary_seconds': ordinary_seconds, 'expert_seconds': expert_seconds,
              'total_seconds': total_seconds, 'soft_warning_seconds': soft_warning_seconds,
              'deadline': monotonic() + total_seconds}
    token = _live_policy.set(policy)
    try:
        yield {key: value for key, value in policy.items() if key != 'deadline'}
    finally:
        _live_policy.reset(token)


def _live_remaining():
    policy = _live_policy.get()
    if policy is None:
        return None
    remaining = policy['deadline'] - monotonic()
    if remaining <= 0:
        raise TimeoutError('live manager decision timed out; worker continues; request retained')
    return remaining


def _live_call_timeout(packet):
    policy = _live_policy.get()
    if policy is None:
        return None
    stage = packet.get('stage')
    limit = policy['expert_seconds'] if stage == 'expert_consultation' else policy['ordinary_seconds']
    return min(limit, _live_remaining())


class ManagerQuotaUnavailable(RuntimeError):
    """Measured quota refusal, for the goal owner's existing role transfer."""

    def __init__(self, receipt):
        super().__init__('Claude manager quota is unavailable; retained receipt permits Codex coordination fallback')
        self.receipt = {'path': str(receipt.resolve()),
                        'sha256': hashlib.sha256(receipt.read_bytes()).hexdigest()}


def _quota_handoff(state, record, row):
    due = (record.get('provider_stop') or {}).get('automatic_resume_at')
    if (state.get('quota_fallback_policy') == 'codex' and state.get('backend') == 'claude'
            and record.get('outcome') == 'provider_limit' and isinstance(due, (int, float))
            and due - time() > state.get('quota_short_wait_seconds', 300)):
        raise ManagerQuotaUnavailable(row['folder'] / 'receipt.json')


def configuration(profile, folder):
    if profile not in ('opus', 'astra'):
        raise ValueError('unknown manager profile')
    return {'profile': profile, 'backend': BACKEND[profile], 'model': model(profile),
            'effort': 'high', 'native_dir': str(Path(folder).resolve()), 'last_call_id': None, 'artifact_delivery': True,
            'design_convergence': 1,
            'review_convergence': 1,
            'quota_recovery': 1,
            'context_reuse': 1,
            'effective_coordination': 1,
            'claude_live_events': 1,
            'live_coordination': 1,
            'expert': {'backend': BACKEND[profile], 'model': model('fable' if profile == 'opus' else 'astra'), 'effort': 'high'}}


def configuration_for(options, folder):
    """One manager-policy constructor for ordinary and prepared upgrade rounds."""
    state = options.get('manager_state')
    profile = options.get('manager_profile')
    if profile is not None and state is not None and profile != state['profile']:
        raise ValueError('manager continuation retains its recorded profile')
    if profile is not None and state is None:
        folder = Path(folder).resolve()
        state = configuration(profile, folder.with_name(folder.name + '-manager'))
        if options.get('coding_launcher', 'app-server') != 'claude':
            state['quota_fallback_policy'] = 'codex'
        if options.get('project_record'):
            state['execution_profiles'] = options['project_record']['context']['execution_profiles']
    return state


def recorded_requests(analyst):
    """Project the native owner's admitted request, including legacy goal drift.

    Goal packets are mutable progress; native stdin and reservations own already
    admitted calls. Native manager_request/__call__ still verify and settle them.
    Retry chains are traversed by that owner, not reconstructed here.
    """
    successors = {}
    for row in analyst._contexts.values():
        allocation = row['allocation']
        if allocation.get('kind') != 'coordinate' or allocation.get('retry_of'):
            continue
        raw = (row['folder'] / 'stdin.txt').read_bytes()
        if hashlib.sha256(raw).hexdigest() != allocation['stdin_sha256']:
            raise ValueError('recorded manager request bytes changed')
        saved = json.loads(raw.rsplit(b'\n\n', 1)[-1])
        saved.pop('response_contract', None)
        predecessor = saved.get('continue_from')
        if predecessor in successors:
            raise ValueError('recorded manager conversation has multiple successors')
        successors[predecessor] = {**saved, 'call_id': allocation['call_id']}
    return successors


def turn(state, workspace, packet, *, operator_update=False, pending_only=False):
    with deadline.scope(state.get('task_deadline_at')):
        return _coordinate(state, workspace, packet, operator_update=operator_update, pending_only=pending_only)


def _coordinate(state, workspace, packet, *, operator_update=False, pending_only=False):
    if state.get('expert'):
        from ._expert_consultation import coordinate
        return coordinate(_turn, state, workspace, packet, operator_update=operator_update, pending_only=pending_only)
    return _turn(state, workspace, packet, operator_update=operator_update, pending_only=pending_only)


def _turn(state, workspace, packet, *, operator_update=False, pending_only=False):
    from ._launch_context import retained_settings
    path = Path(state['native_dir']) / 'native-manifest.json'
    policy = json.loads(path.read_bytes()) if path.exists() else {}
    settings = (policy.get('backend_identity') or {}).get('launch_context')
    with retained_settings(settings):
        return _native_turn(state, workspace, packet, operator_update=operator_update, pending_only=pending_only)


def _native_turn(state, workspace, packet, *, operator_update=False, pending_only=False):
    """Called only while goal holds the existing run lock for this conversation."""
    _live_remaining()
    folder = Path(state['native_dir'])
    if state.get('effective_coordination') == 1:
        # The unanswered list is the existing durable owner. Do not send the
        # same potentially lengthy worker evidence twice in one native request.
        packet = dict(packet)
        if 'events' in packet and packet.get('unanswered_live_events'):
            packet['events'] = [row for row in packet['events']
                                if row not in packet['unanswered_live_events']]
    if state.get('handoff') and not state.get('last_call_id'):
        from ._foundry_goal_usage import _reference
        _reference(state['handoff']['native_manifest'])
        for row in state['handoff']['history']:
            _reference(row['receipt'])
            _reference(row['request'])
        handoff = state['handoff']
        if state.get('context_reuse') == 1:
            history = handoff['history']
            progress = next((row['result'] for row in reversed(history) if row.get('result')), None)
            handoff = {**handoff, 'progress_note': progress,
                       'history': [{k: v for k, v in row.items() if k != 'result'} for row in history]}
        packet = {**packet, 'manager_handoff': handoff}
    request = {'kind': 'coordinate', 'packet': packet, 'instruction_provenance': 1, 'operator_input': True}
    request['review_precondition'] = ('Review requires a committed candidate and a clean worktree. '
        'Resolve project-specific pre-commit/handoff requirements before selecting review. '
        'A review_refusal is worker feedback to correct, not an operator instruction or a review verdict.')
    if state.get('effective_coordination') == 1:
        request['effective_coordination'] = 1
    if state.get('claude_live_events') == 1:
        request['claude_live_events'] = 1
    if state.get('design_convergence') == 1:
        request['design_convergence'] = 1
    if state.get('context_reuse') == 1:
        request['context_reuse'] = 1
    if state.get('live_coordination') == 1:
        request['live_coordination'] = 1
    if state.get('review_convergence') == 1:
        request['review_convergence'] = 1
    if state.get('quota_recovery') == 1:
        request['quota_recovery'] = 1
    if deadline.current() is not None:
        request['task_deadline_at'] = deadline.current()
    if packet.get('member_tier_decisions'):
        request['member_tier_decisions'] = [row['decision_id'] for row in packet['member_tier_decisions']]
    if state.get('expert'):
        request['expert_consultation'] = True
    if state.get('artifact_delivery') is True:
        request['artifact_delivery'] = True
    if state.get('last_call_id'):
        request['continue_from'] = state['last_call_id']
    request['call_id'] = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False,
                                                  separators=(',', ':')).encode()).hexdigest()
    native_manifest = folder / 'native-manifest.json'
    executable = (Path(json.loads(native_manifest.read_bytes())['executable'])
                  if native_manifest.exists() else Path(state['executable']) if state.get('executable') else resolve_executable(state['backend']))
    analyst = NativeAnalyst(executable, folder, backend=state['backend'],
        recorded_coding_model=state.get('recorded_coding_model'),
        workspace=workspace, **compute_kwargs(state['model'], state['effort'], backend=state['backend']),
        timeout_seconds=None, max_total_input_bytes=None, resume=folder.exists(),
        **({"execution_profiles": state["execution_profiles"]} if state.get("execution_profiles") else {}))
    from ._foundry_native_recovery import reconcile_manager
    if packet.get('stage') != 'expert_consultation':
        # The goal owns a pending manager decision. Expert calls have a separate
        # consumption owner: a lost receipt after its answer was used must not
        # create a second paid consultation. Preserve that owner's recovery path.
        reconcile_manager(analyst)
    timed_out = Event()
    call_timeout = _live_call_timeout(packet)
    call_deadline = monotonic() + call_timeout if call_timeout is not None else None
    timer = None
    if call_timeout is not None:
        def cancel_timed_out_call():
            timed_out.set()
            analyst.cancel('live manager decision timeout')
        timer = Timer(call_timeout, cancel_timed_out_call)
        timer.start()
    try:
        desired = request
        successors = recorded_requests(analyst)
        if pending_only and desired.get('continue_from') not in successors:
            return None
        requested_seen = pending_only
        while True:
            # Finish the frozen decision before accepting changed instructions.
            # Only the final decision escapes to goal/worker dispatch. No retry
            # loop on failure: a failed model call exits this invocation.
            request = successors.get(desired.get('continue_from')) or desired
            waited = set()
            while True:
                request = analyst.manager_request(request)
                if (request.get('retry_of') and state.get('quota_recovery') == 1
                        and 'call-' + request['call_id'] not in analyst._contexts):
                    row = analyst._contexts['call-' + request['retry_of']]
                    record, _, _ = analyst._inspect_receipt(row)
                    _quota_handoff(state, record, row)
                    due = (record.get('provider_stop') or {}).get('automatic_resume_at')
                    if due is not None:
                        deadline.check()
                        if call_deadline is not None and due - time() >= max(0, call_deadline - monotonic()):
                            raise TimeoutError('live manager budget precedes quota reset; worker continues; request retained')
                        remaining = _live_remaining()
                        if remaining is not None and due - time() >= remaining:
                            raise TimeoutError('live manager budget precedes quota reset; worker continues; request retained')
                        if deadline.current() is not None and due >= deadline.current():
                            raise TimeoutError('task deadline precedes manager quota reset; request retained')
                        print('manager quota: waiting until %s; same session %s' % (due, record.get('session_id')), flush=True)
                        sleep(max(0, due - time()))
                        deadline.check()
                        waited.add(due)
                try:
                    result = analyst(request)
                    break
                except Exception:
                    row = analyst._contexts.get('call-' + request['call_id'])
                    if state.get('quota_recovery') != 1 or row is None:
                        raise
                    record, _, _ = analyst._inspect_receipt(row)
                    _quota_handoff(state, record, row)
                    due = (record.get('provider_stop') or {}).get('automatic_resume_at')
                    if record['outcome'] != 'provider_limit' or due is None or due in waited:
                        raise
            if result.get('expert_request'):
                break  # Settle this consultation before accepting changed operator input.
            requested_seen = requested_seen or request['packet'] == packet
            # An admitted successor outranks a stale goal packet after a crash.
            # Settle that successor before returning any worker instruction.
            if request['call_id'] not in successors and (
                    request['packet'] == packet or (requested_seen and not operator_update)):
                break
            desired = {**desired, 'continue_from': request['call_id']}
            desired.pop('call_id', None)
            desired['call_id'] = hashlib.sha256(json.dumps(desired, sort_keys=True, ensure_ascii=False,
                separators=(',', ':')).encode()).hexdigest()
        if timed_out.is_set():
            raise TimeoutError('live manager decision timed out; worker continues; request retained')
        session = analyst.native_session(request)
        receipt = analyst.last_evidence / 'receipt.json'
        reference = {'path': str(receipt.resolve()), 'sha256': hashlib.sha256(receipt.read_bytes()).hexdigest()}
        decision = {'backend': state['backend'], 'model': state['model'], 'effort': state['effort'],
                    'session_id': session, 'call_id': request['call_id'],
                    'instruction': result['instruction'], 'action': result['action'], 'evidence': reference}
        if request.get('instruction_provenance') == 1:
            decision['instruction_source'] = 'manager'
            # Copy only admitted operator input; never infer authority from model prose.
            decision['operator_answer'] = request['packet'].get('operator_answer')
        if 'artifacts' in result:
            decision['artifacts'] = result['artifacts']
        if 'member_tier_decisions' in result:
            decision['member_tier_decisions'] = result['member_tier_decisions']
        if request.get('live_coordination') == 1:
            decision['live_coordination'] = 1
        if result.get('expert_request'):
            decision['expert_request'] = result['expert_request']
        return decision, {**state, 'last_call_id': request['call_id'], 'session_id': session}, analyst.drain(), request['packet']
    except BaseException as exc:
        admitted = analyst._contexts.get('call-' + request['call_id'])
        manager_packet = request['packet'] if admitted is not None else None
        if manager_packet is not None:
            exc.manager_packet = manager_packet
        # Native __call__ owns cancellation after admission. A rejected local
        # request must not create a cancellation that poisons a valid session.
        analyst.drain()
        if timed_out.is_set():
            timeout = TimeoutError('live manager decision timed out; worker continues; request retained')
            if manager_packet is not None:
                timeout.manager_packet = manager_packet
            timeout.analyst = analyst
            raise timeout from exc
        exc.analyst = analyst
        try:
            _live_remaining()
        except TimeoutError as timeout:
            if manager_packet is not None:
                timeout.manager_packet = manager_packet
            timeout.analyst = analyst
            raise timeout from exc
        raise
    finally:
        if timer is not None:
            timer.cancel()
            timer.join()
