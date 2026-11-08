"""Read existing execution evidence for the originating host's follow-up tools."""
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

goal_scope = ContextVar('foundry_followup_goal', default=None)


def origin():
    thread = os.environ.get('CODEX_THREAD_ID')
    return {'host': 'codex', 'thread_id': thread} if thread else None


def announce(folder, manifest):
    from ._hosted import active_directory
    if manifest.get('follow_up_goal'):
        folder = Path(manifest['follow_up_goal'])
        manifest = json.loads((folder / 'manifest.json').read_bytes())
    if not manifest.get('originating_session') and active_directory.get() is None:
        return
    # Host delivery is projected separately from this execution binding.
    _announce({
        'goal': str(Path(folder).resolve()), 'run_id': identity(folder, manifest),
        'originating_session': manifest.get('originating_session'),
        'status_argv': [sys.executable, '-m', 'foundry_runtime', '--foundry-project', 'status',
                        '--status-goal', str(Path(folder).resolve())]})


def _announce(report):
    from ._hosted import capture
    if capture(report):
        return  # The launch binding owns the single originating-host follow-up.
    from ._followup_host import enrich
    enrich(report, report['status_argv'])
    print('FOUNDRY_FOLLOW_UP ' + json.dumps(report, ensure_ascii=True), flush=True)


def identity(folder, manifest):
    # Historical auto-amend roots predate run_id; observe without rewriting them.
    if not isinstance(manifest, dict):
        raise ValueError('goal manifest is not an object')
    run_id = manifest.get('run_id')
    if run_id is None and manifest.get('mode') == 'auto-amend' and manifest.get('order'):
        run_id = 'legacy-' + hashlib.sha256(json.dumps(
            [str(Path(folder).resolve()), manifest['order']], sort_keys=True).encode()).hexdigest()
    if not isinstance(run_id, str) or not run_id:
        raise ValueError('goal manifest has no run identity')
    return run_id


def status(folder, *, plan=False, runtime_selection=None):
    from ._followup_host import enrich
    report = _goal_status(folder)
    report['summary'] = _summary(report, runtime_selection=runtime_selection)
    return enrich(report, [sys.executable, '-m', 'foundry_runtime', '--foundry-project',
                           'status', '--status-goal', report['goal']], plan=plan)


def _current_dispatch(folder, manifest):
    """Join only this goal's open packages and the journal's current attempt."""
    from ._dispatch_package import read_intent
    folder = Path(folder).resolve()
    root = manifest.get('dispatch_evidence_root')
    if not root:
        return {'status': 'unrecorded', 'executions': []}
    matches, errors = [], []
    for path in Path(root).glob('dispatch*.package.json'):
        try:
            package = json.loads(path.read_bytes())
            if package.get('goal') != {'directory': str(folder), 'run_id': identity(folder, manifest)}:
                continue
            _, record = read_intent(package)
            if record.get('attempt_id') == package.get('attempt_id') and record['state'] in ('starting', 'active', 'unknown'):
                matches.append(_dispatch_status(path))
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            errors.append({'path': str(path), 'error': str(exc)})
    return {'status': 'ambiguous' if len(matches) > 1 else 'observed' if matches else 'none_observed',
            'executions': matches, 'errors': errors}


