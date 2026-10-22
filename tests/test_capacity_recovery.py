"""Actual stdio turns and dispatcher journals, without provider calls."""
import json
from pathlib import Path
import sys

import pytest

from test_installed_runtime import external, offline
from test_owned_goal_recovery import SERVER, SID
from foundry_runtime import _dispatch_capacity as capacity, _foundry_dispatch as dispatch
from foundry_runtime import app_server_transport as transport, foundry_goal as goal
from foundry_runtime import _project_status


@pytest.fixture
def capacity_worker(external, tmp_path, monkeypatch):
    tree, scope, order, _ = external
    home = tmp_path / 'codex'
    sessions = home / 'sessions/2026/10/12'; sessions.mkdir(parents=True)
    (sessions / f'rollout-fixture-{SID}.jsonl').write_text(json.dumps(
        {'type': 'session_meta', 'payload': {'id': SID, 'cwd': str(tree)}}) + '\n')
    monkeypatch.setenv('CODEX_HOME', str(home))
    calls, waits = [], []
    monkeypatch.setattr(capacity, 'sleep', waits.append)
    from foundry_runtime import _app_server_policy
    monkeypatch.setattr(_app_server_policy, 'DEFAULT_CAPACITY_DELAYS', (5, 15, 30))
    monkeypatch.setattr(capacity, 'time', lambda: 1000)
    faults = ['serverOverloaded']
    original = transport.run_executor
    def run(*args, **kwargs):
        calls.append({'session': kwargs.get('resume_thread_id'), 'body': args[1],
                      'model': kwargs.get('model'), 'effort': kwargs.get('effort')})
        script = 'HANG=False\n' + SERVER
        if faults:
            info = faults.pop(0)
            # Captured SCHED2 terminal error shape, with synthetic identities.
            error = {'message': 'Selected model is at capacity. Please try a different model.',
                     'codexErrorInfo': info, 'additionalDetails': None, 'misalignment': None}
            script = script.replace("'status':'completed','items':[],'error':None",
                                    "'status':'failed','items':[],'error':" + repr(error))
        def spawn(argv, **opts):
            return transport.OwnedProcess.spawn([sys.executable, '-u', '-c', script], **opts)
        return original(*args, process_factory=spawn, **kwargs)
    monkeypatch.setattr(transport, 'run_executor', run)
    return tree, scope, order, calls, waits, faults


def test_capacity_resumes_same_thread_and_replays_without_new_call(capacity_worker):
    tree, scope, order, calls, waits, faults = capacity_worker
    result = dispatch.dispatch_order(order, project=scope, launcher='app-server', model='gpt-6.1-sol', effort='high')
    assert result[0] == 0 and [c['session'] for c in calls] == [None, SID]
    assert waits == [5]
    assert 'Continue the same task' in calls[1]['body']
    assert calls[0]['model'] == calls[1]['model'] == 'gpt-6.1-sol'
    assert calls[0]['effort'] == calls[1]['effort'] == 'high'
    assert len(result.capacity_executions) == 2
    before = len(calls)
    receipts = {p: p.read_bytes() for p in Path(result[1]).parent.glob('*.receipt.json')}
    replay = dispatch.dispatch_order(order, project=scope, launcher='app-server', model='gpt-6.1-sol', effort='high')
    assert replay == result and replay.replayed and replay.new_execution_count == 0
    assert not result.replayed and result.new_execution_count == 2
    assert waits == [5]
    assert len(calls) == before
    assert all(p.read_bytes() == raw for p, raw in receipts.items())


def test_capacity_exhaustion_is_bounded_observable_and_manually_resumable(capacity_worker, tmp_path):
    tree, scope, order, calls, waits, faults = capacity_worker
    faults[:] = ['serverOverloaded'] * 4
    folder = tmp_path / 'goal'
    result = goal.run_goal(order, folder, Path(sys.executable), coding_launcher='app-server',
                           project_record=scope.record())
    assert result['status'] == 'provider_capacity_exhausted'
    assert [c['session'] for c in calls] == [None, SID, SID, SID]
    assert waits == [5, 15, 30]
    status = _project_status.status(scope.path, 'TASK1')
    assert not status['errors']
    final = next(r for r in status['runs'] if r.get('execution_status') == 'provider_capacity_exhausted')
    assert final['resume_argv'][0] == sys.executable
    assert final['provider_recovery']['automatic_retries'] == 3
    assert '--recover-goal' in result['next_action']
    assert goal.recover_goal(order, folder, answer='Capacity restored; continue')['status'] == 'project_handoff'
    assert len(calls) == 5


@pytest.mark.parametrize('info', ['usageLimitExceeded', 'rateLimitExceeded', 'badRequest', 'other'])
def test_noncapacity_errors_do_not_consume_retries(capacity_worker, info):
    _, scope, order, calls, waits, faults = capacity_worker
    faults[:] = [info]
    result = dispatch.dispatch_order(order, project=scope, launcher='app-server')
    assert result[0] == 1 and len(calls) == 1 and waits == []


def test_capacity_then_quota_stop_ends_without_more_retries(capacity_worker):
    _, scope, order, calls, waits, faults = capacity_worker
    faults[:] = ['serverOverloaded', 'usageLimitExceeded']
    result = dispatch.resume_order(SID, scope.workspace, 'TASK1', 'Continue the fixture',
                                   project=scope, launcher='app-server')
    assert result[0] == 1 and result.outcome == 'turn_failed'
    assert [c['session'] for c in calls] == [SID, SID] and waits == [5]


