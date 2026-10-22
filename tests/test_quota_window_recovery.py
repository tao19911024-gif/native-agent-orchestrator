import json
import sys

import pytest

from foundry_runtime import _dispatch_claude as claude, _dispatch_capacity as recovery
from foundry_runtime import _foundry_dispatch as dispatch, _project_status
from foundry_runtime._provider_activity import short_window_reset
from test_installed_runtime import external, offline


@pytest.fixture
def quota_worker(external, monkeypatch):
    calls, waits = [], []
    clock = [1000]
    faults = [{'status': 'rejected', 'rateLimitType': 'five_hour', 'resetsAt': 1100,
               'unifiedWindows': {'five_hour': {'resetsAt': 1100}, 'seven_day': {'utilization': .2}}}]
    monkeypatch.setattr(recovery, 'time', lambda: clock[0])
    monkeypatch.setattr(recovery, 'sleep', waits.append)
    monkeypatch.setattr(claude, 'resolve_executable', lambda _: sys.executable)
    def argv(executable, session, **kw):
        calls.append((session, kw))
        fault = faults.pop(0) if faults else None
        rows = [{'type': 'system', 'subtype': 'init', 'session_id': session}]
        if fault:
            rows.append({'type': 'rate_limit_event', 'session_id': session, 'rate_limit_info': fault})
        rows.append({'type': 'result', 'subtype': 'success', 'session_id': session,
                     'is_error': bool(fault), 'result': "You've hit your session limit" if fault else 'done'})
        script = 'import sys;sys.stdin.buffer.read();print(' + repr('\n'.join(map(json.dumps, rows))) + ');sys.exit(' + str(int(bool(fault))) + ')'
        return [sys.executable, '-B', '-c', script]
    monkeypatch.setattr(claude, 'argv', argv)
    return external, calls, waits, faults, clock


def test_same_claude_session_waits_and_completed_replay_is_free(quota_worker):
    (_, scope, order, _), calls, waits, _, clock = quota_worker
    result = dispatch.dispatch_order(order, project=scope, launcher='claude', model='fixture', effort='high')
    assert result[0] == 0 and waits == [100]
    assert len(calls) == 2 and calls[0][0] == calls[1][0]
    assert calls[0][1]['model'] == calls[1][1]['model'] == 'fixture'
    receipts = {p: p.read_bytes() for p in result[1].parent.glob('*.receipt.json')}
    clock[0] = 1200
    replay = dispatch.dispatch_order(order, project=scope, launcher='claude', model='fixture', effort='high')
    assert replay[0] == 0 and replay.replayed and len(calls) == 2 and waits == [100]
    assert all(p.read_bytes() == data for p, data in receipts.items())


def test_interrupted_wait_is_visible_and_uses_absolute_reset(quota_worker, monkeypatch):
    (_, scope, order, _), calls, waits, _, clock = quota_worker
    def cancel(seconds):
        raise KeyboardInterrupt()
    monkeypatch.setattr(recovery, 'sleep', cancel)
    with pytest.raises(KeyboardInterrupt):
        dispatch.dispatch_order(order, project=scope, launcher='claude')
    rows = _project_status.status(scope.path, 'TASK1')['runs']
    assert any(r.get('execution_status') == 'quota_interrupted' and
               r['provider_recovery']['next_retry_at'] == 1100 for r in rows)
    assert len(calls) == 1
    clock[0] = 1050
    monkeypatch.setattr(recovery, 'sleep', waits.append)
    assert dispatch.dispatch_order(order, project=scope, launcher='claude')[0] == 0
    assert waits == [50] and len(calls) == 2


@pytest.mark.parametrize('change', [dict(rateLimitType='seven_day'), dict(resetsAt=None, unifiedWindows={}),
    dict(resetsAt=900), dict(resetsAt=True), dict(unifiedWindows={'seven_day': {'utilization': 1}})])
def test_ineligible_quota_stays_manually_resumable(quota_worker, change):
    (_, scope, order, _), calls, waits, faults, _ = quota_worker
    faults[0].update(change)
    result = dispatch.dispatch_order(order, project=scope, launcher='claude')
    assert result[0] == 1 and len(calls) == 1 and not waits


def test_unrelated_or_allowed_window_does_not_authorize_wait():
    assert short_window_reset({'latest_events': [{'session_id': 'other', 'status': 'rejected',
                              'limit_type': 'five_hour', 'resets_at': 1100}]}, 'owner') is None
