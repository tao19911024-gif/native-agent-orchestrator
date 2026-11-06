import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from foundry_runtime import foundry_goal as goal, _foundry_dispatch as dispatch, _dispatch_package as packages
from foundry_runtime._foundry_goal_continuation import retained_dispatch


@pytest.mark.parametrize('linked', [False, True])
def test_checkpoint_excludes_same_log_through_junction(tmp_path, monkeypatch, linked):
    evidence = tmp_path / 'evidence'; evidence.mkdir()
    alias = tmp_path / 'alias'
    if linked:
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(evidence)], check=True,
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        alias = evidence
    folder = tmp_path / 'goal'; folder.mkdir()
    checkpoint = folder / 'coding-checkpoint.json'
    checkpoint.write_text(json.dumps({'coding_session': 'session', 'evidence': [
        {'path': str(evidence / 'dispatch.log')}]}))
    manifest = {'tag': 'TEST', 'worktree': str(tmp_path), 'order': {'sha256': 'order'},
                'coding_checkpoint': goal._fact(checkpoint)}
    for stem in ('dispatch', 'dispatch_resumed1'):
        (evidence / (stem + '.package.json')).write_text(json.dumps({'tag': 'TEST', 'attempt_id': stem}))
    monkeypatch.setattr(dispatch, '_project_adapter', lambda _: SimpleNamespace(evidence=lambda _: alias))
    monkeypatch.setattr(packages, 'read_intent', lambda package, **kw: (tmp_path, {
        'launcher': 'app-server', 'attempt_id': package['attempt_id'],
        'frozen': {'worktree': str(tmp_path), 'order_sha256': 'order', 'session_id': 'session'},
        'state': 'completed' if package['attempt_id'] == 'dispatch' else 'unknown'}))
    try:
        selected, _, _, _ = retained_dispatch(goal, folder, manifest)
        assert selected.name == 'dispatch_resumed1.package.json'
    finally:
        if linked:
            alias.rmdir()  # Remove only this test junction, never its target.


def test_ambiguous_selection_never_advertises_preferred_command(tmp_path, monkeypatch):
    from foundry_runtime import _foundry_goal_continuation as continuation
    from foundry_runtime._foundry_goal_delivery import coding_recovery
    def ambiguous(*args):
        raise goal.GoalBlocked('manager hand-back needs one exact retained dispatch; found 2')
    monkeypatch.setattr(continuation, 'retained_dispatch', ambiguous)
    report = coding_recovery(tmp_path, {'manager_state': {'backend': 'codex'}, 'manager_phase': 'dispatch_pending'})
    assert report['manager_recovery']['status'] == 'unavailable'
    assert report['manager_recovery']['preferred'] is False
    assert 'found 2' in report['next_action']
    assert 'argv' not in report['manager_recovery']


def test_handback_selects_current_round_without_reconstructing_assignment(tmp_path, monkeypatch):
    evidence = tmp_path / 'evidence'; evidence.mkdir()
    folder = tmp_path / 'current'; folder.mkdir()
    manifest = {'tag': 'TEST', 'run_id': 'current', 'worktree': str(tmp_path), 'order': {'sha256': 'order'},
                'execution_decision': {'manager': {'instruction': 'Later manager instruction'}}}
    monkeypatch.setattr(dispatch, '_project_adapter', lambda _: SimpleNamespace(evidence=lambda _: evidence))
    monkeypatch.setattr(packages, 'read_intent', lambda package, **kw: (tmp_path, {
        'launcher': 'claude', 'attempt_id': package['attempt_id'], 'frozen': {'worktree': str(tmp_path),
            'order_sha256': package['body_sha256'], 'session_id': 'same-session'}, 'state': 'unknown'}))
    for index, binding in enumerate((None, {'directory': str(tmp_path / 'previous'), 'run_id': 'previous'},
                                     {'directory': str(folder), 'run_id': 'current'})):
        package = {'tag': 'TEST', 'attempt_id': str(index), 'body_sha256': 'resumed' if index else 'order'}
        if binding:
            package['goal'] = binding
        (evidence / f'dispatch_resumed{index}.package.json').write_text(json.dumps(package))
    path, _, record, _ = retained_dispatch(goal, folder, manifest)
    assert path.name == 'dispatch_resumed2.package.json'
    assert record['attempt_id'] == '2'
