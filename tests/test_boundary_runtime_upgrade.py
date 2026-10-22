"""Boundary publication and invocation; all model entrypoints are replaced."""
import json
from pathlib import Path
import sys

import pytest

from test_installed_runtime import external, offline
from test_manager_input_wait import manager_io
from test_settled_failure_recovery import failed_goal
from foundry_runtime import cli, foundry_goal as goal
from foundry_runtime import _foundry_goal_upgrade as upgrade


@pytest.fixture
def boundary(external, tmp_path, monkeypatch):
    tree, scope, order, _ = external
    root = tmp_path/'goal'; root.mkdir()
    native = tmp_path/'native'; native.mkdir()
    old = {'schema_version': 1, 'run_id': 'original-goal', 'order': goal._fact(order),
           'worktree': str(tree), 'base_commit': goal._git(tree, 'rev-parse', 'HEAD').decode().strip(),
           'tag': 'TASK1', 'coding_launcher': 'app-server', 'project_record': scope.record(),
           'runtime_provenance': {'python': 'D:/old/python.exe'},
           'manager_state': {'native_dir': str(native)}, 'manager_phase': 'awaiting_input',
           'unanswered_live_events': [{'item_id': 'original-question', 'text': 'keep this question'}]}
    goal._write(root/'manifest.json', old)
    goal._write(root/'outcome.json', {'status': 'awaiting_input', 'reviews': []})
    prompt = tmp_path/'answer.md'; prompt.write_text('中文：接续上一回合，不重复完成工作', encoding='utf-8')
    from foundry_runtime import foundry_native_support, foundry_entry
    monkeypatch.setattr(foundry_entry, '_analyst_executable', lambda *a: Path(sys.executable))
    monkeypatch.setattr(foundry_native_support, 'resolve_executable', lambda *a: Path(sys.executable))
    def forbidden(*a, **kw):
        pytest.fail('fixture attempted a real model invocation')
    from foundry_runtime import app_server_transport, foundry_native
    monkeypatch.setattr(app_server_transport, 'run_executor', forbidden)
    monkeypatch.setattr(foundry_native.OwnedProcess, 'spawn', forbidden)
    calls = []
    def run(order, folder, prepared):
        options = prepared['options']
        calls.append((folder, options))
        folder.mkdir()
        goal._write(folder/'manifest.json', {**old, 'run_id': 'upgraded-round'})
        result = {'status': 'project_handoff', 'reviews': []}
        goal._write(folder/'outcome.json', result)
        return result
    monkeypatch.setattr(goal, 'run_prepared', run)
    args = ['--project-config', str(scope.path), '--order', str(order), '--foundry-goal',
            '--recover-goal', str(root), '--upgrade-runtime', sys.executable, '--prompt-file', str(prompt)]
    return root, old, args, calls


@pytest.mark.parametrize('outcome', [None, 'review_blocked', 'candidate_verified'])
def test_upgrade_retains_identity_history_and_replays(boundary, outcome):
    root, old, args, calls = boundary
    original = (root/'manifest.json').read_bytes()
    if outcome:
        goal._write(root/'outcome.json', {'status': outcome, 'reviews': []})
        args[args.index('--recover-goal')] = '--amend-from'
        args[args.index(str(root))] = str(root/'outcome.json')
    assert cli.main(args) == 0
    parent = upgrade.read(root/'manifest.json')
    assert parent['run_id'] == old['run_id']
    assert parent['worktree'] == old['worktree']
    assert parent['runtime_upgrades'][0]['python'] == sys.executable
    assert Path(parent['runtime_upgrades'][0]['previous_manifest']['path']).read_bytes() == original
    options = calls[0][1]
    assert options['manager_profile'] == 'opus'
    assert options['task_staffing']['kind'] == 'small'
    packet = upgrade.read(options['continuation']['manager_input']['path'])['manager_packet']
    assert packet['unanswered_live_events'][0]['item_id'] == 'original-question'
    assert '中文' in packet['operator_answer']
    assert cli.main(args) == 0
    assert len(calls) == 1