def _summary(report, *, runtime_selection=None):
    """A consumer view of existing owners, not another task state."""
    selected, errors = None, []
    if runtime_selection:
        try:
            value = json.loads(Path(runtime_selection).read_text(encoding='utf-8-sig'))
            selected = {key: value.get(key) for key in ('python', 'version')}
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            errors.append(str(exc))
    dispatch = report.get('dispatch_observation', {})
    executions = dispatch.get('executions', [])
    worker = executions[0] if len(executions) == 1 else {}
    queued = sum(row.get('status') == 'queued' for row in report.get('manager_interventions', []))
    action, waiting = 'inspect_status', None
    command = [sys.executable, '-m', 'foundry_runtime', '--foundry-project', 'status', '--status-goal', report['goal']]
    if runtime_selection:
        command += ['--runtime-selection', str(runtime_selection)]
    if dispatch.get('status') == 'ambiguous':
        action = 'inspect_dispatch_bindings'
    elif report.get('status') == 'execution_stopped':
        action = 'inspect_retained_execution'
    elif report.get('status') == 'outcome_available':
        action = 'inspect_delivery'
        command = [sys.executable, '-m', 'foundry_runtime', '--foundry-usage',
                   report.get('continuation_goal', report['goal'])]
    elif worker.get('status') == 'pending':
        waiting = 'provider_capacity' if worker.get('provider_observation', {}).get('status') == 'quota_blocked' else 'worker'
        action = 'wait'
    elif report.get('active_native_calls'):
        action, waiting = 'wait', 'coordinator'
    elif report.get('controller_observation', {}).get('state') == 'held':
        action, waiting = 'wait', 'controller'
    feedback = report.get('feedback') or {}
    continuation = feedback.get('continuation') or {}
    invocation = continuation.get('invocation') or {}
    if report.get('outcome_status') == 'candidate_verified':
        # Optional follow-up findings stay in the full feedback report.
        invocation = {}
    if invocation.get('argv'):
        action, waiting, command = 'continue_from_feedback', 'operator', invocation['argv']
    elif report.get('outcome_status') == 'awaiting_input':
        action, waiting = 'answer_input_request', 'operator'
        invocation = report.get('input_continuation') or {}
        command = invocation.get('argv') or command
    phase = report.get('outcome_status') if report.get('status') == 'outcome_available' else report.get('phase')
    return {'status': report['status'], 'phase': phase,
            'waiting_for': waiting, 'pending_operator_inputs': queued,
            'progress': report.get('progress'),
            'time_budget': report.get('time_budget'),
            'scratch': report.get('scratch'),
            'index_observation': report.get('index_observation'),
            'dispatch_status': dispatch.get('status'),
            'runtime': {'controller': report.get('runtime_observation'),
                        'worker': worker.get('runtime_observation'),
                        'observer_python': sys.executable, 'selected': selected},
            'delivery': report.get('delivery'), 'next_action': action, 'next_command_argv': command,
            'next_invocation': invocation or None,
            'execution': (worker.get('execution_binding') or report.get('settled_execution')
                          or (report.get('coding_recovery') or {}).get('retained_dispatch')),
            'operator_inputs': report.get('manager_interventions', []),
            **({'operator_amendment': feedback['operator_amendment']} if 'operator_amendment' in feedback else {}),
            'evidence': {'goal': report['goal'], 'outcome': report.get('outcome_path'),
                         'dispatch': worker.get('dispatch'),
                         'worker_live_event_log': worker.get('live_event_log')}, 'errors': errors}


def _worker_messages(path, *, launcher=None, session=None):
    """Read recent completed root messages from the existing worker evidence."""
    from ._goal_intervention import protocol_markers
    try:
        with Path(path).open('rb') as stream:
            size = stream.seek(0, 2)
            start = max(0, size - 64 * 1024)
            stream.seek(start)
            raw = stream.read(size - start)
    except OSError:
        return None
    if start:
        raw = raw.partition(b'\n')[2]
    latest, question = None, None
    for line in raw.split(b'\n')[:-1]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if launcher == 'claude':
            if (row.get('type') != 'assistant' or row.get('parent_tool_use_id')
                    or (session and row.get('session_id') != session)):
                continue
            message = row.get('message') or {}
            text = '\n'.join(block['text'] for block in message.get('content', [])
                             if isinstance(block, dict) and block.get('type') == 'text'
                             and isinstance(block.get('text'), str))
            row = {'item_id': row.get('uuid') or message.get('id'), 'text': text, 'timestamp': row.get('timestamp')}
        if not isinstance(row.get('text'), str) or not row['text']:
            continue
        latest = {key: row.get(key) for key in ('item_id', 'phase', 'timestamp', 'questions')}
        latest['text'] = row['text'][:1000]
        if row.get('questions') or 'FOUNDRY_MANAGER_QUESTION' in protocol_markers(row['text']):
            question = latest
    return {'path': str(path), 'latest': latest, 'latest_question': question, 'tail_truncated': bool(start)}


