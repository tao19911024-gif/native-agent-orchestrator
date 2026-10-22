"""Retained question/continuation routing without provider calls or live tasks."""
import json
from pathlib import Path
import sys
import subprocess

import pytest

from test_installed_runtime import external, offline
from test_manager_input_wait import manager_io, wait
from foundry_runtime import foundry_goal as goal, _foundry_dispatch as dispatch
from foundry_runtime import _candidate_closure as closure, _foundry_goal_upgrade as upgrade


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_dirty_review_returns_feedback_to_same_manager(external, manager_io, tmp_path, monkeypatch, backend):
    tree, scope, order, workers = external
    requests, decisions = manager_io
    original = dispatch.dispatch_order
    def worker(*args, **kwargs):
        result = original(*args, **kwargs)
        (tree/'unfinished.txt').write_text('retain this work', encoding='utf-8')
        goal._git(tree, 'add', 'unfinished.txt')
        return result
    monkeypatch.setattr(dispatch, 'dispatch_order', worker)
    decisions.extend([{'action':'delegate','instruction':'Prepare candidate'},
        {'action': 'review', 'instruction': 'Review before commit'}, wait('Need project commit ruling')])
    result = goal.run_goal(order, tmp_path/'goal', Path(sys.executable), coding_launcher='exec',
        project_record=scope.record(), manager_state={'backend': backend, 'model': None, 'effort': None,
        'native_dir': str(tmp_path/'manager')})
    assert result['status'] == 'awaiting_input'
    assert len(requests) == 3 and len(workers) == 1
    feedback = requests[2]['packet']['review_refusal']
    assert feedback['reason'] == 'candidate worktree is dirty'
    assert 'unfinished.txt' in feedback['workspace']['status_porcelain']
    assert 'committed' in requests[0]['review_precondition']
    prior = next(p.parent/'stdin.txt' for p in (tmp_path/'manager').glob('call-*/reservation.json')
                 if json.loads(p.read_bytes())['call_id'] == requests[2]['continue_from'])
    assert json.loads(prior.read_bytes().rsplit(b'\n\n',1)[-1]) == requests[1]
    assert (tree/'unfinished.txt').read_text() == 'retain this work'


