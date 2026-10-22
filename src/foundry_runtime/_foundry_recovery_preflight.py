"""Read-only recovery planning through the retained native and goal authorities.

The selected interpreter inspects its own policy and every retained call. This
does not reconcile receipts, acquire goal locks, or start a provider process.
"""
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
from contextlib import contextmanager


def _probe(request):
    # Kept self-contained so an older installed runtime can inspect its own
    # contracts without importing the new runtime into that interpreter.
    import inspect
    import json
    from pathlib import Path
    from foundry_runtime import foundry_goal as goal
    from foundry_runtime.foundry_native import NativeAnalyst
    from foundry_runtime._compute_selection import compute_kwargs
    from foundry_runtime._foundry_goal_transitions import manager_history

    root = Path(request['folder'])
    manifest = json.loads((root / 'manifest.json').read_bytes())
    state = manifest['manager_state']
    native = Path(state['native_dir'])
    policy = json.loads((native / 'native-manifest.json').read_bytes())
    checks = []
    pending_receipts = []

    def check(name, action):
        try:
            result = action()
            checks.append({'check': name, 'status': 'passed', 'detail': result})
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            checks.append({'check': name, 'status': 'unavailable', 'reason': str(exc)})

    def contract():
        if manifest.get('order_revisions') and 'order_revision_file' not in inspect.signature(goal.recover_goal).parameters:
            raise ValueError('recorded interpreter does not support the admitted order revision')
        if not request.get('revision_validated') and not goal.order_revision.matches(goal, Path(request['order']), manifest):
            raise ValueError('order differs; an explicit --order-revision is required')
        return 'recorded goal/order contract readable'

    def checkpoint():
        goal._manager_checkpoint(root, manifest, allow_failed=request.get('answer_supplied', False))
        return 'retained checkpoint and execution evidence verified'

    def native_restore():
        analyst = NativeAnalyst(Path(policy['executable']), native,
            backend=state['backend'], workspace=manifest['worktree'],
            recorded_coding_model=state.get('recorded_coding_model'),
            **compute_kwargs(state.get('model'), state.get('effort'), backend=state['backend']),
            timeout_seconds=None, max_total_input_bytes=None, resume=True,
            **({'execution_profiles': state['execution_profiles']} if state.get('execution_profiles') else {}))
        rows = [{'call_id': row['allocation'].get('call_id'), 'state': row['state'],
                 'folder': str(row['folder']), 'error': row.get('error')} for row in analyst._contexts.values()]
        unknown = [row for row in rows if row['state'] == 'unknown']
        if unknown and all(not (Path(row['folder']) / 'receipt.json').exists() for row in unknown):
            pending_receipts.extend(unknown)
            return {'calls': len(rows), 'reconciliation_required': unknown,
                    'next_action': 'existing goal recovery reconciles missing receipts under its controller lock'}
        if unknown:
            raise ValueError(json.dumps({'unrestored_calls': unknown}, ensure_ascii=True))
        return {'calls': len(rows), 'all_receipts_restored': True,
                'retained_cancellation': analyst._cancelled.is_set()}

    def history():
        if pending_receipts:
            return {'deferred_to': 'existing missing-receipt reconciliation', 'calls': len(pending_receipts)}
        calls, latest, _ = manager_history(goal, state, manifest['worktree'],
                                           source_locked=True, allow_empty=True)
        # A pending latest decision can exist beyond last_call_id. The retained
        # owner consumes it on recovery; do not equate that with corruption.
        return {'calls': len(calls), 'frontier': latest}

    check('goal_contract', contract)
    check('checkpoint', checkpoint)
    check('native_policy_and_invocations', native_restore)
    check('archived_history', history)
    return {'status': ('reconciliation_required' if pending_receipts else 'ready') if all(c['status'] == 'passed' for c in checks) else 'attention_required',
            'checks': checks, 'run_id': manifest['run_id'],
            'scope': 'read-only local recovery prerequisites; no provider health or project acceptance claim'}


