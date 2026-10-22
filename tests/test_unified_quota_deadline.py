import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from foundry_runtime import _task_deadline as deadline, _foundry_manager as manager
from foundry_runtime import foundry_native as native, _code_index
from foundry_runtime import _foundry_dispatch as dispatch, _dispatch_capacity as capacity
from foundry_runtime import app_server_transport as transport
from foundry_runtime._provider_activity import codex_reset
from test_capacity_recovery import capacity_worker
from test_installed_runtime import external, offline
from test_quota_window_recovery import quota_worker


@pytest.fixture(params=['claude', 'codex'])
def quota_manager(request, tmp_path, monkeypatch):
    backend = request.param
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    home = tmp_path/'home'
    monkeypatch.setenv('CODEX_HOME', str(home))
    monkeypatch.setattr(manager, 'resolve_executable', lambda _: Path(sys.executable))
    monkeypatch.setattr(_code_index, 'prepare', lambda *a, **k: None)
    calls, waits = [], []
    due = time.time() + 60
    sid = '00000000-0000-0000-0000-000000000003'
    spawn = native.OwnedProcess.spawn
    def local(argv, **kw):
        source = kw['stdin']; request = json.loads(source.read().rsplit(b'\n\n', 1)[-1]); source.seek(0)
        failed = not calls
        calls.append((request, argv))
        result = {'action': 'delegate', 'instruction': 'Retain work and implement the next step.'}
        if backend == 'claude':
            rows = [{'type': 'system', 'subtype': 'init', 'tools': ['StructuredOutput'],
                     'mcp_servers': [], 'skills': [], 'plugins': [], 'model': 'fixture', 'session_id': sid}]
            if failed:
                rows += [{'type': 'rate_limit_event', 'session_id': sid, 'rate_limit_info':
                          {'status': 'rejected', 'rateLimitType': 'five_hour', 'resetsAt': due}},
                         {'type': 'result', 'session_id': sid, 'is_error': True, 'subtype': 'success',
                          'result': "You've hit your session limit"}]
            else:
                rows += [{'type': 'assistant', 'session_id': sid, 'message': {'role': 'assistant', 'content': [
                    {'type': 'tool_use', 'name': 'StructuredOutput', 'id': 'format', 'input': result}]}},
                    {'type': 'user', 'session_id': sid, 'message': {'role': 'user', 'content': [
                    {'type': 'tool_result', 'tool_use_id': 'format', 'content': 'Structured output provided successfully'}]}},
                    {'type': 'result', 'session_id': sid, 'subtype': 'success', 'is_error': False,
                     'result': json.dumps(result), 'structured_output': result}]
        else:
            rows = [{'type': 'thread.started', 'thread_id': sid}, {'type': 'turn.started'}]
            if failed:
                sessions = home/'sessions/2026/10/14'; sessions.mkdir(parents=True)
                (sessions/('rollout-fixture-'+sid+'.jsonl')).write_text('\n'.join(map(json.dumps, [
                    {'type': 'session_meta', 'payload': {'id': sid, 'cwd': str(tmp_path)}},
                    {'type': 'event_msg', 'timestamp': datetime.now(timezone.utc).isoformat(),
                     'payload': {'type': 'token_count', 'rate_limits': {'primary':
                     {'used_percent': 100, 'window_minutes': 300, 'resets_at': due}}}}
                ]))+'\n')
                rows += [{'type': 'turn.failed', 'error': {'message': 'Usage limit', 'codexErrorInfo': 'usageLimitExceeded'}}]
            else:
                rows += [{'type': 'item.completed', 'item': {'id': 'final', 'type': 'agent_message', 'text': json.dumps(result)}},
                         {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'cached_input_tokens': 0, 'output_tokens': 5}}]
        script = 'import sys;sys.stdin.buffer.read();print(' + repr('\n'.join(map(json.dumps, rows))) + ');sys.exit('+str(int(failed))+')'
        return spawn([sys.executable, '-B', '-c', script], **kw)
    monkeypatch.setattr(native.OwnedProcess, 'spawn', local)
    monkeypatch.setattr(manager, 'sleep', waits.append)
    state = {'backend': backend, 'model': None, 'effort': None, 'last_call_id': None,
             'native_dir': str(tmp_path/'manager'), 'quota_recovery': 1}
    return state, calls, waits, due