@pytest.fixture(params=['plain', 'nested'])
def answered_closure(external, manager_io, tmp_path, monkeypatch, request):
    tree, scope, order, workers = external
    requests, decisions = manager_io
    decisions.append(wait('Old question'))
    root = tmp_path/'goal'
    folder = root if request.param == 'plain' else root/'round-upgrade-fixture'/'round-0'
    folder.parent.mkdir(parents=True,exist_ok=True)
    state = {'backend': 'claude', 'model': None, 'effort': None, 'native_dir': str(tmp_path/'manager')}
    assert goal.run_goal(order, folder, Path(sys.executable), coding_launcher='exec',
        project_record=scope.record(), manager_state=state)['status'] == 'awaiting_input'
    if folder != root:
        for parent,active in ((folder.parent,folder),(root,folder.parent)):
            goal._write(parent/'manifest.json',{'schema_version':1,'mode':'auto-amend',
                'order':goal._fact(order),'run_id':parent.name,'active_round':str(active),
                'state':'completed','rounds':[]})
            goal._write(parent/'outcome.json',{'status':'awaiting_input'})
    original = {p:p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    (tree/'candidate.txt').write_text('delivered candidate')
    goal._git(tree,'add','.'); goal._git(tree,'commit','-qm','candidate')
    head = goal._git(tree,'rev-parse','HEAD').decode().strip()
    decisions.append({'action':'review','instruction':'Candidate committed; review it'})
    monkeypatch.setattr(closure,'verify', lambda *a,**k: {'status':'not_evaluated',
        'candidate_commit':head,'error':'host checkout failed'})
    result = goal.recover_goal(order, root, answer='Approved original question')
    assert result['status'] == 'coding_incomplete'
    child = Path(result['continuation_evidence'])
    assert all(p.read_bytes() == raw for p,raw in original.items())
    return root, child, tree, scope, order, requests, workers, head


def test_plain_recovery_follows_consumed_answer_and_retries_only_closure(answered_closure, monkeypatch):
    root, child, tree, scope, order, requests, workers, head = answered_closure
    counts = len(requests),len(workers)
    observed=[]
    monkeypatch.setattr(closure,'verify',lambda *a,**k: observed.append(a[3]) or
        {'status':'not_configured','candidate_commit':head})
    def finish(checkpoint, folder):
        result={'status':'candidate_verified','candidate_commit':head,'reviews':[]}
        goal._write(folder/'outcome.json',result)
        return result
    monkeypatch.setattr(goal,'_finish_review',finish)
    result=goal.recover_goal(order,root)
    assert result['status']=='candidate_verified'
    assert result['result_outcome']==goal._fact(child/'outcome.json')
    assert observed==[head] and (len(requests),len(workers))==counts
    assert json.loads((root/'outcome.json').read_bytes())['status']=='awaiting_input'


def test_upgrade_resolves_consumed_closure_boundary(answered_closure, monkeypatch):
    root, child, tree, scope, order, requests, workers, head=answered_closure
    old=json.loads((root/'manifest.json').read_bytes())
    selected,current=goal.goal_auto.active_round(root,old,consumed=True)
    assert selected==child and current['candidate_closure']['status']=='not_evaluated'
    prepared=upgrade.prepare(goal,selected,current,{'order':str(order),'answer':'Retry closure on current'})
    assert prepared['candidate_closure']['candidate_commit']==head
    assert prepared['manager_packet']['runtime_upgrade']['previous_manager_history'][-1]['call_id']==current['manager_state']['last_call_id']
    # A newer native request with no consuming continuation must still refuse.
    current['manager_state']['last_call_id']='unconsumed'
    with pytest.raises(ValueError,match='decision is not consumed'):
        upgrade.prepare(goal,selected,current,{'order':str(order),'answer':'Retry'})


def test_upgrade_retries_closure_without_another_manager_or_worker(answered_closure, tmp_path, monkeypatch):
    from foundry_runtime import cli, foundry_entry, foundry_native_support
    root, child, tree, scope, order, requests, workers, head=answered_closure
    monkeypatch.setattr(foundry_entry,'_analyst_executable',lambda *a:Path(sys.executable))
    monkeypatch.setattr(foundry_native_support,'resolve_executable',lambda *a:Path(sys.executable))
    counts=len(requests),len(workers)
    observed=[]
    monkeypatch.setattr(closure,'verify',lambda *a,**k: observed.append(a[3]) or
        {'status':'not_configured','candidate_commit':head})
    def finish(checkpoint, folder):
        result={'status':'candidate_verified','candidate_commit':head,'reviews':[]}
        goal._write(folder/'outcome.json',result)
        return result
    monkeypatch.setattr(goal,'_finish_review',finish)
    prompt=tmp_path/'upgrade.txt';prompt.write_text('Retry host closure on the unchanged candidate')
    args=['--project-config',str(scope.path),'--order',str(order),'--foundry-goal',
          '--recover-goal',str(root),'--upgrade-runtime',sys.executable,'--prompt-file',str(prompt)]
    assert cli.main(args)==0
    assert observed==[head] and (len(requests),len(workers))==counts
    assert cli.main(args)==0
    assert observed==[head] and (len(requests),len(workers))==counts


def test_upgrade_child_refusal_is_visible_through_hidden_process(tmp_path):
    # The target deliberately rejects an unknown CLI option before any project
    # admission. Reproduce redirected stdio through the real Windows relay.
    legacy='D:/Foundry/releases/0.1.0a83/Scripts/python.exe'
    assert Path(legacy).is_file()
    script='''
import pathlib,sys
from types import SimpleNamespace
from foundry_runtime import _foundry_goal_upgrade as u
root=pathlib.Path(sys.argv[1]);(root/'manifest.json').write_text('{}')
u.request=lambda a:(root,{'target':sys.argv[2]},'fixture')
a=SimpleNamespace(upgrade_runtime=sys.argv[2])
raise SystemExit(u.route(a,['--not-a-foundry-option']))
'''
    result=subprocess.run([sys.executable,'-X','utf8','-c',script,str(tmp_path),legacy],
                          capture_output=True,text=True,encoding='utf-8',errors='replace')
    assert result.returncode==2
    assert 'unrecognized arguments' in result.stderr and '--not-a-foundry-option' in result.stderr


def test_multiple_answer_generations_follow_references_not_old_question(external, manager_io, tmp_path):
    tree,scope,order,workers=external
    requests,decisions=manager_io
    decisions.extend([wait('first'),wait('second'),wait('third')])
    root=tmp_path/'goal'
    goal.run_goal(order,root,Path(sys.executable),coding_launcher='exec',project_record=scope.record(),
        manager_state={'backend':'claude','model':None,'effort':None,'native_dir':str(tmp_path/'manager')})
    first=goal.recover_goal(order,root,answer='first answer')
    second=goal.recover_goal(order,Path(first['continuation_evidence']),answer='second answer')
    latest=goal.recover_goal(order,root)
    assert latest['input_request']['question']=='third'
    assert latest['result_outcome']==second['result_outcome']
    assert len(requests)==3 and not workers


@pytest.mark.parametrize('inline', [False, True])
def test_recorded_runtime_receives_resolved_continuation(inline, monkeypatch):
    from foundry_runtime import _foundry_recovery_preflight as preflight
    calls=[]
    monkeypatch.setattr(preflight.subprocess,'call',lambda argv,**kw:calls.append((argv,kw)) or 2)
    args=['--recover-goal=old'] if inline else ['--recover-goal','old']
    report={'runtime_python':'old-python','environment':{},'consumed_continuation':'verified-child'}
    assert preflight.execute_recorded(report,[*args,'--order','中文.md'])==2
    argv,options=calls[0]
    assert 'verified-child' in ' '.join(argv) and '中文.md' in argv
    assert options['stdout'] is sys.stdout and options['stderr'] is sys.stderr
