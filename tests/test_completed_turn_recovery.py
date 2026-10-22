import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from test_installed_runtime import external, offline
from test_manager_input_wait import manager_io
from test_owned_goal_recovery import SERVER, SID, interrupted
from foundry_runtime import foundry_goal as goal, app_server_transport as transport
from foundry_runtime._execution_intent import ExecutionIntent


def test_delayed_root_exit_still_hands_back_to_manager(external, manager_io, tmp_path, monkeypatch):
    tree, scope, order, _ = external
    requests, decisions = manager_io
    decisions.extend([{'action':'delegate','instruction':'work','artifacts':[]},
        {'action':'deliver_artifacts','instruction':'inspect handoff','artifacts':['candidate.txt']}])
    run = transport.run_executor
    def execute(*args, **kwargs):
        def spawn(argv, **opts):
            p = transport.OwnedProcess.spawn([sys.executable,'-B','-c','HANG=False\n'+SERVER],**opts)
            poll, started = p.poll, time.monotonic()
            p.poll = lambda: None if time.monotonic()-started < 3 else poll()
            return p
        return run(*args,process_factory=spawn,**kwargs)
    monkeypatch.setattr(transport,'run_executor',execute)
    result=goal.run_goal(order,tmp_path/'goal',Path(sys.executable),coding_launcher='app-server',
        project_record=scope.record(),manager_state={'backend':'codex','model':None,'effort':None,
            'native_dir':str(tmp_path/'manager'),'artifact_delivery':True})
    assert result['status']=='artifacts_delivered'
    assert requests[-1]['packet']['stage']=='worker_returned'


