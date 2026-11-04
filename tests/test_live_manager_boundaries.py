"""Live readiness decisions terminate paid coordination at the native boundary."""
import json
from pathlib import Path
import sys

import pytest

from test_installed_runtime import external, offline
from test_manager_input_wait import manager_io, wait
from foundry_runtime import foundry_goal as goal, _dispatch_claude as claude
from foundry_runtime._followup import status


@pytest.mark.parametrize('action', ['await_input', 'review', 'deliver_artifacts'])
def test_live_readiness_ends_worker_without_another_manager_call(external, manager_io, tmp_path, monkeypatch, action):
    tree, scope, order, _ = external
    requests, decisions = manager_io
    decision = {'action': action, 'instruction': 'Commander ruling required' if action == 'await_input' else 'Use the completed result', 'artifacts': []}
    if action == 'deliver_artifacts':
        decision['artifacts'] = ['candidate.txt']
    decisions.extend([{'action': 'delegate', 'instruction': 'Complete the assignment', 'artifacts': []}, decision,
                      *[{**wait('Unexpected repeated coordination'), 'artifacts': []} for _ in range(4)]])
    calls = tmp_path / 'worker-inputs.jsonl'
    monkeypatch.setattr(claude, 'resolve_executable', lambda _: Path(sys.executable))
    monkeypatch.setattr(goal, '_session_from_log', lambda path:
        next(line.removeprefix('Claude session: ') for line in Path(path).read_text().splitlines()
             if line.startswith('Claude session: ')))

    def argv(executable, session, **kwargs):
        script = '''
import sys,json,subprocess
from pathlib import Path
def emit(row):
 row['session_id']=SESSION
 print(json.dumps(row),flush=True)
emit({'type':'system','subtype':'init'})
Path('candidate.txt').write_text('Completed assigned work')
subprocess.run(['git','add','candidate.txt'],check=True)
subprocess.run(['git','commit','-qm','complete assignment'],check=True)
for index,line in enumerate(sys.stdin):
 with Path(CALLS).open('a') as out: out.write(line)
 emit(json.loads(line))
 emit({'type':'assistant','uuid':str(index),'message':{'content':[{'type':'text','text':'FOUNDRY_DELIVERABLE_DONE Assigned work complete; commander ruling required.'}]}})
 emit({'type':'result','subtype':'success','is_error':False,'result':'Assigned work complete'})
 if index==2: break
'''.replace('SESSION', repr(session)).replace('CALLS', repr(str(calls)))
        return [executable, '-B', '-c', script]

    monkeypatch.setattr(claude, 'argv', argv)
    reviews = []
    def finish(checkpoint, folder):
        reviews.append(checkpoint)
        return {'status': 'candidate_verified'}
    monkeypatch.setattr(goal, '_finish_review', finish)
    folder = tmp_path / 'goal'
    result = goal.run_goal(order, folder, Path(sys.executable), coding_launcher='claude',
        project_record=scope.record(), manager_state={'backend': 'claude', 'model': None, 'effort': None,
            'native_dir': str(tmp_path / 'manager'), 'live_coordination': 1, 'claude_live_events': 1,
            'artifact_delivery': True})
    assert len(calls.read_text().splitlines()) == 1
    assert len(requests) == 2  # Initial assignment plus exactly one live decision.
    expected = {'await_input': 'awaiting_input', 'review': 'candidate_verified', 'deliver_artifacts': 'artifacts_delivered'}
    assert result['status'] == expected[action]
    saved = json.loads((folder / 'manifest.json').read_bytes())
    checkpoint = json.loads(goal.goal_usage._reference(saved['coding_checkpoint']))
    assert checkpoint['returncode'] == 0 and checkpoint['coding_session']
    if action == 'await_input':
        assert saved['manager_phase'] == 'awaiting_input'
        assert result['input_request']['question'] == decision['instruction']
        assert json.loads((folder / 'outcome.json').read_bytes()) == result
        assert status(folder)['input_request']['question'] == decision['instruction']
        assert goal.recover_goal(order, folder) == result
        assert len(requests) == 2
    assert bool(reviews) == (action == 'review')


