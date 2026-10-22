"""Consultation sequencing over native coordinate receipts, never worker routing."""
import hashlib
import json
import re
from pathlib import Path

from ._execution_usage import _reference


def native_root(state):
    manager = Path(state['native_dir'])
    return Path(state.get('expert_native_dir', manager.with_name(manager.name + '-experts')))


def review_request(packet):
    history = packet.get('review_history') or []
    if len(history) == 2:
        def blocking(row):
            return {(f['file'], f['summary'].strip()) for r in row.get('reviews', [])
                    for f in r.get('findings', []) if f.get('severity') in ('P0', 'P1')}
        repeated = blocking(history[0]) & blocking(history[1])
        if repeated:
            return {'trigger': 'recurring_reasoning',
                    'question': {'recurring_findings': [list(row) for row in sorted(repeated)],
                                 'instruction': 'Resolve the underlying cause; compare attempted corrections and propose a discriminating check.'},
                    'evidence_refs': [r['evidence'] for r in history]}
    tier = packet.get('tier_escalation') or {}
    if tier.get('action') == 'expert_consultation_required':
        return {'trigger': 'repeated_failure', 'question': tier['signals'],
                'evidence_refs': packet.get('worker_evidence') or [packet.get('prior_review')]}
    reference = packet.get('prior_review')
    if not reference:
        return None
    prior = json.loads(_reference(reference))
    advice = [{'guidance': row['correction_guidance'], 'findings': row.get('findings', [])}
              for row in prior.get('reviews', [])
              if (row.get('correction_guidance') or {}).get('model') == 'expert']
    return {'trigger': 'reviewer', 'question': advice, 'evidence_refs': [reference]} if advice else None


def review_history(amendment):
    """Project two existing immutable review references, without a second ledger."""
    if not amendment:
        return []
    rows = []
    try:
        prior_manifest = json.loads(_reference(amendment['prior_manifest']))
        previous = (prior_manifest.get('amendment') or {}).get('prior_outcome')
        for reference in [previous, amendment['prior_outcome']]:
            if reference:
                outcome = json.loads(_reference(reference))
                rows.append({'evidence': reference, 'candidate_commit': outcome.get('candidate_commit'),
                             'reviews': outcome.get('reviews', [])})
    except (OSError, ValueError, KeyError, TypeError):
        # Historical gaps must not introduce a new admission failure. The manager
        # retains its ordinary prior_review and conversation for semantic diagnosis.
        return []
    return rows


