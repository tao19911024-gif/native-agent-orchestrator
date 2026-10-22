"""CLI additions reach the common coding boundary and survive source-file loss."""
import json
from pathlib import Path
import sys

import pytest

from test_installed_runtime import external, offline, git
from foundry_runtime import cli, foundry_entry, foundry_goal as goal, _foundry_dispatch as dispatch
from foundry_runtime import _app_server_policy as policy


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
    if managed:
        from foundry_runtime import _foundry_manager
        manifest['manager_state'] = {'native_dir': str(tmp_path/'manager'), 'last_call_id': None}
        def manager_turn(state, worktree, packet, **kwargs):
            manager_packets.append(packet)
            return {'action': 'delegate', 'instruction': 'Apply the operator additions'}, state, {}, packet
        monkeypatch.setattr(_foundry_manager, 'turn', manager_turn)
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
    if managed:
        assert manager_packets[0]['operator_answer'] == marker
    assert received[0].index('Prior findings') < received[0].index(marker)
    saved = json.loads((folder/'manifest.json').read_bytes())['amendment']
    prompt.unlink()
    assert saved['operator_additions'] == marker
    assert marker in goal.amendment_prompt(order, saved, None, project=scope)


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
