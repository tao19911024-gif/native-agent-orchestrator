"""Read existing execution evidence for the originating host's follow-up tools."""
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import sys

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


def status(folder, *, plan=False):
    from ._followup_host import enrich
    report = _goal_status(folder)
    return enrich(report, [sys.executable, '-m', 'foundry_runtime', '--foundry-project',
                           'status', '--status-goal', report['goal']], plan=plan)


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
    active = manifest
    if manifest.get('runtime_upgrades'):
        from ._foundry_goal_upgrade import advice
        try:
            report['runtime_upgrade'] = advice(folder, manifest)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report['errors'].append('upgrade advice: ' + str(exc))
    try:
        from ._foundry_goal_auto import active_round
        active_folder, active = active_round(folder, manifest)
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
        import time
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
            import time
            elapsed = max(0, time.time() - report['live_manager_call']['started_at'])
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
    from ._foundry_goal_delivery import review_feedback
    try:
        report['feedback'] = review_feedback(outcome_path)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        report['errors'].append('review feedback: ' + str(exc))
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
    from ._dispatch_package import read_intent
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
    from ._provider_activity import execution_activity
    from ._native_session import recorded_session
    log = record.get('evidence', {}).get('log', {}).get('path') or str(path).removesuffix('.package.json') + '.log'
    report['activity_observation'] = execution_activity(log, record.get('frozen', {}).get('launcher'),
                                                       session=recorded_session(record))
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