def _progress(report, active, worker):
    from ._goal_intervention import protocol_markers
    member = worker.get('worker') or {}
    messages = (worker.get('message_projection') if worker else
                next((row['path'] for row in reversed(report.get('commander_messages', []))), None))
    live_events = report['unanswered_live_events']
    decision = active.get('live_manager_decision') or (active.get('execution_decision') or {}).get('manager') or {}
    coordination = report.get('coordination') or {}
    manager = active.get('manager_state') or {}
    live_call = report.get('live_manager_call') or {}
    last_write = None
    log = worker.get('live_event_log')
    if member.get('launcher') == 'claude':
        messages = log
    if log:
        try:
            last_write = datetime.fromtimestamp(Path(log).stat().st_mtime, timezone.utc).isoformat()
        except FileNotFoundError:
            pass
    activity = report.get('activity_observation') or {}
    return {
        'round': report['active_round'], 'coordination_stage': live_call.get('stage'),
        'coordination_state': live_call.get('state'),
        'active_worker': worker.get('worker') if worker.get('status') == 'pending' else None,
        'active_coordinators': report['active_native_calls'],
        'last_worker_event': activity.get('last_native_event'),
        'last_worker_evidence_write_utc': last_write,
        'worker_messages': _worker_messages(messages, launcher=member.get('launcher'),
                                            session=member.get('session_id')) if messages else None,
        'unanswered_questions': [
            {**row, 'text': (row.get('text') or '')[:1000]} for row in live_events
            if row.get('questions') or (row.get('kind') == 'worker_message'
                and 'FOUNDRY_MANAGER_QUESTION' in protocol_markers(row.get('text') or ''))
        ][-5:],
        'commander_question': coordination.get('commander_question') or active.get('assignment_refusal_question'),
        'last_manager_decision': {
            'backend': manager.get('backend'), 'model': manager.get('model'),
            'action': decision.get('action'), 'instruction': (decision.get('instruction') or '')[:1000] or None,
            'receipt': decision.get('evidence') or coordination.get('last_decision'),
        } if decision or coordination.get('last_decision') else None,
    }


def _budget(budget, observed_at, *, settled=False):
    elapsed = max(0, int(observed_at - budget['started_at']))
    exceeded = elapsed >= budget['minutes'] * 60
    return {**budget, 'elapsed_seconds': elapsed, 'exceeded': exceeded, 'report_due': exceeded and not settled}


def _manager_wait(live_call, native_calls):
    """Project existing observations; never infer deadlock from silence or age."""
    calls = [row for row in native_calls if row.get('role') in ('manager', 'expert')]
    pending = [row for row in calls if row.get('activity') != 'receipt_available']
    live_call = live_call or {}
    if any(row.get('process_state') == 'exited' for row in pending):
        state = 'process_exited_receipt_pending'
    elif any(row.get('activity') == 'terminal_observed' for row in pending):
        state = 'native_terminal_receipt_pending'
    elif any(row.get('process_state') == 'exists' for row in pending):
        state = 'native_call_in_flight'
    elif any(row.get('activity') == 'process_not_observed' for row in pending):
        state = 'reserved_process_not_observed'
    elif pending:
        state = 'native_call_unconfirmed'
    elif live_call.get('state') == 'running':
        state = 'coordination_without_active_native_evidence'
    else:
        state = 'no_active_call_observed'
    return {'state': state, 'coordination_stage': live_call.get('stage'),
            'coordination_elapsed_seconds': live_call.get('observed_elapsed_seconds'),
            'native_calls': calls, 'deadlock': 'not_established',
            'note': 'Coordination includes local preparation, native calls and settlement. '
                    'It is not provider response time. Process existence and output silence '
                    'do not establish progress or deadlock; inspect evidence before recovery.'}


