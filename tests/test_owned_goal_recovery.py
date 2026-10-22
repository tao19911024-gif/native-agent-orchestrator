"""Recover a real interrupted stdio worker and retain its goal accounting."""
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from test_installed_runtime import external, offline, git
from test_goal_preflight_recovery import retained_goal, checked_project
from foundry_runtime import recovery, foundry_goal as goal, _foundry_dispatch as dispatch
from foundry_runtime._execution_intent import ExecutionIntent

SID = '00000000-0000-0000-0000-000000000001'
SERVER = '''import sys,json,time
sid="00000000-0000-0000-0000-000000000001"
def send(row): print(json.dumps(row),flush=True)
for line in sys.stdin:
 r=json.loads(line); m=r.get('method'); i=r.get('id')
 if i is None: continue
 result={}
 if m in ('thread/start','thread/resume'): result={'thread':{'id':sid}}
 if m=='model/list': result={'data':[]}
 if m=='turn/start': result={'turn':{'id':'fixture-turn','status':'inProgress'}}
 send({'id':i,'result':result})
 if m=='turn/start':
  if HANG: time.sleep(60)
  from pathlib import Path
  Path('candidate.txt').write_text('worker updated candidate')
  send({'method':'item/completed','params':{'threadId':sid,'turnId':'fixture-turn','item':{'id':'final','type':'agentMessage','text':'worker complete'}}})
  send({'method':'turn/completed','params':{'threadId':sid,'turn':{'id':'fixture-turn','status':'completed','items':[],'error':None}}})
'''