def test_manager_waits_same_session_and_replay_has_no_call(quota_manager, tmp_path):
    state, calls, waits, due = quota_manager
    packet = {'stage': 'assign'}
    result = manager.turn(state, tmp_path, packet)
    assert len(calls) == 2 and len(waits) == 1 and 0 < waits[0] <= 60
    from foundry_runtime.foundry_native import _call_folder
    predecessor = _call_folder(Path(state['native_dir']), calls[1][0]['retry_of'])/'receipt.json'
    assert json.loads(predecessor.read_bytes())['outcome'] == 'provider_limit'
    assert '00000000-0000-0000-0000-000000000003' in calls[1][1]
    assert manager.turn(state, tmp_path, packet)[0] == result[0]
    assert len(calls) == 2 and len(waits) == 1


def test_manager_reset_beyond_deadline_stops_without_wait(quota_manager, tmp_path):
    state, calls, waits, due = quota_manager
    state['task_deadline_at'] = due - 30
    with pytest.raises(native.NativeError):
        manager.turn(state, tmp_path, {'stage': 'assign'})
    assert len(calls) == 1 and not waits


def test_manager_cancelled_wait_recovers_same_request(quota_manager, tmp_path, monkeypatch):
    state, calls, waits, due = quota_manager
    def cancel(_): raise KeyboardInterrupt()
    monkeypatch.setattr(manager, 'sleep', cancel)
    with pytest.raises(KeyboardInterrupt):
        manager.turn(state, tmp_path, {'stage': 'assign'})
    assert len(calls) == 1
    monkeypatch.setattr(manager, 'sleep', waits.append)
    assert manager.turn(state, tmp_path, {'stage': 'assign'})[0]['action'] == 'delegate'
    assert len(calls) == 2 and len(waits) == 1


@pytest.mark.parametrize('fault_sequence,expected_waits', [(['usageLimitExceeded'], [100]),
    (['usageLimitExceeded', 'serverOverloaded'], [100, 5])])
def test_codex_dispatch_quota_wait_and_replay(capacity_worker, monkeypatch, fault_sequence, expected_waits):
    _, scope, order, calls, waits, faults = capacity_worker
    faults[:] = fault_sequence
    original = transport.AppServer._log
    def log(self, line):
        if '"codexErrorInfo": "usageLimitExceeded"' in line:
            original(self, 'stdout: '+json.dumps({'method': 'account/rateLimits/updated', 'params': {'rateLimits': {
                'primary': {'usedPercent': 100, 'windowDurationMins': 300, 'resetsAt': 1100}}}})+'\n')
        original(self, line)
    monkeypatch.setattr(transport.AppServer, '_log', log)
    result = dispatch.dispatch_order(order, project=scope, launcher='app-server')
    assert result[0] == 0 and waits == expected_waits and len(calls) == len(expected_waits)+1
    assert dispatch.dispatch_order(order, project=scope, launcher='app-server').replayed
    assert len(calls) == len(expected_waits)+1 and waits == expected_waits


def test_absolute_deadline_is_inherited_and_never_restarted(external):
    _, scope, order, calls = external
    cutoff = time.time()+60
    first = dispatch.dispatch_order(order, project=scope, task_deadline_at=cutoff)
    package = json.loads(first[1].with_suffix('.package.json').read_bytes())
    from foundry_runtime._dispatch_package import read_intent
    _, saved = read_intent(package)
    assert saved['frozen']['task_deadline_at'] == cutoff
    with deadline.scope(time.time()-1):
        assert dispatch.dispatch_order(order, project=scope).replayed


