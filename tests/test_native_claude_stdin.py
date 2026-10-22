"""Opt-in installed CLI probe against a local Messages fixture, with isolated homes."""
import json
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import sys

import pytest

from foundry_runtime._executor_lifecycle import OwnedProcess
from foundry_runtime._launch_context import claude_args, settings


@pytest.mark.skipif(not os.environ.get('FOUNDRY_TEST_CLAUDE'), reason='explicit native CLI required')
@pytest.mark.parametrize('tool', ['Bash', 'PowerShell'])
def test_native_bash_hook_receives_eof(tmp_path, tool):
    requests = []
    command = 'IFS= read -r input; printf "HOOK_EOF_OK示例应用丁"'
    if tool == 'PowerShell':
        probe = ('import ctypes; k=ctypes.WinDLL("kernel32",use_last_error=True); '
                 'k.GetStdHandle.restype=ctypes.c_void_p; h=k.GetStdHandle(-10); '
                 'k.GetFileType.argtypes=[ctypes.c_void_p]; '
                 'k.PeekNamedPipe.argtypes=[ctypes.c_void_p]*6; '
                 't=k.GetFileType(h); ok=k.PeekNamedPipe(h,None,0,None,None,None); '
                 'print("STDIN_PROBE",t,ok,ctypes.get_last_error()); '
                 'import sys; print("EOF",repr(sys.stdin.buffer.read(1)))')
        command = "& '" + sys.executable + "' -c '" + probe + "'"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            if 'count_tokens' in self.path:
                payload = json.dumps({'input_tokens': 100}).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(payload)
                return
            done = any(block.get('type') == 'tool_result'
                       for msg in body.get('messages', [])
                       for block in msg.get('content', []) if isinstance(block, dict))
            content = ({'type': 'text', 'text': 'fixture done'} if done else
                       {'type': 'tool_use', 'id': 'toolu_fixture', 'name': tool,
                        'input': {'command': command, 'timeout': 10000}})
            reason = 'end_turn' if done else 'tool_use'
            message = {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant',
                       'model': body.get('model'), 'content': [content],
                       'stop_reason': reason, 'stop_sequence': None,
                       'usage': {'input_tokens': 100, 'output_tokens': 20}}
            self.send_response(200)
            if not body.get('stream'):
                self.send_header('Content-Type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps(message).encode()); return
            self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
            start = {**content, **({'text': ''} if done else {'input': {}})}
            delta = ({'type': 'text_delta', 'text': content['text']} if done else
                     {'type': 'input_json_delta', 'partial_json': json.dumps(content['input'])})
            events = [
                {'type': 'message_start', 'message': {**message, 'content': [], 'stop_reason': None}},
                {'type': 'content_block_start', 'index': 0, 'content_block': start},
                {'type': 'content_block_delta', 'index': 0, 'delta': delta},
                {'type': 'content_block_stop', 'index': 0},
                {'type': 'message_delta', 'delta': {'stop_reason': reason, 'stop_sequence': None},
                 'usage': {'output_tokens': 20}}, {'type': 'message_stop'}]
            for event in events:
                self.wfile.write(('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n').encode())
            self.wfile.flush()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    env = dict(os.environ)
    for key in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'CLAUDE_CONFIG_DIR',
                'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'TEMP', 'TMP'):
        folder = tmp_path / key; folder.mkdir()
        env[key] = str(folder)
    env.update(ANTHROPIC_BASE_URL=f'http://127.0.0.1:{server.server_port}',
               ANTHROPIC_API_KEY='local-fixture-not-a-secret',
               CLAUDE_CODE_USE_POWERSHELL_TOOL='1',
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
               CLAUDE_CODE_GIT_BASH_PATH='C:/Program Files/Git/bin/bash.exe')
    args = [os.environ['FOUNDRY_TEST_CLAUDE'], '--print', '--verbose',
            '--output-format', 'stream-json', '--no-session-persistence',
            '--permission-mode', 'bypassPermissions', '--tools', tool,
            '--debug-file', str(tmp_path / 'debug.log'),
            '--setting-sources', '', '--strict-mcp-config']
    args += claude_args(launch={'settings': settings(), 'server': None,
                               'tool_discipline': {'version': 1, 'index_available': False}})
    proc = None
    try:
        with (tmp_path / 'prompt').open('w+b') as source, (tmp_path / 'stdout').open('w+b') as out, (tmp_path / 'stderr').open('w+b') as err:
            source.write(b'Execute the fixture Bash command.'); source.seek(0)
            proc = OwnedProcess.spawn(args, cwd=tmp_path, stdin=source, stdout=out,
                                      stderr=err, env=env, attempt_id='native-hook-fixture')
            deadline = time.monotonic() + 40
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(.1)
            assert proc.poll() == 0, (tmp_path / 'stderr').read_text(errors='replace')
        results = [block for body in requests for msg in body.get('messages', [])
                   for block in msg.get('content', [])
                   if isinstance(block, dict) and block.get('type') == 'tool_result']
        assert results
        if tool == 'PowerShell':
            # Keep the native measurement, including open-pipe vs EOF distinction.
            (tmp_path / 'stdin-measurement.json').write_text(json.dumps(results))
            assert 'STDIN_PROBE 2 0 1' in json.dumps(results)
            assert "EOF b''" in json.dumps(results)
            return
        assert 'HOOK_EOF_OK示例应用丁' in json.dumps(results, ensure_ascii=False)
        debug = (tmp_path / 'debug.log').read_text(errors='replace')
        assert 'foundry_runtime._tool_discipline' in debug
        assert 'modified tool input keys: [command, timeout]' in debug
        assert 'exec </dev/null' in debug
    finally:
        if proc:
            assert proc.stop_and_verify(drain_seconds=0, kill_seconds=3).cleanup_verified
        server.shutdown(); server.server_close(); thread.join(2)
