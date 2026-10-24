"""Real disposable worker processes inherit only their goal's retained scratch."""
import json
import os
from pathlib import Path
import sys
import subprocess

import pytest

from foundry_runtime import _foundry_dispatch as dispatch, foundry_goal as goal
from foundry_runtime import _dispatch_claude, app_server_transport as transport, _code_index
from test_installed_runtime import external, offline, Analyst
from test_manager_input_wait import manager_io


ENV_KEYS = ('TMPDIR', 'TMP', 'TEMP', 'PYTEST_DEBUG_TEMPROOT')
SID = '00000000-0000-0000-0000-000000000001'


@pytest.mark.parametrize('launcher', ['exec', 'app-server', 'claude'])
@pytest.mark.parametrize('enabled', [True, False, 'resume'])
def test_worker_tempfile_and_pytest_use_declared_goal_environment(external, tmp_path, monkeypatch, launcher, enabled):
    tree, scope, order, _ = external
    folder, probe = tmp_path/'goal', tmp_path/'worker.json'
    ambient = tmp_path/'ambient'; ambient.mkdir()
    for key in ENV_KEYS:
        monkeypatch.setenv(key, str(ambient))
    before = {key: os.environ.get(key) for key in ENV_KEYS}
    monkeypatch.setattr(_code_index, 'prepare', lambda *a, **k: None)
    pytest_case = tmp_path/'test_worker_temp.py'
    pytest_case.write_text('from pathlib import Path\ndef test_temp(tmp_path):\n    Path(' +
        repr(str(probe.with_suffix('.pytest'))) + ').write_text(str(tmp_path))\n')
    common = ('import sys,json,os,tempfile,subprocess\nfrom pathlib import Path\n'
        'observed={"tempfile":tempfile.mkdtemp(),"environment":{k:os.environ.get(k) for k in ' + repr(ENV_KEYS) + '}}\n'
        'subprocess.run([sys.executable,"-m","pytest","-q",' + repr(str(pytest_case)) + '],capture_output=True,check=True)\n'
        'observed["pytest"]=Path(' + repr(str(probe.with_suffix('.pytest'))) + ').read_text()\n'
        'Path(' + repr(str(probe)) + ').write_text(json.dumps(observed))\n'
        'Path("candidate.txt").write_text("done")\n'
        'subprocess.run(["git","add","candidate.txt"],capture_output=True,check=True)\n'
        'subprocess.run(["git","commit","--allow-empty","-qm","candidate"],capture_output=True,check=True)\n')
    worker = tmp_path/'worker.py'
    if launcher == 'exec':
        worker.write_text('print("OpenAI Codex vfixture\\n--------\\nsession id: ' + SID + '\\n--------",flush=True)\n' +
            common + 'Path(sys.argv[1]).write_text(sys.stdin.read())')
        monkeypatch.setattr(dispatch, 'coding_argv', lambda *a, **k:
            [sys.executable, '-B', str(worker), str(a[a.index('-o') + 1])])
    elif launcher == 'claude':
        worker.write_text('import sys,json\nsys.stdin.read()\n' + common +
            'print(json.dumps({"type":"system","subtype":"init","session_id":sys.argv[1]}))\n'
            'print(json.dumps({"type":"result","subtype":"success","is_error":False,"session_id":sys.argv[1],"result":"done"}))\n')
        monkeypatch.setattr(_dispatch_claude, 'resolve_executable', lambda _: Path(sys.executable))
        monkeypatch.setattr(_dispatch_claude, 'argv', lambda executable, session, **k:
            [sys.executable, '-B', str(worker), session])
    else:
        worker.write_text('import sys,json\nfor line in sys.stdin:\n'
            ' r=json.loads(line);m=r.get("method");i=r.get("id")\n'
            ' if i is None:continue\n'
            ' result={"thread":{"id":' + repr(SID) + '}} if m in ("thread/start","thread/resume") else {"turn":{"id":"turn","status":"inProgress"}} if m=="turn/start" else {}\n'
            ' print(json.dumps({"id":i,"result":result}),flush=True)\n'
            ' if m=="turn/start":\n'
            '  exec(' + repr(common) + ')\n'
            '  print(json.dumps({"method":"turn/completed","params":{"threadId":' + repr(SID) + ',"turn":{"id":"turn","status":"completed","items":[],"error":None}}}),flush=True)\n')
        run_executor = transport.run_executor
        def executor(*a, **k):
            def spawn(argv, **opts):
                return transport.OwnedProcess.spawn([sys.executable, '-B', str(worker)], **opts)
            return run_executor(*a, process_factory=spawn, **k)
        monkeypatch.setattr(transport, 'run_executor', executor)
    class Review(Analyst):
        def __init__(self, *a, **k):
            assert {key: os.environ.get(key) for key in ENV_KEYS} == before
            assert 'worker_environment' not in (k.get('launch_context') or {})
            super().__init__(*a, **k)
        def __call__(self, request):
            if enabled == 'resume':
                raise RuntimeError('fixture retains goal for worker continuation')
            return super().__call__(request)
    monkeypatch.setattr(goal, 'NativeAnalyst', Review)
    def run():
        return goal.run_goal(order, folder, Path(sys.executable), coding_launcher=launcher,
            project_record=scope.record(), disposable_scratch=bool(enabled))
    if enabled == 'resume':
        with pytest.raises(RuntimeError, match='retains goal'):
            run()
        checkpoint = json.loads((folder/'coding-checkpoint.json').read_bytes())
        from foundry_runtime import _native_session
        monkeypatch.setattr(_native_session, 'read_session_worktree', lambda _: tree)
        result = dispatch.resume_order(checkpoint['coding_session'], tree, 'TASK1', 'Continue retained work',
            launcher=launcher, project=scope)
        assert result[0] == 0
    else:
        assert run()['status'] == 'candidate_verified'
    observed = json.loads(probe.read_bytes())
    manifest = json.loads((folder/'manifest.json').read_bytes())
    package = json.loads(next(Path(scope.project['evidence_root']).glob('TASK1/dispatch*.package.json')).read_bytes())
    if enabled:
        root = folder/'disposable'
        expected = {key: str(root/('pytest' if key == 'PYTEST_DEBUG_TEMPROOT' else 'tmp')) for key in ENV_KEYS}
        assert observed['environment'] == expected
        assert root/'tmp' in Path(observed['tempfile']).parents
        assert root/'pytest' in Path(observed['pytest']).parents
        assert manifest['launch_context']['worker_environment'] == expected
        assert package['launch_context']['worker_environment'] == expected
        checkpoint = json.loads((folder/'coding-checkpoint.json').read_bytes())
        assert checkpoint['options']['launch_context']['worker_environment'] == expected
        assert root.exists() is (enabled == 'resume')
    else:
        assert observed['environment'] == before
        assert ambient in Path(observed['tempfile']).parents
        assert 'disposable_scratch' not in manifest
        assert 'worker_environment' not in package['launch_context']
    assert {key: os.environ.get(key) for key in ENV_KEYS} == before