def test_window_parser_does_not_treat_weekly_or_missing_values_as_short():
    short = {'usedPercent': 100, 'windowDurationMins': 300, 'resetsAt': 1100}
    assert codex_reset({'primary': short}) == 1100
    assert codex_reset({'primary': short, 'secondary': {**short, 'windowDurationMins': 10080}}) is None
    assert codex_reset({'primary': {**short, 'resetsAt': True}}) is None
    with pytest.raises(ValueError): deadline.normalize('2026-10-14T12:00:00')


def test_sparse_codex_update_keeps_exhausted_weekly_window(tmp_path):
    from foundry_runtime._provider_activity import stream_reset
    path = tmp_path/'log'
    snapshots = [{'secondary': {'usedPercent': 100, 'windowDurationMins': 10080, 'resetsAt': 3000}},
                 {'primary': {'usedPercent': 100, 'windowDurationMins': 300, 'resetsAt': 1100}}]
    path.write_text('\n'.join(json.dumps({'method': 'account/rateLimits/updated', 'params': {'rateLimits': s}}) for s in snapshots)+'\n')
    assert stream_reset(path, 'codex', None) is None


def test_claude_reset_past_task_deadline_does_not_launch_successor(quota_worker, monkeypatch):
    (_, scope, order, _), calls, waits, _, _ = quota_worker
    monkeypatch.setattr(deadline.time, 'time', lambda: 1000)
    result = dispatch.dispatch_order(order, project=scope, launcher='claude', task_deadline_at=1050)
    assert result[0] == 1 and len(calls) == 1 and not waits
    receipt = json.loads(result[1].with_suffix('.receipt.json').read_bytes())
    assert receipt['provider_recovery']['status'] == 'deadline_prevents_retry'


def test_successful_claude_resume_retains_deadline(quota_worker):
    (_, scope, order, _), calls, waits, faults, _ = quota_worker
    faults.clear()
    cutoff = time.time()+120
    first = dispatch.dispatch_order(order, project=scope, launcher='claude', task_deadline_at=cutoff)
    result = dispatch.resume_order(calls[0][0], scope.workspace, 'TASK1', 'Next step', project=scope, launcher='claude')
    from foundry_runtime._dispatch_package import read_intent
    _, record = read_intent(json.loads(result[1].with_suffix('.package.json').read_bytes()))
    assert record['frozen']['task_deadline_at'] == cutoff


def test_expired_new_dispatch_starts_no_child(external):
    _, scope, order, calls = external
    with pytest.raises(TimeoutError, match='deadline'):
        dispatch.dispatch_order(order, project=scope, task_deadline_at=time.time()-1)
    assert not calls


def test_claude_running_process_uses_existing_cleanup_at_deadline(quota_worker, monkeypatch):
    from foundry_runtime import _dispatch_claude
    (_, scope, order, _), _, _, faults, _ = quota_worker
    faults.clear()
    monkeypatch.setattr(_dispatch_claude, 'argv', lambda *a, **k:
                        [sys.executable, '-B', '-c', 'import time; time.sleep(30)'])
    with pytest.raises(TimeoutError, match='deadline'):
        dispatch.dispatch_order(order, project=scope, launcher='claude', task_deadline_at=time.time()+1)
    receipts = list(scope.evidence('TASK1').glob('*.cleanup.json'))
    assert receipts and all(json.loads(p.read_bytes())['cleanup_verified'] for p in receipts)


def test_goal_records_shared_deadline(capacity_worker, tmp_path):
    from foundry_runtime import foundry_goal as goal
    _, scope, order, _, _, faults = capacity_worker
    faults.clear()
    folder = tmp_path/'goal'
    cutoff = time.time()+120
    result = goal.run_goal(order, folder, Path(sys.executable), coding_launcher='app-server',
                           project_record=scope.record(), task_deadline_at=cutoff)
    assert result['status'] == 'project_handoff'
    assert json.loads((folder/'manifest.json').read_bytes())['task_deadline_at'] == cutoff
