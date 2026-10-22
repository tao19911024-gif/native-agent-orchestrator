"""Read-only follow-up across silence, controller exit and ordinary dispatch."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from foundry_runtime import cli, foundry_goal as goal, _foundry_dispatch as dispatch
from foundry_runtime import _followup
from foundry_runtime._member_activity import MemberActivity
from test_installed_runtime import external, offline


def test_claude_complete_silence_warns_once_and_progress_resets(tmp_path):
    stream = tmp_path / 'stream'
    stream.write_text(json.dumps({'type': 'assistant', 'message': {'content': [
        {'type': 'tool_use', 'name': 'Agent', 'id': 'member'}]}}) + '\n')
    monitor = MemberActivity(10)
    assert monitor.read(stream, 0) == []
    assert monitor.read(stream, 11)[0]['signal'] == 'member_no_transcript_progress'
    assert monitor.read(stream, 22) == []
    with stream.open('a') as f:
        f.write(json.dumps({'type': 'assistant', 'parent_tool_use_id': 'member'}) + '\n')
    assert monitor.read(stream, 23) == []
    assert monitor.read(stream, 34)
    with stream.open('a') as f:
        f.write(json.dumps({'type': 'system', 'subtype': 'task_notification', 'tool_use_id': 'member'}) + '\n')
    assert monitor.read(stream, 100) == []


def test_goal_released_controller_does_not_remain_pending(tmp_path):
    folder = tmp_path / 'goal'
    folder.mkdir()
    (folder / 'manifest.json').write_text('{"run_id":"run"}')
    with goal._run_lock(folder):
        assert _followup.status(folder)['status'] == 'pending'
    before = {p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    report = _followup.status(folder)
    assert report['status'] == 'execution_stopped'
    assert report['event_id'] == _followup.status(folder)['event_id']
    assert report['task_acceptance'] == 'not_evaluated'
    assert before == {p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with goal._run_lock(folder):
        assert _followup.status(folder)['status'] == 'pending'
    assert report['event_id'] != _followup.status(folder)['event_id']


@pytest.mark.skipif(os.name != 'nt', reason='Windows recorded process identity')
def test_killed_goal_controller_is_visible_without_mutating_its_records(tmp_path):
    folder = tmp_path / 'goal'; folder.mkdir()
    script = '''import sys,time
from pathlib import Path
from foundry_runtime.foundry_goal import _run_lock
folder=Path(sys.argv[1])
with _run_lock(folder):
    (folder/'manifest.json').write_text('{"run_id":"killed"}')
    print('ready', flush=True)
    time.sleep(60)
'''
    proc = subprocess.Popen([sys.executable, '-c', script, str(folder)], stdout=subprocess.PIPE,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        assert proc.stdout.readline().strip() == b'ready'
        assert _followup.status(folder)['status'] == 'pending'
    finally:
        proc.kill(); proc.wait(timeout=5); proc.stdout.close()
    # Process signalling does not synchronise Windows byte-lock cleanup. Wait
    # for the actual kernel lock release, not for a status retry or a sleep.
    # A separate bounded probe allows a synchronous LockFileEx wait without
    # hanging the test runner. It opens the existing file read-only, takes a
    # shared lock once, and closes it before the status observation.
    barrier = '''import ctypes,msvcrt,sys
from ctypes import wintypes as w
class Overlapped(ctypes.Structure):
    _fields_=[('Internal',ctypes.c_size_t),('InternalHigh',ctypes.c_size_t),
              ('Offset',w.DWORD),('OffsetHigh',w.DWORD),('hEvent',w.HANDLE)]
api=ctypes.WinDLL('kernel32',use_last_error=True)
api.LockFileEx.argtypes=[w.HANDLE,w.DWORD,w.DWORD,w.DWORD,w.DWORD,ctypes.POINTER(Overlapped)]
api.LockFileEx.restype=w.BOOL
with open(sys.argv[1],'rb',buffering=0) as stream:
    if not api.LockFileEx(msvcrt.get_osfhandle(stream.fileno()),0,0,1,0,ctypes.byref(Overlapped())):
        raise ctypes.WinError(ctypes.get_last_error())
'''
    subprocess.run([sys.executable, '-c', barrier,
                    str(folder.with_name('.' + folder.name + '.owner.lock'))],
                   check=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    report = _followup.status(folder)
    assert report['status'] == 'execution_stopped', report
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_delayed_kernel_lock_release_is_pending_until_observed(tmp_path, monkeypatch):
    from foundry_runtime import _executor_lifecycle
    (tmp_path / 'manifest.json').write_text('{"run_id":"delayed-release"}')
    observation = {'state': 'held', 'generation': 1}
    monkeypatch.setattr(_executor_lifecycle, 'observe_locked_byte', lambda _: dict(observation))
    before = (tmp_path / 'manifest.json').read_bytes()
    report = _followup.status(tmp_path)
    assert report['status'] == 'pending' and report['event_id'] is None
    assert 'pending does not prove a live process' in report['note']
    observation['state'] = 'released'
    report = _followup.status(tmp_path)
    assert report['status'] == 'execution_stopped' and report['event_id']
    assert 'child cleanup is not established' in report['reason']
    assert (tmp_path / 'manifest.json').read_bytes() == before


def test_ordinary_dispatch_announces_bound_status_and_preserves_evidence(external, monkeypatch, capsys):
    monkeypatch.setenv('CODEX_THREAD_ID', 'origin')
    tree, project, order, launches = external
    rc, log, output = dispatch.dispatch_order(order, project=project)
    announcements = [json.loads(row.split(' ', 1)[1]) for row in capsys.readouterr().out.splitlines()
                     if row.startswith('FOUNDRY_FOLLOW_UP ')]
    assert rc == 0 and len(announcements) == 1
    notice = announcements[0]
    assert notice['originating_session']['thread_id'] == 'origin'
    before = {p:p.read_bytes() for p in tree.parent.rglob('*.json')}
    assert cli.main(notice['status_argv'][3:]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['run_id'] == notice['run_id']
    assert report['status'] == 'result_available' and report['returncode'] == 0
    assert before == {p:p.read_bytes() for p in tree.parent.rglob('*.json')}
    assert len(launches) == 1


def test_unknown_controller_evidence_never_becomes_a_stopped_goal(tmp_path):
    from foundry_runtime._executor_lifecycle import observe_locked_byte
    folder = tmp_path / 'goal'; folder.mkdir()
    (folder / 'manifest.json').write_text('{"run_id":"legacy"}')
    assert _followup.status(folder)['controller_observation']['state'] == 'unobserved'
    lock = tmp_path / '.goal.owner.lock'
    lock.write_bytes(b'')
    assert observe_locked_byte(lock)['state'] == 'unobserved'
    assert _followup.status(folder)['status'] == 'pending'


def test_terminal_publication_wins_over_released_controller(tmp_path, monkeypatch):
    from foundry_runtime import _executor_lifecycle
    (tmp_path / 'manifest.json').write_text('{"run_id":"race"}')
    def observed(path):
        (tmp_path / 'outcome.json').write_text('{"status":"project_handoff"}')
        return {'state': 'released', 'generation': 1}
    monkeypatch.setattr(_executor_lifecycle, 'observe_locked_byte', observed)
    assert _followup.status(tmp_path)['status'] == 'outcome_available'


def test_dispatch_status_does_not_attach_old_binding_to_another_attempt(external):
    _, project, order, _ = external
    _, log, _ = dispatch.dispatch_order(order, project=project)
    path = log.with_suffix('.package.json')
    receipt = json.loads(path.read_bytes())
    receipt['attempt_id'] = 'earlier-attempt'
    path.write_text(json.dumps(receipt))
    report = _followup.dispatch_status(path)
    assert report['status'] == 'superseded'
    assert report['run_id'].endswith(':earlier-attempt')


def test_controller_access_error_is_unknown_not_released(tmp_path, monkeypatch):
    import ctypes
    from types import SimpleNamespace
    from foundry_runtime import _executor_lifecycle as lifecycle
    lock = tmp_path / 'owner.lock'; lock.write_bytes(b'\0')
    def denied(*args):
        ctypes.set_last_error(5)
        return 0
    monkeypatch.setattr(lifecycle, '_api', lambda: SimpleNamespace(ReadFile=denied))
    assert lifecycle.observe_locked_byte(lock)['state'] == 'unobserved'


def test_real_native_subagent_lifecycle_without_collab_spawn_item():
    from foundry_runtime._member_activity import CodexMemberActivity
    monitor = CodexMemberActivity(10)
    def event(ident, kind, now, child='child'):
        monitor.observe({'method': 'item/completed', 'params': {'threadId': 'parent',
            'turnId': 'turn', 'item': {'type': 'subAgentActivity', 'id': ident,
            'kind': kind, 'agentThreadId': child}}}, now, thread_id='parent', turn_id='turn')
    event('start', 'started', 0)
    event('start', 'started', 8)
    event('message', 'interacted', 9)
    assert monitor.warnings(11)[0]['member'] == 'child'
    event('done', 'completed', 12)
    assert monitor.warnings(30) == []
    event('self', 'started', 31, child='parent')
    assert monitor.members == {}


def test_unknown_intent_with_live_process_requires_reconciliation_not_stopped(external, monkeypatch):
    from foundry_runtime._execution_intent import ExecutionIntent
    from foundry_runtime import _project_status
    _, project, order, _ = external
    _, log, _ = dispatch.dispatch_order(order, project=project)
    path = log.with_suffix('.package.json')
    receipt = json.loads(path.read_bytes())
    record, sequence, last = ExecutionIntent.read_history(Path(receipt['intent_journal']), task_key=receipt['tag'])
    monkeypatch.setattr(ExecutionIntent, 'read_history', lambda *a, **kw: ({**record, 'state': 'unknown'}, sequence, last))
    monkeypatch.setattr(_project_status, '_liveness', lambda _: {'owner': 'alive', 'root': 'alive'})
    report = _followup.dispatch_status(path)
    assert report['status'] == 'reconciliation_required'
    assert report['process_observation'] == {'owner': 'alive', 'root': 'alive'}
