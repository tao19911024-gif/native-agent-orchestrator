"""Coding launch evidence follows the real dispatch owner, never a second journal."""
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from test_installed_runtime import external, offline
from foundry_runtime import _code_index as index, _foundry_dispatch as dispatch
from foundry_runtime import _dispatch_claude as claude, _launch_context, _native_session
from foundry_runtime import _project_status


@pytest.mark.parametrize('operation', ['status', 'ls-files'])
def test_git_verification_timeout_degrades_to_direct_reads(tmp_path, monkeypatch, operation):
    folder = tmp_path / '.codegraph'; folder.mkdir()
    (tmp_path / 'source.py').write_bytes(b'pass\n')
    with sqlite3.connect(folder / 'codegraph.db') as db:
        db.execute('create table files(path text, content_hash text)')
        db.execute('insert into files values (?,?)', ('source.py', hashlib.sha256(b'pass\n').hexdigest()))
    monkeypatch.setenv('FOUNDRY_CODEGRAPH_ARGV', '["node", "codegraph"]')
    monkeypatch.setenv('FOUNDRY_CODEGRAPH_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setattr(index, '_run', lambda argv, *_: '1.6.2' if '--version' in argv else '')
    calls = []
    def git(argv, **kw):
        calls.append((argv, kw))
        assert 0 < kw.get('timeout', 0) <= 30, 'Git verification is unbounded'
        if operation in argv:
            raise subprocess.TimeoutExpired(argv, kw['timeout'])
        return subprocess.CompletedProcess(argv, 0, b'', b'')
    monkeypatch.setattr(index.subprocess, 'run', git)
    launch = _launch_context.capture(tmp_path)
    assert launch['server'] is None
    assert launch['index_observation']['status'] == 'unavailable'
    assert operation in launch['index_observation']['reason']
    assert launch['tool_discipline']['index_available'] is False
    _launch_context.codex_settings(launch=launch)
    _launch_context.claude_args(launch=launch)
    assert len(calls) == (1 if operation == 'status' else 2)


def test_content_verification_uses_shared_elapsed_deadline(tmp_path, monkeypatch):
    folder = tmp_path / '.codegraph'; folder.mkdir()
    with sqlite3.connect(folder / 'codegraph.db') as db:
        db.execute('create table files(path text, content_hash text)')
    ticks = iter([0, 31])
    monkeypatch.setattr(index.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(subprocess.TimeoutExpired):
        index.verify(tmp_path)


@pytest.mark.parametrize('launcher', ['exec', 'claude'])
@pytest.mark.parametrize('available', [False, True])
@pytest.mark.parametrize('change_model', [False, True])
def test_dispatch_resume_reuses_recorded_launch_policy(external, monkeypatch, launcher, available, change_model):
    tree, scope, order, _ = external
    captures, consumed = [], []
    def prepare(workspace, *, observation):
        captures.append(workspace)
        observation.update(status='ready' if available else 'unavailable',
                           reason=None if available else '索引被占用', fallback='scoped-direct-reads')
        return {'command': 'fixture', 'args': [], 'env': {}} if available else None
    monkeypatch.setattr(index, 'prepare', prepare)
    if launcher == 'claude':
        monkeypatch.setattr(claude, 'resolve_executable', lambda _: sys.executable)
        def argv(executable, session, **kw):
            consumed.append(kw['launch_context'])
            # Exercise the native builder while replacing only the provider process.
            claude_argv(executable, session, **kw)
            rows = [{'type': 'system', 'subtype': 'init', 'session_id': session},
                    {'type': 'result', 'subtype': 'success', 'is_error': False,
                     'session_id': session, 'result': 'fixture completed'}]
            script = 'import sys;sys.stdin.read();print(' + repr('\n'.join(map(json.dumps, rows))) + ')'
            return [sys.executable, '-c', script]
        claude_argv = claude.argv
        monkeypatch.setattr(claude, 'argv', argv)
    else:
        original = dispatch.coding_argv
        def argv(*args, **kw):
            consumed.append(kw['launch_context'])
            argv = original(*args, **kw)
            header = 'OpenAI Codex vfixture\n--------\nsession id: original-session\n--------'
            argv[-1] = 'print(' + repr(header) + '); ' + argv[-1]
            return argv
        monkeypatch.setattr(dispatch, 'coding_argv', argv)
    from foundry_runtime._model_catalogue import model
    initial = model('sonnet' if launcher == 'claude' else 'sol')
    resumed = model('opus' if launcher == 'claude' else 'astra')
    compute = {'model': initial, 'effort': 'high'} if change_model else {}
    first = dispatch.dispatch_order(order, project=scope, launcher=launcher, **compute)
    assert first[0] == 0
    receipt = json.loads(first[1].with_suffix('.package.json').read_bytes())
    assert receipt['launch_context'] == consumed[0]
    assert len(captures) == 1
    saved = first[1].with_suffix('.package.json').read_bytes()
    assert dispatch.dispatch_order(order, project=scope, launcher=launcher, **compute) == first
    observed = _project_status.status(scope.path, 'TASK1')['runs']
    assert observed[0]['index_observation'] == consumed[0]['index_observation']
    assert len(captures) == 1
    assert first[1].with_suffix('.package.json').read_bytes() == saved
    if launcher == 'claude':
        session = json.loads(first[1].with_suffix('.terminal.json').read_bytes())['session_id']
    else:
        session = 'original-session'
        monkeypatch.setattr(_native_session, 'read_session_worktree', lambda _: tree)
    monkeypatch.setattr(_launch_context, 'capture', lambda *a, **k: pytest.fail('resume must retain the recorded launch'))
    result = dispatch.resume_order(session, tree, 'TASK1', '继续中文任务', project=scope, launcher=launcher,
        **({'model': resumed, 'effort': 'high'} if change_model else {}))
    assert result[0] == 0 and len(captures) == 1
    expected = json.loads(json.dumps(consumed[0]))
    if change_model:
        expected['tool_discipline'].update(recorded_coding_model=resumed,
                                         recorded_coding_models=sorted([initial, resumed]))
    assert consumed[1] == expected
    assert json.loads(result[1].with_suffix('.package.json').read_bytes())['launch_context'] == consumed[1]
    assert dispatch.resume_order(session, tree, 'TASK1', '继续中文任务', project=scope, launcher=launcher,
        **({'model': resumed, 'effort': 'high'} if change_model else {})) == result
    assert len(captures) == 1


def test_historical_package_reports_index_unrecorded(external, monkeypatch):
    _, scope, order, _ = external
    result = dispatch.dispatch_order(order, project=scope)
    path = result[1].with_suffix('.package.json')
    row = json.loads(path.read_bytes())
    row.pop('launch_context', None)
    path.write_text(json.dumps(row))
    monkeypatch.setattr(index, 'prepare', lambda *a, **k: pytest.fail('status must be read-only'))
    assert _project_status.status(scope.path, 'TASK1')['runs'][0]['index_observation']['status'] == 'unrecorded'


def test_app_server_dispatch_does_not_prepare_unused_exec_args(external, monkeypatch):
    tree, scope, order, _ = external
    calls = []
    monkeypatch.setattr(index, 'prepare', lambda *a, **k: calls.append('prepare'))
    monkeypatch.setattr(dispatch, 'coding_argv', lambda *a, **k: pytest.fail('unused exec argv built'))
    from foundry_runtime import app_server_transport as transport
    run_executor = transport.run_executor
    def no_child(argv, **kw):
        assert 'features.hooks=true' in argv
        assert calls == ['prepare']
        raise OSError('fixture no native child')
    def before_native(*args, **kw):
        assert kw['launch_context']['server'] is None
        assert calls == ['prepare']
        return run_executor(*args, process_factory=no_child, **kw)
    monkeypatch.setattr(transport, 'run_executor', before_native)
    with pytest.raises(OSError, match='fixture no native child'):
        dispatch.dispatch_order(order, project=scope, launcher='app-server')
    assert calls == ['prepare']
    assert not (tree / '.pdcl-executor.lock').exists()