def test_boundary_upgrade_refreshes_v19_team_without_rewriting_old_policy(boundary, monkeypatch):
    root, old, args, calls = boundary
    old['native_team'] = {'profile':'adaptive', 'version':19,
        'config':{'features.multi_agent_v2':{'enabled':True}}}
    goal._write(root/'manifest.json', old)
    before = (root/'manifest.json').read_bytes()
    monkeypatch.setenv('FOUNDRY_WAIT_INTERVAL_SECONDS', '1800')
    args += ['--team-profile', 'adaptive']
    assert cli.main(args) == 0
    team = calls[0][1]['native_team']
    assert team['process_completion_wait'] == 1
    assert team['config']['features.multi_agent_v2']['min_wait_timeout_ms'] == 1800000
    row = upgrade.read(root/'manifest.json')['runtime_upgrades'][0]
    assert Path(row['previous_manifest']['path']).read_bytes() == before
    assert old['native_team']['version'] == 19
    assert 'process_completion_wait' not in old['native_team']


def test_upgrade_freezes_launch_before_publication_retry(boundary, monkeypatch):
    from foundry_runtime import _code_index
    root, old, args, calls = boundary
    monkeypatch.setattr(_code_index, 'prepare', lambda *a, **k: None)
    monkeypatch.setenv('FOUNDRY_COMPACT_TOKENS', '240000')
    run = goal.run_prepared
    monkeypatch.setattr(goal, 'run_prepared',
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('fixture publication interruption')))
    assert cli.main(args) == 2
    row = upgrade.read(root/'manifest.json')['runtime_upgrades'][0]
    assert row['start']['options']['launch_context']['settings']['compact_tokens'] == 240000
    monkeypatch.setenv('FOUNDRY_COMPACT_TOKENS', '120000')
    monkeypatch.setattr(goal, 'run_prepared', run)
    assert cli.main(args) == 0
    assert calls[0][1]['launch_context']['settings']['compact_tokens'] == 240000


@pytest.mark.parametrize('threshold', [240000, 0])
def test_boundary_launch_reaches_native_and_survives_plain_resume(
        external, manager_io, tmp_path, monkeypatch, threshold):
    from foundry_runtime import app_server_transport as transport, foundry_entry, foundry_native_support
    from foundry_runtime import _native_session, _foundry_dispatch as dispatch
    from test_owned_goal_recovery import SERVER, SID
    tree, scope, order, _ = external
    _, decisions = manager_io
    decisions.extend([{'action': 'delegate', 'instruction': 'initial work', 'artifacts': []},
                      {'action': 'deliver_artifacts', 'instruction': 'inspect', 'artifacts': ['candidate.txt']}])
    actual_argv = []
    original = transport.run_executor
    def spawn(argv, **opts):
        actual_argv.append(argv)
        return transport.OwnedProcess.spawn([sys.executable, '-B', '-c', 'HANG=False\n'+SERVER], **opts)
    monkeypatch.setattr(transport, 'run_executor',
        lambda *a, **k: original(*a, process_factory=spawn, **k))
    monkeypatch.setattr(goal, '_session_from_log', lambda _: SID)
    monkeypatch.setattr(_native_session, 'read_session_worktree', lambda _: tree)
    monkeypatch.setenv('FOUNDRY_COMPACT_TOKENS', '120000')
    root = tmp_path/'real-goal'
    assert goal.run_goal(order, root, Path(sys.executable), coding_launcher='app-server',
        project_record=scope.record(), manager_state={'backend': 'claude', 'model': None,
        'effort': None, 'native_dir': str(tmp_path/'old-manager'),
        'artifact_delivery': True})['status'] == 'artifacts_delivered'
    old_packages = {p: p.read_bytes() for p in scope.evidence('TASK1').glob('*.package.json')}
    assert old_packages
    assert 'model_auto_compact_token_limit=120000' in actual_argv[-1]
    # A plain resume still ignores changed ambient configuration.
    monkeypatch.setenv('FOUNDRY_COMPACT_TOKENS', str(threshold))
    assert dispatch.resume_order(SID, tree, 'TASK1', 'plain continuation',
                                 project=scope, launcher='app-server')[0] == 0
    assert 'model_auto_compact_token_limit=120000' in actual_argv[-1]
    monkeypatch.setattr(foundry_entry, '_analyst_executable', lambda *a: Path(sys.executable))
    monkeypatch.setattr(foundry_native_support, 'resolve_executable', lambda *a: Path(sys.executable))
    decisions.extend([{'action': 'delegate', 'instruction': 'new boundary work', 'artifacts': [],
                       'expert_request': None},
                      {'action': 'deliver_artifacts', 'instruction': 'inspect new work',
                       'artifacts': ['candidate.txt'], 'expert_request': None}])
    prompt = tmp_path/'decision.md'; prompt.write_text('Continue with explicit launch policy.')
    args = ['--project-config', str(scope.path), '--order', str(order), '--foundry-goal',
            '--recover-goal', str(root), '--upgrade-runtime', sys.executable, '--prompt-file', str(prompt)]
    assert cli.main(args) == 2
    expected = [f'model_auto_compact_token_limit={threshold}'] if threshold else []
    compact = lambda argv: [v for v in argv if v.startswith('model_auto_compact_token_limit=')]
    assert compact(actual_argv[-1]) == expected
    count = len(actual_argv)
    monkeypatch.setenv('FOUNDRY_COMPACT_TOKENS', '99999')
    assert cli.main(args) == 2
    assert len(actual_argv) == count
    assert dispatch.resume_order(SID, tree, 'TASK1', 'after boundary continuation',
                                 project=scope, launcher='app-server')[0] == 0
    assert compact(actual_argv[-1]) == expected
    assert all(p.read_bytes() == raw for p, raw in old_packages.items())


