"""CLI additions reach the common coding boundary and survive source-file loss."""
import json
from pathlib import Path
import sys

import pytest

from test_installed_runtime import external, offline, git
from foundry_runtime import cli, foundry_entry, foundry_goal as goal, _foundry_dispatch as dispatch
from foundry_runtime import _app_server_policy as policy
from foundry_runtime import _foundry_manager as manager


@pytest.fixture(autouse=True)
def local_manager_only(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('amendment fixture attempted a native manager call')
    monkeypatch.setattr(manager, 'NativeAnalyst', forbidden)


@pytest.mark.parametrize('launcher', ['exec', 'app-server', 'claude'])
@pytest.mark.parametrize('managed', [False, True])
def test_pass_amendment_carries_additions_and_retains_them(external, tmp_path, monkeypatch, launcher, managed):
    tree, scope, order, _ = external
    base = git(tree, 'rev-parse', 'HEAD')
    (tree/'candidate.txt').write_text('candidate')
    git(tree, 'add', '.'); git(tree, 'commit', '-qm', 'candidate')
    head = git(tree, 'rev-parse', 'HEAD')
    prior = tmp_path/'prior'; prior.mkdir()
    manifest = {'order': goal._fact(order), 'worktree': str(tree), 'base_commit': base,
                'coding_launcher': launcher, 'project_record': scope.record()}
    manager_packets = []
    decision_receipt = tmp_path/'manager-decision.json'
    goal._write(decision_receipt, {'action': 'delegate', 'instruction': 'Apply the operator additions'})
    if managed:
        manifest['manager_state'] = {'native_dir': str(tmp_path/'manager'), 'last_call_id': None}
    def manager_turn(state, worktree, packet, **kwargs):
        manager_packets.append(packet)
        return {'action': 'delegate', 'instruction': 'Apply the operator additions',
                'evidence': goal._fact(decision_receipt)}, state, {}, packet
    monkeypatch.setattr(manager, 'turn', manager_turn)
    (prior/'manifest.json').write_text(json.dumps(manifest))
    review = {'candidate_commit': head, 'packet_sha256': 'b'*64, 'verdict': 'PASS', 'findings': []}
    (prior/'outcome.json').write_text(json.dumps({'status': 'candidate_verified', 'candidate_commit': head,
                                                'reviews': [review, review]}))
    marker = '新增要求：统一权威并合并目标引用。AMEND_MARKER'
    prompt = tmp_path/'prompt.md'; prompt.write_text(marker, encoding='utf-8-sig')
    monkeypatch.setattr(foundry_entry, '_analyst_executable', lambda backend: Path(sys.executable))
    received = []
    def capture(session, worktree, tag, text, **kwargs):
        received.append(text)
        raise RuntimeError('fixture stops after coding prompt capture')
    monkeypatch.setattr(dispatch, 'resume_order', capture)
    folder = tmp_path/'amended'
    argv = ['--project-config', str(scope.path), '--order', str(order), '--foundry-goal',
            '--amend-from', str(prior/'outcome.json'), '--coding-session', 'fixture-session',
            '--launcher', launcher, '--prompt-file', str(prompt), '--goal-evidence', str(folder)]
    assert cli.main(argv) == 2
    assert len(received) == 1 and marker in received[0]
    assert manager_packets[0]['operator_answer'] == marker
    assert received[0].index('Prior findings') < received[0].index(marker)
    saved = json.loads((folder/'manifest.json').read_bytes())['amendment']
    prompt.unlink()
    assert saved['operator_additions'] == marker
    assert marker in goal.amendment_prompt(order, saved, None, project=scope)


def test_removed_bundled_reviewer_names_explicit_upgrade(external, tmp_path, monkeypatch, capsys):
    tree, scope, order, launches = external
    base = git(tree, 'rev-parse', 'HEAD')
    (tree/'candidate.txt').write_text('candidate')
    git(tree, 'add', '.'); git(tree, 'commit', '-qm', 'candidate')
    head = git(tree, 'rev-parse', 'HEAD')
    prior = tmp_path/'prior'; prior.mkdir()
    old_runtime = tmp_path/'removed-runtime'
    missing = old_runtime/'native/claude/version/digest/claude.exe'
    manifest = {'order': goal._fact(order), 'worktree': str(tree), 'base_commit': base,
        'coding_launcher': 'exec', 'project_record': scope.record(),
        'runtime_provenance': {'environment': str(old_runtime), 'python': str(old_runtime/'Scripts/python.exe')},
        'reviewers': [{'backend': 'claude', 'executable': str(missing), 'model': 'fixture-reviewer', 'effort': 'high'},
                      {'backend': 'codex', 'executable': sys.executable, 'model': 'fixture-reviewer', 'effort': 'high'}]}
    goal._write(prior/'manifest.json', manifest)
    review = {'candidate_commit': head, 'packet_sha256': 'b'*64, 'verdict': 'PASS', 'findings': []}
    goal._write(prior/'outcome.json', {'status': 'candidate_verified', 'candidate_commit': head, 'reviews': [review, review]})
    before = {p: p.read_bytes() for p in prior.iterdir()}
    prompt = tmp_path/'answer.md'; prompt.write_text('Apply the additional correction.')
    monkeypatch.setattr(foundry_entry, '_analyst_executable', lambda backend: Path(sys.executable))
    assert cli.main(['--project-config', str(scope.path), '--order', str(order), '--foundry-goal',
        '--amend-from', str(prior/'outcome.json'), '--coding-session', 'fixture-session',
        '--prompt-file', str(prompt), '--goal-evidence', str(tmp_path/'amended')]) == 2
    error = capsys.readouterr().err
    assert 'removed-runtime' in error
    assert '--upgrade-runtime' in error and 'current' in error
    invocation = json.JSONDecoder().raw_decode(error.split('in --prompt-file:\n', 1)[1])[0]
    argv = invocation['argv']
    assert argv[0] == sys.executable and '--amend-from' in argv
    assert argv[argv.index('--amend-from') + 1] == str(prior/'outcome.json')
    assert argv[argv.index('--prompt-file') + 1] == str(prompt)
    assert not launches and {p: p.read_bytes() for p in prior.iterdir()} == before


@pytest.mark.parametrize('completed', [False, True])
def test_provider_failed_review_allows_operator_amendment(external, tmp_path, monkeypatch, capsys, completed):
    tree, scope, order, launches = external
    base = git(tree, 'rev-parse', 'HEAD')
    (tree/'candidate.txt').write_text('candidate')
    git(tree, 'add', '.'); git(tree, 'commit', '-qm', 'candidate')
    head = git(tree, 'rev-parse', 'HEAD')
    prior = tmp_path/'prior'; prior.mkdir()
    session = 'original-session'
    context = {'base_commit': base, 'candidate_commit': head}
    checkpoint = prior/'review-checkpoint.json'
    goal._write(checkpoint, {'run_id': 'original-run', 'order': goal._fact(order), 'worktree': str(tree),
                            'context': context, 'coding_session': session})
    goal._write(prior/'manifest.json', {'run_id': 'original-run', 'order': goal._fact(order), 'worktree': str(tree),
        'base_commit': base, 'coding_launcher': 'claude', 'project_record': scope.record(),
        'review_checkpoint': goal._fact(checkpoint), 'runtime_provenance': {'python': sys.executable}})
    review = {'candidate_commit': head, 'packet_sha256': goal._sha(goal._json_bytes(context)),
              'verdict': 'PASS', 'findings': []}
    outcome = prior/'outcome.json'
    recorded = {'status': 'review_blocked', 'candidate_commit': head, 'review_completion_required': True,
                'reason': 'provider_failure: request timed out', 'reviews': [review] if completed else []}
    goal._write(outcome, recorded)
    with pytest.raises(goal.GoalBlocked, match='--prompt-file'):
        goal._amendment(order, tree, outcome, session)
    with pytest.raises(goal.GoalBlocked, match='recorded coding session'):
        goal._amendment(order, tree, outcome, 'other-session', operator_additions='Merge upstream.')
    goal._write(outcome, {**recorded, 'reason': 'schema_rejected: invalid verdict'})
    with pytest.raises(goal.GoalBlocked, match='completed pair'):
        goal._amendment(order, tree, outcome, session, operator_additions='Merge upstream.')
    goal._write(outcome, recorded)
    before = outcome.read_bytes()
    from foundry_runtime._foundry_goal_delivery import review_feedback
    feedback = review_feedback(outcome)
    assert feedback['continuation']['status'] == 'review_completion_required'
    assert '--recover-goal' in feedback['continuation']['invocation']['argv']
    assert feedback['operator_amendment']['invocation']['argv'][-4:] == [
        '--coding-session', session, '--prompt-file', '<prompt-file>']
    prompt = tmp_path/'answer.md'; prompt.write_text('Merge upstream.')
    monkeypatch.setattr(foundry_entry, '_analyst_executable', lambda backend: Path(sys.executable))
    decision = tmp_path/'decision.json'
    goal._write(decision, {'action': 'delegate', 'instruction': 'Apply the operator instructions.'})
    monkeypatch.setattr(manager, 'turn', lambda state, tree, packet, **kwargs: (
        {'action': 'delegate', 'instruction': 'Apply the operator instructions.', 'evidence': goal._fact(decision)},
        state, {}, packet))
    received = []
    def capture(worker_session, worktree, tag, message, **kwargs):
        received.append((worker_session, message))
        raise RuntimeError('fixture stops at the original coding session')
    monkeypatch.setattr(dispatch, 'resume_order', capture)
    assert cli.main(['--project-config', str(scope.path), '--order', str(order), '--foundry-goal',
        '--amend-from', str(outcome), '--coding-session', session, '--prompt-file', str(prompt),
        '--goal-evidence', str(tmp_path/'amended')]) == 2
    assert len(received) == 1, capsys.readouterr().err
    assert received[0][0] == session and 'Merge upstream.' in received[0][1]
    assert outcome.read_bytes() == before and not launches


def test_capacity_policy_defaults_and_validation():
    normalized = policy.normalize_policy(capacity_defaults=True)
    assert sum(normalized['capacity_retry_seconds']) >= 2*3600
    assert 'capacity_retry_seconds' not in policy.normalize_policy({})  # old stored policies remain comparable
    assert policy.normalize_policy({'capacity_retry_seconds': []})['capacity_retry_seconds'] == []
    for delays in ([0], [-1], [True], [float('inf')], '60,300'):
        with pytest.raises(ValueError):
            policy.normalize_policy({'capacity_retry_seconds': delays})


def test_goal_cli_passes_explicit_capacity_policy(external, monkeypatch):
    _, scope, order, _ = external
    monkeypatch.setattr(foundry_entry, '_analyst_executable', lambda backend: Path(sys.executable))
    received = []
    monkeypatch.setattr(goal, 'run_goal', lambda *args, **kwargs: received.append(kwargs) or {'status': 'project_handoff'})
    assert cli.main(['--project-config', str(scope.path), '--order', str(order), '--foundry-goal',
                     '--launcher', 'app-server', '--app-server-capacity-retry-seconds', '60', '7200']) == 0
    assert received[0]['app_server_policy']['capacity_retry_seconds'] == [60, 7200]


@pytest.mark.parametrize('delays', [[], [60, 7200]])
def test_status_resume_argv_roundtrips_capacity_policy(delays):
    import argparse
    from foundry_runtime._project_status import resume_argv
    parser = argparse.ArgumentParser()
    policy.add_policy_arguments(parser)
    argv = resume_argv('project.json', 'TASK1', 'session',
                       {'launcher': 'app-server', 'app_server_policy': {'capacity_retry_seconds': delays}})
    args, _ = parser.parse_known_args(argv)
    args.order, args.resume, args.launcher, args.steer, args.foundry_analyze = None, 'session', 'app-server', False, False
    assert policy.policy_from_arguments(args, parser)['capacity_retry_seconds'] == delays
