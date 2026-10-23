"""Recovery routing must not mistake a prior successful turn for this worker."""
import json
from pathlib import Path
import sys

import pytest

from test_installed_runtime import external, offline
from test_owned_goal_recovery import interrupted, native_manager, prepare
from foundry_runtime import foundry_goal as goal
from foundry_runtime import _foundry_goal_continuation as continuation
from foundry_runtime import _foundry_goal_transitions as transitions
from foundry_runtime import _foundry_manager as manager


@pytest.mark.parametrize('phase,migrated', [
    ('feedback_pending', False),
    ('feedback_pending', True),
    ('dispatch_pending', True),
])
def test_published_recovery_with_old_rc0_still_recovers_worker(
        interrupted, tmp_path, monkeypatch, phase, migrated):
    case = interrupted
    tree, scope, order, predecessor, cleanup, header, ruling, root, active = case
    native_manager(case, monkeypatch)
    recovery_file = prepare(case, tmp_path)
    manifest = json.loads((active / 'manifest.json').read_bytes())
    old_checkpoint = json.loads(goal.goal_usage._reference(manifest['coding_checkpoint']))
    assert old_checkpoint['returncode'] == 0
    retained_events = {p: p.read_bytes() for p in predecessor.iterdir()}
    manifest['execution_recovery'] = goal._fact(recovery_file)
    if migrated:
        monkeypatch.setattr(transitions, 'resolve_executable', lambda _: Path(sys.executable))
        manifest = transitions.manager(goal, active, manifest, provider=None,
            migrate=True, answer=ruling.read_text(), live=True)
        decision, state, _, _ = manager.turn(manifest['manager_state'], tree, {'stage': 'worker_returned'})
        manifest['manager_state'] = state
        manifest['execution_decision'] = {**manifest['execution_decision'], 'manager': decision}
    manifest['manager_phase'] = phase
    goal._write(active / 'manifest.json', manifest)
    received = []
    def run(*args, **kwargs):
        received.append(kwargs)
        return {'status': 'test-dispatch-captured'}
    monkeypatch.setattr(goal.goal_execution, 'run_owned', run)
    monkeypatch.setattr(goal, '_recover_manager_feedback',
        lambda *args, **kwargs: pytest.fail('old rc0 checkpoint bypassed unfinished recovery'))
    result = continuation.recover_worker(goal, order, active, manifest,
        ruling.read_text(), recovery_file, migrate_runtime=migrated)
    assert result == {'status': 'test-dispatch-captured'}
    assert len(received) == 1
    request = received[0]
    assert request['worker_recovery'] is True
    assert request['recovery_file'] == recovery_file
    assert request['retained']['returncode'] is None
    assert request['retained']['evidence'] == old_checkpoint['evidence']
    assert request['manager_state'] == manifest['manager_state']
    assert all(p.read_bytes() == raw for p, raw in retained_events.items())


@pytest.mark.parametrize('changed', ['source_state', 'run_id'])
def test_published_recovery_does_not_allow_unrelated_manager_or_goal(
        interrupted, tmp_path, monkeypatch, changed):
    case = interrupted
    tree, scope, order, predecessor, cleanup, header, ruling, root, active = case
    native_manager(case, monkeypatch)
    recovery_file = prepare(case, tmp_path)
    manifest = json.loads((active / 'manifest.json').read_bytes())
    manifest['execution_recovery'] = goal._fact(recovery_file)
    monkeypatch.setattr(transitions, 'resolve_executable', lambda _: Path(sys.executable))
    manifest = transitions.manager(goal, active, manifest, provider=None,
        migrate=True, answer=ruling.read_text(), live=True)
    if changed == 'source_state':
        manifest['manager_state']['handoff']['source_state']['native_dir'] += '-unrelated'
    else:
        manifest['run_id'] = 'another-goal'
    manifest['manager_phase'] = 'dispatch_pending'
    goal._write(active / 'manifest.json', manifest)
    monkeypatch.setattr(goal.goal_execution, 'run_owned',
        lambda *args, **kwargs: pytest.fail('unrelated recovery reached dispatch'))
    with pytest.raises((ValueError, RuntimeError)):
        continuation.recover_worker(goal, order, active, manifest,
            ruling.read_text(), recovery_file, migrate_runtime=True)


@pytest.mark.parametrize('binding', ['exact', 'wrong_digest', 'wrong_attempt'])
def test_two_retained_dispatches_use_explicit_recovery_predecessor(
        tmp_path, monkeypatch, binding):
    from types import SimpleNamespace
    from foundry_runtime import _foundry_dispatch as dispatch
    from foundry_runtime import _dispatch_package as packages
    folder = tmp_path / 'goal'
    folder.mkdir()
    tree = tmp_path / 'tree'
    tree.mkdir()
    old_id, target_id = 'a' * 64, 'b' * 64
    decision_path = tmp_path / 'recovery.json'
    goal._write(decision_path, {'task_key': 'TASK1', 'predecessor': {
        'intent_id': target_id, 'attempt_id': 'target-attempt'}})
    fact = goal._fact(decision_path)
    manifest = {'tag': 'TASK1', 'worktree': str(tree), 'order': {'sha256': 'order'},
                'execution_recovery': fact,
                'execution_decision': {'manager': {'instruction': 'retained assignment'}}}
    monkeypatch.setattr(continuation, '_matches_manager_assignment',
        lambda *a: pytest.fail('manager input replaced explicit recovery authorization'))
    records = {}
    # The unrelated earlier predecessor has the lexically later package name.
    # Selection must follow the grant, never filename or timestamp ordering.
    for name, intent, attempt in [('dispatch-z', old_id, 'old-attempt'),
                                  ('dispatch-a', target_id, 'target-attempt')]:
        path = folder / (name + '.package.json')
        goal._write(path, {'tag': 'TASK1', 'attempt_id': attempt, 'fixture_id': intent})
        records[intent] = {'intent_id': intent, 'attempt_id': attempt, 'launcher': 'app-server',
            'state': 'unknown', 'frozen': {'worktree': str(tree), 'order_sha256': 'order'},
            'recovery_authorization': {'decision_sha256': fact['sha256']}}
    if binding == 'wrong_digest':
        records[target_id]['recovery_authorization']['decision_sha256'] = '0' * 64
    elif binding == 'wrong_attempt':
        records[target_id]['attempt_id'] = 'different-attempt'
        goal._write(folder / 'dispatch-a.package.json',
            {'tag': 'TASK1', 'attempt_id': 'different-attempt', 'fixture_id': target_id})
    monkeypatch.setattr(dispatch, '_project_adapter',
        lambda _: SimpleNamespace(evidence=lambda tag: folder))
    monkeypatch.setattr(packages, 'read_intent',
        lambda package, **kw: (folder / package['fixture_id'], records[package['fixture_id']]))
    if binding == 'exact':
        path, _, record, _ = continuation.retained_dispatch(goal, folder, manifest)
        assert path.name == 'dispatch-a.package.json'
        assert record['intent_id'] == target_id
    else:
        with pytest.raises(goal.GoalBlocked, match='found 2'):
            continuation.retained_dispatch(goal, folder, manifest)