def _goal_status(folder):
    folder = Path(folder).resolve(strict=True)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    run_id = identity(folder, manifest)
    report = {'goal': str(folder), 'run_id': run_id, 'tag': manifest.get('tag'),
              'originating_session': manifest.get('originating_session'),
              'read_only': True, 'errors': [], 'task_acceptance': 'not_evaluated'}
    from ._expert_consultation import observe as observe_experts
    active_folder, active = folder, manifest
    if manifest.get('runtime_upgrades'):
        from ._foundry_goal_upgrade import advice
        try:
            report['runtime_upgrade'] = advice(folder, manifest)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report['errors'].append('upgrade advice: ' + str(exc))
    try:
        from ._foundry_goal_auto import active_round
        active_folder, active = active_round(folder, manifest)
        from . import foundry_goal as owner
        from ._foundry_goal_continuation import observed_continuation
        try:
            child, _ = observed_continuation(owner, active_folder, active)
            if child != active_folder:
                current = _goal_status(child)
                return {**current, **report, 'errors': current['errors'] + report['errors'],
                        'continuation_goal': current.get('continuation_goal', str(child))}
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            report['errors'].append('continuation observation: ' + str(exc))
        report['phase'] = active.get('manager_phase')
        report['active_round'] = str(active_folder)
        report['runtime_observation'] = active.get('runtime_provenance')
        report['dispatch_observation'] = _current_dispatch(active_folder, active)
        executions = report['dispatch_observation']['executions']
        worker = executions[0] if len(executions) == 1 else {}
        if len(executions) == 1:
            report['activity_observation'] = executions[0].get('activity_observation')
        from ._launch_context import compaction_observation
        launch = active.get('launch_context')
        reference = active.get('pending_dispatch')
        if reference:
            from ._foundry_goal_usage import _reference
            try:
                launch = json.loads(_reference(reference)).get('launch_context')
                from ._provider_activity import execution_activity
                report['activity_observation'] = execution_activity(
                    str(reference['path']).removesuffix('.package.json') + '.log',
                    active.get('coding_launcher'), session=active.get('coding_session'))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                launch = None
                report['compaction_observation_error'] = str(exc)
        report['index_observation'] = worker.get('index_observation') or (launch or {}).get('index_observation')
        report['compaction'] = compaction_observation(launch,
            'claude' if active.get('coding_launcher') == 'claude' else 'codex')
        from ._foundry_goal_delivery import coding_recovery
        report['coding_recovery'] = coding_recovery(active_folder, active)
        report['expert_consultations'] = observe_experts(active.get('manager_state') or active.get('expert_state'))
        report['task_deadline_at'] = active.get('task_deadline_at')
        if manifest.get('runtime_upgrades'):
            report['active_runtime_provenance'] = active.get('runtime_provenance')
        report['coordination'] = active.get('coordination')
        report['live_manager_error'] = active.get('live_manager_error')
        from ._foundry_cli_help import unresolved_native_calls
        report['unresolved_native_calls'] = unresolved_native_calls(active_folder)
        if report['unresolved_native_calls']:
            from ._foundry_cli_help import recovery_advice
            try:
                report['native_reconciliation'] = recovery_advice(active['order']['path'], folder)['native_reconciliation']
            except (ValueError, KeyError, TypeError) as exc:
                report['native_reconciliation'] = {'status': 'unavailable', 'reason': str(exc)}
        from ._goal_intervention import retain_events
        report['unanswered_live_events'] = retain_events(active, [])
        observed_at = time.time()
        report['unanswered_event_ages'] = [
            {'item_id': event.get('item_id'), 'received_at': event.get('received_at'),
             'waiting_seconds': max(0, observed_at - event['received_at'])
                if type(event.get('received_at')) in (int, float) else None}
            for event in report['unanswered_live_events']]
        from ._member_activity import native_call_status
        state = active.get('manager_state') or {}
        report['active_native_calls'] = []
        if state.get('native_dir'):
            report['active_native_calls'].extend(
                {'role': 'manager', **native_call_status(call, state.get('backend'), now=observed_at)}
                for call in sorted(Path(state['native_dir']).glob('call-*'))
                if not (call / 'receipt.json').exists())
        for consultation in report['expert_consultations']:
            report['active_native_calls'].extend(
                {'role': 'expert', **native_call_status(call['evidence'], call.get('backend'), now=observed_at)}
                for call in consultation['calls'] if call.get('outcome') is None)
        report['live_manager_call'] = active.get('live_manager_call')
        if (report['live_manager_call'] or {}).get('state') == 'running':
            elapsed = max(0, observed_at - report['live_manager_call']['started_at'])
            warning_at = report['live_manager_call'].get('soft_warning_seconds')
            report['live_manager_call'] = {**report['live_manager_call'],
                'observed_elapsed_seconds': elapsed,
                'soft_warning': isinstance(warning_at, (int, float)) and elapsed >= warning_at,
                'liveness': 'not_proven_by_elapsed_time'}
        report['manager_wait_observation'] = _manager_wait(
            report['live_manager_call'], report['active_native_calls'])
        report['candidate_closure'] = active.get('candidate_closure')
        coding_reference = active.get('coding_checkpoint')
        if coding_reference:
            from ._foundry_goal_usage import _reference
            try:
                coding_checkpoint = json.loads(_reference(coding_reference))
                report['commander_messages'] = [fact for fact in coding_checkpoint.get('evidence', [])
                                               if fact.get('path', '').endswith('.messages.jsonl')]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                report['commander_messages_gap'] = str(exc)
        report['progress'] = _progress(report, active, worker)
        if active.get('disposable_scratch'):
            report['scratch'] = {**active['disposable_scratch'], 'status': 'registered'}
            usage = active_folder / 'usage.json'
            if usage.is_file():
                recorded_scratch = json.loads(usage.read_bytes()).get('scratch')
                if recorded_scratch:
                    report['scratch'].update(recorded_scratch)
        state = active.get('manager_state') or {}
        if state.get('native_dir'):
            waits = []
            for call in Path(state['native_dir']).glob('call-*/receipt.json'):
                receipt = json.loads(call.read_bytes())
                stop = receipt.get('provider_stop') or {}
                if stop.get('automatic_resume_at') is not None:
                    waits.append({'call_id': receipt['call_id'], 'session_id': receipt.get('session_id'),
                                  **stop, 'evidence': str(call)})
            report['manager_quota_observations'] = waits
        from ._goal_intervention import observe as observe_interventions, input_root
        report['manager_interventions'] = observe_interventions(input_root(folder, manifest))
        if active.get('order_revisions'):
            from . import foundry_goal as owner
            effective, revisions = owner.order_revision.records(owner, active)
            report['order_revision'] = {
                'original_order': active['order'], 'effective_order': effective,
                'evidence': revisions[-1][0],
                'recovery_runtime': active.get('order_revision_runtime'),
                'manager_reconciliation_pending': owner.order_revision.pending(active),
                'note': 'Contract admission and manager receipt are not implementation acceptance.'}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        report['errors'].append('expert observation: ' + str(exc))
    from ._foundry_goal_auto import outcome_path as selected_outcome_path
    outcome_path = selected_outcome_path(folder, manifest)
    budget = active.get('time_budget') or manifest.get('time_budget')
    if budget:
        report['time_budget'] = _budget(budget, time.time())
    if not outcome_path.is_file() or (manifest.get('runtime_upgrades') and manifest.get('state') != 'completed'):
        from ._executor_lifecycle import observe_locked_byte
        controller = observe_locked_byte(folder.with_name('.' + folder.name + '.owner.lock'))
        if controller['state'] == 'held':
            # A pending worker is not a recovery incident. The controller owns
            # handback; exposing executable recovery advice here invites replay.
            report['coding_recovery'] = None
        if controller['state'] == 'released':
            # Re-read after observation: publishing an outcome precedes lock release.
            latest = json.loads((folder/'manifest.json').read_bytes())
            if outcome_path.is_file() and not (latest.get('runtime_upgrades') and latest.get('state') != 'completed'):
                return _goal_status(folder)
            if active.get('review_checkpoint') and not (active_folder / 'outcome.json').is_file():
                from ._foundry_goal_delivery import review_feedback
                try:
                    report['feedback'] = review_feedback(active_folder / 'outcome.json')
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    report['errors'].append('review feedback: ' + str(exc))
            recovered = report.get('coding_recovery') or {}
            settled = recovered.get('status') == 'explicit_continuation_available'
            return {**report, 'status': 'execution_stopped', 'controller_observation': controller,
                    'event_id': hashlib.sha256(json.dumps([run_id, controller], sort_keys=True).encode()).hexdigest(),
                    'reason': ('Controller stopped without a goal outcome; its recorded coding attempt failed and has verified cleanup.'
                               if settled else 'Controller released its lock without a goal outcome; child cleanup is not established.'),
                    'next_action': recovered.get('next_action') or
                        'Inspect retained exit/cleanup evidence and the original goal recovery command; do not relaunch automatically.'}
        return {**report, 'status': 'pending', 'event_id': None,
                'controller_observation': controller,
                'phase': active.get('manager_phase'),
                'recorded_error': manifest.get('error'),
                'note': 'No goal outcome yet; pending does not prove a live process.'}
    raw = outcome_path.read_bytes()
    outcome = json.loads(raw)
    if not isinstance(outcome, dict) or not isinstance(outcome.get('status'), str):
        raise ValueError('goal outcome has no status')
    from ._foundry_goal_usage import delivery
    report['delivery'] = delivery(outcome)
    from ._dispatch_package import read_intent, execution_binding
    round_identity = {'directory': str(active_folder), 'run_id': active.get('run_id')}
    for fact in reversed((outcome.get('coding') or {}).get('evidence', [])):
        log = Path(fact['path'])
        if log.suffix != '.log':
            continue
        package_path = log.with_suffix('.package.json')
        if not package_path.is_file():
            continue
        try:
            package = json.loads(package_path.read_bytes())
            if package.get('goal') != round_identity or not package.get('intent_journal'):
                continue
            _, intent = read_intent(package)
            if (intent.get('evidence') or {}).get('log') == fact:
                report['settled_execution'] = execution_binding(package, intent)
                break
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            report['errors'].append('settled execution: ' + str(exc))
    if outcome['status'] == 'awaiting_input':
        from ._foundry_cli_help import recovery_invocation
        try:
            report['input_continuation'] = recovery_invocation(
                manifest['order']['path'], folder, manifest, prompt_file='<answer.md>')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report['errors'].append('input continuation: ' + str(exc))
    if outcome.get('review_completion_required'):
        from ._executor_lifecycle import observe_locked_byte
        controller = observe_locked_byte(folder.with_name('.' + folder.name + '.owner.lock'))
        if controller['state'] == 'held':
            return {**report, 'coding_recovery': None, 'status': 'pending', 'event_id': None,
                    'controller_observation': controller, 'phase': 'review'}
    from ._foundry_goal_delivery import review_feedback
    try:
        report['feedback'] = review_feedback(outcome_path)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        report['errors'].append('review feedback: ' + str(exc))
    if budget:
        report['time_budget'] = _budget(budget, outcome_path.stat().st_mtime, settled=True)
    return {**report, 'status': 'outcome_available', 'outcome_status': outcome['status'],
            'event_id': hashlib.sha256((run_id + '\0').encode() + raw).hexdigest(),
            'outcome_path': str(outcome_path),
            **{key: outcome[key] for key in ('reason', 'next_action', 'project_action', 'input_request') if key in outcome}}


