"""Run with an installed candidate: the inspector uses that actual interpreter."""
import json
from pathlib import Path
import sys

import pytest

from test_manager_recovery import conversation
from test_installed_runtime import external, offline
from test_goal_preflight_recovery import retained_goal
from foundry_runtime import _foundry_manager as manager, foundry_goal as goal, cli
from foundry_runtime import _foundry_recovery_preflight as preflight


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_installed_probe_restores_native_and_cli_does_not_execute(conversation, external, tmp_path, monkeypatch, capsys, backend):
    state, calls, failures = conversation
    state['backend'] = backend
    failures.clear()
    tree, scope, order, _ = external
    monkeypatch.setenv('FOUNDRY_COMPACT_TOKENS', '240000')
    _, state, _, _ = manager.turn(state, tree, {'stage': 'assign'})
    _, folder, manifest = retained_goal(external, scope, tmp_path)
    manifest['manager_state'] = state
    goal._write(folder / 'manifest.json', manifest)
    monkeypatch.setenv('FOUNDRY_COMPACT_TOKENS', '0')
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    report = preflight.inspect_recovery(folder, manifest, order)
    assert report['status'] == 'ready', report
    assert report['environment']['FOUNDRY_COMPACT_TOKENS'] == '240000'
    capsys.readouterr()
    assert cli.main(['--project-config', str(scope.path), '--order', str(order), '--foundry-goal',
                     '--recover-goal', str(folder), '--recovery-preflight']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'ready'
    assert len(calls) == 1
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    # Missing receipts remain the existing reconciliation owner's work. A
    # read-only plan neither fabricates a receipt nor mistakes it for completion.
    receipt = next(Path(state['native_dir']).glob('call-*/receipt.json'))
    receipt.rename(receipt.with_suffix('.saved'))
    report = preflight.inspect_recovery(folder, manifest, order)
    assert report['status'] == 'reconciliation_required', report
    assert not receipt.exists() and len(calls) == 1
