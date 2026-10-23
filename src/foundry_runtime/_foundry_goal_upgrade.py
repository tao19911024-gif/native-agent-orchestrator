"""Explicit next-round runtime selection through the existing goal controller.

The parent goal manifest owns publication. Retained inputs are evidence, not a
second scheduler. Ordinary recovery continues to use its frozen configuration.
"""
import copy
import json
from pathlib import Path
import subprocess
import sys

PROTOCOL = 1


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def request(a):
    from ._foundry_goal_transitions import identity
    if getattr(a, 'order_revision', None):
        raise ValueError('recover and reconcile the contract revision before whole-goal runtime upgrade')
    if not a.foundry_goal or bool(a.recover_goal) == bool(a.amend_from):
        raise ValueError('--upgrade-runtime requires --foundry-goal and one of --recover-goal/--amend-from')
    if not a.prompt_file or not Path(a.prompt_file).read_text(encoding='utf-8-sig').strip():
        raise ValueError('runtime upgrade requires --prompt-file describing the next-round decision')
    folder = Path(a.recover_goal or Path(a.amend_from).parent).resolve(strict=True)
    from ._goal_intervention import input_root
    folder = input_root(folder, read(folder/'manifest.json'))
    value = {'order': str(Path(a.order).resolve(strict=True)),
             'answer': Path(a.prompt_file).read_text(encoding='utf-8-sig'),
             'target': a.upgrade_requested_runtime or a.upgrade_runtime,
             'overrides': getattr(a, '_upgrade_options', {})}
    return folder, value, identity(value)


