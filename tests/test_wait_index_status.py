import json
from pathlib import Path

import pytest

from foundry_runtime import _code_index as index, _compute_selection as compute
from foundry_runtime import _followup as followup, foundry_goal as goal


@pytest.mark.parametrize('race', [False, True])
def test_existing_index_reuses_verified_bytes_without_contending(tmp_path, monkeypatch, race):
    (tmp_path / '.codegraph').mkdir()
    (tmp_path / '.codegraph/codegraph.db').touch()
    monkeypatch.setenv('FOUNDRY_CODEGRAPH_ARGV', '["node","codegraph.js"]')
    monkeypatch.setenv('FOUNDRY_CODEGRAPH_STATE_DIR', str(tmp_path / 'state'))
    calls, reads = [], []
    def verify(root):
        reads.append(root)
        if race and len(reads) == 1:
            raise ValueError('index differs from worktree')
        return {'indexed_files': 2}
    def run(argv, *args):
        calls.append(argv)
        if argv[-1] == '--version':
            return '1.6.2'
        raise ValueError('writer lock held')
    monkeypatch.setattr(index, 'verify', verify)
    monkeypatch.setattr(index, '_run', run)
    observation = {}
    assert index.prepare(tmp_path, observation=observation)
    assert len(calls) == (2 if race else 1)
    assert observation['status'] == 'ready'
    assert observation['preparation'] == ('verified_after_writer_conflict' if race else 'verified_reuse')


def test_busy_stale_index_falls_back_without_unlock_or_retry(tmp_path, monkeypatch):
    (tmp_path / '.codegraph').mkdir()
    (tmp_path / '.codegraph/codegraph.db').touch()
    lock = tmp_path / '.codegraph/codegraph.lock'; lock.write_text('owned by another task')
    monkeypatch.setenv('FOUNDRY_CODEGRAPH_ARGV', '["node","codegraph.js"]')
    monkeypatch.setenv('FOUNDRY_CODEGRAPH_STATE_DIR', str(tmp_path / 'state'))
    calls = []
    def run(argv, *args):
        calls.append(argv)
        if argv[-1] == '--version': return '1.6.2'
        raise ValueError('writer lock held')
    def stale(*args): raise ValueError('index differs from worktree: changed.py')
    monkeypatch.setattr(index, '_run', run); monkeypatch.setattr(index, 'verify', stale)
    observation = {}
    assert index.prepare(tmp_path, observation=observation) is None
    assert observation['status'] == 'unavailable' and 'changed.py' in observation['reason']
    assert observation['preparation_error'] == 'writer lock held'
    assert len(calls) == 2 and lock.read_text() == 'owned by another task'
    monkeypatch.setattr(index, 'verify', lambda *args: {'indexed_files': 1})
    assert index.prepare(tmp_path, observation=observation)
    assert observation['status'] == 'ready'
    assert 'preparation_error' not in observation


def test_live_controller_has_no_recovery_advice_then_release_restores_it(tmp_path, monkeypatch):
    from foundry_runtime import _foundry_goal_delivery as delivery
    folder = tmp_path / 'goal'; folder.mkdir()
    (folder / 'manifest.json').write_text(json.dumps({'run_id': 'fixture', 'manager_phase': 'dispatch_pending'}))
    monkeypatch.setattr(delivery, 'coding_recovery', lambda *a: {
        'status': 'manager_handback_reconciliation_required', 'next_action': 'recover'})
    with goal._run_lock(folder):
        observed = followup._goal_status(folder)
        assert observed['status'] == 'pending' and observed['coding_recovery'] is None
        assert observed['phase'] == 'dispatch_pending'
    stopped = followup._goal_status(folder)
    assert stopped['status'] == 'execution_stopped'
    assert stopped['coding_recovery']['next_action'] == 'recover'


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_wait_contract_is_frozen_into_both_teams(monkeypatch, backend):
    monkeypatch.setattr(compute, 'model', lambda tier: 'fixture-' + tier)
    team = compute.resolve_native_team('adaptive', backend=backend)
    rendered = compute.team_prompt('task', team)
    assert 'A wait timeout alone is not progress' in rendered
    assert 'no wait-count ceiling' in rendered
    # Historical teams keep their existing instructions; release does not hotload them.
    assert compute.team_prompt('task', {**team, 'instructions': 'retained'}) == 'retained\nTask:\ntask'