def announce_dispatch(path, receipt):
    from ._hosted import active_directory
    if goal_scope.get() or (not receipt.get('originating_session') and active_directory.get() is None):
        return  # The outer goal owns completion; a coding return is intermediate.
    _announce({
        'dispatch': str(path.resolve()), 'run_id': receipt['intent_id'] + ':' + receipt['attempt_id'],
        'originating_session': receipt.get('originating_session'),
        'status_argv': [sys.executable, '-m', 'foundry_runtime', '--foundry-project', 'status',
                        '--status-dispatch', str(path.resolve())]})


def dispatch_status(path, *, plan=False):
    from ._followup_host import enrich
    report = _dispatch_status(path)
    return enrich(report, [sys.executable, '-m', 'foundry_runtime', '--foundry-project',
                           'status', '--status-dispatch', report['dispatch']], plan=plan)


def _dispatch_status(path):
    from ._dispatch_package import read_intent, execution_binding
    from ._project_status import _liveness
    path = Path(path).resolve(strict=True)
    package = json.loads(path.read_bytes())
    journal, record = read_intent(package)
    run_id = package['intent_id'] + ':' + package['attempt_id']
    report = {'run_id': run_id, 'dispatch': str(path), 'journal': str(journal),
              'originating_session': package.get('originating_session'), 'read_only': True,
              'task_acceptance': 'not_evaluated'}
    if record.get('attempt_id') != package['attempt_id']:
        return {**report, 'status': 'superseded', 'event_id': run_id + ':superseded',
                'next_action': 'This attempt was replaced; inspect the explicit retry binding.'}
    live = _liveness(record)
    report['execution_binding'] = execution_binding(package, record)
    from ._provider_activity import execution_activity
    from ._native_session import recorded_session
    log = record.get('evidence', {}).get('log', {}).get('path') or str(path).removesuffix('.package.json') + '.log'
    launcher = record.get('frozen', {}).get('launcher')
    session = recorded_session(record, log)
    report['worker'] = {'launcher': launcher, 'session_id': session,
                        'model': record['frozen'].get('requested_compute', {}).get('model')}
    report['index_observation'] = (package.get('launch_context') or {}).get('index_observation')
    live_log = Path(log).with_suffix('.claude.jsonl') if launcher == 'claude' else Path(log)
    report['live_event_log'] = str(live_log.resolve()) if launcher in ('claude', 'app-server', 'exec') else None
    report['message_projection'] = str(Path(log).with_suffix('.messages.jsonl'))
    report['activity_observation'] = execution_activity(log, launcher, session=session)
    from ._provider_activity import observe
    report['provider_observation'] = observe(log, record.get('frozen', {}).get('launcher'))
    report['runtime_observation'] = package.get('runtime_provenance')
    state = record['state']
    result = ('result_available' if state == 'completed' else
              'execution_stopped' if state in ('not_started', 'closed') or live['owner'] == 'exited' else
              'reconciliation_required' if state == 'unknown' else 'pending')
    if result != 'pending' and state in ('active', 'unknown'):
        from .recovery import command_plan
        try:
            report['recovery_commands'] = command_plan(path, journal, record)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report['recovery_commands'] = {'status': 'unavailable', 'reason': str(exc)}
    return {**report, 'status': result, 'recorded_state': state,
            'process_observation': live,
            'returncode': record.get('returncode'), 'evidence': record.get('evidence', {}),
            'event_id': None if result == 'pending' else hashlib.sha256(json.dumps(
                [run_id, result, state, record.get('returncode')], sort_keys=True).encode()).hexdigest()}
