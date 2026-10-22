"""Read the existing project execution journal; never maintain another task table."""
import json
from pathlib import Path

from ._execution_intent import ExecutionIntent
from ._executor_lifecycle import process_identity_exited
from ._foundry_project import identity, load
from ._task_identity import normalized_line


def resume_argv(config, tag, session, frozen, python='<recorded-python>', *, entry=None,
                prompt_file='<new-instructions.md>'):
    launcher = frozen.get('launcher', 'exec').replace('exec-resume', 'exec')
    argv = list(entry) if entry is not None else [python, '-m', 'foundry_runtime', '--project-config', str(config)]
    argv += ['--resume', session, '--tag', tag, '--launcher', launcher, '--prompt-file', prompt_file]
    for key in ('model', 'effort'):
        if (value := frozen.get('requested_compute', {}).get(key)) is not None:
            argv += ['--coding-' + key, value]
    for key, value in frozen.get('app_server_policy', {}).items():
        if value is not None:
            argv += ['--app-server-' + key.replace('_', '-')]
            argv += [str(item) for item in value] if key == 'capacity_retry_seconds' else [str(value)]
    # Per-call preflight parameters deliberately do not follow the prior turn.
    return argv


def bound_package(record, intent_id, package_dir=None):
    """Locate an existing receipt by attempt and intent, including live attempts."""
    log = record.get('evidence', {}).get('log', {}).get('path')
    paths = ([Path(log).with_suffix('.package.json')] if log else
             sorted(Path(package_dir).glob('*.package.json')) if package_dir else [])
    found = []
    for path in paths:
        try:
            package = json.loads(path.read_bytes())
            if package['attempt_id'] != record['attempt_id'] or package['intent_id'] != intent_id:
                continue
            found.append((path, package))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return found[0] if len(found) == 1 else (None, {})


def recorded_python(record, intent_id, package_dir=None):
    """Read this attempt's interpreter, never today's selection."""
    _, package = bound_package(record, intent_id, package_dir)
    return package.get('runtime_provenance', {}).get('python') or '<recorded-python>'


def _liveness(record):
    result = {}
    for role in ('owner', 'root'):
        pid, created = record.get(role + '_pid'), record.get(role + '_creation_time')
        if pid is None or created is None:
            result[role] = 'unobserved'
        else:
            try:
                result[role] = 'exited' if process_identity_exited(pid, created) else 'alive'
            except (OSError, RuntimeError):
                result[role] = 'unobserved'
    return result


