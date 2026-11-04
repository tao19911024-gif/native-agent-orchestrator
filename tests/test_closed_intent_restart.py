"""Explicit new goals follow closed executions without replacing their evidence."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from foundry_runtime import _foundry_dispatch as dispatch, foundry_goal as goal
from foundry_runtime._execution_intent import ExecutionIntent, IntentConflict
from test_installed_runtime import external, offline, Analyst
from test_manager_input_wait import manager_io, wait


@pytest.fixture
def abandoned(external, tmp_path):
    _, scope, order, _ = external
    # A local Python child publishes the package and then exits through normal
    # owned cleanup. No native provider executable is resolved or launched.
    script = '''
import sys
from pathlib import Path
from foundry_runtime import _foundry_dispatch as d
from foundry_runtime._project_execution import open_project
d._codex = lambda: sys.executable
def argv(*args, **kwargs):
    return [sys.executable, '-B', '-c', 'import sys;sys.stdin.read()']
d.coding_argv = argv
original = d._record_package
def interrupted(*args):
    original(*args)
    raise RuntimeError('fixture controller handback')
d._record_package = interrupted
d.dispatch_order(Path(sys.argv[2]), project=open_project(sys.argv[1]))
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(scope.path), str(order)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode != 0 and 'fixture controller handback' in result.stderr
    folder = Path(scope.project['evidence_root']) / 'TASK1'
    package = json.loads((folder / 'dispatch.package.json').read_bytes())
    journal = Path(package['intent_journal'])
    record, _, _ = ExecutionIntent.read_history(journal, task_key='TASK1')
    assert record['state'] == 'unknown' and record['cleanup']['cleanup_verified']
    return folder, journal, record


def close(external, abandoned, tmp_path):
    folder, journal, record = abandoned
    result = ExecutionIntent.settle_abandoned(external[1].common_dir, 'TASK1', record['frozen'],
        intent_id=record['intent_id'], output=tmp_path / 'closure.json', close_reason='Retire the abandoned fixture.')
    assert result['state'] == 'closed'
    return {p: p.read_bytes() for root in (folder, journal) for p in root.glob('*') if p.is_file()}


def test_new_unmanaged_goal_after_identical_closed_execution(external, abandoned, tmp_path, monkeypatch):
    before = close(external, abandoned, tmp_path)
    _, scope, order, workers = external
    monkeypatch.setattr(goal, 'NativeAnalyst', Analyst)
    dispatch_order = dispatch.dispatch_order
    selected = []
    def invoke(*args, **kwargs):
        selected.append(kwargs['goal_run_id'])
        return dispatch_order(*args, **kwargs)
    monkeypatch.setattr(dispatch, 'dispatch_order', invoke)
    directory = tmp_path / 'new-goal'
    result = goal.run_goal(order, directory, Path(sys.executable), coding_launcher='exec', project_record=scope.record())
    assert len(workers) == 1 and result['coding']['returncode'] == 0
    manifest = json.loads((directory / 'manifest.json').read_bytes())
    package = json.loads((abandoned[0] / 'dispatch_new1.package.json').read_bytes())
    latest, _, _ = ExecutionIntent.read_history(Path(package['intent_journal']), task_key='TASK1')
    assert latest['state'] == 'completed' and selected == [manifest['run_id']]
    assert all(path.read_bytes() == raw for path, raw in before.items())


def test_managed_new_goal_preserves_claimed_file_families(external, abandoned, manager_io, tmp_path):
    before = close(external, abandoned, tmp_path)
    folder = abandoned[0]
    reserved = folder / 'dispatch_new1.package.json'
    reserved.write_text('Reserved package evidence')
    requests, decisions = manager_io
    decisions.extend([{'action': 'delegate', 'instruction': 'Begin the explicitly new goal.'}, wait('Completed fixture')])
    result = goal.run_goal(external[2], tmp_path / 'managed-goal', Path(sys.executable),
        coding_launcher='exec', project_record=external[1].record(), manager_state={
            'backend': 'codex', 'model': None, 'effort': None, 'native_dir': str(tmp_path / 'manager')})
    assert result['status'] == 'awaiting_input' and len(external[3]) == 1 and len(requests) == 2
    assert (folder / 'dispatch_new2.package.json').exists()
    assert reserved.read_text() == 'Reserved package evidence'
    assert all(path.read_bytes() == raw for path, raw in before.items())


def test_closed_raw_intent_names_the_working_new_goal_route(external, abandoned, tmp_path):
    before = close(external, abandoned, tmp_path)
    with pytest.raises(IntentConflict, match='--foundry-goal'):
        dispatch.dispatch_order(external[2], project=external[1])
    assert not external[3] and all(path.read_bytes() == raw for path, raw in before.items())


def test_new_goal_identity_does_not_reopen_unresolved_work(external, abandoned, monkeypatch):
    before = {p: p.read_bytes() for p in abandoned[1].glob('*.json')}
    from foundry_runtime._executor_lifecycle import OwnedProcess
    monkeypatch.setattr(OwnedProcess, 'spawn', lambda *a, **k: pytest.fail('Unresolved execution launched a child'))
    with pytest.raises(IntentConflict):
        dispatch.dispatch_order(external[2], project=external[1], goal_run_id='new-controller')
    assert all(path.read_bytes() == raw for path, raw in before.items())


def test_identical_closed_binding_uses_stable_goal_successor(external, abandoned, tmp_path):
    before = close(external, abandoned, tmp_path)
    result = dispatch.dispatch_order(external[2], project=external[1], goal_run_id='persisted-goal-id')
    package = json.loads(result[1].with_suffix('.package.json').read_bytes())
    record, _, _ = ExecutionIntent.read_history(Path(package['intent_journal']), task_key='TASK1')
    assert record['state'] == 'completed' and record['frozen']['goal_run_id'] == 'persisted-goal-id'
    replay = dispatch.dispatch_order(external[2], project=external[1], goal_run_id='persisted-goal-id')
    assert replay.replayed and replay.intent_id == result.intent_id and len(external[3]) == 1
    assert all(path.read_bytes() == raw for path, raw in before.items())


def test_new_goal_after_closure_can_select_new_compute(external, abandoned, tmp_path):
    before = close(external, abandoned, tmp_path)
    result = dispatch.dispatch_order(external[2], project=external[1], goal_run_id='new-goal',
                                     model='custom-worker', effort='high')
    assert result[0] == 0 and len(external[3]) == 1
    assert all(path.read_bytes() == raw for path, raw in before.items())
