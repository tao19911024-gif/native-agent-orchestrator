from concurrent.futures import ThreadPoolExecutor
from contextvars import Context, copy_context

import pytest

from foundry_runtime.foundry_goal import _run_lock, GoalBlocked
from test_manager_recovery import conversation


def acquire(folder, reuse):
    with _run_lock(folder, reuse_owned=reuse):
        return 'acquired'


def test_serialized_live_callback_reuses_actual_controller_ownership(tmp_path):
    manager = tmp_path / 'transferred-manager'
    with _run_lock(manager):
        with pytest.raises(GoalBlocked):
            acquire(manager, False)
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(copy_context().run, acquire, manager, True).result() == 'acquired'
        # Callback exit must not release the outer controller's file lock.
        with pytest.raises(GoalBlocked):
            Context().run(acquire, manager, True)
    assert Context().run(acquire, manager, False) == 'acquired'


def test_expired_copied_context_cannot_bypass_a_new_controller(tmp_path):
    manager = tmp_path / 'manager'
    with _run_lock(manager):
        old = copy_context()
    with _run_lock(manager):
        with pytest.raises(GoalBlocked):
            old.run(acquire, manager, True)
    assert old.run(acquire, manager, True) == 'acquired'


def test_reuse_does_not_cover_other_manager_directory(tmp_path):
    with _run_lock(tmp_path / 'first'):
        with _run_lock(tmp_path / 'second'):
            assert acquire(tmp_path / 'first', True) == 'acquired'
        # Independent context has no authority despite sharing the process PID.
        with pytest.raises(GoalBlocked):
            Context().run(acquire, tmp_path / 'first', True)


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_live_events_transferred_manager_answers_inside_outer_lock(conversation, tmp_path, backend):
    import io
    from types import SimpleNamespace
    from foundry_runtime.app_server_transport import AppServer
    from foundry_runtime._goal_intervention import live_scope
    from foundry_runtime import _foundry_manager as manager
    from test_live_manager_events import settle
    state, calls, failures = conversation
    state['backend'] = backend
    failures.clear()
    replies = []
    def decide(events):
        nonlocal state
        with _run_lock(state['native_dir'], reuse_owned=True):
            decision, state, _, _ = manager.turn(state, tmp_path,
                {'stage': 'worker_running', 'events': events})
        return decision['instruction']
    with _run_lock(state['native_dir']):
        with live_scope(decide):
            app = AppServer(SimpleNamespace(), io.StringIO(), deadline=None)
        app.thread_id, app.turn_id = 'root', 'turn'
        app.steer = lambda text, turn: replies.append((text, turn))
        app._observe({'method': 'item/completed', 'params': {'threadId': 'root', 'turnId': 'turn',
            'item': {'type': 'agentMessage', 'id': 'question', 'text': 'FOUNDRY_MANAGER_QUESTION decision'}}})
        settle(app)
        app.live_events.close(app)
        assert len(calls) == 1 and len(replies) == 1 and replies[0][1] == 'turn'
        assert not app.outcome_ready.is_set()
        with pytest.raises(GoalBlocked):
            Context().run(acquire, state['native_dir'], True)


def test_other_process_cannot_reuse_and_exception_releases(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    from foundry_runtime import foundry_goal
    folder = tmp_path / 'manager'
    script = '''import sys
from foundry_runtime.foundry_goal import _run_lock, GoalBlocked
try:
    with _run_lock(sys.argv[1], reuse_owned=True):
        pass
except GoalBlocked:
    sys.exit(23)
'''
    env = dict(os.environ, PYTHONPATH=str(Path(foundry_goal.__file__).parents[1]))
    with pytest.raises(RuntimeError, match='callback failed'):
        with _run_lock(folder):
            result = subprocess.run([sys.executable, '-c', script, str(folder)], env=env,
                                    capture_output=True, text=True, timeout=60)
            assert result.returncode == 23, result.stderr
            with _run_lock(folder, reuse_owned=True):
                raise RuntimeError('callback failed')
    assert acquire(folder, False) == 'acquired'