def test_parallel_worker_environment_does_not_change_controller(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from foundry_runtime import _goal_scratch, _launch_context
    original = dict(os.environ)
    launches = []
    for name in ('first', 'second'):
        folder = tmp_path/name; folder.mkdir()
        launches.append({'worker_environment': _goal_scratch.register(folder, {'run_id': name})})
    def probe(launch):
        child = _launch_context.worker_environment(original, launch)
        result = subprocess.run([sys.executable, '-B', '-c',
            'import tempfile;print(tempfile.mkdtemp())'], env=child, capture_output=True, text=True, check=True)
        return Path(result.stdout.strip())
    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(pool.map(probe, launches))
    for path, launch in zip(paths, launches):
        assert Path(launch['worker_environment']['TEMP']) in path.parents
    assert paths[0] != paths[1] and dict(os.environ) == original


@pytest.mark.parametrize('mode', ['auto', 'manual'])
def test_each_amendment_gets_its_own_scratch(external, tmp_path, monkeypatch, mode):
    from foundry_runtime import _native_session
    tree, scope, order, _ = external
    ambient = tmp_path/'ambient'; ambient.mkdir()
    for key in ENV_KEYS:
        monkeypatch.setenv(key, str(ambient))
    monkeypatch.setattr(_native_session, 'read_session_worktree', lambda _: tree)
    monkeypatch.setattr(_code_index, 'prepare', lambda *a, **k: None)
    calls, candidates = [], []
    observed = tmp_path/'workers.jsonl'
    def argv(*args, **kwargs):
        calls.append(kwargs)
        out = str(args[args.index('-o') + 1])
        code = ('import sys,tempfile,json,subprocess;from pathlib import Path;'
            'print("OpenAI Codex vfixture\\n--------\\nsession id: ' + SID + '\\n--------",flush=True);'
            'Path(' + repr(out) + ').write_text(sys.stdin.read());'
            'f=Path(' + repr(str(observed)) + ').open("a");f.write(json.dumps(tempfile.mkdtemp())+"\\n");f.close();'
            'Path("candidate.txt").write_text(' + repr(str(len(calls))) + ');'
            'subprocess.run(["git","add","candidate.txt"],check=True);'
            'subprocess.run(["git","commit","-qm","candidate"],check=True);'
            'print("session id: ' + SID + '")')
        return [sys.executable, '-B', '-c', code]
    class Review(Analyst):
        def __call__(self, request):
            result = super().__call__(request)
            candidate = request['packet']['candidate_commit']
            if candidate not in candidates:
                candidates.append(candidate)
            if mode == 'auto' and candidate == candidates[0]:
                result.update(verdict='BLOCK', findings=[{'severity': 'P1', 'file': 'candidate.txt',
                    'line': '1', 'summary': 'Fixture correction required'}])
            return result
    monkeypatch.setattr(dispatch, 'coding_argv', argv)
    monkeypatch.setattr(goal, 'NativeAnalyst', Review)
    folder = tmp_path/mode
    options = dict(coding_launcher='exec', project_record=scope.record())
    if mode == 'auto':
        result = goal.run_goal(order, folder, Path(sys.executable), **options, disposable_scratch=True,
            auto_amend_model='fixture-correction', auto_amend_effort='high')
        rounds = [folder/'round-0', folder/'round-1']
    else:
        result = goal.run_goal(order, folder, Path(sys.executable), **options, disposable_scratch=True)
        assert result['status'] == 'candidate_verified' and not (folder/'disposable').exists()
        prior = {p: p.read_bytes() for p in (folder/'manifest.json', folder/'outcome.json')}
        amended = tmp_path/'amended'
        result = goal.run_goal(order, amended, Path(sys.executable), **options,
            amend_from=folder/'outcome.json', coding_session=SID, amend_prompt='Apply the next requested change')
        assert all(p.read_bytes() == content for p, content in prior.items())
        rounds = [folder, amended]
    assert result['status'] == 'candidate_verified' and len(calls) == 2
    paths = [Path(json.loads(line)) for line in observed.read_text().splitlines()]
    for round_folder, path in zip(rounds, paths):
        root = round_folder/'disposable'
        manifest = json.loads((root.parent/'manifest.json').read_bytes())
        assert root/'tmp' in path.parents
        assert manifest['launch_context']['worker_environment']['TEMP'] == str(root/'tmp')
    assert (rounds[0]/'disposable').exists() is (mode == 'auto')
    assert not (rounds[1]/'disposable').exists()


def test_artifact_answer_gets_new_scratch_after_parent_reclaimed(external, manager_io, tmp_path, monkeypatch):
    _, scope, order, _ = external
    _, decisions = manager_io
    ambient = tmp_path/'ambient'; ambient.mkdir()
    for key in ENV_KEYS:
        monkeypatch.setenv(key, str(ambient))
    monkeypatch.setattr(goal, '_session_from_log', lambda _: SID)
    observed = tmp_path/'artifact-workers.jsonl'
    launches = []
    def argv(*args, **kwargs):
        launches.append(1)
        out = str(args[args.index('-o') + 1])
        code = ('import sys,tempfile,json,subprocess;from pathlib import Path;'
            'print("OpenAI Codex vfixture\\n--------\\nsession id: ' + SID + '\\n--------",flush=True);'
            'Path(' + repr(out) + ').write_text(sys.stdin.read());'
            'f=Path(' + repr(str(observed)) + ').open("a");f.write(json.dumps(tempfile.mkdtemp())+"\\n");f.close();'
            'Path("candidate.txt").write_text(' + repr(str(len(launches))) + ');'
            'subprocess.run(["git","add","candidate.txt"],check=True);'
            'subprocess.run(["git","commit","-qm","candidate"],check=True)')
        return [sys.executable, '-B', '-c', code]
    monkeypatch.setattr(dispatch, 'coding_argv', argv)
    pair = [{'action': 'delegate', 'instruction': 'Produce requested artifact', 'artifacts': []},
            {'action': 'deliver_artifacts', 'instruction': 'Deliver artifact', 'artifacts': ['candidate.txt']}]
    decisions.extend(pair)
    folder = tmp_path/'artifacts'
    result = goal.run_goal(order, folder, Path(sys.executable), coding_launcher='exec',
        project_record=scope.record(), disposable_scratch=True,
        manager_state={'backend': 'codex', 'model': None, 'effort': None,
            'native_dir': str(tmp_path/'manager'), 'artifact_delivery': True})
    assert result['status'] == 'artifacts_delivered' and not (folder/'disposable').exists()
    prior = {p: p.read_bytes() for p in (folder/'manifest.json', folder/'outcome.json')}
    decisions.extend(pair)
    result = goal.recover_goal(order, folder, answer='Produce the additional artifact')
    assert result['status'] == 'artifacts_delivered' and len(launches) == 2
    child = Path(result['continuation_evidence'])
    paths = [Path(json.loads(line)) for line in observed.read_text().splitlines()]
    for target, path in zip((folder, child), paths):
        manifest = json.loads((target/'manifest.json').read_bytes())
        assert target/'disposable'/'tmp' in path.parents
        assert manifest['launch_context']['worker_environment']['TEMP'] == str(target/'disposable'/'tmp')
        assert not (target/'disposable').exists()
    assert all(p.read_bytes() == content for p, content in prior.items())
