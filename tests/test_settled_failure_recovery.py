"""The same lifecycle admits explicit answers independently of vendor error names."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_installed_runtime import external, offline
from test_owned_goal_recovery import SERVER, SID
from test_manager_recovery import conversation
from foundry_runtime import foundry_goal as goal, app_server_transport as transport
from foundry_runtime import _foundry_manager as manager, foundry_native as native
from foundry_runtime._recovery_reconciliation import completed_coding
from foundry_runtime._execution_intent import ExecutionIntent

ERRORS = [
    {'httpConnectionFailed': {'httpStatusCode': code}} for code in (401, 403, 408, 429, 500, 502, 503, 504, 529, 599)
] + ['internalServerError', 'contextWindowExceeded', 'badRequest', 'other', None, '__interrupted__']


@pytest.fixture
def failed_goal(external, tmp_path, monkeypatch, request):
    info = getattr(request, 'param', {'httpConnectionFailed': {'httpStatusCode': 503}})
    tree, scope, order, _ = external
    home = tmp_path / 'codex'
    sessions = home / 'sessions/2026/10/09'; sessions.mkdir(parents=True)
    (sessions / f'rollout-fixture-{SID}.jsonl').write_text(json.dumps(
        {'type': 'session_meta', 'payload': {'id': SID, 'cwd': str(tree)}}) + '\n')
    monkeypatch.setenv('CODEX_HOME', str(home))
    calls, original = [], transport.run_executor
    def run(*args, **kwargs):
        calls.append(kwargs.get('resume_thread_id'))
        script = 'HANG=False\n' + SERVER
        if len(calls) == 1:
            error = {'message': 'fixture service/request failure', 'codexErrorInfo': info}
            script = script.replace("'status':'completed','items':[],'error':None",
                "'status':'interrupted','items':[],'error':None" if info == '__interrupted__' else
                "'status':'failed','items':[],'error':" + repr(error))
        def spawn(argv, **opts):
            return transport.OwnedProcess.spawn([sys.executable, '-u', '-c', script], **opts)
        return original(*args, process_factory=spawn, **kwargs)
    monkeypatch.setattr(transport, 'run_executor', run)
    folder = tmp_path / 'goal'
    result = goal.run_goal(order, folder, Path(sys.executable), coding_launcher='app-server',
                           project_record=scope.record())
    assert result['status'] == 'coding_incomplete'
    return tree, scope, order, folder, calls


@pytest.mark.parametrize('failed_goal', ERRORS, indirect=True)
def test_settled_failure_continues_same_session_once_after_explicit_answer(failed_goal):
    tree, scope, order, folder, calls = failed_goal
    checkpoint = json.loads((folder / 'coding-checkpoint.json').read_bytes())
    assert completed_coding(checkpoint)['cleanup_verified']
    before = {p: p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    from foundry_runtime._foundry_goal_delivery import coding_recovery
    report = coding_recovery(folder, json.loads((folder / 'manifest.json').read_bytes()))
    assert report['status'] == 'explicit_continuation_available'
    assert report['automatic_retry'] is False
    preferred = report['manager_recovery']
    assert preferred['preferred'] is True
    assert preferred['argv'][0] == json.loads((folder/'manifest.json').read_bytes())['runtime_provenance']['python']
    assert preferred['argv'][-4:] == ['--recover-goal', str(folder), '--prompt-file', '<prompt-file>']
    assert '$PromptFile' in preferred['powershell']
    assert calls == [None]
    result = goal.recover_goal(order, folder, answer='已确认失败，保留现有工作，继续执行')
    assert result['status'] == 'project_handoff'
    assert calls == [None, SID]
    assert all(p.read_bytes() == content for p, content in before.items())
    assert goal.recover_goal(order, folder, answer='已确认失败，保留现有工作，继续执行') == result
    assert calls == [None, SID]


@pytest.mark.parametrize('state', ['active', 'starting', 'unknown', 'not_started'])
def test_unsettled_journal_never_enters_continuation(failed_goal, monkeypatch, state):
    _, _, order, folder, calls = failed_goal
    original = ExecutionIntent.read_history
    def changed(*args, **kwargs):
        record, seq, digest = original(*args, **kwargs)
        return dict(record, state=state), seq, digest
    monkeypatch.setattr(ExecutionIntent, 'read_history', changed)
    with pytest.raises(goal.GoalBlocked, match='original dispatch is ' + state):
        goal.recover_goal(order, folder, answer='继续')
    assert calls == [None]


def test_cleanup_damage_is_not_a_provider_error(failed_goal):
    _, _, order, folder, calls = failed_goal
    checkpoint = json.loads((folder / 'coding-checkpoint.json').read_bytes())
    cleanup = next(Path(f['path']) for f in reversed(checkpoint['evidence']) if f['path'].endswith('.cleanup.json'))
    row = json.loads(cleanup.read_bytes()); row['cleanup_verified'] = False
    cleanup.write_text(json.dumps(row))
    with pytest.raises(goal.GoalBlocked, match='evidence changed'):
        goal.recover_goal(order, folder, answer='继续')
    assert calls == [None]


def test_legacy_generic_goal_status_can_use_proven_failed_execution(failed_goal):
    _, _, order, folder, calls = failed_goal
    outcome = json.loads((folder / 'outcome.json').read_bytes())
    outcome['status'] = 'execution_unknown'
    (folder / 'outcome.json').write_text(json.dumps(outcome))
    assert goal.recover_goal(order, folder, answer='确认已清理，继续')['status'] == 'project_handoff'
    assert calls == [None, SID]


@pytest.mark.parametrize('conversation', [
    {'type': 'error', 'error': {'type': kind, 'message': 'fixture failure'}}
    for kind in ('api_error', 'authentication_error', 'invalid_request_error', 'future_error')
] + [{'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
       'api_error_status': code, 'result': 'fixture failure'} for code in (500, 502, 503, 504, 599)], indirect=True)
def test_manager_existing_recovery_accepts_new_instruction_for_unfamiliar_failure(conversation, tmp_path):
    state, requests, _ = conversation
    with pytest.raises(native.NativeError):
        manager.turn(state, tmp_path, {'stage': 'assign'})
    before = {p: p.read_bytes() for p in Path(state['native_dir']).glob('call-*/*') if p.is_file()}
    result, _, _, _ = manager.turn(state, tmp_path, {'stage': 'assign', 'operator_answer': '新裁决'})
    assert result['instruction'] == '新裁决'
    assert all(p.read_bytes() == raw for p, raw in before.items())
    count = len(requests)
    manager.turn(state, tmp_path, {'stage': 'assign', 'operator_answer': '新裁决'})
    assert len(requests) == count


def test_a31_failed_checkpoint_resumes_without_rewriting_original_records(external, tmp_path, monkeypatch):
    tree, scope, order, _ = external
    home = tmp_path / 'codex'; sessions = home / 'sessions/2026/10/09'; sessions.mkdir(parents=True)
    (sessions / f'rollout-fixture-{SID}.jsonl').write_text(json.dumps(
        {'type': 'session_meta', 'payload': {'id': SID, 'cwd': str(tree)}}) + '\n')
    monkeypatch.setenv('CODEX_HOME', str(home))
    old = 'D:/Foundry/releases/0.1.0a31/Scripts/python.exe'
    if not Path(old).is_file():
        pytest.skip('optional historical a31 installation was retired')
    script = tmp_path / 'a31-fixture.py'
    failed_server = ('HANG=False\n' + SERVER).replace("'status':'completed','items':[],'error':None",
        "'status':'failed','items':[],'error':{'message':'503 fixture','codexErrorInfo':{'httpConnectionFailed':{'httpStatusCode':503}}}")
    script.write_text('''
import sys
from pathlib import Path
from foundry_runtime import foundry_goal as goal, _foundry_dispatch as dispatch, app_server_transport as transport
from foundry_runtime._project_execution import open_project
dispatch._codex=lambda:sys.executable
original=transport.run_executor
def run(*args, **kwargs):
 def spawn(argv, **opts): return transport.OwnedProcess.spawn([sys.executable,'-u','-c',SERVER],**opts)
 return original(*args,process_factory=spawn,**kwargs)
transport.run_executor=run
goal.run_goal(Path(sys.argv[2]),Path(sys.argv[3]),Path(sys.executable),coding_launcher='app-server',project_record=open_project(sys.argv[1]).record())
'''.replace('SERVER', repr(failed_server)), encoding='utf-8')
    folder = tmp_path / 'old-goal'
    result = subprocess.run([old, '-I', str(script), str(scope.path), str(order), str(folder)],
        capture_output=True, text=True, encoding='utf-8', timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    assert (folder / 'coding-checkpoint.json').exists(), result.stderr
    before = {p: p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    calls, original = [], transport.run_executor
    def resume(*args, **kwargs):
        calls.append(kwargs.get('resume_thread_id'))
        def spawn(argv, **opts):
            return transport.OwnedProcess.spawn([sys.executable, '-u', '-c', 'HANG=False\n' + SERVER], **opts)
        return original(*args, process_factory=spawn, **kwargs)
    monkeypatch.setattr(transport, 'run_executor', resume)
    result = goal.recover_goal(order, folder, answer='503 后显式继续，保留全部工作')
    assert result['status'] == 'project_handoff' and calls == [SID]
    assert all(p.read_bytes() == data for p, data in before.items())


def test_missing_command_provenance_does_not_hide_settled_execution(failed_goal):
    from foundry_runtime._foundry_goal_delivery import coding_recovery
    _, _, _, folder, _ = failed_goal
    manifest = json.loads((folder/'manifest.json').read_bytes())
    manifest.pop('runtime_provenance', None)
    report = coding_recovery(folder, manifest)
    assert report['status'] == 'explicit_continuation_available'
    assert report['manager_recovery']['status'] == 'unavailable'
    assert 'runtime_provenance.python' in report['manager_recovery']['reason']
