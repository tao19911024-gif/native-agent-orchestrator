"""Explicit role transfers through existing goal manifests and native receipts."""
import hashlib
import json
from pathlib import Path
import sys

from ._model_catalogue import model
from .foundry_native import NativeAnalyst, _native_adapter_sha256
from .foundry_native_support import resolve_executable


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def assignment(provider, *, previous=None):
    if previous is not None:
        return {**previous, 'executable': str(resolve_executable(provider))}
    return {'backend': provider, 'executable': str(resolve_executable(provider)),
            'model': model('opus' if provider == 'claude' else 'astra'), 'effort': 'high'}


def manager_history(owner, state, worktree, *, source_locked=False, allow_empty=False):
    """Read the retained manager chain through the native receipt authority."""
    source = Path(state['native_dir'])
    calls = {}
    from contextlib import nullcontext
    with nullcontext() if source_locked else owner._run_lock(source):
        for call in sorted(source.glob('call-*')):
            envelope = json.loads((call/'stdin.txt').read_bytes().rsplit(b'\n\n', 1)[-1])
            envelope.pop('response_contract', None)
            allocation = json.loads((call/'reservation.json').read_bytes())
            envelope['call_id'] = allocation['call_id']
            archived = NativeAnalyst.inspect_call(source, envelope, worktree, state)
            receipt = json.loads((call/'receipt.json').read_bytes())
            calls[envelope['call_id']] = {'envelope': envelope,
                'parent': (allocation.get('retry_of') or {}).get('call_id') or envelope.get('continue_from'),
                'row': {'request': owner._fact(call/'stdin.txt'), 'call_id': envelope['call_id'], 'result': archived['value'],
                            'receipt': archived['receipt'], 'ended_utc': receipt.get('ended_utc'),
                            'outcome': receipt['outcome']}}
        if not calls and not allow_empty:
            raise owner.GoalBlocked('manager has no retained call to transfer; resume its original assignment')
    if not calls:
        return {}, None, []
    frontier = set(calls) - {call['parent'] for call in calls.values()}
    if len(frontier) != 1:
        raise owner.GoalBlocked('manager history has no unique latest request; reconcile its retained chain')
    latest = frontier.pop()
    cursor, ordered, seen = latest, [], set()
    while cursor in calls and cursor not in seen:
        seen.add(cursor); ordered.append(calls[cursor]['row']); cursor = calls[cursor]['parent']
    if cursor is not None or len(seen) != len(calls):
        raise owner.GoalBlocked('manager history is not one complete retained conversation')
    history = list(reversed(ordered))
    return calls, latest, history