def test_older_request_replay_does_not_rewind_newer_round(boundary):
    root, old, args, calls = boundary
    assert cli.main(args) == 0
    first = (root/'manifest.json').read_bytes()
    prompt = Path(args[args.index('--prompt-file') + 1])
    initial_answer = prompt.read_text(encoding='utf-8')
    prompt.write_text('Next boundary with new instructions', encoding='utf-8')
    assert cli.main(args) == 0
    newer = (root/'manifest.json').read_bytes()
    assert first != newer
    prompt.write_text(initial_answer, encoding='utf-8')
    assert cli.main(args) == 0
    assert (root/'manifest.json').read_bytes() == newer
    assert len(calls) == 2


def test_live_goal_refuses_without_changing_manifest(boundary):
    root, old, args, calls = boundary
    before = (root/'manifest.json').read_bytes()
    with goal._run_lock(root):
        assert cli.main(args) == 2
    assert (root/'manifest.json').read_bytes() == before
    assert not calls


def test_failed_input_does_not_publish_round(boundary):
    root, old, args, calls = boundary
    old['coding_checkpoint'] = {'path': str(root/'missing.json'), 'sha256': '0'*64}
    goal._write(root/'manifest.json', old)
    before = (root/'manifest.json').read_bytes()
    assert cli.main(args) == 2
    assert (root/'manifest.json').read_bytes() == before
    assert not calls


def test_publication_before_child_creation_retries_same_round(boundary, monkeypatch):
    root, old, args, calls = boundary
    first = goal.run_prepared
    monkeypatch.setattr(goal, 'run_prepared', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('fixture crash')))
    assert cli.main(args) == 2
    published = upgrade.read(root/'manifest.json')['active_round']
    monkeypatch.setattr(goal, 'run_prepared', first)
    monkeypatch.setattr(goal, 'prepare_goal', lambda *a, **k: pytest.fail('published request recomputed staffing'))
    assert cli.main(args) == 0
    assert str(calls[0][0]) == published
    assert len(upgrade.read(root/'manifest.json')['runtime_upgrades']) == 1


def test_invalid_target_configuration_does_not_publish(boundary, monkeypatch):
    root, old, args, calls = boundary
    before = (root/'manifest.json').read_bytes()
    def invalid(*a, **kw):
        raise ValueError('fixture incompatible target configuration')
    monkeypatch.setattr(goal, 'prepare_goal', invalid)
    assert cli.main(args) == 2
    assert (root/'manifest.json').read_bytes() == before
    assert not calls


