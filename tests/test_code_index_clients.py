"""Opt-in real CodeGraph clients on disposable projects, never a live index."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import uuid

import pytest

from foundry_runtime import _code_index


class Client:
    def __init__(self, argv, env, root, name, owned=False):
        self.owned, self.closed = owned, False
        self.log = (root.parent / (name + '.stderr')).open('wb')
        if owned:
            from foundry_runtime._executor_lifecycle import OwnedProcess
            self.process = OwnedProcess.spawn(argv, cwd=root, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self.log, attempt_id=str(uuid.uuid4()))
        else:
            self.process = subprocess.Popen(argv, cwd=root, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self.log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.input = self.process.stdin.buffer if owned else self.process.stdin
        self.output = self.process.stdout.buffer if owned else self.process.stdout
        self.messages = queue.Queue()
        def read():
            for line in self.output:
                self.messages.put(json.loads(line))
            self.messages.put(None)
        self.reader = threading.Thread(target=read, daemon=True); self.reader.start()

    def request(self, method, params, ident):
        self.input.write((json.dumps({'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params})+'\n').encode())
        self.input.flush()
        while True:
            message = self.messages.get(timeout=45)
            if message is None:
                return None
            if message.get('id') == ident:
                return message

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.process.stdin.close()
        if self.owned:
            assert self.process.stop_and_verify(drain_seconds=0.2, kill_seconds=5).cleanup_verified
        else:
            try:
                self.process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                self.process.terminate(); self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        self.process.stdout.close(); self.log.close()


@pytest.mark.skipif(not os.environ.get('FOUNDRY_TEST_CODEGRAPH_ARGV'), reason='explicit local CodeGraph installation required')
@pytest.mark.parametrize('direct,owned', [(True, False), (False, False), (False, True)])
def test_two_clients_share_one_index(external, tmp_path, monkeypatch, direct, owned):
    # external creates a disposable Git project and has no business paths.
    root = external[0]
    (root / 'fixture.py').write_text('def foundry_index_fixture(value):\n    return value + 1\n')
    command = json.loads(os.environ['FOUNDRY_TEST_CODEGRAPH_ARGV'])
    monkeypatch.setenv('FOUNDRY_CODEGRAPH_ARGV', json.dumps(command))
    monkeypatch.setenv('FOUNDRY_CODEGRAPH_STATE_DIR', str(tmp_path / 'state'))
    config = _code_index.prepare(root)
    assert config is not None
    env = {**os.environ, **config['env']}
    if direct:
        env['CODEGRAPH_NO_DAEMON'] = '1'
    argv = [config['command'], *config['args']]
    clients = []
    try:
        first = Client(argv, env, root, 'first', owned); clients.append(first)
        hello = {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'foundry-test', 'version': '1'}}
        assert first.request('initialize', hello, 1)['result']
        params = {'name': 'codegraph_explore', 'arguments': {'query': 'foundry_index_fixture'}}
        response = first.request('tools/call', params, 2)
        assert 'error' not in response and not response['result'].get('isError')
        assert 'foundry_index_fixture' in json.dumps(response)
        if not direct:
            observed = {}
            reused = _code_index.prepare(root, observation=observed)
            assert reused == config
            assert observed['preparation'] == 'verified_reuse'
        second = Client(argv, env, root, 'second', owned); clients.append(second)
        response = second.request('initialize', hello, 1)
        if direct:
            assert response is None
            second.process.wait(timeout=5)
            assert 'writer lock held' in (root.parent/'second.stderr').read_text(encoding='utf-8')
        else:
            assert response['result']
            response = second.request('tools/call', params, 2)
            assert 'error' not in response and not response['result'].get('isError')
            assert 'foundry_index_fixture' in json.dumps(response)
            writer = json.loads((root/'.codegraph/writer.pid').read_bytes())
            assert writer['mode'] == 'daemon'
            if owned:
                # Closing the first native Job kills its daemon. The other
                # client's upstream proxy must reconnect/fall back, not strand it.
                first.close()
                response = second.request('tools/call', params, 3)
                assert 'error' not in response and not response['result'].get('isError')
                assert 'foundry_index_fixture' in json.dumps(response)
    finally:
        for client in reversed(clients):
            client.close()
        # Reuse CodeGraph's identity-checked stop for this exact disposable root.
        # Never kill by name or enumerate unrelated daemon registrations.
        module = Path(command[-1]).parent.parent / 'mcp/daemon-registry.js'
        script = 'require(process.argv[1]).stopDaemonAt(process.argv[2],{preserveUnverified:true}).then(r=>console.log(JSON.stringify(r)))'
        result = subprocess.run([command[0], *command[1:-1], '-e', script, str(module), str(root.resolve())],
            cwd=root, env=env, capture_output=True, text=True, encoding='utf-8', timeout=20)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)['outcome'] in ('term', 'kill', 'not-running', 'no-daemon')


from test_installed_runtime import external, offline


@pytest.mark.skipif(not os.environ.get('FOUNDRY_TEST_CODEGRAPH_ARGV'), reason='explicit local CodeGraph installation required')
def test_parallel_review_panel_prepares_once_and_both_query(external, tmp_path, monkeypatch):
    from foundry_runtime import _foundry_review_group as group
    import sys
    root = external[0]
    (root/'fixture.py').write_text('def foundry_review_fixture(value):\n    return value + 1\n')
    command = json.loads(os.environ['FOUNDRY_TEST_CODEGRAPH_ARGV'])
    monkeypatch.setenv('FOUNDRY_CODEGRAPH_ARGV', json.dumps(command))
    monkeypatch.setenv('FOUNDRY_CODEGRAPH_STATE_DIR', str(tmp_path/'state'))
    original, prepared, clients = _code_index.prepare, [], []
    def prepare(tree, **kw):
        value = original(tree); prepared.append(value); return value
    monkeypatch.setattr(_code_index, 'prepare', prepare)
    class Reviewer:
        def __init__(self, executable, folder, **options):
            self.config = options['launch_context']['server']
        def __call__(self, request):
            config = self.config
            client = Client([config['command'], *config['args']], {**os.environ, **config['env']}, root, request['call_id'], True)
            clients.append(client)
            hello = {'protocolVersion':'2024-11-05', 'capabilities':{}, 'clientInfo':{'name':'review-test','version':'1'}}
            assert client.request('initialize', hello, 1)['result']
            response = client.request('tools/call', {'name':'codegraph_explore', 'arguments':{'query':'foundry_review_fixture'}}, 2)
            assert 'error' not in response and not response['result'].get('isError')
            assert 'foundry_review_fixture' in json.dumps(response)
            return response
    requests = {f'review-{i}':{'call_id':str(i)} for i in range(2)}
    panel = group.Panel([{'executable':sys.executable}]*2, requests, Reviewer, tmp_path/'reviews', root)
    try:
        outcomes = group.prewarm(list(requests), requests, panel)
        assert all(error is None for value,error in outcomes.values()), outcomes
        assert len(prepared) == 1 and len(clients) == 2
    finally:
        for client in reversed(clients):
            client.close()
        if prepared and prepared[0]:
            module = Path(command[-1]).parent.parent/'mcp/daemon-registry.js'
            script = 'require(process.argv[1]).stopDaemonAt(process.argv[2],{preserveUnverified:true}).then(r=>console.log(JSON.stringify(r)))'
            result = subprocess.run([command[0], *command[1:-1], '-e', script, str(module), str(root.resolve())],
                cwd=root, env={**os.environ, **prepared[0]['env']}, capture_output=True, encoding='utf-8', timeout=20)
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout)['outcome'] in ('term','kill','not-running','no-daemon')