def test_cancel_during_backoff_does_not_launch(capacity_worker, monkeypatch):
    _, scope, order, calls, waits, _ = capacity_worker
    def cancel(_): raise KeyboardInterrupt()
    monkeypatch.setattr(capacity, 'sleep', cancel)
    with pytest.raises(KeyboardInterrupt):
        dispatch.dispatch_order(order, project=scope, launcher='app-server')
    assert len(calls) == 1
    monkeypatch.setattr(capacity, 'sleep', waits.append)
    resumed = dispatch.dispatch_order(order, project=scope, launcher='app-server')
    assert resumed[0] == 0 and not resumed.replayed
    assert resumed.new_execution_count == 1 and len(calls) == 2 and waits == [5]


def test_exhausted_chain_replay_has_no_wait_or_new_attempt(capacity_worker, capsys):
    _, scope, order, calls, waits, faults = capacity_worker
    faults[:] = ['serverOverloaded'] * 4
    first = dispatch.dispatch_order(order, project=scope, launcher='app-server')
    assert first.outcome == 'provider_capacity_exhausted'
    receipts = {p: p.read_bytes() for p in Path(first[1]).parent.glob('*.receipt.json')}
    capsys.readouterr()
    replay = dispatch.dispatch_order(order, project=scope, launcher='app-server')
    assert replay == first and replay.replayed and replay.new_execution_count == 0
    assert replay.outcome == 'provider_capacity_exhausted'
    assert len(calls) == 4 and waits == [5, 15, 30]
    assert 'provider capacity: retry' not in capsys.readouterr().out
    assert all(p.read_bytes() == raw for p, raw in receipts.items())


def test_cli_failed_resume_distinguishes_replay_from_new_turn(capacity_worker, tmp_path, capsys):
    from foundry_runtime.cli import main
    _, scope, _, calls, waits, faults = capacity_worker
    faults[:] = ['badRequest']
    prompt = tmp_path / 'prompt.txt'; prompt.write_text('Continue retained work')
    argv = ['--project-config', str(scope.path), '--resume', SID, '--tag', 'TASK1',
            '--worktree', str(scope.workspace), '--prompt-file', str(prompt), '--launcher', 'app-server']
    assert main(argv) == 1
    assert 'app-server turn failed' in capsys.readouterr().out
    assert main(argv) == 1
    output = capsys.readouterr().out
    assert 'replayed prior outcome, no turn started' in output
    assert 'execution intent:' in output and 'log    :' in output
    assert 'app-server turn failed' not in output
    assert len(calls) == 1 and not waits
    prompt.write_text('Continue after correcting the request')
    assert main(argv) == 0
    assert 'replayed prior outcome' not in capsys.readouterr().out
    assert len(calls) == 2


def test_unverified_cleanup_never_retries(capacity_worker, monkeypatch):
    _, scope, order, calls, waits, _ = capacity_worker
    original = transport.run_executor
    def damage(*args, **kwargs):
        result = original(*args, **kwargs)
        path = Path(args[3]).with_suffix('.cleanup.json')
        record = json.loads(path.read_bytes()); record['cleanup_verified'] = False
        path.write_text(json.dumps(record))
        return result
    monkeypatch.setattr(transport, 'run_executor', damage)
    with pytest.raises(Exception, match='cleanup|evidence'):
        dispatch.dispatch_order(order, project=scope, launcher='app-server')
    assert len(calls) == 1 and not waits


def test_hour_scale_policy_resume_waits_only_remaining_time(capacity_worker, monkeypatch):
    _, scope, order, calls, waits, _ = capacity_worker
    def cancel(_): raise KeyboardInterrupt()
    monkeypatch.setattr(capacity, 'sleep', cancel)
    policy = {'capacity_retry_seconds': [7200]}
    with pytest.raises(KeyboardInterrupt):
        dispatch.dispatch_order(order, project=scope, launcher='app-server', app_server_policy=policy)
    status = _project_status.status(scope.path, 'TASK1')
    interrupted = next(r for r in status['runs'] if r.get('execution_status') == 'capacity_interrupted')
    assert interrupted['provider_recovery']['next_retry_at'] == 8200
    monkeypatch.setattr(capacity, 'time', lambda: 4600)
    monkeypatch.setattr(capacity, 'sleep', waits.append)
    result = dispatch.dispatch_order(order, project=scope, launcher='app-server', app_server_policy=policy)
    assert result[0] == 0 and len(calls) == 2 and waits == [3600]
    assert result.capacity_recovery['schedule_seconds'] == [7200]


def test_empty_capacity_schedule_reports_recovery_without_retry(capacity_worker):
    _, scope, order, calls, waits, _ = capacity_worker
    result = dispatch.dispatch_order(order, project=scope, launcher='app-server',
                                     app_server_policy={'capacity_retry_seconds': []})
    assert result.outcome == 'provider_capacity_exhausted'
    assert result.capacity_recovery['resume_argv'] and len(calls) == 1 and not waits


def test_damaged_optional_backoff_receipt_does_not_block_continuation(capacity_worker, monkeypatch):
    tree, scope, order, calls, waits, _ = capacity_worker
    def cancel(_): raise KeyboardInterrupt()
    monkeypatch.setattr(capacity, 'sleep', cancel)
    with pytest.raises(KeyboardInterrupt):
        dispatch.dispatch_order(order, project=scope, launcher='app-server')
    next(tree.parent.rglob('dispatch.receipt.json')).write_text('[]')
    monkeypatch.setattr(capacity, 'sleep', waits.append)
    result = dispatch.dispatch_order(order, project=scope, launcher='app-server')
    assert result[0] == 0 and len(calls) == 2 and waits == [5]