def test_stale_outcome_does_not_report_completion(boundary, monkeypatch):
    root, old, args, calls = boundary
    goal._write(root/'outcome.json', {'status': 'candidate_verified', 'reviews': []})
    monkeypatch.setattr(goal, 'run_prepared', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('fixture crash')))
    assert cli.main(args) == 2
    from foundry_runtime._followup import _goal_status
    assert _goal_status(root)['status'] == 'execution_stopped'


def test_current_replay_keeps_published_target_after_pointer_changes(boundary, monkeypatch):
    root, old, args, calls = boundary
    args[args.index('--upgrade-runtime')+1] = 'current'
    original = upgrade.read
    selection_reads = []
    def read(path):
        if str(path) == 'D:/Foundry/current.json':
            selection_reads.append(1)
            assert len(selection_reads) == 1, 'retry reselected current instead of its published target'
            return {'python': sys.executable}
        return original(path)
    monkeypatch.setattr(upgrade, 'read', read)
    assert cli.main(args) == 0
    assert cli.main(args) == 0
    assert len(calls) == 1


def test_hosted_submission_contains_concrete_target(boundary, monkeypatch, tmp_path):
    root, old, args, calls = boundary
    from foundry_runtime import _hosted
    captured = []
    monkeypatch.setattr(_hosted, 'submit', lambda directory, argv, **kw:
        captured.append(argv) or {'status': 'pending'})
    original = upgrade.read
    monkeypatch.setattr(upgrade, 'read', lambda path: {'python': sys.executable}
        if str(path) == 'D:/Foundry/current.json' else original(path))
    args[args.index('--upgrade-runtime')+1] = 'current'
    assert cli.main([*args, '--hosted', str(tmp_path/'hosted')]) == 0
    argv = captured[0]
    assert argv[argv.index('--upgrade-runtime')+1] == str(Path(sys.executable).resolve())
    assert argv[argv.index('--upgrade-requested-runtime')+1] == 'current'
    assert not calls


def test_incompatible_target_is_rejected_before_publication(boundary, monkeypatch, tmp_path):
    from types import SimpleNamespace
    root, old, args, calls = boundary
    target = tmp_path/'old-python.exe'; target.touch()
    args[args.index('--upgrade-runtime')+1] = str(target)
    monkeypatch.setattr(upgrade.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1, stdout=''))
    before = (root/'manifest.json').read_bytes()
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2
    assert (root/'manifest.json').read_bytes() == before
    assert not calls


def test_usage_follows_new_round_without_rewriting_old_outcome(boundary):
    root, old, args, calls = boundary
    result = {'status': 'candidate_verified', 'reviews': []}
    goal._write(root/'outcome.json', result)
    before = (root/'outcome.json').read_bytes()
    old_manifest = goal._fact(root/'manifest.json')
    assert cli.main(args) == 0
    assert (root/'outcome.json').read_bytes() == before
    assert json.loads(goal.goal_usage._reference(old_manifest)) == old
    report = goal.goal_usage.task_report([str(root)])
    assert any(row['kind'] == 'completed_upgraded_root' for row in report['records'])
    from foundry_runtime._followup import _goal_status
    observed = _goal_status(root)
    assert observed['outcome_status'] == 'project_handoff'
    assert Path(observed['outcome_path']).parent != root


@pytest.mark.parametrize('directory_created', [False, True])
def test_interrupted_round_initialization_uses_frozen_start(external, tmp_path, monkeypatch, directory_created):
    tree, scope, order, _ = external
    root = tmp_path/'automatic'; root.mkdir()
    active = root/'round-0'
    if directory_created:
        active.mkdir()
    parent = {'schema_version': 1, 'mode': 'auto-amend', 'run_id': 'stable-goal',
              'order': goal._fact(order), 'active_round': str(active), 'rounds': [], 'state': 'interrupted',
              'round_start': {'executable': sys.executable, 'options': {'coding_model': 'frozen-model'}}}
    goal._write(root/'manifest.json', parent)
    calls = []
    def run(order, folder, exe, **options):
        calls.append(options)
        folder.mkdir(exist_ok=True)
        goal._write(folder/'manifest.json', {'schema_version': 1, 'run_id': 'round', 'order': goal._fact(order)})
        value = {'status': 'project_handoff', 'reviews': []}
        goal._write(folder/'outcome.json', value)
        return value
    monkeypatch.setattr(goal, '_run', run)
    assert goal.recover_goal(order, root)['status'] == 'project_handoff'
    assert goal.recover_goal(order, root)['status'] == 'project_handoff'
    assert calls == [{'coding_model': 'frozen-model', 'initializing': True}]


