"""Prepare recovery provenance from an existing named command or project grant.

Writes evidence or closes an abandoned intent only. dispatch.py --resume --recovery-file remains the sole
execution entry; this producer neither authorizes principal text nor consumes it.
"""
from datetime import datetime, timezone
import argparse
import copy
import json
from pathlib import Path
import uuid


def blocker_advice(journal, record):
    """Observe a predecessor; settlement remains an explicit operator action."""
    from ._executor_lifecycle import process_identity_exited
    report = {'holder_line': record['task_key'], 'intent_id': record['intent_id'],
              'alive': None, 'processes': {}, 'journal': str(journal)}
    try:
        for role in ('owner', 'root'):
            pid, created = record.get(role + '_pid'), record.get(role + '_creation_time')
            if pid is None and role == 'root' and record.get('state') == 'starting':
                continue
            if not pid or not created:
                raise ValueError('missing ' + role + ' process identity')
            report['processes'][role] = {'pid': pid, 'creation_time': created,
                                        'exited': process_identity_exited(pid, created)}
        report['alive'] = any(not row['exited'] for row in report['processes'].values())
        if report['alive']:
            report['next_action'] = 'The predecessor is alive; wait or inspect its existing task. No close command offered.'
            return report
        context = record['frozen'].get('project_context') or {}
        if context.get('descriptor'):
            evidence = Path(context['evidence_root']) / record['task_key']
        else:
            from ._foundry_dispatch import _project_adapter
            evidence = _project_adapter(None).evidence(record['task_key'])
        matches = []
        for path in evidence.glob('dispatch*.package.json'):
            package = json.loads(path.read_bytes())
            if (package.get('intent_id') == record['intent_id']
                    and package.get('attempt_id') == record.get('attempt_id')):
                matches.append(path)
        if len(matches) != 1:
            raise ValueError('settlement command needs one exact original package; found ' + str(len(matches)))
        plan = command_plan(matches[0], journal, record)
        if plan['status'] == 'recovery_already_recorded':
            report.update(plan)
            return report
        argv = plan['argv']['reconcile'] + ['--close-reason', '<close-reason>']
        report.update(reconcile_argv=argv, reconcile_powershell='& ' + ' '.join(
            '$CloseReason' if arg == '<close-reason>' else "'" + str(arg).replace("'", "''") + "'" for arg in argv),
            next_action='Set $CloseReason to the operator decision ending this predecessor, then run the command. '
                        'It remeasures cleanup and settles the matching lock and intent; no task is started.')
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        report['diagnostic_error'] = str(exc)
    return report