def status(config=None, tag=None, *, workspace=None, evidence_root=None):
    if config:
        config = Path(config).resolve(strict=True)
        project = load(config)
        current = identity(project['workspace'])
        if any(current[key] != project[key] for key in current):
            raise ValueError('project repository identity changed')
    else:
        project = {**identity(workspace), 'evidence_root': str(Path(evidence_root).resolve())}
    root = Path(project['git_common_dir']) / 'pdcl-dispatch'
    folders = [root / ('task-' + normalized_line(tag))] if tag else sorted(root.glob('task-*'))
    results, errors = [], []
    for folder in folders:
        task = folder.name.removeprefix('task-')
        for path in sorted((folder / 'intents').glob('*')):
            try:
                record, sequence, _ = ExecutionIntent.read_history(path, task_key=task)
                frozen = record['frozen']
                if config and (Path(frozen['worktree']).resolve() != Path(project['workspace']).resolve()
                        or frozen.get('project_context', {}).get('project_id') != project['project_id']
                        or frozen.get('project_context', {}).get('descriptor') != str(config)):
                    continue
                last = json.loads((path / f'{sequence:08d}.json').read_bytes())
                log = record.get('evidence', {}).get('log', {}).get('path')
                from ._native_session import recorded_session
                session = recorded_session(record)
                state = record['state']
                package_path, package = bound_package(record, path.name, Path(project['evidence_root']) / task)
                python = package.get('runtime_provenance', {}).get('python') or '<recorded-python>'
                if not log and package_path:
                    log = str(package_path.with_name(package_path.name.removesuffix('.package.json') + '.log'))
                from ._provider_activity import observe, execution_activity
                provider = observe(log, frozen.get('launcher'))
                live = _liveness(record) if state in ('starting', 'active', 'unknown') else {}
                action = ('inspect_recovery' if record.get('recovery_authorization') else
                          'reconcile' if state == 'unknown' or
                          (state in ('starting', 'active') and live.get('owner') == 'exited') else
                          'wait_for_provider_capacity' if state in ('starting', 'active') and provider['status'] == 'quota_blocked' else
                          'wait' if state in ('starting', 'active') else
                          'resume_after_provider_reset' if state == 'completed' and session and provider['status'] == 'quota_blocked' else
                          'new_instructions' if state == 'completed' and session else
                          'retry_original_command' if state == 'not_started' else 'inspect_evidence')
                from ._launch_context import compaction_observation
                row = {'compaction': compaction_observation(package.get('launch_context'),
                           'claude' if frozen.get('launcher') == 'claude' else 'codex'), 'tag': task, 'intent_id': path.name, 'recorded_state': state,
                       'updated_utc': last['created_utc'], 'operation': frozen['operation'],
                       'launcher': frozen.get('launcher'), 'session_id': session,
                       'journal': str(path), 'evidence_root': project['evidence_root'],
                       'evidence': record.get('evidence', {}), 'returncode': record.get('returncode'),
                       'process_observation': live, 'provider_observation': provider, 'next_action': action,
                       'activity_observation': execution_activity(log, frozen.get('launcher'), session=session),
                       'index_observation': package.get('launch_context', {}).get('index_observation') or
                                            {'status': 'unrecorded', 'reason': 'no retained launch observation'},
                       'runtime_observation': {key: package.get('runtime_provenance', {}).get(key)
                                               for key in ('python', 'version')},
                       'task_acceptance': 'not_evaluated'}
                if log and state == 'completed':
                    receipt_path = Path(log).with_suffix('.receipt.json')
                    if receipt_path.exists():
                        try:
                            receipt = json.loads(receipt_path.read_bytes())
                            if receipt.get('execution_intent_id') == path.name and receipt.get('attempt_id') == record.get('attempt_id'):
                                recovery = receipt.get('provider_recovery')
                                if isinstance(recovery, dict):
                                    row['provider_recovery'] = recovery
                                    if recovery.get('status') == 'deadline_prevents_retry':
                                        row['execution_status'] = 'deadline_prevents_retry'
                                        row['next_action'] = 'inspect_deadline_and_retained_session'
                                    if recovery.get('status') == 'provider_capacity_exhausted':
                                        row['execution_status'] = 'provider_capacity_exhausted'
                                    elif recovery.get('status') in ('backoff', 'interrupted'):
                                        prefix = 'quota_' if recovery.get('reason') == 'provider_limit' else 'capacity_'
                                        row['execution_status'] = prefix + recovery['status']
                                        row['next_action'] = ('retry_original_command_to_continue_capacity_wait'
                                            if recovery['status'] == 'interrupted' else
                                            'inspect_controller_before_retrying_original_command')
                        except (OSError, ValueError, AttributeError) as exc:
                            row['receipt_observation_error'] = str(exc)
                if log:
                    messages = Path(log).with_suffix('.messages.jsonl')
                    if messages.is_file():
                        row['commander_messages'] = {'path': str(messages),
                            'scope': 'completed root messages; not proof of receipt or answered questions'}
                if action in ('new_instructions', 'resume_after_provider_reset') and config:
                    row['resume_argv'] = resume_argv(config, task, session, frozen, python)
                if action in ('new_instructions', 'resume_after_provider_reset') and not config:
                    adapter = package.get('runtime_provenance', {}).get('adapter') or {}
                    if adapter.get('module') == '_pdcl_project' and adapter.get('path'):
                        # Reuse transport/model/policy arguments from descriptor resumes.
                        continuation = resume_argv('', task, session, frozen, python)
                        row['resume_argv'] = [python, str(Path(adapter['path']).with_name('dispatch.py')),
                                              *continuation[5:], '--worktree', frozen['worktree']]
                if action == 'reconcile' and not package_path:
                    row['recovery_commands'] = {'status': 'unavailable',
                        'reason': 'Original dispatch package is missing or ambiguous; recorded interpreter and entry cannot be determined.'}
                if action == 'reconcile' and package_path:
                    from .recovery import command_plan
                    try:
                        row['recovery_commands'] = command_plan(package_path, path, record)
                        if row['recovery_commands'].get('argv'):
                            row['reconcile_argv'] = row['recovery_commands']['argv']['reconcile']
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        row['recovery_commands'] = {'status': 'unavailable', 'reason': str(exc)}
                results.append(row)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                errors.append({'journal': str(path), 'error': str(exc)})
    return {'status': 'observed' if not errors else 'partial', 'project_id': project['project_id'],
            'workspace': project['workspace'], 'runs': sorted(results, key=lambda r: r['updated_utc']),
            'errors': errors, 'read_only': True,
            'note': 'Recorded execution and live process observations are separate; no project acceptance is inferred.'}
