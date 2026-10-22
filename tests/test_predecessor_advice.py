import json
from pathlib import Path

import pytest

from test_installed_runtime import external, offline
from test_owned_goal_recovery import interrupted
from foundry_runtime import recovery
from foundry_runtime._execution_intent import ExecutionIntent, IntentConflict


def test_live_identity_never_offers_close_command(tmp_path, monkeypatch):
    from foundry_runtime import _executor_lifecycle as lifecycle
    monkeypatch.setattr(lifecycle, 'process_identity_exited', lambda *args: False)
    report = recovery.blocker_advice(tmp_path, {'task_key': 'OLD', 'intent_id': 'id',
        'owner_pid': 1, 'owner_creation_time': 2, 'root_pid': 3, 'root_creation_time': 4})
    assert report['alive'] is True
    assert 'reconcile_argv' not in report


def test_dead_holder_advice_settles_lock_and_allows_reservation(interrupted):
    tree, scope, order, folder, *rest = interrupted
    row, _, _ = ExecutionIntent.read_history(folder, task_key='TASK1')
    lock = tree / '.pdcl-executor.lock'
    lock.write_text(json.dumps({'schema_version': 'pdcl-executor-lock-2', 'tag': 'TASK1',
        'worktree': str(tree), 'launcher': row['launcher'], 'state': 'running',
        **{key: row[key] for key in ('attempt_id', 'owner_pid', 'owner_creation_time', 'root_pid', 'root_creation_time')}}))
    intent = ExecutionIntent(scope.common_dir, 'TASK1', row['frozen'])
    with pytest.raises(IntentConflict) as caught:
        intent.acquire_lease(tree, 'NEXT', 'app-server')
    report = json.loads(str(caught.value).split('\n', 1)[1])
    assert report['holder_line'] == 'TASK1' and report['intent_id'] == row['intent_id']
    assert report['alive'] is False
    assert lock.exists()
    argv = report['reconcile_argv']
    assert argv[:3] == [__import__('sys').executable, '-m', 'foundry_runtime.recovery']
    assert recovery.main(['fixture predecessor ended' if arg == '<close-reason>' else arg for arg in argv[3:]]) == 0
    assert not lock.exists()
    closed, _, _ = ExecutionIntent.read_history(folder, task_key='TASK1')
    assert closed['state'] == 'closed'
    lease = intent.acquire_lease(tree, 'NEXT', 'app-server')
    from foundry_runtime._foundry_dispatch import _not_started
    assert lease.release(_not_started(lease))
