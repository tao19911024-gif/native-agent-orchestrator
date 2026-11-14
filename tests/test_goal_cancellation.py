"""Goal cancellation and early occupancy use disposable execution journals."""
import json
from pathlib import Path
import sys

import pytest

from foundry_runtime import cli, foundry_goal as goal
from foundry_runtime._execution_intent import ExecutionIntent, IntentConflict, IntentBusy
from foundry_runtime import _execution_stop
from test_installed_runtime import external, offline, git
from test_owned_goal_recovery import interrupted


def bind_goal(interrupted, tmp_path):
    tree, scope, order, journal, *_ = interrupted
    package_path = Path(scope.project['evidence_root']) / 'TASK1/dispatch.package.json'
    package = json.loads(package_path.read_bytes())
    folder = tmp_path / 'cancel-goal'; folder.mkdir()
    manifest = {'run_id': 'goal-fixture', 'tag': 'TASK1', 'worktree': str(tree),
                'dispatch_evidence_root': str(package_path.parent), 'project_record': scope.record()}
    (folder / 'manifest.json').write_text(json.dumps(manifest))
    package['goal'] = {'directory': str(folder), 'run_id': manifest['run_id']}
    package_path.write_text(json.dumps(package))
    return folder, package_path, journal


def test_cancel_dead_goal_closes_original_and_releases_worktree(interrupted, tmp_path):
    folder, package, journal = bind_goal(interrupted, tmp_path)
    tree = interrupted[0]
    marker = tree / 'unfinished.txt'; marker.write_text('retain original work')
    original = {p: p.read_bytes() for p in journal.glob('*.json')}
    assert cli.main(['--cancel-goal', str(folder), '--cancel-reason', 'Requirements changed']) == 0
    record, _, _ = ExecutionIntent.read_history(journal, task_key='TASK1')
    assert record['state'] == 'closed' and not (tree / '.pdcl-executor.lock').exists()
    assert marker.read_text() == 'retain original work'
    assert all(p.read_bytes() == value for p, value in original.items())
    saved = {p: p.read_bytes() for p in journal.glob('*.json')}
    assert _execution_stop.cancel_goal(folder, 'Requirements changed')['action'] == 'no_unresolved_execution'
    assert saved == {p: p.read_bytes() for p in journal.glob('*.json')}
    ExecutionIntent.check_goal_occupancy(interrupted[1].common_dir, 'TASK1', tree)


def test_cancel_rejected_goal_never_closes_its_predecessor(interrupted, tmp_path):
    folder, package, journal = bind_goal(interrupted, tmp_path)
    rejected = tmp_path / 'rejected'; rejected.mkdir()
    manifest = json.loads((folder/'manifest.json').read_bytes()); manifest['run_id'] = 'rejected'
    (rejected/'manifest.json').write_text(json.dumps(manifest))
    before = {p: p.read_bytes() for p in journal.glob('*.json')}
    assert _execution_stop.cancel_goal(rejected, 'Cancel rejected launch')['action'] == 'no_unresolved_execution'
    with goal._run_lock(rejected):
        with pytest.raises(IntentBusy, match='without a bound executor'):
            _execution_stop.cancel_goal(rejected, 'Starting controller has no execution binding')
    assert before == {p: p.read_bytes() for p in journal.glob('*.json')}


@pytest.mark.parametrize('same_task', [True, False])
def test_occupied_new_goal_stops_before_paid_coordination(interrupted, tmp_path, monkeypatch, same_task):
    tree, scope, order, journal, *_ = interrupted
    from foundry_runtime import _foundry_manager as manager
    monkeypatch.setattr(manager, 'turn', lambda *a, **k: pytest.fail('paid coordination must not start'))
    if not same_task:
        order = tmp_path / 'NEXT_order.md'; order.write_text('new task')
        row, _, _ = ExecutionIntent.read_history(journal, task_key='TASK1')
        (tree/'.pdcl-executor.lock').write_text(json.dumps({
            'schema_version': 'pdcl-executor-lock-2', 'tag': 'TASK1', 'worktree': str(tree),
            'launcher': row['launcher'], 'state': 'running',
            **{key: row[key] for key in ('attempt_id', 'owner_pid', 'owner_creation_time', 'root_pid', 'root_creation_time')}}))
    git(tree, 'add', '.'); git(tree, 'commit', '--allow-empty', '-qm', 'fixture clean snapshot')
    before = {p: p.read_bytes() for p in journal.glob('*.json')}
    with pytest.raises(IntentConflict) as error:
        goal._run(order, tmp_path/'next-goal', Path(sys.executable), project_record=scope.record(),
                  coding_launcher='app-server', manager_profile='opus')
    assert error.value.blocker['holder_line'] == 'TASK1'
    assert error.value.blocker['intent_id'] == journal.name
    assert before == {p: p.read_bytes() for p in journal.glob('*.json')}
    assert not (tmp_path/'next-goal/manifest.json').exists()


def test_predecessor_advice_suppresses_rejected_goal_recovery(external, tmp_path, monkeypatch, capsys):
    tree, scope, order, _ = external
    from foundry_runtime import foundry_entry as entry
    from foundry_runtime import _foundry_cli_help as help
    monkeypatch.setattr(entry, '_analyst_executable', lambda *a: Path(sys.executable))
    blocker = {'holder_line': 'ORIGINAL', 'next_action': 'close original'}
    def refuse(*a, **k):
        raise IntentConflict('original executor is occupied\n' + json.dumps(blocker), blocker=blocker)
    monkeypatch.setattr(goal, 'run_goal', refuse)
    monkeypatch.setattr(help, 'announce_recovery_failure', lambda *a, **k: pytest.fail('wrong-goal recovery advice'))
    rc = cli.main(['--project-config', str(scope.path), '--foundry-goal', '--order', str(order),
                   '--team-profile', 'none'])
    assert rc == 2
    assert 'ORIGINAL' in capsys.readouterr().err