@pytest.mark.parametrize('old_manager', ['claude', 'codex'])
def test_completed_worker_reaches_new_manager_without_reexecution(external, manager_io, tmp_path, monkeypatch, old_manager):
    from foundry_runtime import app_server_transport as transport, foundry_entry, foundry_native_support
    from test_owned_goal_recovery import SERVER, SID
    tree, scope, order, _ = external
    requests, decisions = manager_io
    decisions.extend([{'action': 'delegate', 'instruction': 'work', 'artifacts': []},
                      {'action': 'deliver_artifacts', 'instruction': 'inspect', 'artifacts': ['candidate.txt']}])
    original = transport.run_executor
    def execute(*args, **kwargs):
        return original(*args, process_factory=lambda argv, **opts:
            transport.OwnedProcess.spawn([sys.executable, '-B', '-c', 'HANG=False\n'+SERVER], **opts), **kwargs)
    monkeypatch.setattr(transport, 'run_executor', execute)
    monkeypatch.setattr(goal, '_session_from_log', lambda _: SID)
    root = tmp_path/'real-goal'
    assert goal.run_goal(order, root, Path(sys.executable), coding_launcher='app-server',
        project_record=scope.record(), manager_state={'backend': old_manager, 'model': None,
        'effort': None, 'native_dir': str(tmp_path/'old-manager'), 'artifact_delivery': True})['status'] == 'artifacts_delivered'
    frozen = {p: p.read_bytes() for p in root.iterdir() if p.is_file() and p.name not in ('manifest.json', 'outcome.json', 'usage.json')}
    monkeypatch.setattr(transport, 'run_executor', lambda *a, **k: pytest.fail('upgrade repeated completed worker'))
    monkeypatch.setattr(foundry_entry, '_analyst_executable', lambda *a: Path(sys.executable))
    monkeypatch.setattr(foundry_native_support, 'resolve_executable', lambda *a: Path(sys.executable))
    decisions.append({'action': 'deliver_artifacts', 'instruction': 'inspect retained work',
                      'artifacts': ['candidate.txt'], 'expert_request': None})
    prompt = tmp_path/'decision.md'; prompt.write_text('Review existing work without repeating it.')
    args = ['--project-config', str(scope.path), '--order', str(order), '--foundry-goal',
            '--recover-goal', str(root), '--upgrade-runtime', sys.executable, '--prompt-file', str(prompt)]
    assert cli.main(args) == 2  # Artifact delivery is not project acceptance.
    parent = upgrade.read(root/'manifest.json')
    assert parent['state'] == 'completed'
    assert upgrade.read(root/'outcome.json')['status'] == 'artifacts_delivered'
    assert len(requests) == 3
    assert requests[-1]['packet']['stage'] == 'worker_returned'
    assert requests[-1]['packet']['runtime_upgrade']['previous_manager_history']
    assert all(p.read_bytes() == raw for p, raw in frozen.items())
    assert cli.main(args) == 2
    assert len(requests) == 3


def test_unsettled_manager_is_not_an_upgrade_boundary(boundary):
    root, old, args, calls = boundary
    (root/'outcome.json').unlink()
    before = (root/'manifest.json').read_bytes()
    assert cli.main(args) == 2
    assert (root/'manifest.json').read_bytes() == before
    assert not list(root.glob('upgrade-input-*'))
    assert not calls


def test_policy_change_records_actual_frozen_manager(boundary):
    from foundry_runtime._model_catalogue import model
    root, old, args, calls = boundary
    old['task_staffing'] = {'kind': 'cross-module'}
    old['manager_state']['live_coordination'] = 0
    goal._write(root/'manifest.json', old)
    assert cli.main(args) == 0
    row = upgrade.read(root/'manifest.json')['runtime_upgrades'][0]
    change = row['policy_change']
    assert change['old']['manager']['live_coordination'] == 0
    assert change['new']['manager']['live_coordination'] == 1
    assert change['new']['manager']['model'] == model('opus')
    assert change['new']['manager']['expert']['model'] == model('fable')
    assert change['new'] == upgrade.policy(row['start']['options'])
    assert change['reason'] == row['request']['answer']
    assert cli.main(args) == 0
    assert upgrade.read(root/'manifest.json')['runtime_upgrades'][0]['policy_change'] == change