@pytest.mark.parametrize('legacy,later_turn,publication_crash',[(False,False,False),(True,False,False),(False,True,False),(False,True,True)])
def test_late_cleanup_recovery_reaches_manager_without_repeating_worker(external, manager_io, tmp_path, monkeypatch, legacy,later_turn,publication_crash):
    tree, scope, order, _ = external
    folder=tmp_path/'goal'
    script = '''import sys,json
from pathlib import Path
from foundry_runtime import foundry_goal as g, _foundry_manager as m, _foundry_dispatch as d, app_server_transport as t, _code_index, _native_session
d._codex=lambda:sys.executable
_native_session.read_session_worktree=lambda _:Path(json.loads(sys.argv[3])['context']['workspace'])
_code_index.prepare=lambda *a,**k:None
m.resolve_executable=lambda _:Path(sys.executable)
real_spawn=t.OwnedProcess.spawn
def native_spawn(argv,**opts):
 if argv[1:3]==['-B','-c']:return real_spawn(argv,**opts)
 result={'action':'delegate','instruction':'write handoff','artifacts':[]}
 rows=[{'type':'thread.started','thread_id':'00000000-0000-0000-0000-000000000001'},
  {'type':'turn.started'}, {'type':'item.completed','item':{'id':'final','type':'agent_message','text':json.dumps(result)}},
  {'type':'turn.completed','usage':{'input_tokens':10,'cached_input_tokens':0,'output_tokens':5}}]
 return real_spawn([sys.executable,'-B','-c','import sys;sys.stdin.buffer.read();print('+repr('\\n'.join(map(json.dumps,rows)))+')'],**opts)
t.OwnedProcess.spawn=native_spawn
run=t.run_executor
launches=[]
def execute(*a,**kw):
 launches.append(1)
 def spawn(argv,**opts):
  p=t.OwnedProcess.spawn([sys.executable,'-B','-c',%r],**opts)
  if len(launches) < (2 if LATER else 1): return p
  stop=p.stop_and_verify
  def delayed(**kwargs):
   from dataclasses import replace
   receipt=stop(**kwargs)
   return replace(receipt,root_exited=False,cleanup_verified=False,reason='fixture delayed root observation')
  p.stop_and_verify=delayed
  return p
 return run(*a,process_factory=spawn,**kw)
t.run_executor=execute
g.run_goal(Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.executable),coding_launcher='app-server',project_record=json.loads(sys.argv[3]),
 manager_state={'backend':'codex','model':None,'effort':None,'native_dir':str(Path(sys.argv[2]).with_name('manager')),'artifact_delivery':True})
''' % ('HANG=False\n'+SERVER)
    script=script.replace('LATER',repr(later_turn))
    completed=subprocess.run([sys.executable,'-B','-c',script,str(order),str(folder),json.dumps(scope.record())],
        capture_output=True,text=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=45)
    assert completed.returncode != 0 and 'cleanup' in completed.stderr
    manifest=json.loads((folder/'manifest.json').read_bytes())
    assert manifest['manager_phase']=='dispatch_pending', completed.stderr
    assert bool(manifest.get('coding_checkpoint'))==later_turn, completed.stderr
    package=Path(manifest['pending_dispatch']['path'])
    original={p:p.read_bytes() for p in package.parent.iterdir() if p.is_file() and p.suffix in ('.log','.json','.md')}
    if legacy:
        manifest.pop('pending_dispatch');goal._write(folder/'manifest.json',manifest)
    manifest['unanswered_live_events']=[{'item_id':'question','text':'保留未回答问题'}]
    goal._write(folder/'manifest.json',manifest)
    requests, decisions=manager_io
    decisions.append({'action':'deliver_artifacts','instruction':'inspect recovered handoff','artifacts':['candidate.txt']})
    monkeypatch.setattr(transport,'run_executor',lambda *a,**k:pytest.fail('recovery repeated worker'))
    from foundry_runtime._foundry_goal_delivery import coding_recovery
    report = coding_recovery(folder, manifest)
    preferred = report['manager_recovery']
    assert preferred['preferred'] and preferred['argv'][0] == manifest['runtime_provenance']['python']
    assert preferred['argv'][-4:] == ['--recover-goal', str(folder), '--prompt-file', '<prompt-file>']
    assert '$PromptFile' in preferred['powershell']
    advice = report['recovery_commands']
    assert advice['status'] == 'explicit_recovery_commands', advice
    from types import SimpleNamespace
    from foundry_runtime._project_context import adapter_scope
    with adapter_scope(SimpleNamespace(evidence=scope.evidence, goal_authority=lambda: scope)):
        pdcl = coding_recovery(folder, {**manifest, 'project_record': None})['recovery_commands']
        assert pdcl['status'] == 'explicit_recovery_commands', pdcl
    assert advice['argv']['resume'][advice['argv']['resume'].index('--launcher')+1] == 'app-server'
    header = Path(advice['argv']['authorize'][advice['argv']['authorize'].index('--session-header')+1])
    assert json.loads(header.read_bytes())['kind'] == 'turn_bound'
    if publication_crash:
        write = goal._write
        def interrupted_write(path, value):
            if path == folder/'manifest.json' and value.get('manager_phase') == 'feedback_pending':
                raise RuntimeError('fixture checkpoint published before manifest')
            return write(path, value)
        with monkeypatch.context() as crash:
            crash.setattr(goal, '_write', interrupted_write)
            with pytest.raises(RuntimeError, match='fixture checkpoint published'):
                goal.recover_goal(order,folder,answer='中文补充：交回经理继续裁决')
        assert not requests
    if legacy:
        from foundry_runtime.cli import main
        from foundry_runtime import _foundry_goal_transitions
        monkeypatch.setattr(_foundry_goal_transitions,'resolve_executable',lambda _:Path(sys.executable))
        prompt=tmp_path/'补充.md';prompt.write_text('中文补充：交回经理继续裁决',encoding='utf-8')
        generated = [str(prompt) if arg == '<prompt-file>' else arg
                     for arg in preferred['migration_argv']]
        assert main(generated[3:]) == 2
        result=json.loads((folder/'outcome.json').read_bytes())
    else:
        result=goal.recover_goal(order,folder,answer='中文补充：交回经理继续裁决')
    assert result['status']=='artifacts_delivered'
    assert len(requests)==1 and requests[0]['packet']['stage']=='worker_returned'
    assert requests[0]['packet']['operator_answer']=='中文补充：交回经理继续裁决'
    assert requests[0]['packet']['unanswered_live_events'][0]['item_id']=='question'
    assert all(p.read_bytes()==raw for p,raw in original.items())
    checkpoint=json.loads((folder/'coding-checkpoint.json').read_bytes())
    assert checkpoint['coding_session']==SID and checkpoint['returncode']==0
    assert len(checkpoint['coding_usage']['managed_turns'])==(2 if later_turn else 1)
    from foundry_runtime._recovery_reconciliation import completed_coding
    assert completed_coding(checkpoint)['state']=='completed'
    assert goal.recover_goal(order,folder)==result
    assert len(requests)==1


@pytest.mark.parametrize('invalid',['pending_input','conflicting_terminal','missing_terminal'])
def test_retained_reader_reuses_live_terminal_rules(tmp_path,invalid):
    from foundry_runtime._app_server_policy import normalize_policy
    binding={'thread_id':'s','turn_id':'t'}
    terminal={'method':'turn/completed','params':{'threadId':'s','turn':{
        'id':'t','status':'completed','items':[],'error':None}}}
    rows=[{'id':1,'result':{'turn':{'id':'t','status':'inProgress'}}}]
    if invalid=='pending_input':
        rows.append({'id':'input','method':'item/tool/requestUserInput','params':{
            'threadId':'s','turnId':'t','questions':[{'id':'q','question':'decision?'}]}})
    if invalid!='missing_terminal': rows.append(terminal)
    if invalid=='conflicting_terminal':
        rows.append({'method':'turn/completed','params':{'threadId':'s','turn':{
            'id':'t','status':'failed','items':[],'error':{'message':'failed'}}}})
    log=tmp_path/'dispatch.log'
    log.write_text(''.join('stdout: '+json.dumps(r)+'\n' for r in rows)+json.dumps({'event':'executor_stopped'})+'\n')
    with pytest.raises(transport.UncertainExecution):
        transport.AppServer.retained_terminal(log,binding,normalize_policy(None))