@pytest.fixture
def interrupted(external, tmp_path, monkeypatch, request):
    tree, scope, order, _ = external
    home = tmp_path/'codex'; sessions = home/'sessions/2026/10/08'; sessions.mkdir(parents=True)
    (sessions/f'rollout-fixture-{SID}.jsonl').write_text(json.dumps(
        {'type':'session_meta','payload':{'id':SID,'cwd':str(tree)}})+'\n')
    monkeypatch.setenv('CODEX_HOME', str(home))
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
d.dispatch_order(Path(sys.argv[2]),project=open_project(sys.argv[1],preflight_input=json.loads(sys.argv[3])),launcher='app-server')
''' % ('HANG=True\n'+SERVER)
    log = (tmp_path/'controller.log').open('wb')
    proc = subprocess.Popen([sys.executable,'-u','-c',script,str(scope.path),str(order),json.dumps(scope.preflight_input)],
        stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline=time.monotonic()+15; folder=None
        while time.monotonic()<deadline:
            for p in (scope.common_dir/'pdcl-dispatch/task-TASK1/intents').glob('*'):
                # mkdir precedes the first atomic journal publication. Wait for
                # that fixture initialization, then retain the strict chain read.
                if p.is_dir() and not any(p.iterdir()):
                    continue
                row,_,_=ExecutionIntent.read_history(p,task_key='TASK1')
                if row.get('turn_binding'): folder=p; break
            if folder: break
            assert proc.poll() is None, (tmp_path/'controller.log').read_text(errors='replace')
            time.sleep(.05)
        assert folder
        # Windows venv python.exe is a redirector: killing Popen.pid can leave
        # the actual controller alive, legitimately retaining the intent lock.
        # Interrupt the recorded controller identity, then let its redirector exit.
        from foundry_runtime._execution_stop import _terminate
        from foundry_runtime._executor_lifecycle import process_identity_exited
        controller = {'root_pid':row['owner_pid'], 'root_creation_time':row['owner_creation_time']}
        if getattr(request, 'param', None) == 'recoverable_stop':
            assert recovery.main(['--project-config',str(scope.path),'--task','TASK1',
                '--predecessor',folder.name,'--stop-reason','preserve unfinished work',
                '--keep-recoverable','--output',str(tmp_path/'cleanup.json')]) == 0
        else:
            _terminate(controller)
        proc.wait(timeout=10)
        assert process_identity_exited(controller['root_pid'], controller['root_creation_time'])
    finally:
        if proc.poll() is None: proc.terminate(); proc.wait(timeout=10)
        log.close()
    cleanup=tmp_path/'cleanup.json'
    assert recovery.main(['--project-config',str(scope.path),'--task','TASK1',
        '--predecessor',folder.name,'--reconcile','--output',str(cleanup)])==0
    row,_,_=ExecutionIntent.read_history(folder,task_key='TASK1')
    assert row['state']=='unknown' and row['cleanup']['job_active_processes']==0
    header=next(p for p in folder.glob('*.json') if json.loads(p.read_bytes()).get('kind')=='turn_bound')
    (tree/'candidate.txt').write_text('unfinished candidate')
    ruling=tmp_path/'ruling.md'
    ruling.write_text('Continue this candidate.\n\n## owner-recovery\n'
        '- **Authority:** operator\n- **Granted:** 2026-10-08\n'
        '- **Scope:** TASK1; recover this uncommitted candidate and its original worker.\n')
    root,active,manifest=retained_goal(external,scope,tmp_path)
    manifest['manager_phase']='dispatch_pending'
    checkpoint=json.loads((active/'coding-checkpoint.json').read_bytes())
    checkpoint.update(coding_session=SID)
    checkpoint['options']['coding_launcher']='app-server'
    checkpoint['options']['coding_model']=None
    checkpoint['options']['coding_effort']=None
    goal._write(active/'coding-checkpoint.json',checkpoint)
    manifest['coding_checkpoint']=goal._fact(active/'coding-checkpoint.json')
    goal._write(active/'manifest.json',manifest)
    return tree,scope,order,folder,cleanup,header,ruling,root,active


def prepare(case,tmp_path):
    tree,scope,order,folder,cleanup,header,ruling,root,active=case
    output=tmp_path/'recovery.json'
    assert recovery.main(['--project-config',str(scope.path),'--task','TASK1',
        '--predecessor',folder.name,'--cleanup',str(cleanup),'--session-header',str(header),
        '--amendment',str(ruling),'--foundry-manifest',str(active/'manifest.json'),
        '--operator-grant-document',str(ruling),'--operator-grant','owner-recovery',
        '--uncommitted-candidate','--output',str(output)])==0
    return output


def test_dirty_initial_goal_producer_uses_real_cleanup(interrupted,tmp_path):
    output=prepare(interrupted,tmp_path)
    value=json.loads(output.read_bytes())
    assert value['successor']['candidate_snapshot']
    assert value['successor']['foundry_manifest']
    assert value['source']['authority']=='named-project-document'
    assert 'recovery_grants' not in interrupted[1].project['execution']


def admit_revision(case, tmp_path):
    tree, scope, order, journal, cleanup, header, ruling, root, active = case
    manifest = json.loads((active/'manifest.json').read_bytes())
    previous = tmp_path/'previous-order.md'; previous.write_bytes(order.read_bytes())
    order.write_bytes(order.read_bytes() + b'\nExplicitly approved incremental contract.\n')
    revised = tmp_path/'revised-order.md'; revised.write_bytes(order.read_bytes())
    decision = tmp_path/'revision.json'
    goal._write(decision, {'schema_version':1, 'authorized_by':'operator', 'goal':str(active),
        'run_id':manifest['run_id'], 'order':manifest['order'], 'previous':goal._fact(previous),
        'revised':goal._fact(revised), 'authorization':goal._fact(ruling)})
    with goal._run_lock(active):
        return goal.order_revision.admit(goal, active, manifest, order, decision)


@pytest.mark.parametrize('later_edit', [False, True])
def test_unknown_worker_producer_uses_admitted_contract_revision(interrupted, tmp_path, later_edit):
    tree, scope, order, journal, cleanup, header, ruling, root, active = interrupted
    manifest = json.loads((active/'manifest.json').read_bytes())
    original_checkpoint = (active/'coding-checkpoint.json').read_bytes()
    admitted = admit_revision(interrupted, tmp_path)
    if later_edit:
        order.write_bytes(order.read_bytes() + b'Unapproved edit.\n')
        with pytest.raises(RuntimeError, match='manifest differs'):
            prepare(interrupted, tmp_path)
        assert not (tmp_path/'recovery.json').exists()
    else:
        output = prepare(interrupted, tmp_path)
        value = json.loads(output.read_bytes())
        retained = json.loads(Path(value['successor']['foundry_manifest']['path']).read_bytes())
        assert retained['order'] == manifest['order']
        assert retained['order_revisions'] == admitted['order_revisions']
        assert goal.order_revision.matches(goal, order, retained)
        from foundry_runtime._recovery_authorization import RecoveryAuthorization
        verified = RecoveryAuthorization.load(output, task_key='TASK1', worktree=tree,
            session_id=SID, candidate_head=None, amendment=ruling.read_text(encoding='utf-8-sig').encode())
        assert verified.data['predecessor']['intent_id'] == journal.name
        assert verified.candidate_matches()
    assert (active/'coding-checkpoint.json').read_bytes() == original_checkpoint
    record, _, _ = ExecutionIntent.read_history(journal, task_key='TASK1')
    assert record['state'] == 'unknown' and not record.get('recovery_authorization')


def native_manager(case, monkeypatch):
    from foundry_runtime import _foundry_manager as manager, foundry_native as native, _code_index
    tree,scope,order,folder,cleanup,header,ruling,root,active=case
    monkeypatch.setattr(_code_index,'prepare',lambda *a,**kw:None)
    monkeypatch.setattr(manager,'resolve_executable',lambda _:Path(sys.executable))
    real_spawn=native.OwnedProcess.spawn
    calls=[]
    def spawn(argv,**opts):
        envelope=json.loads(opts['stdin'].read().rsplit(b'\n\n',1)[-1]); opts['stdin'].seek(0)
        calls.append(envelope)
        result={'action':'delegate','instruction':'initial assignment','artifacts':[]} if len(calls)==1 else {
            'action':'deliver_artifacts','instruction':'worker recovered; acceptance pending','artifacts':['candidate.txt']}
        rows=[{'type':'thread.started','thread_id':SID}, {'type':'turn.started'},
            {'type':'item.completed','item':{'id':'final','type':'agent_message','text':json.dumps(result)}},
            {'type':'turn.completed','usage':{'input_tokens':10,'cached_input_tokens':0,'output_tokens':5}}]
        script='import sys;sys.stdin.buffer.read();print('+repr('\n'.join(map(json.dumps,rows)))+')'
        return real_spawn([sys.executable,'-u','-c',script],**opts)
    monkeypatch.setattr(native.OwnedProcess,'spawn',spawn)
    state={'backend':'codex','model':None,'effort':None,'last_call_id':None,
        'native_dir':str(root/'native-manager'),'artifact_delivery':True,'profile':'astra'}
    _,state,_,_=manager.turn(state,tree,{'stage':'assign'})
    manifest=json.loads((active/'manifest.json').read_bytes()); manifest['manager_state']=state
    goal._write(active/'manifest.json',manifest)
    return calls, real_spawn


@pytest.mark.parametrize('migrate',[False,True])
@pytest.mark.parametrize('revised_contract',[False,True])
@pytest.mark.parametrize('interrupted', [None, 'recoverable_stop'], indirect=True)
def test_owned_recovery_returns_real_terminal_to_manager_and_replays_once(interrupted,tmp_path,monkeypatch,migrate,revised_contract):
    from foundry_runtime import app_server_transport as transport
    case=interrupted; tree,scope,order,folder,cleanup,header,ruling,root,active=case
    calls,real_spawn=native_manager(case,monkeypatch)
    if revised_contract:
        admit_revision(case, tmp_path)
    output=prepare(case,tmp_path)
    if not migrate:
        checkpoint=json.loads((active/'coding-checkpoint.json').read_bytes())
        checkpoint['options']['coding_launcher']='exec'
        goal._write(active/'coding-checkpoint.json',checkpoint)
        manifest=json.loads((active/'manifest.json').read_bytes())
        manifest['coding_checkpoint']=goal._fact(active/'coding-checkpoint.json')
        goal._write(active/'manifest.json',manifest)
    launches=[]; run=transport.run_executor
    def execute(*a,**kw):
        def spawn(argv,**opts):
            launches.append(1)
            return real_spawn([sys.executable,'-u','-c','HANG=False\n'+SERVER],**opts)
        return run(*a,process_factory=spawn,**kw)
    monkeypatch.setattr(transport,'run_executor',execute)
    saved={p:p.read_bytes() for p in folder.iterdir()}
    if migrate:
        from foundry_runtime.cli import main
        assert main(['--project-config',str(scope.path),'--order',str(order),
            '--foundry-goal','--recover-goal',str(root),'--prompt-file',str(ruling),
            '--recovery-file',str(output),'--migrate-runtime'])==2  # artifact handoff is not candidate acceptance
        result=json.loads((root/'outcome.json').read_bytes())
    else:
        result=goal.recover_goal(order,root,answer=ruling.read_text(),recovery_file=output)
    assert result['status']=='artifacts_delivered' and result['acceptance_pending']
    assert len(launches)==1 and len(calls)==2
    assert calls[-1]['packet']['stage']=='worker_returned'
    checkpoint=json.loads((active/'coding-checkpoint.json').read_bytes())
    evidence=checkpoint['evidence']
    terminal=next(x for x in evidence if x['path'].endswith('.terminal.json'))
    proof=json.loads(Path(terminal['path']).read_bytes())
    assert proof['binding']['thread_id']==SID
    assert len(evidence)>=3
    old,_,_=ExecutionIntent.read_history(folder,task_key='TASK1')
    assert old['state']=='unknown' and 'returncode' not in old
    assert all(p.read_bytes()==raw for p,raw in saved.items())
    assert goal.recover_goal(order,root,answer=ruling.read_text(),recovery_file=output,migrate_runtime=migrate)==result
    assert len(launches)==1 and len(calls)==2


@pytest.mark.parametrize('change',['source','candidate','session','attempt'])
def test_changed_recovery_inputs_never_launch(interrupted,tmp_path,monkeypatch,change):
    from foundry_runtime import app_server_transport as transport
    case=interrupted; tree,scope,order,folder,cleanup,header,ruling,root,active=case
    native_manager(case,monkeypatch)
    output=prepare(case,tmp_path)
    answer=ruling.read_text()
    if change=='source': ruling.write_text(ruling.read_text()+'changed\n')
    elif change=='candidate': (tree/'candidate.txt').write_text('changed after freeze')
    else:
        value=json.loads(output.read_bytes())
        target=value['successor'] if change=='session' else value['predecessor']
        target['session_id' if change=='session' else 'attempt_id']='00000000-0000-0000-0000-000000000099'
        output.write_text(json.dumps(value))
    monkeypatch.setattr(transport,'run_executor',lambda *a,**kw:pytest.fail('changed inputs launched'))
    with pytest.raises((ValueError,RuntimeError)):
        goal.recover_goal(order,root,answer=answer,recovery_file=output)
    row,_,_=ExecutionIntent.read_history(folder,task_key='TASK1')
    assert row['state']=='unknown' and 'recovery_authorization' not in row


@pytest.mark.parametrize('interrupt_feedback',[False,True])
def test_worker_provider_stop_retains_migration_for_answer_continuation(interrupted,tmp_path,monkeypatch,interrupt_feedback):
    from foundry_runtime import app_server_transport as transport
    case=interrupted; tree,scope,order,folder,cleanup,header,ruling,root,active=case
    calls,real_spawn=native_manager(case,monkeypatch)
    output=prepare(case,tmp_path)
    launches=[]; run=transport.run_executor
    def execute(*a,**kw):
        def spawn(argv,**opts):
            launches.append(1)
            server=SERVER
            if len(launches)==1:
                server=server.replace("'status':'completed','items':[],'error':None",
                    "'status':'failed','items':[],'error':{'message':'fixture quota','codexErrorInfo':'usageLimitExceeded'}")
            return real_spawn([sys.executable,'-u','-c','HANG=False\n'+server],**opts)
        return run(*a,process_factory=spawn,**kw)
    monkeypatch.setattr(transport,'run_executor',execute)
    result=goal.recover_goal(order,root,answer=ruling.read_text(),recovery_file=output,migrate_runtime=True)
    assert result['status']=='coding_incomplete' and len(calls)==1
    checkpoint=json.loads((active/'coding-checkpoint.json').read_bytes())
    assert checkpoint['options']['migrate_recovered_manager'] is True
    assert json.loads((active/'manifest.json').read_bytes())['worker_recovery_migration'] is True
    assert goal.recover_goal(order,root,answer=ruling.read_text(),recovery_file=output,migrate_runtime=True)==result
    assert len(launches)==1
    write=goal._write; continued=[]
    def interrupt(path,value):
        write(path,value)
        if (Path(path).name=='manifest.json' and value.get('continuation')
                and value.get('manager_phase')=='feedback_pending'):
            continued.append(Path(path).parent)
            raise RuntimeError('fixture interrupted after continued worker feedback')
    if interrupt_feedback:
        monkeypatch.setattr(goal,'_write',interrupt)
        with pytest.raises(RuntimeError,match='continued worker feedback'):
            goal.recover_goal(order,root,answer='The fixture quota has ended; continue the same task.')
        monkeypatch.setattr(goal,'_write',write)
        result=goal.recover_goal(order,root,answer='The fixture quota has ended; continue the same task.')
    else:
        result=goal.recover_goal(order,root,answer='The fixture quota has ended; continue the same task.')
    assert result['status']=='artifacts_delivered' and len(launches)==2 and len(calls)==2
    continued=continued[0] if interrupt_feedback else Path(result['continuation_evidence'])
    manager_state=json.loads((continued/'manifest.json').read_bytes())['manager_state']
    assert manager_state.get('handoff') and manager_state['backend']=='codex'


@pytest.mark.parametrize('window',['terminal','checkpoint'])
def test_completed_successor_replays_after_goal_checkpoint_interruption(interrupted,tmp_path,monkeypatch,window):
    from foundry_runtime import app_server_transport as transport
    case=interrupted; tree,scope,order,folder,cleanup,header,ruling,root,active=case
    calls,real_spawn=native_manager(case,monkeypatch)
    output=prepare(case,tmp_path)
    launches=[]; run=transport.run_executor
    server=SERVER.replace("Path('candidate.txt').write_text('worker updated candidate')",
        "Path('candidate.txt').write_text('worker updated candidate'); import subprocess; "
        "subprocess.run(['git','add','candidate.txt'],check=True); subprocess.run(['git','commit','-qm','worker result'],check=True)")
    def execute(*a,**kw):
        def spawn(argv,**opts):
            launches.append(1)
            return real_spawn([sys.executable,'-u','-c','HANG=False\n'+server],**opts)
        return run(*a,process_factory=spawn,**kw)
    monkeypatch.setattr(transport,'run_executor',execute)
    resume=dispatch.resume_order; write=goal._write; proofs={}
    def interrupt(*a,**kw):
        result=resume(*a,**kw); _,log,_=result
        for suffix in ('.terminal.json','.cleanup.json'):
            path=Path(log).with_suffix(suffix); proofs[path]=path.read_bytes()
        if window=='terminal': raise RuntimeError('fixture interrupted before goal checkpoint')
        return result
    def interrupted_write(path,value):
        if Path(path).name=='manifest.json' and value.get('manager_phase')=='feedback_pending':
            raise RuntimeError('fixture interrupted after goal checkpoint')
        write(path,value)
    monkeypatch.setattr(dispatch,'resume_order',interrupt)
    if window=='checkpoint': monkeypatch.setattr(goal,'_write',interrupted_write)
    with pytest.raises(RuntimeError,match='fixture interrupted'):
        goal.recover_goal(order,root,answer=ruling.read_text(),recovery_file=output)
    assert len(launches)==1 and len(calls)==1
    assert proofs
    monkeypatch.setattr(dispatch,'resume_order',resume)
    monkeypatch.setattr(goal,'_write',write)
    result=goal.recover_goal(order,root,answer=ruling.read_text(),recovery_file=output)
    assert result['status']=='artifacts_delivered' and len(launches)==1 and len(calls)==2
    assert calls[-1]['packet']['stage']=='worker_returned'
    assert all(p.read_bytes()==raw for p,raw in proofs.items())


def test_cli_owned_recovery_applies_explicit_resume_preflight(external,tmp_path,monkeypatch):
    from foundry_runtime import app_server_transport as transport
    from foundry_runtime.cli import main
    tree,_,order,records=external
    scope=checked_project(external,tmp_path)
    case=interrupted.__wrapped__((tree,scope,order,records),tmp_path,monkeypatch,None)
    calls,real_spawn=native_manager(case,monkeypatch)
    output=prepare(case,tmp_path)
    archived=json.loads(output.read_bytes())['successor']['foundry_manifest']
    assert json.loads(Path(archived['path']).read_bytes())['project_record']['preflight_input']['requireCleanWorktree']
    replacement=tmp_path/'resume-input.json'
    replacement.write_text(json.dumps({**scope.preflight_input,'requireCleanWorktree':False}))
    run=transport.run_executor
    def execute(*a,**kw):
        return run(*a,process_factory=lambda argv,**opts:real_spawn(
            [sys.executable,'-u','-c','HANG=False\n'+SERVER],**opts),**kw)
    monkeypatch.setattr(transport,'run_executor',execute)
    descriptor=scope.path.read_bytes()
    assert main(['--project-config',str(scope.path),'--order',str(order),
        '--foundry-goal','--recover-goal',str(case[7]),'--prompt-file',str(case[6]),
        '--recovery-file',str(output),'--project-resume-preflight-input',str(replacement)])==2
    manifest=json.loads((case[8]/'manifest.json').read_bytes())
    assert manifest['project_record']['resume_preflight_input']['requireCleanWorktree'] is False
    assert scope.path.read_bytes()==descriptor and len(calls)==2


def test_manager_transfer_write_retains_real_worker_feedback(interrupted,tmp_path,monkeypatch):
    from foundry_runtime import app_server_transport as transport
    case=interrupted; tree,scope,order,folder,cleanup,header,ruling,root,active=case
    calls,real_spawn=native_manager(case,monkeypatch)
    output=prepare(case,tmp_path)
    launches=[]; run=transport.run_executor
    def execute(*a,**kw):
        def spawn(argv,**opts):
            launches.append(1)
            return real_spawn([sys.executable,'-u','-c','HANG=False\n'+SERVER],**opts)
        return run(*a,process_factory=spawn,**kw)
    monkeypatch.setattr(transport,'run_executor',execute)
    write=goal._write; checkpoint_writes=[]
    def interrupt(path,value):
        write(path,value)
        if Path(path).name=='coding-checkpoint.json': checkpoint_writes.append(Path(path).read_bytes())
        if Path(path).name=='manifest.json' and value.get('manager_state',{}).get('transition_request'):
            raise RuntimeError('fixture interrupted after accepted manager transfer')
    monkeypatch.setattr(goal,'_write',interrupt)
    with pytest.raises(RuntimeError,match='accepted manager transfer'):
        goal.recover_goal(order,root,answer=ruling.read_text(),recovery_file=output,migrate_runtime=True)
    assert len(launches)==1 and len(calls)==1 and len(checkpoint_writes)==1
    monkeypatch.setattr(goal,'_write',write)
    result=goal.recover_goal(order,root,answer=ruling.read_text(),recovery_file=output,migrate_runtime=True)
    assert result['status']=='artifacts_delivered' and len(launches)==1 and len(calls)==2
    assert calls[-1]['packet']['stage']=='worker_returned'
    assert (active/'coding-checkpoint.json').read_bytes()==checkpoint_writes[0]