def manager(owner, folder, manifest, *, provider, migrate, answer, source_locked=False, worker_feedback=None,
            quota_receipt=None, live=False):
    state = manifest.get('manager_state')
    if quota_receipt is not None:
        from ._foundry_goal_usage import _reference
        receipt = json.loads(_reference(quota_receipt))
        quota_reset = (receipt.get('provider_stop') or {}).get('automatic_resume_at')
        if ((state or {}).get('backend') != 'claude' or provider != 'codex'
                or receipt.get('outcome') != 'provider_limit'
                or not Path(quota_receipt['path']).resolve().is_relative_to(Path(state['native_dir']).resolve())):
            raise ValueError('quota transfer requires the current Claude manager refusal receipt')
    if not live:
        owner._manager_checkpoint(folder, manifest, allow_failed=bool(answer))
    if state is None or manifest.get('manager_phase') not in ({'dispatch_pending'} if live else {'assignment_pending', 'feedback_pending', 'delegation_ready'}):
        raise owner.GoalBlocked('manager transfer requires a settled assignment or feedback phase')
    if manifest.get('review_checkpoint') or (folder/'outcome.json').exists():
        raise owner.GoalBlocked('manager decision is already delivered; recover its next phase')
    request_key = identity({'provider': provider, 'migrate': migrate, 'answer': answer,
                            'python': sys.executable, 'adapter': NativeAnalyst.__module__,
                            'implementation': _native_adapter_sha256(),
                            **({'quota_receipt': quota_receipt} if quota_receipt else {})})
    if state.get('transition_request') == request_key:
        return manifest
    target = provider or state['backend']
    if target == state['backend'] and not migrate:
        raise ValueError('manager already uses that provider; omit handoff or explicitly migrate runtime')
    source = Path(state['native_dir'])
    calls, latest, history = manager_history(owner, state, manifest['worktree'], source_locked=source_locked)
    if quota_receipt is not None and (history[-1]['receipt'] != quota_receipt
                                      or history[-1]['outcome'] != 'provider_limit'):
        raise ValueError('quota fallback receipt is not the latest manager outcome')
    handoff = {'source_state': {key: state[key] for key in ('backend', 'model', 'effort', 'native_dir', 'last_call_id')},
               'native_manifest': owner._fact(source/'native-manifest.json'),
               'history': history, 'operator_instructions': answer,
               'semantics': 'New manager session. Continue the task using retained evidence; do not repeat completed worker actions.'}
    selected = assignment(target, previous=state if target == state['backend'] else None)
    next_state = {k: selected[k] for k in ('backend', 'model', 'effort', 'executable')}
    next_state.update(profile='opus' if target == 'claude' else 'astra', last_call_id=None,
        native_dir=str(folder/('manager-transfer-'+identity({'handoff': handoff, 'target': selected, 'request': request_key})[:16])),
        artifact_delivery=state.get('artifact_delivery', False), handoff=handoff,
        transition_request=request_key)
    for key in ('design_convergence', 'review_convergence', 'quota_recovery', 'context_reuse', 'live_coordination',
                'quota_fallback_policy', 'task_deadline_at', 'effective_coordination', 'claude_live_events'):
        if key in state:
            next_state[key] = state[key]
    if state.get('execution_profiles'):
        next_state['execution_profiles'] = state['execution_profiles']
    if state.get('expert'):
        # Explicit transfer preserves already admitted consultations and model choice.
        from ._expert_consultation import native_root
        next_state['expert'] = state['expert']
        next_state['expert_native_dir'] = str(native_root(state))
        question = (history[-1]['result'] or {}).get('expert_request')
        if question:
            next_state['expert_pending_request'] = {'question': question, 'source': history[-1]['receipt']}
        elif state.get('expert_pending_request'):
            next_state['expert_pending_request'] = state['expert_pending_request']
    if quota_receipt is not None:
        next_state['expert'] = {'backend': 'codex', 'model': model('astra'), 'effort': 'high'}
        next_state.pop('expert_native_dir', None)
        next_state['quota_fallback'] = quota_receipt
        next_state['quota_return_at'] = quota_reset
    elif state.get('quota_fallback') and target == 'claude':
        next_state['expert'] = {'backend': 'claude', 'model': model('fable'), 'effort': 'high'}
        next_state.pop('expert_native_dir', None)
    updated = {**manifest, 'manager_state': next_state,
               'manager_packet': (worker_feedback if worker_feedback is not None else
                   {key: value for key, value in calls[latest]['envelope']['packet'].items() if key != 'manager_handoff'}),
               'manager_phase': ('dispatch_pending' if live else 'feedback_pending' if manifest.get('coding_checkpoint') else 'assignment_pending')}
    if worker_feedback is not None:
        updated.pop('worker_recovery_migration', None)
    if quota_receipt is not None:
        updated['coordination'] = {'status': 'codex_quota_fallback', 'reason': 'measured_claude_provider_limit',
                                   'evidence': quota_receipt}
        from ._compute_selection import independent_reviewers
        excluded = [selected['model'], state.get('model'), state.get('recorded_coding_model')]
        reviewers = independent_reviewers([selected, selected], [m for m in excluded if m], available_backends={'codex'})
        reviewers = [{**r, 'executable': selected['executable']} for r in reviewers]
        updated['reviewers'] = reviewers
        updated['requested_compute'] = {**manifest.get('requested_compute', {}), 'review': None, 'reviews': reviewers}
        if manifest.get('manager_start'):
            updated['manager_start'] = {**manifest['manager_start'], 'options': {
                **manifest['manager_start']['options'], 'reviewers': reviewers}}
    # All source receipts have been read and reconciled before accepting the
    # transfer. New admission lives in a separate directory; old bytes remain.
    owner._write(folder/'manifest.json', updated)
    return updated