def test_generated_recovery_commands_prepare_real_authorization(interrupted, tmp_path):
    from foundry_runtime import recovery
    tree, scope, order, journal, cleanup, header, ruling, root, active = interrupted
    package = next(scope.evidence('TASK1').glob('dispatch*.package.json'))
    record, _, _ = ExecutionIntent.read_history(journal, task_key='TASK1')
    before = {p: p.read_bytes() for p in journal.glob('*.json')}
    plan = recovery.command_plan(package, journal, record)
    from foundry_runtime._project_status import status
    reported = status(scope.path, 'TASK1')['runs'][0]
    assert reported['recovery_commands']['argv'] == plan['argv']
    assert '<new-cleanup.json>' not in reported['reconcile_argv']
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert plan['runtime_python'] == json.loads(package.read_bytes())['runtime_provenance']['python']
    commands = {key: [str(ruling) if arg == '<prompt-file>' else 'owner-recovery' if arg == '<grant-anchor>' else arg
                     for arg in argv] for key, argv in plan['argv'].items()}
    assert recovery.main(commands['reconcile'][3:]) == 0
    # This descriptor fixture uses an explicitly named document, rather than a
    # production project's preconfigured grant document.
    assert recovery.main([*commands['authorize'][3:], '--operator-grant-document', str(ruling)]) == 0
    value = json.loads(Path(commands['authorize'][-1]).read_bytes())
    assert value['successor']['session_id'] == SID
    assert value['successor']['launcher'] == 'app-server'
    assert value['successor']['session_header']['path'] == str(header)
    assert commands['resume'][commands['resume'].index('--recovery-file')+1] == commands['authorize'][-1]
    expected_tail = ['--resume', SID, '--tag', 'TASK1', '--launcher', 'app-server',
                     '--prompt-file', str(ruling)]
    assert commands['resume'][5:13] == expected_tail
    assert commands['resume'].count('--launcher') == 1
    assert commands['resume'].count('--worktree') == 1
    pdcl_package = tmp_path/'pdcl.package.json'
    pdcl = json.loads(package.read_bytes())
    pdcl['runtime_provenance']['adapter'] = {'module': '_pdcl_project', 'path': 'D:/PDCL/command/bin/_pdcl_project.py'}
    pdcl['runtime_provenance']['python'] = 'D:/retained-runtime/Scripts/python.exe'
    pdcl_package.write_text(json.dumps(pdcl))
    pdcl_record = {**record, 'frozen': {**record['frozen'], 'project_context': None}}
    pdcl_plan = recovery.command_plan(pdcl_package, journal, pdcl_record)
    assert pdcl_plan['argv']['reconcile'][:2] == ['D:/retained-runtime/Scripts/python.exe',
        str(Path('D:/PDCL/command/bin/produce_recovery_authorization.py'))]
    assert pdcl_plan['argv']['resume'][1] == str(Path('D:/PDCL/command/bin/dispatch.py'))
    assert pdcl_plan['argv']['resume'][2:10] == [
        '--resume', SID, '--tag', 'TASK1', '--launcher', 'app-server', '--prompt-file', '<prompt-file>']
    assert '$PromptFile' in pdcl_plan['powershell']['resume']
    assert '$GrantAnchor' in pdcl_plan['powershell']['authorize']


def test_oversized_header_explains_limit_and_expected_input(tmp_path):
    from foundry_runtime._execution_intent import _reference_bytes, IntentConflict
    path = tmp_path/'dispatch.log'
    path.write_bytes(b'x'*33)
    with pytest.raises(IntentConflict, match='exceeds 32 byte read limit.*kind=turn_bound'):
        _reference_bytes(goal._fact(path), 'session header', limit=32)


def test_project_status_cli_keeps_its_supplied_adapter(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from foundry_runtime import cli, _foundry_project
    from foundry_runtime._project_context import invocation_adapter
    adapter = SimpleNamespace(configure_cli=lambda *a: None, validate_cli=lambda *a: None)
    def observe(*args):
        assert invocation_adapter() is adapter
        return 0
    monkeypatch.setattr(_foundry_project, 'handle', observe)
    assert cli.main(['--foundry-project', 'status', '--status-goal', str(tmp_path)], project_adapter=adapter) == 0


def test_goal_recovery_invocation_preserves_pdcl_entry_and_native_migration_is_explicit(tmp_path):
    from foundry_runtime._foundry_cli_help import recovery_invocation
    manifest = {'runtime_provenance': {'python': 'D:/original/Scripts/python.exe',
        'adapter': {'module': '_pdcl_project', 'path': 'D:/PDCL/command/bin/_pdcl_project.py'}}}
    result = recovery_invocation(tmp_path/'中文订单.md', tmp_path/'goal', manifest,
                                 prompt_file='<prompt-file>')
    assert result['argv'][:2] == ['D:/original/Scripts/python.exe',
                                str(Path('D:/PDCL/command/bin/dispatch.py'))]
    assert '--migrate-runtime' not in result['argv']
    assert result['argv'][2:] == ['--order', str(tmp_path/'中文订单.md'), '--foundry-goal',
                                 '--recover-goal', str(tmp_path/'goal'), '--prompt-file', '<prompt-file>']
    assert '$PromptFile' in result['powershell']
