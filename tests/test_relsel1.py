import json
import os
from pathlib import Path
import subprocess
import time
import sys

import pytest

from foundry_runtime._claude_tool_input import update, PREFIX
from foundry_runtime._member_activity import MemberActivity
from foundry_runtime import _project_status as status


@pytest.mark.parametrize('command', ['rg 示例应用 src', 'printf 丁'])
def test_installed_isolated_hook_reads_utf8(command, tmp_path):
    # 丁 includes UTF-8 byte 0x81, undefined in cp1252. Do not escape non-ASCII.
    payload = {'tool_name': 'Bash', 'tool_input': {'command': command, 'timeout': 3000}}
    result = subprocess.run([sys.executable, '-I', '-m', 'foundry_runtime._claude_tool_input'],
                            input=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                            capture_output=True, cwd=tmp_path, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['hookSpecificOutput']['updatedInput'] == {
        **payload['tool_input'], 'command': PREFIX + command}


def test_hook_keeps_tool_options_and_explicit_input():
    original = {'command': "printf 'hello\\n' | cat", 'timeout': 3000}
    result = update({'tool_name': 'Bash', 'tool_input': original})['hookSpecificOutput']
    assert result['updatedInput'] == {**original, 'command': PREFIX + original['command']}
    assert 'permissionDecision' not in result
    assert update({'tool_name': 'Bash', 'tool_input': result['updatedInput']})['hookSpecificOutput'] == result
    assert update({'tool_name': 'mcp__server', 'tool_input': original}) == {}


@pytest.mark.skipif(os.name != 'nt', reason='Windows owned-process regression')
def test_open_stdin_hangs_before_hook_and_eof_after_it(tmp_path):
    from foundry_runtime._executor_lifecycle import OwnedProcess
    bash = Path('C:/Program Files/Git/bin/bash.exe')
    assert bash.is_file()
    # Bash read reproduces an open-pipe wait without launching a paid CLI.
    command = 'IFS= read -r input; printf "done:%s" "$input"'
    for fixed in (False, True):
        with (tmp_path / str(fixed)).open('w+b') as output:
            proc = OwnedProcess.spawn([str(bash), '--noprofile', '--norc', '-c',
                                       (PREFIX if fixed else '') + command], cwd=tmp_path,
                                      stdin=subprocess.PIPE, stdout=output, stderr=output,
                                      attempt_id='stdin-' + str(fixed))
            try:
                deadline = time.monotonic() + (5 if fixed else .5)
                while proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                assert (proc.poll() is not None) == fixed
            finally:
                receipt = proc.stop_and_verify(drain_seconds=0, kill_seconds=3)
                assert receipt.cleanup_verified
            if fixed:
                output.seek(0)
                assert b'done:' in output.read()
    run = subprocess.run([str(bash), '--noprofile', '--norc', '-c',
                          PREFIX + "printf 'hello\\n' | cat; cat <<'EOF'\nbody\nEOF"],
                         capture_output=True, timeout=5)
    assert run.returncode == 0
    assert run.stdout == b'hello\nbody\n'


def heartbeat():
    return {'type': 'tool_progress', 'tool_name': 'Agent', 'heartbeat': True, 'parent_tool_use_id': 'member'}


def test_heartbeat_is_not_progress_and_warning_is_not_a_kill():
    monitor = MemberActivity(threshold=300)
    assert monitor.observe(heartbeat(), 0) is None
    assert monitor.observe(heartbeat(), 299) is None
    signal = monitor.observe(heartbeat(), 301)
    assert signal['signal'] == 'member_no_transcript_progress'
    assert 'no automatic termination' in signal['action']
    assert monitor.observe(heartbeat(), 600) is None
    monitor.observe({'type': 'assistant', 'parent_tool_use_id': 'member'}, 601)
    assert monitor.observe(heartbeat(), 602) is None
    assert monitor.observe(heartbeat(), 902) is not None


def test_incremental_partial_lines_and_completed_member(tmp_path):
    path = tmp_path / 'stream'
    raw = json.dumps(heartbeat()).encode() + b'\n'
    path.write_bytes(raw[:20])
    monitor = MemberActivity(10)
    assert monitor.read(path, 0) == []
    with path.open('ab') as f:
        f.write(raw[20:])
    assert monitor.read(path, 1) == []
    assert monitor.read(path, 12)[0]['signal'] == 'member_no_transcript_progress'  # silence is observable too
    monitor.observe({'type': 'system', 'subtype': 'task_notification',
                     'tool_use_id': 'member'}, 12)
    assert 'member' not in monitor.members


def test_status_uses_bound_package_and_never_current_python(tmp_path):
    log = tmp_path / 'dispatch.log'
    record = {'attempt_id': 'attempt', 'evidence': {'log': {'path': str(log)}}}
    assert status.recorded_python(record, 'intent') == '<recorded-python>'
    package = {'attempt_id': 'attempt', 'intent_id': 'intent',
               'runtime_provenance': {'python': 'D:/retained/python.exe'}}
    log.with_suffix('.package.json').write_text(json.dumps(package))
    selected = status.recorded_python(record, 'intent')
    assert selected == 'D:/retained/python.exe'
    assert status.resume_argv('project.json', 'T', 'session', {}, selected)[0] == selected
    assert status.recorded_python(record, 'other') == '<recorded-python>'
    package['attempt_id'] = 'other'
    log.with_suffix('.package.json').write_text(json.dumps(package))
    assert status.recorded_python(record, 'intent') == '<recorded-python>'


def test_observation_io_failure_does_not_interrupt_execution(tmp_path):
    monitor = MemberActivity()
    missing = tmp_path / 'missing'
    assert monitor.read(missing, 0)[0]['signal'] == 'member_observation_unavailable'
    assert monitor.read(missing, 1) == []
    missing.write_text('{"type":"assistant","message":null}\n')
    assert monitor.read(missing, 2) == []
    assert monitor.read_error is None
    assert monitor.observe({**heartbeat(), 'tool_name': 'Bash'}, 1000) is None
    assert monitor.observe({**heartbeat(), 'parent_tool_use_id': None, 'tool_use_id': None}, 1000) is None
    assert monitor.observe({'message': {'content': [{'type': 'tool_use', 'name': 'Agent'}]}}, 1000) is None


def test_dispatch_hint_names_running_interpreter(tmp_path, capsys):
    from types import SimpleNamespace
    from foundry_runtime import _foundry_dispatch
    log = tmp_path / 'dispatch.log'
    log.write_text('Claude session: 00000000-0000-4000-8000-ae1908d5eef6')
    _foundry_dispatch.report(log, project=SimpleNamespace(path=tmp_path / 'project.json'), tag='TEST', launcher='claude')
    text = capsys.readouterr().out
    assert '<recorded-python>' not in text
    assert 'python.exe' in text


@pytest.mark.skipif(os.name != 'nt', reason='Windows owned-process regression')
@pytest.mark.parametrize('broken_stderr', [False, True])
@pytest.mark.parametrize('silent', [False, True])
def test_dispatch_surfaces_stall_without_changing_success(tmp_path, monkeypatch, capsys, broken_stderr, silent):
    import sys
    from types import SimpleNamespace
    from foundry_runtime import _dispatch_claude as transport, _member_activity
    from foundry_runtime._executor_lifecycle import OwnedProcess, SpawnFailure
    monkeypatch.setattr(_member_activity, 'MemberActivity', lambda: MemberActivity(.1))
    monkeypatch.setattr(transport, 'resolve_executable', lambda _: sys.executable)
    def argv(executable, session, **kwargs):
        rows = [{'type': 'system', 'subtype': 'init', 'session_id': session},
                *([{'type': 'assistant', 'message': {'content': [
                    {'type': 'tool_use', 'name': 'Agent', 'id': 'member'}]}}] if silent else
                  [heartbeat(), heartbeat(), heartbeat()]),
                {'type': 'result', 'subtype': 'success', 'is_error': False,
                 'session_id': session, 'result': 'done'}]
        return [executable, '-c', 'import time; rows=' + repr(rows) +
                '; import json\nfor row in rows:\n print(json.dumps(row),flush=True); time.sleep(.25)']
    monkeypatch.setattr(transport, 'argv', argv)
    receipts = []
    owner = SimpleNamespace(OwnedProcess=OwnedProcess, SpawnFailure=SpawnFailure,
                            _record_cleanup=lambda lease, receipt, path: receipts.append(receipt))
    lease = SimpleNamespace(attempt_id='stall-fixture', require_reserved=lambda *args: None,
                            bind_process=lambda *args: None)
    log, out = tmp_path / 'dispatch.log', tmp_path / 'output'
    class BrokenPipe:
        def write(self, text):
            raise BrokenPipeError('caller disconnected')
        def flush(self):
            raise BrokenPipeError('caller disconnected')
    with monkeypatch.context() as patch:
        if broken_stderr:
            patch.setattr(sys, 'stderr', BrokenPipe())
        assert transport.run(owner, 'prompt', tmp_path, log, out, 'FIXTURE',
                             lease=lease, on_started=lambda sid: None) == 0
    assert 'member_no_transcript_progress' in log.read_text()
    if not broken_stderr:
        assert 'member_no_transcript_progress' in capsys.readouterr().err
    assert out.read_text() == 'done'
    assert receipts[0].cleanup_verified
