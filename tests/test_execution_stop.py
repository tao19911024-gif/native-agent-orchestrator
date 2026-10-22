"""Stop a real disposable controller/owned Job without any provider request."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import pytest

from test_installed_runtime import external, offline
from foundry_runtime import recovery
from foundry_runtime._execution_intent import ExecutionIntent


@pytest.mark.parametrize('backend', ['claude', 'app-server'])
@pytest.mark.parametrize('keep_recoverable,residual_controller', [(False, False), (True, False), (True, True)])
def test_stop_settles_executor_and_preserves_work(external, tmp_path, backend, keep_recoverable, residual_controller):
    tree, scope, order, _ = external
    marker = tree / 'unfinished.txt'
    marker.write_text('保留未提交的工作', encoding='utf-8')
    script = '''
import sys,json
from pathlib import Path
from foundry_runtime import _dispatch_claude as c, _foundry_dispatch as d
from foundry_runtime._project_execution import open_project
c.resolve_executable=lambda backend: Path(sys.executable)
def argv(executable,session,**kw):
    row=json.dumps({'type':'system','subtype':'init','session_id':session})
    return [sys.executable,'-u','-c', 'import sys,time;sys.stdin.read();print('+repr(row)+',flush=True);time.sleep(60)']
c.argv=argv
d.dispatch_order(Path(sys.argv[2]),project=open_project(sys.argv[1]),launcher='claude')
'''
    if backend == 'app-server':
        from test_owned_goal_recovery import SERVER
        script = '''import sys,json
from pathlib import Path
from foundry_runtime import _foundry_dispatch as d, app_server_transport as t
from foundry_runtime._project_execution import open_project
d._codex=lambda:sys.executable
original=t.run_executor
def run(*a,**kw):
    def spawn(argv,**opts): return t.OwnedProcess.spawn([sys.executable,'-u','-c',%r],**opts)
    return original(*a,process_factory=spawn,**kw)
t.run_executor=run
d.dispatch_order(Path(sys.argv[2]),project=open_project(sys.argv[1]),launcher='app-server')
''' % ('HANG=True\n' + SERVER)
    if residual_controller:
        script = 'try:\n    exec(' + repr(script) + ')\nfinally:\n    import time; time.sleep(60)\n'
    with (tmp_path / 'controller.log').open('wb') as log:
        controller_python = os.environ.get('FOUNDRY_TEST_STOP_PYTHON', sys.executable)
        isolation = ['-I'] if os.environ.get('FOUNDRY_TEST_STOP_PYTHON') else []
        proc = subprocess.Popen([controller_python, *isolation, '-c', script, str(scope.path), str(order)],
            cwd=tree, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            intents = scope.common_dir / 'pdcl-dispatch/task-TASK1/intents'
            deadline = time.monotonic() + 15
            row = None
            while time.monotonic() < deadline:
                for folder in intents.glob('*'):
                    row, _, _ = ExecutionIntent.read_history(folder, task_key='TASK1')
                    if row['state'] == 'active' and (backend != 'app-server' or row.get('turn_binding')):
                        break
                if row and row['state'] == 'active' and (backend != 'app-server' or row.get('turn_binding')):
                    break
                assert proc.poll() is None, (tmp_path/'controller.log').read_text(errors='replace')
                time.sleep(.1)
            assert row and row['state'] == 'active'
            args = ['--project-config', str(scope.path), '--task', 'TASK1', '--predecessor', folder.name,
                    '--stop-reason', '交接到另一个执行方', '--output', str(tmp_path/'stop.json')]
            if keep_recoverable:
                args.append('--keep-recoverable')
            if residual_controller:
                from foundry_runtime._execution_stop import _terminate
                _terminate(row)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    latest, _, _ = ExecutionIntent.read_history(folder, task_key='TASK1')
                    if latest['state'] == 'unknown':
                        break
                    time.sleep(.1)
                assert latest['state'] == 'unknown' and latest['cleanup']['cleanup_verified']
                assert proc.poll() is None
                args.append('--stop-controller-after-cleanup')
            assert recovery.main(args) == 0
            proc.wait(timeout=10)
            settled, _, _ = ExecutionIntent.read_history(folder, task_key='TASK1')
            assert settled['state'] == ('unknown' if keep_recoverable else 'closed')
            assert settled['cleanup']['cleanup_verified'] and settled['cleanup']['job_active_processes'] == 0
            assert 'outcome' not in settled
            assert marker.read_text(encoding='utf-8') == '保留未提交的工作'
            saved = {p: p.read_bytes() for p in folder.iterdir()}
            assert recovery.main(args) == 0
            assert saved == {p: p.read_bytes() for p in folder.iterdir()}
        finally:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=10)


def test_stop_never_terminates_a_reused_pid():
    from foundry_runtime._execution_stop import _terminate
    from foundry_runtime._execution_intent import IntentConflict
    proc = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'],
                            creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        with pytest.raises(IntentConflict, match='reused'):
            _terminate({'root_pid': proc.pid, 'root_creation_time': 1})
        assert proc.poll() is None
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_stop_pending_never_closes_or_kills_controller(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from foundry_runtime import _execution_stop as stop
    record = dict(state='active', root_pid=21, root_creation_time=22,
                  owner_pid=31, owner_creation_time=32, attempt_id='attempt')
    owner = SimpleNamespace(path=tmp_path, _read=lambda _: (record, None, None))
    monkeypatch.setattr(stop, 'ExecutionIntent', lambda *a, **k: owner)
    killed = []
    monkeypatch.setattr(stop, '_terminate', lambda r: killed.append(r['root_pid']))
    monkeypatch.setattr(stop, 'process_identity_exited', lambda *a: False)
    with pytest.raises(TimeoutError, match='cleanup is still pending'):
        stop.stop(tmp_path, 'TASK', {}, intent_id='intent', output=tmp_path/'out.json',
                  reason='handoff', keep_recoverable=True, wait_seconds=0)
    assert killed == [21] and record['state'] == 'active'
    assert not (tmp_path/'out.json').exists()
    with pytest.raises(stop.IntentConflict, match='another request'):
        stop.stop(tmp_path, 'TASK', {}, intent_id='intent', output=tmp_path/'out.json',
                  reason='handoff', keep_recoverable=False, wait_seconds=0)
    assert killed == [21]


def test_keep_recoverable_requires_explicit_stop_reason():
    with pytest.raises(SystemExit) as error:
        recovery.main(['--project-config','unused.json','--task','TASK', '--predecessor','0'*64,
                       '--keep-recoverable','--output','unused-output.json'])
    assert error.value.code == 2


@pytest.mark.parametrize('defect', ['active', 'unverified', 'children', 'wrong_attempt', 'root_alive', 'successor', 'owner_changed', 'busy'])
def test_residual_stop_requires_original_released_cleaned_attempt(tmp_path, monkeypatch, defect):
    from types import SimpleNamespace
    from foundry_runtime import _execution_stop as stop
    record = dict(state='unknown', root_pid=21, root_creation_time=22,
                  owner_pid=31, owner_creation_time=32, attempt_id='attempt')
    original = dict(record)
    record['cleanup'] = dict(attempt_id='attempt', root_pid=21, root_creation_time=22,
                             cleanup_verified=True, root_exited=True, job_active_processes=0)
    if defect == 'active': record['state'] = 'active'
    if defect == 'unverified': record['cleanup']['cleanup_verified'] = False
    if defect == 'children': record['cleanup']['job_active_processes'] = 1
    if defect == 'wrong_attempt': record['cleanup']['attempt_id'] = 'other'
    if defect == 'successor': record['recovery_authorization'] = {}
    if defect == 'owner_changed': record['owner_creation_time'] = 99
    def acquire():
        if defect == 'busy': raise stop.IntentBusy('owner still holds intent')
    released, killed = [], []
    owner = SimpleNamespace(path=tmp_path, _read=lambda _: (record, None, None),
                            _acquire=acquire, __exit__=lambda *a: released.append(True))
    monkeypatch.setattr(stop, '_terminate', lambda r: killed.append(r))
    monkeypatch.setattr(stop, 'process_identity_exited', lambda *a: defect != 'root_alive')
    with pytest.raises((stop.IntentConflict, stop.IntentBusy)):
        stop._stop_cleaned_controller(owner, original)
    assert killed == [] and released == [True]


def test_controller_stop_requires_reason():
    with pytest.raises(SystemExit) as error:
        recovery.main(['--project-config','unused.json','--task','TASK', '--predecessor','0'*64,
                       '--stop-controller-after-cleanup','--output','unused-output.json'])
    assert error.value.code == 2
