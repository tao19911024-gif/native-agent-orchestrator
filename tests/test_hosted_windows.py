"""Local OS fault injection only: no providers, desktop restart or business work."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from foundry_runtime import _hosted as hosted, _windows_host as windows
from foundry_runtime._executor_lifecycle import OwnedProcess, process_identity_exited
from test_installed_runtime import external, offline


def until(predicate, seconds=25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.1)
    raise AssertionError('Timed out waiting for local fixture evidence')


CONTROLLER = '''
from dataclasses import asdict
import json, pathlib, subprocess, sys, time
from foundry_runtime._executor_lifecycle import OwnedProcess
root = pathlib.Path(sys.argv[1])
import ctypes as c
from ctypes import wintypes as w
kernel=c.WinDLL('kernel32',use_last_error=True)
kernel.GetCurrentProcess.restype=w.HANDLE
kernel.GetPriorityClass.argtypes=[w.HANDLE]
native=c.WinDLL('ntdll')
native.NtQueryInformationProcess.argtypes=[w.HANDLE,c.c_int,c.c_void_p,w.ULONG,c.c_void_p]
handle=kernel.GetCurrentProcess()
priorities={'cpu':kernel.GetPriorityClass(handle)}
for name, kind in [('io',33),('memory',39)]:
    value=w.ULONG()
    assert native.NtQueryInformationProcess(handle,kind,c.byref(value),c.sizeof(value),None)==0
    priorities[name]=value.value
(root/'priorities.json').write_text(json.dumps(priorities))
print('中文参数:', sys.argv[2], flush=True)
child = OwnedProcess.spawn([sys.executable, str(root/'worker.py'), str(root)],
    cwd=root, stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr, attempt_id='fixture-child')
try:
    (root/'child.json').write_text(json.dumps({'pid':child.pid,'creation_time':child.creation_time}))
    limit=time.monotonic()+45
    while not (root/'finish').exists() and time.monotonic()<limit:
        time.sleep(.1)
finally:
    receipt=child.stop_and_verify(drain_seconds=0,kill_seconds=3)
    (root/'child-cleanup.json').write_text(json.dumps(asdict(receipt)))
assert receipt.cleanup_verified
'''

WORKER = '''
import pathlib, sys, time
root=pathlib.Path(sys.argv[1])
for i in range(600):
    with (root/'progress.txt').open('a') as f: f.write(str(i)+'\\n')
    time.sleep(.1)
'''


@pytest.mark.parametrize('shutdown', ['normal_exit', 'forced_job_cleanup'])
def test_service_controller_and_worker_survive_submitting_host(shutdown, tmp_path):
    root = tmp_path / '中文 空间'; root.mkdir()
    directory = root / 'launch'
    (root / 'controller.py').write_text(CONTROLLER, encoding='utf-8')
    (root / 'worker.py').write_text(WORKER, encoding='utf-8')
    (root / 'submit.py').write_text('''
import json, pathlib, sys, time
from foundry_runtime._hosted import submit
root=pathlib.Path(sys.argv[1])
report=submit(root/'launch',[str(root),'rg 示例应用 src'],entry=str(root/'controller.py'))
(root/'submitted.json').write_text(json.dumps(report))
if sys.argv[2]=='forced_job_cleanup': time.sleep(60)
''', encoding='utf-8')
    process = OwnedProcess.spawn([sys.executable, str(root / 'submit.py'), str(root), shutdown],
        cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        attempt_id='fixture-submitting-host')
    request = None
    try:
        until(lambda: (root / 'submitted.json').exists())
        request = hosted.read(directory / 'request.json')
        until(lambda: (root / 'child.json').exists())
        controller = hosted.read(directory / 'started.json')
        child = hosted.read(root / 'child.json')
        assert hosted.read(root / 'priorities.json') == {'cpu': 32, 'io': 2, 'memory': 5}
        assert not process.contains(controller['pid'], controller['creation_time'])
        assert not process.contains(child['pid'], child['creation_time'])
        if shutdown == 'normal_exit':
            assert process.wait(5) == 0
        host_cleanup = process.stop_and_verify(drain_seconds=0, kill_seconds=3)
        assert host_cleanup.cleanup_verified
        # child.json records spawn, not the worker's first successful write.
        until(lambda: (root / 'progress.txt').exists())
        before = (root / 'progress.txt').stat().st_size
        until(lambda: (root / 'progress.txt').stat().st_size > before + 5)
        assert not process_identity_exited(controller['pid'], controller['creation_time'])
        assert not process_identity_exited(child['pid'], child['creation_time'])
        (root / 'finish').touch()
        until(lambda: (directory / 'finished.json').exists())
        report = hosted.status(directory)
        assert report['status'] == 'launch_finished', report
        assert report['completion']['returncode'] == 0
        assert hosted.read(root / 'child-cleanup.json')['cleanup_verified']
        assert 'rg 示例应用 src' in (directory / 'controller.log').read_text(encoding='utf-8')
        assert report['scheduler']['execution_limit'] == 'PT0S'
        assert report['scheduler']['priority'] == 4
        hosted.write(root / 'fault-evidence.json', {'shutdown': shutdown,
            'submitting_host_cleanup': asdict(host_cleanup), 'controller': controller,
            'child': child, 'progress_after_host_exit': True, 'status': report})
    finally:
        (root / 'finish').touch()
        process.stop_and_verify(drain_seconds=0, kill_seconds=3)
        # Delete only this test's inert registration, after its bounded controller
        # exits. A failed test retains a still-active definition for diagnosis.
        if request and (directory / 'finished.json').exists():
            windows.call('remove', {'name': request['task_name']})


def test_service_reports_cli_error_without_business_execution(tmp_path):
    directory = tmp_path / 'launch'
    try:
        report = hosted.submit(directory, ['--not-a-foundry-option'])
        until(lambda: (directory / 'finished.json').exists())
        report = hosted.status(directory)
        assert report['status'] == 'launch_finished'
        assert report['completion']['returncode'] == 2
        assert 'unrecognized arguments' in (directory / 'controller.log').read_text(encoding='utf-8')
    finally:
        if (directory / 'finished.json').exists():
            windows.call('remove', {'name': hosted.read(directory / 'request.json')['task_name']})


def test_wrapper_can_delete_temporary_input_after_submission(tmp_path):
    source = tmp_path / 'temporary.json'
    source.write_text('{"expectHeadSha":"中文预检"}', encoding='utf-8')
    entry = tmp_path / 'fixture.py'
    entry.write_text('''
import json, pathlib, sys, time
root=pathlib.Path(sys.argv[1])
limit=time.monotonic()+25
while not (root/'read-now').exists() and time.monotonic()<limit: time.sleep(.1)
values=json.loads(pathlib.Path(sys.argv[3]).read_text(encoding='utf-8'))
print(values['expectHeadSha'], flush=True)
''', encoding='utf-8')
    directory = tmp_path / 'launch'
    try:
        hosted.submit(directory, [str(tmp_path), '--project-preflight-input', str(source)], entry=str(entry))
        source.unlink()
        (tmp_path / 'read-now').touch()
        until(lambda: (directory / 'finished.json').exists())
        report = hosted.status(directory)
        assert report['completion']['returncode'] == 0
        assert '中文预检' in (directory / 'controller.log').read_text(encoding='utf-8')
    finally:
        if (directory / 'finished.json').exists():
            windows.call('remove', {'name': hosted.read(directory / 'request.json')['task_name']})


def test_pdcl_entry_uses_recorded_runtime_without_dispatch(tmp_path):
    # --help exits before business admission. --order exercises the real entry's
    # current.json selection, overridden by its existing --runtime-python pin.
    entry = Path('D:/PDCL/command/bin/dispatch.py')
    assert entry.is_file(), 'This installed-host integration check requires the existing PDCL entry'
    directory = tmp_path / 'launch'
    try:
        hosted.submit(directory, ['--runtime-python', sys.executable,
            '--order', 'fixture-do-not-dispatch', '--help'], entry=str(entry))
        until(lambda: (directory / 'finished.json').exists())
        report = hosted.status(directory)
        assert report['completion']['returncode'] == 0
        assert '--hosted-status' in (directory / 'controller.log').read_text(encoding='utf-8')
    finally:
        if (directory / 'finished.json').exists():
            windows.call('remove', {'name': hosted.read(directory / 'request.json')['task_name']})


def test_actual_example_app_wrapper_with_synthetic_rejected_preflight(external, tmp_path):
    workspace, selected, order, _ = external
    probe = workspace / 'fixture-preflight.py'
    probe.write_text('''
import pathlib, sys
pathlib.Path('observed-input.json').write_text(sys.stdin.read(),encoding='utf-8')
raise SystemExit(7)
''', encoding='utf-8')
    project = selected.project
    project['execution']['preflight_argv'] = [sys.executable, str(probe)]
    selected.path.write_text(json.dumps(project), encoding='utf-8')
    directory = tmp_path / 'launch'
    wrapper = Path('C:/Users/example/Desktop/example_app/scripts/dispatch-codex.ps1')
    powershell = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
    try:
        result = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-File', str(wrapper),
            '-SpecPath', str(order), '-Tag', 'TASK1', '-ProjectConfig', str(selected.path),
            '-PythonExecutable', sys.executable, '-ExpectHeadSha', 'fixture-head'],
            cwd=workspace, env=dict(os.environ, FOUNDRY_HOSTED_DIRECTORY=str(directory)),
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW)
        assert 'FOUNDRY_FOLLOW_UP ' in result.stdout, (result.stdout, result.stderr)
        until(lambda: (directory / 'finished.json').exists())
        request = hosted.read(directory / 'request.json')
        index = request['argv'].index('--project-preflight-input')
        assert not Path(request['argv'][index + 1]).exists(), 'The real wrapper removes its temp file'
        observed = hosted.read(workspace / 'observed-input.json')
        assert observed['preflight_input'] == {'expectHeadSha': 'fixture-head'}
        report = hosted.status(directory)
        assert report['completion']['returncode'] != 0
        assert 'project preflight did not pass' in (directory / 'controller.log').read_text(encoding='utf-8')
        assert not (workspace / '.pdcl-executor.lock').exists()
    finally:
        if (directory / 'finished.json').exists():
            windows.call('remove', {'name': hosted.read(directory / 'request.json')['task_name']})