@pytest.mark.parametrize('role', ['manager', 'expert'])
def test_unreadable_native_history_refused_before_publication(boundary, role, capsys):
    root, old, args, calls = boundary
    native = Path(old['manager_state']['native_dir'])
    if role == 'expert':
        native = native.with_name(native.name + '-experts')/'consultation'
        old['manager_state']['expert'] = {'backend': 'claude'}
    call = native/'call-broken'; call.mkdir(parents=True)
    (call/'stdin.txt').write_text('broken input')
    goal._write(root/'manifest.json', old)
    before = (root/'manifest.json').read_bytes()
    assert cli.main(args) == 2
    assert (root/'manifest.json').read_bytes() == before
    assert not calls
    assert 'unreadable ' + role + ' history at ' + str(native) in capsys.readouterr().err


def test_unconsumed_manager_decision_refuses_upgrade(boundary, monkeypatch):
    from foundry_runtime import _foundry_goal_transitions as transitions
    root, old, args, calls = boundary
    monkeypatch.setattr(transitions, 'manager_history', lambda *a, **k: ({}, 'unconsumed-call', []))
    before = (root/'manifest.json').read_bytes()
    assert cli.main(args) == 2
    assert (root/'manifest.json').read_bytes() == before
    assert not calls


def test_pass_with_feedback_keeps_pass_and_carries_new_decision(boundary):
    root, old, args, calls = boundary
    outcome = {'status': 'candidate_verified', 'reviews': [{'verdict': 'PASS'}, {'verdict': 'PASS'}]}
    goal._write(root/'outcome.json', outcome)
    frozen = (root/'outcome.json').read_bytes()
    args[args.index('--recover-goal')] = '--amend-from'
    args[args.index(str(root))] = str(root/'outcome.json')
    assert cli.main(args) == 0
    row = upgrade.read(root/'manifest.json')['runtime_upgrades'][0]
    packet = upgrade.read(row['input']['path'])['manager_packet']
    assert packet['previous_outcome'] == outcome
    assert packet['operator_answer'] == row['request']['answer']
    assert (root/'outcome.json').read_bytes() == frozen


def test_unreadable_review_history_refused_before_publication(boundary, capsys):
    root, old, args, calls = boundary
    checkpoint = root/'review-checkpoint.json'
    goal._write(checkpoint, {'run_id': old['run_id'], 'context': {},
        'analyst': {'executable': sys.executable, 'backend': 'claude'}})
    old['review_checkpoint'] = goal._fact(checkpoint)
    reviews = root/'reviews'; reviews.mkdir()
    (reviews/'native-manifest.json').write_text('{broken')
    goal._write(root/'manifest.json', old)
    before = (root/'manifest.json').read_bytes()
    assert cli.main(args) == 2
    assert (root/'manifest.json').read_bytes() == before
    assert not calls
    assert 'unreadable review history at ' + str(reviews) in capsys.readouterr().err


def test_protocol_one_rollback_routes_to_explicit_older_runtime(boundary, monkeypatch, tmp_path):
    from types import SimpleNamespace
    root, old, args, calls = boundary
    target = tmp_path/'older-protocol-one/python.exe'; target.parent.mkdir(); target.touch()
    args[args.index('--upgrade-runtime')+1] = str(target)
    routed = []
    monkeypatch.setattr(upgrade.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='1\n'))
    monkeypatch.setattr(upgrade.subprocess, 'call', lambda argv, **k: routed.append(argv) or 0)
    assert cli.main(args) == 0
    assert routed[0][0] == str(target.resolve())
    assert routed[0][routed[0].index('--upgrade-runtime')+1] == str(target.resolve())
    assert routed[0][routed[0].index('--upgrade-requested-runtime')+1] == str(target)
    assert not calls