def route(a, argv, *, project_adapter=None):
    """Pin target before hosting; a retry of a published request retains it."""
    a._upgrade_options = {name: getattr(a, name, None) for name in (
        'provider', 'launcher', 'coding_model', 'coding_effort', 'compute_profile',
        'analyst_backend', 'analyst_model', 'analyst_effort', 'analyst_exe', 'review_profile',
        'task_kind', 'team_profile', 'manager_profile', 'auto_amend_model', 'auto_amend_effort', 'task_deadline')}
    root, value, key = request(a)
    saved = next((row for row in read(root/'manifest.json').get('runtime_upgrades', [])
                  if row['request_id'] == key), None)
    target = saved['python'] if saved else a.upgrade_runtime
    if target == 'current':
        target = read('D:/Foundry/current.json')['python']
    elif not Path(target).is_absolute():
        import re
        if not re.fullmatch(r'\d+\.\d+\.\d+(?:[ab]\d+)?', target):
            raise ValueError('upgrade target must be current, an installed version or an absolute interpreter')
        target = str(Path('D:/Foundry/releases')/target/'Scripts/python.exe')
    target = str(Path(target).resolve(strict=True))
    # This probe never starts a model. Old releases cannot promise the boundary
    # protocol merely because they understand --migrate-runtime.
    if Path(target) != Path(sys.executable).resolve():
        probe = subprocess.run([target, '-I', '-c',
            'from foundry_runtime._foundry_goal_upgrade import PROTOCOL; print(PROTOCOL)'],
            capture_output=True, text=True, encoding='utf-8', timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        if probe.returncode or probe.stdout.strip() != str(PROTOCOL):
            raise ValueError('target does not support whole-goal boundary upgrade protocol: ' + target)
    remaining, skip = [], False
    for item in argv:
        if skip:
            skip = False
        elif item in ('--upgrade-runtime', '--upgrade-requested-runtime'):
            skip = True
        elif not item.startswith(('--upgrade-runtime=', '--upgrade-requested-runtime=')):
            remaining.append(item)
    remaining += ['--upgrade-runtime', target, '--upgrade-requested-runtime', value['target']]
    argv[:] = remaining
    if Path(target) == Path(sys.executable).resolve():
        a.upgrade_runtime = target
        a.upgrade_requested_runtime = value['target']
        return None
    entry = ([str(Path(sys.argv[0]).resolve()), '--runtime-python', target]
             if project_adapter is not None else ['-m', 'foundry_runtime'])
    return subprocess.call([target, *entry, *remaining],
                           stdout=sys.stdout, stderr=sys.stderr,
                           creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)


def handle(a, ap, *, project=None, project_adapter=None):
    from . import foundry_goal as owner
    from .foundry_entry import handle_mode, _print_report
    from ._foundry_cli_help import announce_recovery_failure
    root = None
    try:
        root, value, key = request(a)
        if any((a.migrate_runtime, a.handoff_provider, a.handoff_role, a.retained_only,
                a.recovery_file, a.goal_evidence)):
            raise ValueError('whole-goal upgrade is separate from role transfer, worker authorization and retained-only reads')
        with owner._run_lock(root):
            parent = read(root/'manifest.json')
            prior = next((row for row in parent.get('runtime_upgrades', []) if row['request_id'] == key), None)
            if prior:
                target = Path(prior['round'])
                if Path(prior['python']).resolve() != Path(sys.executable).resolve():
                    raise ValueError('retry requires the recorded upgrade interpreter: ' + prior['python'])
                if parent.get('active_round') != str(target):
                    # An old request is an observation, never a rewind of the
                    # controller after a later boundary has been published.
                    if not (target/'outcome.json').exists():
                        raise ValueError('this upgrade was superseded; inspect the current active round: ' + str(parent.get('active_round')))
                    result = read(target/'outcome.json')
                    _print_report(result)
                    return 0 if result.get('status') in ('candidate_verified', 'project_handoff') else 2
                if (target/'manifest.json').exists():
                    result = owner.recover_goal(Path(a.order), target)
                    publish(owner, root, parent, target, result)
                    _print_report(result)
                    return 0 if result.get('status') in ('candidate_verified', 'project_handoff') else 2
                prepared = read(prior['input']['path'])
                owner.goal_usage._reference(prior['input'])
            else:
                active, active_manifest = owner.goal_auto.active_round(root, parent, consumed=True)
                from contextlib import nullcontext
                with nullcontext() if active == root else owner._run_lock(active):
                    if project is not None:
                        from ._project_execution import from_record
                        from_record(active_manifest.get('project_record'), selected=project)
                    if a.task_deadline is not None and a.task_deadline != active_manifest.get('task_deadline_at'):
                        raise ValueError('runtime upgrade retains the existing task deadline; change it through the task owner')
                    prepared = prepare(owner, active, active_manifest, value,
                                       checkpoint_path=root/('upgrade-checkpoint-' + key + '.json'))
                target = root/('round-upgrade-' + key[:16])
                input_path = root/('upgrade-input-' + key + '.json')
                owner._write(input_path, prepared)
                # Preserve the legacy root documents before turning its manifest
                # into the existing round-controller view. Never move evidence.
                previous = root/('manifest-before-' + owner._fact(root/'manifest.json')['sha256'] + '.json')
                if not previous.exists():
                    previous.write_bytes((root/'manifest.json').read_bytes())
                parent = dict(parent)
                if parent.get('mode') != 'auto-amend':
                    parent.update(mode='auto-amend', rounds=[], legacy_initial=owner._fact(previous))
                if (root/'outcome.json').exists():
                    old = root/'outcome.json'
                    parent['previous_outcome'] = owner._fact(old)
                    if not parent['rounds']:
                        parent['rounds'] = [{'outcome': owner._fact(old)}]
                prior = {'request_id': key, 'request': value, 'python': sys.executable,
                         'round': str(target), 'input': owner._fact(input_path),
                         'previous_manifest': owner._fact(previous)}
                prior['start'] = prepare_invocation(owner, a, ap, root, target, prior, prepared,
                    project=project, project_adapter=project_adapter)
                prior['policy_change'] = {'old': policy(prepared),
                    'new': policy(prior['start']['options']), 'reason': value['answer'],
                    'basis': 'Explicit next-round upgrade; target shared preparation resolves current policy.'}
                parent['runtime_upgrades'] = [*parent.get('runtime_upgrades', []), prior]
                parent.pop('round_start', None)
                parent.update(active_round=str(target), state='running')
                owner._write(root/'manifest.json', parent)
            result = owner.run_prepared(Path(a.order), target, prior['start'])
            publish(owner, root, parent, target, result)
            _print_report(result)
            return 0 if result.get('status') in ('candidate_verified', 'project_handoff') else 2
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        print('REFUSE Foundry boundary upgrade: ' + str(exc), file=sys.stderr)
        if root is not None:
            announce_recovery_failure(Path(a.order), root)
        return 2



def prepare_invocation(owner, a, ap, root, target, prior, prepared, *, project, project_adapter):
    from .foundry_entry import handle_mode
    value, key = prior['request'], prior['request_id']
    child = copy.copy(a)
    child.upgrade_runtime = child.upgrade_requested_runtime = None
    child.recover_goal = child.amend_from = child.coding_session = None
    child.goal_evidence = str(target)
    child.worktree = prepared['worktree']
    child.task_deadline = prepared.get('task_deadline_at')
    child.task_kind = child.task_kind or (prepared.get('task_staffing') or {}).get('kind')
    # Preserve the worker transport without inventing an all-roles --provider
    # override. Explicit CLI choices have already been normalized by the entry.
    if not any(value.get('overrides', {}).get(k) for k in ('provider', 'launcher')):
        child.launcher = prepared.get('coding_launcher', 'app-server')
    child.launcher_explicit = True
    child._boundary_session = (json.loads(owner.goal_usage._reference(prepared['coding_checkpoint'])).get('coding_session')
                               if prepared.get('coding_checkpoint') else None)
    child._boundary_continuation = {
        'prior_record': prior['previous_manifest'], 'base_commit': prepared['base_commit'],
        'answer': value['answer'], 'manager_input': prior['input'],
        'checkpoint': prepared.get('coding_checkpoint') or prior['input'],
        'runtime_upgrade': {'request_id': key, 'goal': str(root), 'python': sys.executable}}
    child._prepare_goal = True
    if handle_mode(child, ap, project=project, project_adapter=project_adapter) != 0:
        raise ValueError('target round configuration could not be prepared')
    from ._foundry_manager import configuration_for
    options = child._prepared_goal['options']
    from ._launch_context import capture
    options['launch_context'] = capture(
        prepared['worktree'],
        root_role=(options.get('native_team') or {}).get('lead_role', 'executor'),
        backend='claude' if options.get('coding_launcher') == 'claude' else 'codex')
    state = configuration_for(options, target)
    if state is not None:
        options['manager_state'] = state
    closure = prepared.get('candidate_closure') or {}
    if closure.get('status') == 'not_evaluated':
        # The manager has already delivered this candidate. Upgrade the host
        # verification/review lifecycle, not another paid worker assignment.
        owner._review_range(Path(prepared['worktree']), prepared['base_commit'], closure['candidate_commit'])
        options['review_range'] = [prepared['base_commit'], closure['candidate_commit']]
        child._prepared_goal['correction'] = None
    return json.loads(json.dumps(child._prepared_goal, default=str))


def policy(record):
    """Audit projection of the same frozen configuration execution consumes."""
    state = record.get('manager_state') or {}
    return {'task_staffing': copy.deepcopy(record.get('task_staffing')),
            'compute_assignment': copy.deepcopy(record.get('compute_assignment')),
            'native_team': {k: copy.deepcopy(v) for k, v in (record.get('native_team') or {}).items()
                            if k != 'calibration'},
            'manager': {k: copy.deepcopy(v) for k, v in state.items() if k not in
                {'native_dir', 'expert_native_dir', 'last_call_id', 'session_id', 'handoff'}},
            'coding_launcher': record.get('coding_launcher'),
            'launch_context': copy.deepcopy(record.get('launch_context')),
            'task_deadline_at': record.get('task_deadline_at')}


def publish(owner, root, parent, target, result):
    record = owner.goal_auto.round_record(owner, target)
    rounds = [row for row in parent['rounds'] if row.get('outcome', {}).get('path') != str(target/'outcome.json')]
    parent['rounds'] = [*rounds, record]
    parent['state'] = 'completed'
    parent.pop('error', None)
    owner._write(root/'manifest.json', parent)


def advice(root, manifest):
    """Project the published request, including its original option identity."""
    from ._foundry_cli_help import recovery_invocation
    saved = manifest['runtime_upgrades'][-1]
    extra = ['--upgrade-runtime', saved['request']['target']]
    for key, value in saved['request'].get('overrides', {}).items():
        if value is not None:
            extra.extend(['--' + key.replace('_', '-'), str(value)])
    command = recovery_invocation(manifest['order']['path'], root, manifest,
        python=saved['python'], prompt_file='<prompt-file>', extra_args=extra)
    return {**{key: saved[key] for key in ('request_id', 'round', 'python', 'input')},
            'recovery': command,
            'note': 'Reuse the recorded operator answer in $PromptFile. The published request pins this interpreter even if current changes.'}


def prepare(owner, folder, manifest, request, *, checkpoint_path=None):
    """Read the original boundary through existing native/dispatch verifiers."""
    if manifest.get('schema_version') != 1:
        raise ValueError('unsupported goal manifest schema: ' + str(folder/'manifest.json'))
    if not owner.order_revision.matches(owner, request['order'], manifest):
        raise ValueError('upgrade order differs from the original goal')
    prepared = copy.deepcopy(manifest)
    if manifest.get('manager_phase') == 'dispatch_pending':
        if checkpoint_path is None:
            raise ValueError('completed-turn upgrade needs a retained checkpoint destination')
        from ._foundry_goal_continuation import restore_completed_feedback
        prepared = restore_completed_feedback(owner, folder, prepared,
            checkpoint_path=checkpoint_path, publish=False)
    outcome = read(folder/'outcome.json') if (folder/'outcome.json').exists() else None
    checkpoint = prepared.get('coding_checkpoint')
    if not checkpoint and not outcome:
        raise ValueError('not at an upgrade boundary: no settled checkpoint or outcome; '
                         'recover the interrupted decision on its recorded runtime first')
    if checkpoint:
        value = json.loads(owner.goal_usage._reference(checkpoint))
        from ._recovery_reconciliation import completed_coding
        completed_coding(value)
        for fact in value['evidence']:
            owner.goal_usage._reference(fact)
    elif not manifest.get('manager_state') and not manifest.get('expert_state'):
        raise ValueError('no retained manager or settled coding checkpoint at this boundary')
    history = []
    state = manifest.get('manager_state')
    if state:
        from ._foundry_goal_transitions import manager_history
        try:
            _, latest, history = manager_history(owner, state, manifest['worktree'],
                                                allow_empty=not state.get('last_call_id'))
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            raise ValueError('unreadable manager history at ' + state['native_dir'] + ': ' + str(exc)) from exc
        if latest != state.get('last_call_id'):
            raise ValueError('not at an upgrade boundary: manager decision is not consumed; '
                             'recover the recorded manager first')
    review_history = {}
    if manifest.get('review_checkpoint'):
        checkpoint = json.loads(owner.goal_usage._reference(manifest['review_checkpoint']))
        from ._foundry_review_group import Panel
        from .foundry_native import NativeAnalyst
        reference = manifest['review_checkpoint'] if checkpoint.get('context_delivery') == 'reference-v1' else None
        requests = {name: owner._role_request(checkpoint['run_id'], name, checkpoint['context'], context_reference=reference)
                    for name in ('review-0', 'review-1')}
        settings = checkpoint.get('reviewers') or [checkpoint['analyst']]*2
        for call in (folder/'reviews').rglob('call-*'):
            if call.is_dir() and not (call/'receipt.json').exists():
                raise ValueError('not at an upgrade boundary: interrupted review; recover it first: ' + str(call))
        try:
            with owner._run_lock(folder/'reviews'):
                panel = Panel(settings, requests, NativeAnalyst, folder/'reviews', Path(manifest['worktree']),
                              resume=True, transfers=manifest.get('review_transfers'))
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            raise ValueError('unreadable review history at ' + str(folder/'reviews') + ': ' + str(exc)) from exc
        review_history = panel.history
    expert_history = []
    expert_owner = state or manifest.get('expert_state')
    if (expert_owner or {}).get('expert'):
        from ._expert_consultation import native_root
        from ._foundry_goal_transitions import manager_history
        for source in sorted(native_root(expert_owner).glob('*')):
            if source.is_dir():
                try:
                    _, _, calls = manager_history(owner, {**expert_owner['expert'], 'native_dir': str(source)},
                                                  manifest['worktree'], allow_empty=True)
                except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                    raise ValueError('unreadable expert history at ' + str(source) + ': ' + str(exc)) from exc
                expert_history.extend(calls)
    from ._goal_intervention import carry_events
    prepared['manager_packet'] = carry_events(manifest, {
        **prepared.get('manager_packet', {}),
        'stage': 'worker_returned' if prepared.get('coding_checkpoint') else 'assign',
        'original_order': Path(request['order']).read_text(encoding='utf-8-sig'),
        'operator_answer': request['answer'], 'previous_outcome': outcome,
        'runtime_upgrade': {'previous_runtime': manifest.get('runtime_provenance'),
            'previous_manager_history': history,
            'previous_review_history': review_history, 'previous_expert_history': expert_history,
            'instruction': 'Continue this same goal from retained evidence. Do not repeat completed work. Apply the new round staffing and review policy.'}})
    return prepared