def retained_environment(policy):
    """Translate the existing launch authority, not a second set of defaults."""
    settings = (policy.get('backend_identity') or {}).get('launch_context') or {}
    return {'FOUNDRY_' + key.upper(): str(value) for key, value in settings.items()
            if key in {'context_tokens', 'compact_tokens', 'compact_percent', 'bash_chars', 'tool_tokens'}}


def inspect_recovery(folder, manifest, order, *, answer_supplied=False, revision_validated=False, project_adapter=None):
    state = manifest.get('manager_state') or {}
    native = state.get('native_dir')
    if not native or not (Path(native) / 'native-manifest.json').exists():
        return {'status': 'not_applicable', 'reason': 'no retained native manager policy'}
    policy = json.loads((Path(native) / 'native-manifest.json').read_bytes())
    python = (policy.get('runtime_provenance') or {}).get('python')
    if revision_validated and manifest.get('manager_phase') == 'awaiting_input':
        # The current admission owner implements question-successor revision.
        # It must prove compatibility before admitting that revision, or offer
        # an explicit migration through the same successor lifecycle.
        python = sys.executable
    environment = retained_environment(policy)
    report = {'runtime_python': python, 'environment': environment, 'evidence': str(folder)}
    conflicts = {}
    descriptor = ((manifest.get('project_record') or {}).get('context') or {}).get('descriptor')
    if descriptor:
        launch = json.loads(Path(descriptor).read_bytes()).get('launch', {})
        conflicts = {key: {'recorded': value, 'descriptor': str(launch[key])}
                     for key, value in environment.items() if key in launch and str(launch[key]) != value}
    elif project_adapter is not None:
        from contextlib import nullcontext
        with launch_scope(environment), getattr(project_adapter, 'launch_scope', nullcontext)():
            conflicts = {key: {'recorded': value, 'project_adapter': os.environ.get(key)}
                         for key, value in environment.items() if os.environ.get(key) != value}
    source = inspect.getsource(_probe) + '\nimport json,sys\nprint(json.dumps(_probe(json.load(sys.stdin)), ensure_ascii=True))\n'
    env = {**os.environ, **environment, 'PYTHONDONTWRITEBYTECODE': '1'}
    env.pop('PYTHONPATH', None)
    request = {'folder': str(folder), 'order': str(order), 'answer_supplied': answer_supplied,
               'revision_validated': revision_validated}
    if not python or not Path(python).is_file():
        # The current archive reader can still explain whether an explicit
        # transition is possible; absence of the old executable is not cleanup.
        try:
            with launch_scope(environment):
                observed = _probe(request)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            observed = {'checks': [], 'archive_error': str(exc)}
        return {**report, **observed, 'status': 'attention_required',
                'reason': 'recorded native interpreter is missing; restore it or use an eligible explicit runtime transition'}
    try:
        result = subprocess.run([python, '-B', '-c', source],
            input=json.dumps(request),
            text=True, encoding='utf-8', capture_output=True, env=env,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=120)
        if result.returncode:
            raise ValueError(result.stderr.strip() or 'recorded runtime inspection failed')
        observed = json.loads(result.stdout)
        if conflicts and Path(python).resolve() != Path(sys.executable).resolve():
            observed.update(status='attention_required', launch_conflicts=conflicts,
                reason='historical project entry overrides retained settings; use the explicit runtime transition')
        return {**report, **observed}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {**report, 'status': 'attention_required', 'reason': str(exc)}