def test_a46_written_policy_rederived_in_target(boundary):
    from foundry_runtime._model_catalogue import model
    root, old, args, calls = boundary
    retained = upgrade.read(Path(__file__).parent/'fixtures/a46-retained-policy.json')
    assert retained['source_version'] == '0.1.0a46'
    old['task_staffing'] = retained['task_staffing']
    old['manager_state'].update(retained['manager_policy'])
    goal._write(root/'manifest.json', old)
    assert cli.main(args) == 0
    row = upgrade.read(root/'manifest.json')['runtime_upgrades'][0]
    state = row['start']['options']['manager_state']
    assert state['profile'] == 'opus'
    assert state['live_coordination'] == 1
    assert state['expert']['model'] == model('fable')
    assert row['start']['options']['task_staffing']['kind'] == 'cross-module'
    assert row['policy_change']['old']['task_staffing'] == retained['task_staffing']
    assert [r['model'] for r in row['start']['options']['reviewers']] == [model('opus'), model('sonnet')]


def test_interrupted_review_does_not_write_history_or_publish(boundary):
    root, old, args, calls = boundary
    checkpoint = root/'review-checkpoint.json'
    goal._write(checkpoint, {'run_id': old['run_id'], 'context': {},
        'analyst': {'executable': sys.executable, 'backend': 'claude'}})
    old['review_checkpoint'] = goal._fact(checkpoint)
    call = root/'reviews/review-0/attempt-000001/call-pending'; call.mkdir(parents=True)
    (call/'reservation.json').write_text('{}')
    goal._write(root/'manifest.json', old)
    before = (root/'manifest.json').read_bytes()
    assert cli.main(args) == 2
    assert (root/'manifest.json').read_bytes() == before
    assert not (call/'interrupted.json').exists()
    assert not calls


def test_explicit_provider_override_is_retained(boundary):
    root, old, args, calls = boundary
    old['task_staffing'] = {'kind': 'cross-module'}
    goal._write(root/'manifest.json', old)
    assert cli.main([*args, '--provider', 'codex']) == 0
    row = upgrade.read(root/'manifest.json')['runtime_upgrades'][0]
    from foundry_runtime._model_catalogue import model
    options = row['start']['options']
    assert options.get('reviewers') is None  # Native default pair, not a named review profile.
    assert options['backend'] == 'codex'
    assert options['analyst_model'] == model('astra')
    assert options['analyst_effort'] == 'high'
    assert row['start']['options']['manager_state']['profile'] == 'opus'


def test_explicit_launcher_override_is_not_replaced_by_old_transport(boundary):
    root, old, args, calls = boundary
    assert old['coding_launcher'] == 'app-server'
    assert cli.main([*args, '--launcher', 'exec']) == 0
    row = upgrade.read(root/'manifest.json')['runtime_upgrades'][0]
    assert row['start']['options']['coding_launcher'] == 'exec'


@pytest.mark.parametrize('failed_goal', ['other'], indirect=True)
def test_settled_checkpoint_without_outcome_is_a_boundary(failed_goal):
    _, _, order, folder, calls = failed_goal
    (folder/'outcome.json').unlink()
    manifest = upgrade.read(folder/'manifest.json')
    prepared = upgrade.prepare(goal, folder, manifest, {'order': str(order), 'answer': 'Continue after a settled failure'})
    assert prepared['coding_checkpoint'] == manifest['coding_checkpoint']
    assert prepared['manager_packet']['stage'] == 'worker_returned'
    assert calls == [None]


def test_unrecognized_wire_terminal_is_not_a_settled_boundary(external, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from foundry_runtime.app_server_transport import UncertainExecution
    with pytest.raises(UncertainExecution, match='unknown object variant'):
        failed_goal.__wrapped__(external, tmp_path, monkeypatch,
            SimpleNamespace(param={'futureProviderError': {}}))
    folder = tmp_path/'goal'
    manifest = upgrade.read(folder/'manifest.json')
    assert not manifest.get('coding_checkpoint')
    with pytest.raises(ValueError, match='no settled checkpoint or outcome'):
        upgrade.prepare(goal, folder, manifest, {'order': str(external[2]), 'answer': 'Inspect original execution'})