@pytest.mark.parametrize('returncode', [0, 1])
def test_claude_completed_dispatch_restores_manager_feedback(external, manager_io, tmp_path, monkeypatch, returncode):
    tree, scope, order, _ = external
    requests, decisions = manager_io
    decisions.extend([{'action': 'delegate', 'instruction': 'Complete the assignment'}, wait('Inspect recovered result')])
    monkeypatch.setattr(claude, 'resolve_executable', lambda _: Path(sys.executable))
    def argv(executable, session, **kwargs):
        rows = [{'type': 'system', 'subtype': 'init', 'session_id': session},
                {'type': 'result', 'subtype': 'error_during_execution' if returncode else 'success',
                 'is_error': bool(returncode), 'session_id': session, 'result': 'Complete' if not returncode else 'Worker failed'}]
        return [executable, '-B', '-c', 'import sys;sys.stdin.read();print(' + repr('\n'.join(map(json.dumps, rows))) + ');sys.exit(' + str(returncode) + ')']
    monkeypatch.setattr(claude, 'argv', argv)
    monkeypatch.setattr(goal, '_session_from_log', lambda path:
        next(line.removeprefix('Claude session: ') for line in Path(path).read_text().splitlines()
             if line.startswith('Claude session: ')))
    folder = tmp_path / 'goal'
    def crash(*args, **kwargs):
        raise RuntimeError('fixture crash before coding checkpoint')
    with monkeypatch.context() as patch:
        patch.setattr(goal, '_publish_coding_checkpoint', crash)
        with pytest.raises(RuntimeError, match='fixture crash'):
            goal.run_goal(order, folder, Path(sys.executable), coding_launcher='claude', project_record=scope.record(),
                manager_state={'backend': 'claude', 'model': None, 'effort': None, 'native_dir': str(tmp_path / 'manager')})
    retained = {p: p.read_bytes() for p in scope.evidence('TASK1').glob('*')
                if p.is_file() and p.name.endswith(('.log', '.claude.jsonl', '.terminal.json', '.cleanup.json'))}
    monkeypatch.setattr(claude, 'argv', lambda *a, **k: pytest.fail('recovery repeated completed coding'))
    result = goal.recover_goal(order, folder, answer='Inspect the retained failure' if returncode else None)
    assert result['status'] == 'awaiting_input'
    assert result['input_request']['question'] == 'Inspect recovered result'
    assert len(requests) == 2
    assert requests[-1]['packet']['returncode'] == returncode
    assert all(path.read_bytes() == raw for path, raw in retained.items())


@pytest.mark.parametrize('returncode', [0, 1])
def test_claude_terminal_reconciles_after_controller_exit(external, manager_io, tmp_path, monkeypatch, returncode):
    import subprocess
    tree, scope, order, _ = external
    folder = tmp_path / 'goal'
    script = '''
import sys,json
from pathlib import Path
from foundry_runtime import foundry_goal as g, _foundry_manager as m, _foundry_dispatch as d, _dispatch_claude as c, foundry_native as n, _code_index, _native_session
_code_index.prepare=lambda *a,**k:None
_native_session.read_session_worktree=lambda _:Path(sys.argv[4])
m.resolve_executable=c.resolve_executable=lambda _:Path(sys.executable)
spawn=n.OwnedProcess.spawn
def native(argv,**opts):
 if argv[1:3]==['-B','-c']:return spawn(argv,**opts)
 decision={'action':'delegate','instruction':'Complete assignment'}
 rows=[{'type':'thread.started','thread_id':'00000000-0000-0000-0000-000000000001'},
 {'type':'turn.started'},{'type':'item.completed','item':{'id':'answer','type':'agent_message','text':json.dumps(decision)}},
 {'type':'turn.completed','usage':{'input_tokens':1,'cached_input_tokens':0,'output_tokens':1}}]
 return spawn([sys.executable,'-B','-c','import sys;sys.stdin.read();print('+repr('\\n'.join(map(json.dumps,rows)))+')'],**opts)
n.OwnedProcess.spawn=native
def argv(executable,session,**opts):
 rows=[{'type':'system','subtype':'init','session_id':session},
 {'type':'result','session_id':session,'subtype':'error_during_execution' if RC else 'success','is_error':bool(RC),'result':'Retained worker result'}]
 return [sys.executable,'-B','-c','import sys;sys.stdin.read();print('+repr('\\n'.join(map(json.dumps,rows)))+');sys.exit('+str(RC)+')']
c.argv=argv
g._session_from_log=lambda path:next(line.removeprefix('Claude session: ') for line in Path(path).read_text().splitlines() if line.startswith('Claude session: '))
def crash(*a,**k):raise RuntimeError('fixture interrupted intent finish')
d._finish_intent=crash
try:g.run_goal(Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.executable),coding_launcher='claude',project_record=json.loads(sys.argv[3]),manager_state={'backend':'codex','model':None,'effort':None,'native_dir':str(Path(sys.argv[2]).with_name('manager'))})
except RuntimeError as e:
 assert str(e)=='fixture interrupted intent finish',str(e)
 sys.exit(77)
'''.replace('RC', str(returncode))
    stopped = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(order), str(folder),
        json.dumps(scope.record()), str(tree)], capture_output=True, text=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW)
    assert stopped.returncode == 77, stopped.stderr
    requests, decisions = manager_io
    decisions.append(wait('Inspect reconciled terminal'))
    monkeypatch.setattr(claude, 'argv', lambda *a, **k: pytest.fail('recovery repeated worker'))
    result = goal.recover_goal(order, folder, answer='Inspect original terminal' if returncode else None)
    assert result['status'] == 'awaiting_input' and len(requests) == 1
    assert requests[0]['packet']['returncode'] == returncode
    assert result['coding_session']