@contextmanager
def launch_scope(environment):
    """Apply the selected launch to this command, restoring the caller on exit."""
    previous = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def prepare(args, *, project_adapter=None):
    """Plan only ordinary manager recovery; explicit transitions retain ownership."""
    if not args.recover_goal:
        if args.recovery_preflight:
            raise ValueError('--recovery-preflight requires --recover-goal')
        return None
    if args.migrate_runtime or args.handoff_provider or args.upgrade_runtime or args.retained_only or args.recovery_file:
        if args.recovery_preflight:
            raise ValueError('preflight inspects same-session recovery; explicit transitions use their retained-history checks')
        return None
    from ._foundry_goal_auto import active_round
    from ._foundry_cli_help import recovery_invocation
    folder = Path(args.recover_goal)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    active, current = active_round(folder, manifest, consumed=not args.prompt_file)
    if (current.get('review_checkpoint') or current.get('manager_phase') == 'dispatch_pending'
            or ((active / 'outcome.json').exists() and current.get('manager_phase') != 'awaiting_input')):
        return {'status': 'not_applicable', 'reason': 'review, delivered outcome or dispatch reconciliation is owned by its existing recovery route'}
    if args.order_revision:
        from . import foundry_goal as goal
        goal.order_revision.validate(goal, active, current, Path(args.order), Path(args.order_revision))
    report = inspect_recovery(active, current, args.order, answer_supplied=bool(args.prompt_file),
                              revision_validated=bool(args.order_revision), project_adapter=project_adapter)
    recorded, _ = active_round(folder, manifest)
    if active != recorded:
        report['consumed_continuation'] = str(active)
    if report['status'] != 'not_applicable':
        report['invocation'] = recovery_invocation(args.order, folder, current,
            python=report.get('runtime_python'), prompt_file=args.prompt_file or None,
            extra_args=['--order-revision', args.order_revision] if args.order_revision else [])
        report['invocation']['verification'] = report['status']
        checks = {row['check']: row['status'] for row in report.get('checks', [])}
        if (report['status'] == 'attention_required'
                and all(checks.get(name) == 'passed' for name in ('goal_contract', 'checkpoint', 'archived_history'))
                and (current.get('manager_phase') in {'assignment_pending', 'feedback_pending', 'delegation_ready'}
                     and not (active / 'outcome.json').exists() or current.get('manager_phase') == 'awaiting_input')):
            from ._foundry_order_revision import pending
            needs_answer = (current.get('manager_phase') == 'awaiting_input'
                            and not args.order_revision and not pending(current) and not args.prompt_file)
            report['explicit_migration'] = recovery_invocation(args.order, folder, current,
                python=sys.executable, prompt_file='<prompt-file>' if needs_answer else args.prompt_file or None,
                extra_args=['--migrate-runtime', '--handoff-role', 'manager',
                            *(['--order-revision', args.order_revision] if args.order_revision else [])])
            report['explicit_migration']['semantics'] = 'New manager generation using verified retained history; no automatic migration or worker replay'
            report['explicit_migration']['operator_answer_required'] = needs_answer
    return report


def execute_recorded(report, argv, *, entry=None):
    """Delegate the unchanged request to the existing recorded CLI, not a new runner."""
    python = report['runtime_python']
    if report.get('consumed_continuation'):
        # Old interpreters do not know the new resolver. Pass the already
        # verified continuation explicitly instead of asking them the old question.
        selected, skip = [], False
        for value in argv:
            if skip:
                skip = False
            elif value == '--recover-goal':
                selected.extend([value, report['consumed_continuation']]); skip = True
            elif value.startswith('--recover-goal='):
                selected.append('--recover-goal=' + report['consumed_continuation'])
            else:
                selected.append(value)
        argv = selected
    if entry:
        # PDCL's wrapper owns its runtime pin. Replace its explicit selection,
        # retaining every business option and its existing admission rules.
        remaining, skip = [], False
        for value in argv:
            if skip:
                skip = False
            elif value == '--runtime-python':
                skip = True
            elif not value.startswith('--runtime-python='):
                remaining.append(value)
        command = [python, entry, '--runtime-python', python, *remaining]
    else:
        command = [python, '-m', 'foundry_runtime', *argv]
    return subprocess.call(command, env={**os.environ, **report['environment']},
                           stdout=sys.stdout, stderr=sys.stderr,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