def consult(turn, state, workspace, question, source):
    # Bind to the admitted question and source receipt, not a mutable goal phase.
    packet = {'stage': 'expert_consultation', 'question': question, 'source': source}
    key = hashlib.sha256(json.dumps(packet, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    before = evidence_snapshot(question, workspace) if state.get('effective_coordination') == 1 else None
    if before:
        packet['evidence_snapshot'] = before
    # Snapshot changes stay in the same native conversation. Its recovery owner
    # must settle an admitted call before processing a changed source packet.
    # Native call directories already carry the full request digest. Keep their
    # parent compact on Windows; native admission verifies the complete packet.
    folder = native_root(state) / key[:16]
    expert = {**state['expert'], 'native_dir': str(folder), 'last_call_id': None}
    if state.get('design_convergence') == 1:
        expert['design_convergence'] = 1
    for name in ('quota_recovery', 'task_deadline_at', 'context_reuse'):
        if name in state:
            expert[name] = state[name]
    if state.get('execution_profiles'):
        expert['execution_profiles'] = state['execution_profiles']
    result, _, _, accepted = turn(expert, workspace, packet)
    answer = {'consultation_id': key, 'trigger': question['trigger'], 'question': question,
            'answer': result['instruction'], 'evidence': result['evidence'],
            'backend': expert['backend'], 'model': expert['model']}
    recorded = accepted.get('evidence_snapshot')
    if recorded and recorded == evidence_snapshot(question, workspace):
        answer['evidence_snapshot'] = recorded
        answer['effort'] = expert.get('effort')
    return answer


def coordinate(turn, state, workspace, packet, *, operator_update=False, pending_only=False):
    original, usage, admitted, consulting = packet, None, None, False
    try:
        pending = state.get('expert_pending_request')
        question = pending['question'] if pending else review_request(packet)
        if question:
            consulting = True
            answer = consult(turn, state, workspace, question, pending['source'] if pending else
                             packet.get('worker_evidence') or packet.get('prior_review'))
            consulting = False
            packet = {**packet, 'expert_consultation': answer}
        while True:
            result = turn(state, workspace, packet, operator_update=operator_update, pending_only=pending_only)
            if result is None:
                return None
            decision, next_state, usage, accepted = result
            admitted = accepted
            question = decision.get('expert_request')
            if question is None:
                if accepted.get('expert_consultation'):
                    decision['expert_consultation'] = accepted['expert_consultation']
                next_state.pop('expert_pending_request', None)
                return decision, next_state, usage, accepted
            consulting = True
            previous = packet.get('expert_consultation')
            answer = (previous if state.get('effective_coordination') == 1 and not operator_update
                      and reusable_answer(previous, question, state, workspace) else
                      consult(turn, state, workspace, question, decision['evidence']))
            consulting = False
            # Existing manager conversation consumes the answer and keeps the
            # user's newest packet; expert advice never becomes operator authority.
            state = next_state
            packet = {**original, 'expert_consultation': answer}
            pending_only = False
    except BaseException as exc:
        if consulting:
            # An expert admission is not admission of a changed operator packet.
            # Preserve only a manager packet that its native owner accepted.
            if admitted is not None:
                exc.manager_packet = admitted
            elif hasattr(exc, 'manager_packet'):
                del exc.manager_packet
            exc.manager_usage = usage
            exc.analyst = None  # Expert and manager usage have different owners.
        if getattr(exc, 'manager_packet', None) is not None:
            exc.manager_packet = {k: v for k, v in exc.manager_packet.items() if k != 'expert_consultation'}
        raise


def evidence_snapshot(question, workspace):
    """Hash only named files, never search a repository to manufacture provenance."""
    refs = question.get('evidence_refs')
    if not isinstance(refs, list) or not refs:
        return None
    rows = []
    try:
        for ref in refs:
            if isinstance(ref, dict):
                _reference(ref, retain=False)
                rows.append(dict(ref))
            else:
                # Existing expert contract uses file:line strings, including
                # Windows absolute paths. URLs/unknown notation stay unknown.
                path = Path(re.sub(r':\d+(?::\d+)?$', '', ref))
                if not path.is_absolute():
                    path = Path(workspace) / path
                digest = hashlib.sha256()
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(chunk)
                rows.append({'path': str(path.resolve()), 'sha256': digest.hexdigest()})
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return rows


def reusable_answer(answer, question, state, workspace):
    """Reuse only explicitly unchanged, hash-bound advice in this decision packet.

    The packet/receipt remains the owner. Missing provenance asks the existing
    expert normally; it never refuses work or guesses validity from similar prose.
    """
    if (not answer or answer.get('question') != question
            or answer.get('backend') != state['expert']['backend']
            or answer.get('model') != state['expert']['model']
            or answer.get('effort') != state['expert'].get('effort')):
        return False
    snapshot = answer.get('evidence_snapshot')
    if not snapshot or snapshot != evidence_snapshot(question, workspace):
        return False
    try:
        _reference(answer['evidence'], retain=False)
    except (OSError, ValueError, TypeError, KeyError):
        return False
    return True


def observe(state):
    """Read-only view of native allocations/receipts, including live calls.

    Status is observation, not proof that an unreceipted process still lives.
    Usage consumers reconcile these attempts through their existing receipt owner.
    """
    rows = []
    if not state or not state.get('expert'):
        return rows
    for folder in sorted(native_root(state).glob('*')):
        calls, question, errors, key = [], None, [], folder.name
        for call in sorted(folder.glob('call-*')):
            try:
                allocation = json.loads((call / 'reservation.json').read_bytes())
                raw = (call / 'stdin.txt').read_bytes()
                if hashlib.sha256(raw).hexdigest() != allocation['stdin_sha256']:
                    raise ValueError('consultation request hash differs')
                packet = json.loads(raw.rsplit(b'\n\n', 1)[-1])['packet']
                question = packet['question']
                identity = {k: v for k, v in packet.items() if k != 'evidence_snapshot'}
                key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                receipt = call / 'receipt.json'
                record = json.loads(receipt.read_bytes()) if receipt.exists() else {}
                calls.append({'call_id': allocation['call_id'], 'attempt_id': allocation['attempt_id'],
                    'backend': state['expert']['backend'], 'requested_compute': {
                        k: state['expert'][k] for k in ('model', 'effort')},
                    'evidence': str(call), 'outcome': record.get('outcome'),
                    'usage': record.get('usage'), 'input_bytes': allocation['input_bytes'],
                    'provider_usage_observed': record.get('usage') is not None,
                    'cumulative_session': record.get('session_id'),
                    'cleanup': record.get('cleanup'), 'provider_stop': record.get('provider_stop'),
                    'error': record.get('error'), 'ended_utc': record.get('ended_utc'),
                    'elapsed_seconds': record.get('elapsed_seconds')})
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(str(exc))
        successful = [c for c in calls if c['outcome'] == 'success']
        unsettled = [c for c in calls if c['outcome'] is None]
        status = ('unknown' if errors else 'pending' if unsettled else 'answered' if successful
                  else 'blocked' if calls else 'requested')
        rows.append({'consultation_id': key if question else folder.name, 'status': status, 'question': question,
                     'assignment': state['expert'], 'calls': calls, 'errors': errors,
                     'observation': 'Native receipts; pending does not prove a live process.'})
    return rows
