import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace

import pytest

from test_installed_runtime import external, offline, git
from foundry_runtime import _candidate_closure as closure, foundry_goal as goal, cli
from foundry_runtime._executor_lifecycle import CleanupReceipt, SpawnFailure


@pytest.mark.parametrize('stage', ['before', 'partial', 'registered', 'interrupt'])
def test_failed_add_cleans_only_owned_scratch(external, tmp_path, monkeypatch, stage):
    tree, _, _, _ = external
    head = git(tree, 'rev-parse', 'HEAD')
    observed = []
    def command(checkout, source):
        observed.append(checkout)
        return [sys.executable, '-c', 'pass']
    original = closure.OwnedProcess.spawn
    def fail_add(argv, **kwargs):
        if 'add' in argv:
            checkout = observed[0]
            if stage == 'partial':
                checkout.mkdir(); (checkout/'partial.txt').write_text('incomplete')
            if stage == 'registered':
                goal._git(tree, *argv[1:])
            if stage == 'interrupt':
                raise KeyboardInterrupt()
            raise RuntimeError('fixture worktree add failure')
        return original(argv, **kwargs)
    monkeypatch.setattr(closure.OwnedProcess, 'spawn', fail_add)
    adapter = SimpleNamespace(candidate_closure_command=command)
    if stage == 'interrupt':
        with pytest.raises(KeyboardInterrupt):
            closure.verify(goal, adapter, tree, head, tmp_path/'evidence', None)
    else:
        result = closure.verify(goal, adapter, tree, head, tmp_path/'evidence', None)
        assert result['status'] == 'not_evaluated'
        assert 'fixture worktree add failure' in result['error']
        assert result['checkout_cleanup'] == 'removed'
        assert Path(result['receipt']['path']).is_file()
    assert not observed[0].parent.exists()
    assert git(tree, 'worktree', 'list', '--porcelain').count('worktree ') == 1
    assert (tree/'AGENTS.md').exists()


@pytest.mark.parametrize('spawn_error,verified', [(False, False), (True, False), (True, True)])
def test_unverified_child_retains_checkout(external, tmp_path, monkeypatch, spawn_error, verified):
    tree, _, _, _ = external
    head = git(tree, 'rev-parse', 'HEAD')
    receipt = CleanupReceipt('fixture', None, None, verified, 0 if verified else None, verified, 'fixture')
    process = SimpleNamespace(poll=lambda: 0, stop_and_verify=lambda **kw: receipt)
    original = closure.OwnedProcess.spawn
    def spawn(argv, **kwargs):
        if Path(argv[0]).stem == 'git':
            return original(argv, **kwargs)
        if spawn_error:
            raise SpawnFailure(RuntimeError('fixture spawn failure'), process, receipt)
        return process
    monkeypatch.setattr(closure.OwnedProcess, 'spawn', spawn)
    adapter = SimpleNamespace(candidate_closure_command=lambda *a: [sys.executable, '-c', 'pass'])
    result = closure.verify(goal, adapter, tree, head, tmp_path/'evidence', None)
    checkout = Path(result['checkout']); work = checkout.parent.resolve()
    try:
        assert result['process_cleanup']['cleanup_verified'] is verified
        assert work.exists() is not verified
        assert (result['checkout_cleanup'] == 'removed') is verified
        if not verified:
            assert 'retain checkout' in result['checkout_cleanup']
    finally:
        # This fixture spawns no process. Remove only its recorded short workspace.
        assert work.name and len(work.name) == 32 and all(c in '0123456789abcdef' for c in work.name)
        expected = Path(checkout.anchor)/'Foundry/tmp' if os.name == 'nt' else Path(tempfile.gettempdir())/'foundry-closure'
        assert work.parent == expected.resolve()
        if work.exists():
            git(tree, 'worktree', 'remove', '--force', str(checkout))
            shutil.rmtree(work)


@pytest.mark.parametrize('option,value,message', [
    ('team_profile', 'adaptive', 'requires a coding answer'),
    ('handoff_role', 'manager', 'requires a provider'),
])
def test_failed_closure_staffing_flags_are_already_rejected(external, tmp_path, monkeypatch, capsys, option, value, message):
    tree, scope, order, _ = external
    folder = tmp_path/'goal'; folder.mkdir()
    goal._write(folder/'manifest.json', {'order': goal._fact(order), 'worktree': str(tree),
        'candidate_closure': {'status': 'failed'}})
    monkeypatch.setattr(closure, 'verify', lambda *a, **kw: pytest.fail('invalid staffing must not enter closure retry'))
    with pytest.raises(ValueError, match=message):
        goal.recover_goal(order, folder, **{option: value})
    flag = '--team-profile' if option == 'team_profile' else '--handoff-role'
    argv = ['--project-config', str(scope.path), '--order', str(order), '--foundry-goal',
            '--recover-goal', str(folder), flag, value]
    try:
        code = cli.main(argv)
    except SystemExit as exc:
        code = exc.code
    assert code == 2
    assert ('requires --prompt-file' if option == 'team_profile' else 'requires') in capsys.readouterr().err