def command_plan(package_path, journal, record):
    """Read-only commands for the existing producer and execution entry."""
    package_path, journal = Path(package_path), Path(journal)
    package = json.loads(package_path.read_bytes())
    provenance = package.get('runtime_provenance') or {}
    python = provenance.get('python')
    if not python:
        raise ValueError('recovery commands need the original package runtime_provenance.python')
    if record.get('attempt_id') != package.get('attempt_id'):
        raise ValueError('recovery package is superseded by another attempt')
    if record.get('recovery_authorization') or record.get('recovery_consumption'):
        return {'status': 'recovery_already_recorded', 'next_action':
                'Read the recorded recovery successor; do not create a second authorization.',
                'recovery_authorization': record.get('recovery_authorization'),
                'recovery_consumption': record.get('recovery_consumption')}
    launcher = record['frozen']['launcher']
    kind = 'turn_bound' if launcher == 'app-server' else 'active'
    events = [(p, json.loads(p.read_bytes())) for p in sorted(journal.glob('*.json'))]
    headers = [p for p, event in events if event.get('kind') == kind and
               (event.get('data', {}).get('turn_binding') or event.get('data', {})).get('attempt_id') == record['attempt_id']]
    session = (record.get('turn_binding') or {}).get('thread_id') or record.get('session_id')
    descriptor = (record['frozen'].get('project_context') or {}).get('descriptor')
    if descriptor:
        producer = [python, '-m', 'foundry_runtime.recovery', '--project-config', descriptor]
        executor = [python, '-m', 'foundry_runtime', '--project-config', descriptor]
        prerequisites = ['Use an existing named project grant; preserve the task workspace and authorized prompt.']
    else:
        adapter = provenance.get('adapter') or {}
        if adapter.get('module') != '_pdcl_project' or not adapter.get('path'):
            raise ValueError('recovery commands need the recorded project entry')
        entry = Path(adapter['path']).parent
        producer = [python, str(entry/'produce_recovery_authorization.py')]
        executor = [python, str(entry/'dispatch.py')]
        prerequisites = ['The commander must set this LINE to blocked in canonical PORTFOLIO.md before authorized resume; status changes nothing.']
    stem = package_path.name.removesuffix('.package.json')
    cleanup = str(package_path.with_name(stem + '.recovery.cleanup.json'))
    output = str(package_path.with_name(stem + '.recovery.json'))
    common = [*producer, '--task', record['task_key'], '--predecessor', record['intent_id']]
    commands = {'reconcile': [*common, '--reconcile', '--output', cleanup]}
    def powershell(argv):
        variables = {'<prompt-file>': '$PromptFile', '<grant-anchor>': '$GrantAnchor'}
        return '& ' + ' '.join(variables.get(arg, "'" + str(arg).replace("'", "''") + "'") for arg in argv)
    if launcher not in ('app-server', 'claude') or len(headers) != 1 or not session:
        return {'status': 'reconcile_only', 'runtime_python': python, 'argv': commands,
                'powershell': {key: powershell(argv) for key, argv in commands.items()},
                'reason': 'Authorization needs a recorded native session and original app-server turn_bound or Claude active event; '
                          'exec recovery requires its original Codex startup header. Reconciliation alone does not authorize execution.'}
    from ._project_status import resume_argv
    continuation = resume_argv(descriptor or '', record['task_key'], session, record['frozen'], python,
                               entry=executor, prompt_file='<prompt-file>')
    commands.update({
        'authorize': [*common, '--session-header', str(headers[0]), '--cleanup', cleanup,
                      '--amendment', '<prompt-file>', '--operator-grant', '<grant-anchor>',
                      '--uncommitted-candidate', '--output', output],
        'resume': [*continuation, '--worktree', record['frozen']['worktree'],
                   '--recovery-file', output]})
    return {'status': 'explicit_recovery_commands', 'runtime_python': python,
            'inputs': {'GrantAnchor': 'Existing operator grant anchor', 'PromptFile': 'Absolute path to the exact resume prompt'},
            'prerequisites': prerequisites,
            'note': 'Read-only advice, not authorization. Reconcile measures original processes; it can refuse a live owner. '
                    'Authorize freezes the current candidate. Resume starts a worker; for a completed turn prefer goal manager hand-back reconciliation.',
            'existing_outputs': [p for p in (cleanup, output) if Path(p).exists()],
            'argv': commands, 'powershell': {key: powershell(argv) for key, argv in commands.items()}}