def restore_opus(owner, folder, manifest, *, live=False):
    """A measured reset permits a real Opus request at the next decision, not a quota probe."""
    from time import time
    state = manifest.get('manager_state') or {}
    due = state.get('quota_return_at')
    if state.get('quota_fallback') and isinstance(due, (int, float)) and time() >= due:
        manifest = manager(owner, folder, manifest, provider='claude', migrate=False,
                           answer=None, source_locked=True, live=live)
        manifest['coordination'] = {'status': 'opus_recovery_pending',
                                    'reason': 'measured_reset_elapsed; next native request confirms availability'}
        owner._write(folder/'manifest.json', manifest)
    return manifest


def reviewers(checkpoint, saved, *, role, provider, migrate, completed=()):
    """Freeze only explicitly selected targets; completed slots remain reusable."""
    base = checkpoint.get('reviewers') or [checkpoint['analyst']]*2
    transfers = {name: list(rows) for name, rows in saved.items()}
    names = ['review-0', 'review-1'] if role is None else [role]
    for name in names:
        if name in completed:
            continue
        current = transfers.get(name, [base[int(name[-1])]])[-1]
        backend = provider or current.get('backend', 'codex')
        if provider == current.get('backend', 'codex') and not migrate:
            # Retrying an already accepted transfer keeps its resolved model.
            if name in transfers:
                continue
            raise ValueError('reviewer already uses that provider; use ordinary recovery')
        chosen = assignment(backend, previous=current if backend == current.get('backend', 'codex') else None)
        if migrate:
            chosen['runtime_transition'] = identity({'python': sys.executable, 'implementation': _native_adapter_sha256()})
        if chosen != current:
            transfers.setdefault(name, []).append(chosen)
    return transfers


def select_reviewers(settings, excluded, manifest, compute_assignment, *, resolve, pending=None):
    """Launch and recovery share the recorded model/provider selection policy."""
    from ._compute_selection import independent_reviewers
    quota_fallback = bool((manifest.get('manager_state') or {}).get('quota_fallback'))
    staffing = manifest.get('task_staffing') or {}
    source = staffing.get('review_source_detail') or {}
    explicit = (source.get('model') == 'explicit' if source else
                ((staffing.get('sources') or {}).get('reviewers') or
                 (compute_assignment or {}).get('selection', {}).get('source')) == 'explicit') and not quota_fallback
    if explicit:
        return settings  # The shared final admission reports an explicit conflict.
    try:
        independent_reviewers(settings, excluded, explicit=True)
        return settings
    except ValueError:
        pass
    permitted = (('codex',) if quota_fallback else tuple({r['backend'] for r in manifest.get('reviewers') or settings})
                 if source.get('backend') == 'explicit' else ('claude', 'codex'))
    executables = {r['backend']: r['executable'] for r in settings
                   if r.get('executable') and r['backend'] in permitted}
    for provider in permitted:
        if provider not in executables:
            try:
                executables[provider] = str(resolve(provider))
            except (OSError, ValueError, RuntimeError):
                pass
    pending = list(range(len(settings))) if pending is None else list(pending)
    if not pending:
        return settings
    reserved = [row['model'] for index, row in enumerate(settings) if index not in pending]
    selected = independent_reviewers(settings, [*excluded, *reserved],
        available_backends=executables, count=len(pending))
    result = list(settings)
    for index, row in zip(pending, selected):
        result[index] = {**row, 'executable': row.get('executable') or executables.get(row['backend'])}
    return result