def prepare(*, task_key, predecessor, successor, operator_grant, output, session_root=None, project=None, project_adapter=None, operator_grant_document=None):
    """Caller supplies execution bindings; all attribution is read from GRANTS."""
    from ._recovery_authorization import RecoveryAuthorization
    from ._execution_bindings import _sha
    from ._project_recovery import grant_path, attribution as project_attribution, source_fields
    if project is not None:
        grants = Path(operator_grant_document).resolve(strict=True) if operator_grant_document else grant_path(project.project)
    else:
        grants = project_adapter.canonical_grants()
    raw = grants.read_bytes()
    attribution = (project_attribution(operator_grant, raw, grants) if project is not None
                   else project_adapter.attribution(operator_grant, raw))
    output = Path(output).resolve()
    source = output.with_suffix(output.suffix + '.grant.md')
    if output.exists() or source.exists():
        raise ValueError('recovery output/source already exists; preserve the original evidence')
    value = {
        'schema_version': 'pdcl-execution-recovery-5' if successor.get('candidate_snapshot') else {'claude': 'pdcl-execution-recovery-4',
                           'app-server': 'pdcl-execution-recovery-3'}.get(
                               successor.get('launcher'), 'pdcl-execution-recovery-2'),
        'decision_id': str(uuid.uuid4()), 'authorized_by': 'operator',
        'authorized_utc': datetime.now(timezone.utc).isoformat(),
        'source': {'authority': ('named-project-document' if operator_grant_document else 'named-project-grant') if project is not None else 'named-command-grant', 'path': str(source),
                   'sha256': _sha(raw), 'origin_path': str(grants.resolve()),
                   'grant_anchor': operator_grant, 'operator_authorization': attribution},
        'task_key': task_key, 'predecessor': copy.deepcopy(predecessor),
        'successor': copy.deepcopy(successor),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    if project is not None:
        value['source'].update(source_fields(project, output))
    with source.open('xb') as stream:
        stream.write(raw)
    with output.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    recovery = RecoveryAuthorization.load(
        output, task_key=task_key, worktree=successor['worktree'],
        session_id=successor['session_id'], candidate_head=successor['candidate_head'],
        amendment=None, session_root=session_root,
        source_verifier=project_adapter.verify_named_source if project is None else None)
    recovery.require_execution_source()
    return recovery


def main(argv=None, *, project_adapter=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True)
    parser.add_argument('--project-config', help='project descriptor; omit when the project entry supplies its adapter')
    parser.add_argument('--predecessor', required=True, help='original unknown intent ID')
    parser.add_argument('--reconcile', action='store_true', help='observe a dead controller and record unknown cleanup only; no authorization or execution')
    parser.add_argument('--close-reason', help='with --reconcile: close a landed or abandoned task after measuring cleanup; never starts an executor')
    parser.add_argument('--stop-reason', help='abort only the recorded executor, await owned cleanup and close its intent; requires a reason')
    parser.add_argument('--keep-recoverable', action='store_true',
                        help='with --stop-reason: preserve unknown for explicit recovery instead of closing the task; not a graceful interrupt')
    parser.add_argument('--stop-controller-after-cleanup', action='store_true',
                        help='with --stop-reason: explicitly abort the residual controller only after durable unknown/verified worker cleanup and intent lock release; final usage may remain incomplete')
    parser.add_argument('--session-header', help='Codex startup header, app-server turn_bound event, or Claude active intent event')
    parser.add_argument('--cleanup', help='existing measured predecessor cleanup receipt')
    parser.add_argument('--amendment', help='exact resume prompt; required with an initial --foundry-manifest')
    parser.add_argument('--foundry-manifest', help='original interrupted goal manifest; amendment recovery retains its prompt construction')
    parser.add_argument('--operator-grant', help='existing anchor in the selected project recovery grants')
    parser.add_argument('--operator-grant-document', help='explicit project document with the named grant; descriptor stays unchanged')
    parser.add_argument('--uncommitted-candidate', action='store_true', help='freeze index, working diff and untracked content for unfinished recovery')
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    if not args.project_config and project_adapter is None:
        parser.error('select --project-config or supply the project recovery adapter')
    if args.operator_grant_document and not args.project_config:
        parser.error('--operator-grant-document requires --project-config')
    if args.close_reason is not None and (not args.reconcile or not args.close_reason.strip()):
        parser.error('--close-reason requires --reconcile and a nonempty reason')
    if args.stop_reason is not None and (not args.stop_reason.strip() or args.reconcile or args.close_reason):
        parser.error('--stop-reason must be nonempty and cannot be combined with --reconcile/--close-reason')
    if args.keep_recoverable and not args.stop_reason:
        parser.error('--keep-recoverable requires --stop-reason')
    if args.stop_controller_after_cleanup and not args.stop_reason:
        parser.error('--stop-controller-after-cleanup requires --stop-reason')
    recovery_inputs = (args.session_header, args.cleanup, (args.amendment or args.foundry_manifest), args.operator_grant)
    if (args.reconcile or args.stop_reason) and (args.operator_grant_document or args.uncommitted_candidate):
        parser.error('reconciliation/stop does not prepare a grant or candidate')
    if (args.reconcile or args.stop_reason) and any(recovery_inputs):
        parser.error('--reconcile uses only --task, --predecessor and --output (cleanup receipt)')
    if not (args.reconcile or args.stop_reason) and not all(recovery_inputs):
        parser.error('authorization requires --session-header, --cleanup, (--amendment or --foundry-manifest) and --operator-grant')
    from ._task_identity import normalized_line
    from ._execution_intent import ExecutionIntent, IntentConflict, _reference_bytes
    from ._execution_bindings import _sha
    from ._foundry_dispatch import _worktree_clean, _worktree_head, _repository_options
    from ._native_session import read_session_header
    import re
    task = normalized_line(args.task)
    if not re.fullmatch(r'[0-9a-f]{64}', args.predecessor):
        parser.error('predecessor must be a full intent ID')
    from ._project_execution import open_project
    project = open_project(args.project_config) if args.project_config else None
    if project is not None:
        common = project.common_dir
    else:
        common = project_adapter.common_dir()
    path = common / 'pdcl-dispatch' / ('task-' + task) / 'intents' / args.predecessor
    first = json.loads((path / '00000001.json').read_bytes())
    if project is not None:
        frozen = first['data']['frozen']
        if (frozen.get('project_context', {}).get('project_id') != project.project['project_id']
                or frozen.get('project_context', {}).get('descriptor') != str(project.path)
                or Path(frozen['worktree']).resolve() != project.workspace.resolve()):
            raise ValueError('predecessor does not belong to the selected project/workspace')
    if args.reconcile or args.stop_reason:
        if args.stop_reason:
            from ._execution_stop import stop
            record = stop(common, task, first['data']['frozen'], intent_id=args.predecessor,
                          output=args.output, reason=args.stop_reason, keep_recoverable=args.keep_recoverable,
                          stop_controller_after_cleanup=args.stop_controller_after_cleanup)
        else:
            record = ExecutionIntent.settle_abandoned(common, task, first['data']['frozen'],
                intent_id=args.predecessor, output=args.output, close_reason=args.close_reason)
        # The killed controller never removed its active-turn handle; settlement stands in
        # for that exit step, and only for the handle naming exactly the settled attempt.
        from .app_server_transport import remove_attempt_handle
        handle = (project.evidence(task) if project is not None else project_adapter.evidence(task)) / 'active_turn.json'
        removed = record['state'] in {'unknown', 'closed'} and remove_attempt_handle(
            handle, record['attempt_id'], record['root_pid'], record['root_creation_time'])
        print(json.dumps({'status': record['state'], 'cleanup': str(Path(args.output).resolve()) if Path(args.output).is_file() else None,
                          'active_turn_removed': removed,
                          'execution': 'stop_requested' if args.stop_reason else 'not_started',
                          'authorization': 'not_granted'}))
        return 0
    reader = ExecutionIntent(common, task, first['data']['frozen'])
    record, _, _ = reader._read(path)
    if record['state'] != 'unknown' or 'recovery_authorization' in record:
        raise IntentConflict('preparation requires an original unknown without an existing recovery')
    tree = Path(record['frozen']['worktree'])
    if not args.uncommitted_candidate and not _worktree_clean(tree, **_repository_options(project)):
        raise IntentConflict('recovery candidate worktree must be clean')
    def reference(name):
        p = Path(name).resolve(strict=True)
        return {'path': str(p), 'sha256': _sha(p.read_bytes())}
    predecessor = {
        'intent_id': args.predecessor,
        **{k: record[k] for k in ('attempt_id', 'root_pid', 'root_creation_time')},
        'event_sha256s': {p.name: _sha(p.read_bytes()) for p in sorted(path.iterdir())},
        'cleanup': reference(args.cleanup),
    }
    if json.loads(Path(args.cleanup).read_bytes()) != record.get('cleanup'):
        raise IntentConflict('cleanup source differs from original unknown observation')
    launcher = record['frozen'].get('launcher')
    if launcher == 'app-server':
        session = record.get('turn_binding', {}).get('thread_id')
    elif launcher == 'claude':
        session = record.get('session_id')
    else:
        session = read_session_header(args.session_header, tree)
    if args.foundry_manifest:
        from . import foundry_goal
        from .foundry_goal import _amendment, amendment_prompt
        manifest = json.loads(Path(args.foundry_manifest).read_bytes())
        order = Path(manifest['order']['path'])
        if (not foundry_goal.order_revision.matches(foundry_goal, order, manifest)
                or Path(manifest['worktree']).resolve() != tree.resolve()
                or manifest.get('tag') != task):
            raise IntentConflict('Foundry recovery manifest differs from retained amendment')
        if args.amendment:
            if manifest.get('manager_phase') != 'dispatch_pending' or not manifest.get('coding_checkpoint'):
                raise IntentConflict('explicit goal worker recovery requires its pending dispatch checkpoint')
            checkpoint = json.loads(_reference_bytes(manifest['coding_checkpoint'], 'coding checkpoint'))
            if checkpoint.get('coding_session') != session:
                raise IntentConflict('pending goal worker session differs from predecessor')
            message = Path(args.amendment).read_text(encoding='utf-8-sig')
        else:
            amendment = _amendment(order, tree, manifest['amendment']['prior_outcome']['path'], session)
            if manifest['amendment'] != amendment:
                raise IntentConflict('Foundry recovery manifest differs from retained amendment')
            message = amendment_prompt(order, amendment, manifest.get('execution_decision'), args.predecessor)
    else:
        message = Path(args.amendment).read_text(encoding='utf-8-sig')
    successor = {
        'session_id': session,
        'session_header': reference(args.session_header), 'worktree': str(tree),
        'candidate_head': _worktree_head(tree, **_repository_options(project)),
        'amendment_sha256': _sha(message.encode('utf-8')),
    }
    if launcher in {'app-server', 'claude'}:
        successor['launcher'] = launcher
    if args.foundry_manifest and manifest.get('manager_state') is not None:
        if args.amendment:
            # Keep the old worker checkpoint before goal progress advances.
            checkpoint_copy = Path(args.output).resolve().with_suffix('.checkpoint.json')
            goal_copy = Path(args.output).resolve().with_suffix('.goal.json')
            checkpoint_copy.parent.mkdir(parents=True, exist_ok=True)
            with checkpoint_copy.open('xb') as stream:
                stream.write(_reference_bytes(manifest['coding_checkpoint'], 'coding checkpoint'))
            saved = {**manifest, 'coding_checkpoint': reference(checkpoint_copy)}
            with goal_copy.open('x', encoding='utf-8', newline='\n') as stream:
                json.dump(saved, stream, ensure_ascii=False, sort_keys=True)
            successor['foundry_manifest'] = reference(goal_copy)
        else:
            successor['foundry_manifest'] = reference(args.foundry_manifest)
    if args.uncommitted_candidate:
        from ._repository_observation import snapshot
        candidate = Path(args.output).resolve().with_suffix('.candidate.json')
        candidate.parent.mkdir(parents=True, exist_ok=True)
        with candidate.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(snapshot(tree, **_repository_options(project)), stream, ensure_ascii=False, sort_keys=True)
        successor['candidate_snapshot'] = reference(candidate)
    recovery = prepare(task_key=task, predecessor=predecessor, successor=successor,
                       operator_grant=args.operator_grant, output=args.output, project=project, project_adapter=project_adapter,
                       operator_grant_document=args.operator_grant_document)
    print(json.dumps({'status': 'prepared', 'path': str(recovery.path),
                      'sha256': recovery.decision_sha256, 'execution': 'not_started'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
