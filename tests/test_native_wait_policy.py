import json
import io
import tomllib

import pytest

from foundry_runtime._compute_selection import resolve_native_team, team_prompt
from foundry_runtime._codex_command import coding_argv
from foundry_runtime._launch_context import validate_project_launch
from foundry_runtime._context_signals import tool_record, waiting_signals


@pytest.mark.parametrize('profile', ['none', 'adaptive'])
def test_new_team_configures_native_wait_without_execution_timeout(monkeypatch, profile):
    monkeypatch.delenv('FOUNDRY_WAIT_INTERVAL_SECONDS', raising=False)
    team = resolve_native_team(profile)
    config = team['config']
    native = config['features.multi_agent_v2']
    assert native['min_wait_timeout_ms'] == native['default_wait_timeout_ms'] == 1800000
    assert native['max_wait_timeout_ms'] == 3600000
    assert native['non_code_mode_only'] is True
    assert 'background_terminal_max_timeout' not in config
    assert 'features.code_mode.default_exec_yield_time_ms' not in config
    assert team['process_completion_wait'] == 1
    assert not any('deadline' in k or 'runtime_seconds' in k for k in config)
    assert 'One implementation owner retains each process handle' in team['instructions']


@pytest.mark.parametrize('value,expected', [('60',60000),('7200',3600000),('bad',1800000),('-1',1800000)])
def test_optional_wait_interval_is_frozen_in_team(monkeypatch, tmp_path, value, expected):
    monkeypatch.setenv('FOUNDRY_WAIT_INTERVAL_SECONDS', value)
    team = resolve_native_team('adaptive')
    frozen = json.loads(json.dumps(team))
    monkeypatch.setenv('FOUNDRY_WAIT_INTERVAL_SECONDS', '5')
    argv = coding_argv('codex', 'app-server', workspace=tmp_path, native_team=frozen,
        launch_context={'settings': {'tool_tokens':3000}, 'server':None})
    config=tomllib.loads(next(arg for arg in argv if arg.startswith('features.multi_agent_v2=')))
    assert config['features']['multi_agent_v2']['min_wait_timeout_ms']==expected
    assert frozen == team
    assert team_prompt('task', frozen).startswith(frozen['instructions'])


def test_old_frozen_team_does_not_acquire_new_wait_policy(monkeypatch, tmp_path):
    monkeypatch.setenv('FOUNDRY_WAIT_INTERVAL_SECONDS', '1800')
    old = {'profile':'adaptive', 'version':19, 'config':{'agents.enabled': True}, 'instructions':'old\n'}
    argv = coding_argv('codex', 'app-server', workspace=tmp_path, native_team=old,
        launch_context={'settings': {'tool_tokens':3000}, 'server':None})
    assert not any('wait_timeout' in arg or 'yield_time' in arg or 'background_terminal' in arg for arg in argv)
    assert team_prompt('task', old) == 'old\n\nTask:\ntask'


def test_native_default_opt_out_and_claude_scope(monkeypatch):
    validate_project_launch({'FOUNDRY_WAIT_INTERVAL_SECONDS':0})
    monkeypatch.setenv('FOUNDRY_WAIT_INTERVAL_SECONDS', '0')
    team = resolve_native_team('adaptive')
    assert 'min_wait_timeout_ms' not in team['config']['features.multi_agent_v2']
    assert 'background_terminal_max_timeout' not in team['config']
    claude = resolve_native_team('adaptive', backend='claude')
    assert claude['config'] == {}
    assert 'One implementation owner retains each process handle' in claude['instructions']


def test_wait_interval_records_requested_duration_and_observed_model_responses():
    def row(stamp, payload, kind='tool_observation'):
        return {'timestamp':'2026-10-19T12:'+stamp+'Z', 'type':kind, 'payload':payload}
    call = tool_record({'type':'function_call', 'name':'collaboration.wait_agent', 'call_id':'w',
                        'arguments':json.dumps({'timeout_ms':1800000})})
    output = tool_record({'type':'function_call_output', 'call_id':'w',
                         'output':json.dumps({'timed_out':True})})
    rows = [row('00:00',call),row('30:00',output)]
    responses = [row('10:00',{'response_id':'r'},'token_usage_record'),
                 row('10:00',{'response_id':'r'},'token_usage_record'),
                 row('30:01',{'response_id':'after'},'token_usage_record')]
    result=waiting_signals(rows+responses)
    assert result['intervals'] == [{'call_id':'w','start':'2026-10-19T12:00:00Z',
        'end':'2026-10-19T12:30:00Z','observed_seconds':1800.0,'tool':'wait_agent',
        'response_records_in_call_window':1,'requested_wait_ms':1800000,
        'additional_model_calls_caused_by_wait':None,
        'response_scope':'Record timestamps may include the response issuing this wait; not model start times or added wait cost.',
        'response_coverage':'observed_native_records'}]
    assert waiting_signals(rows)['intervals'][0]['response_records_in_call_window'] is None


@pytest.mark.parametrize('name,args', [
    ('write_stdin',{'session_id':5,'chars':'secret input'}),
    ('TaskOutput',{'task_id':'t','block':False}),('wait',{'cell_id':'c','terminate':True})])
def test_interaction_or_probe_is_not_a_blocked_wait_interval(name,args):
    call=tool_record({'type':'function_call','name':name,'call_id':'w','arguments':json.dumps(args)})
    rows=[{'timestamp':'2026-10-19T12:00:00Z','payload':call},
          {'timestamp':'2026-10-19T12:00:10Z','payload':{'kind':'output','call_id':'w'}}]
    assert waiting_signals(rows)['intervals']==[]
    assert 'secret input' not in json.dumps(call)


def test_claude_wait_response_count_keeps_member_identity(tmp_path):
    from foundry_runtime._context_signals import claude_stream
    rows=[{'type':'assistant','timestamp':'2026-10-19T12:00:00Z','agent_id':'a',
           'message':{'id':'m0','usage':{'input_tokens':1},'content':[{'type':'tool_use','id':'w','name':'TaskOutput',
               'input':{'task_id':'job','block':True,'timeout':1800000}}]}},
          {'type':'assistant','timestamp':'2026-10-19T12:00:10Z','agent_id':'b',
           'message':{'id':'other','usage':{'input_tokens':1},'content':[]}},
          {'type':'user','timestamp':'2026-10-19T12:01:00Z','agent_id':'a',
           'message':{'content':[{'type':'tool_result','tool_use_id':'w','content':'finished'}]}}]
    path=tmp_path/'stream.jsonl';path.write_text('\n'.join(map(json.dumps,rows)))
    result=claude_stream(path)['waiting_signals']
    assert result['intervals'][0]['response_records_in_call_window']==0
    assert result['intervals'][0]['requested_wait_ms']==1800000


def observer():
    from foundry_runtime.app_server_transport import AppServer
    app=AppServer(None,io.StringIO(),deadline=None)
    app.thread_id,app.turn_id='root','t'
    app.member_activity.members['child']={'status':'running'}
    return app


def event(kind, identity, *, method='item/completed', thread='root',turn='t',**extra):
    return {'method':method,'params':{'threadId':thread,'turnId':turn,
        'item':{'id':identity,'type':kind,**({'processId':'native-process'} if kind=='commandExecution' else {}),**extra}}}


@pytest.mark.parametrize('completion_first',[False,True])
@pytest.mark.parametrize('thread',['root','child'])
def test_native_process_completion_wakes_only_its_waiting_owner(completion_first,thread):
    app=observer()
    wait=event('collabAgentToolCall','w',method='item/started',thread=thread,tool='wait')
    done=event('commandExecution','cmd',thread=thread,status='completed',exitCode=0)
    first,second=(done,wait) if completion_first else (wait,done)
    assert app._process_wait_notice(first) is None
    notice=app._process_wait_notice(second)
    assert notice=={'thread_id':thread,'turn_id':'t','wait_id':'w',
                    'commands':[{'item_id':'cmd','process_id':'native-process','status':'completed','exit_code':0}]}
    assert app._process_wait_notice(done) is None
    assert app._process_wait_notice(wait) is None
    assert app._process_wait_notice(event('commandExecution','foreign',thread='unrelated')) is None


def test_operator_wake_keeps_late_process_result_for_next_wait():
    app=observer()
    app._process_wait_notice(event('collabAgentToolCall','w',method='item/started',tool='wait'))
    app._process_wait_notice(event('collabAgentToolCall','w',tool='wait'))
    assert app._process_wait_notice(event('commandExecution','cmd',exitCode=7)) is None
    notice=app._process_wait_notice(event('collabAgentToolCall','w2',method='item/started',tool='wait'))
    assert notice['commands'][0]['exit_code']==7
    app._process_wait_notice({'method':'turn/completed','params':{'threadId':'root','turn':{'id':'t'}}})
    assert not app._process_waiting and not app._process_completed and not app._process_seen


def test_native_wait_notice_delivery_failure_does_not_retry_or_cancel():
    app=observer();calls=[]
    def request(method,params,**kw):
        calls.append((method,params,kw));raise TimeoutError('unknown delivery')
    app.request=request
    notice={'thread_id':'child','turn_id':'child-turn','wait_id':'w','commands':[]}
    app._deliver_process_wait_notice(notice)
    for thread in app._writers:thread.join(2)
    assert len(calls)==1
    assert calls[0][1]['threadId']=='root'
    assert calls[0][1]['expectedTurnId']=='t'
    assert 'collaboration.send_message' in calls[0][1]['input'][0]['text']
    assert calls[0][2]['stop_on_timeout'] is False
    assert not app._stopping.is_set()
    assert 'delivery_unconfirmed' in app._log_handle.getvalue()


@pytest.mark.parametrize('member',[False,True])
def test_native_completion_routes_child_via_parent_without_claiming_child_delivery(member):
    app=observer();calls=[]
    def request(method,params,**kw):
        calls.append(params)
        return {'turnId':params['expectedTurnId']}
    app.request=request
    app._deliver_process_wait_notice({'thread_id':'child' if member else 'root',
        'turn_id':'child-turn' if member else 't','wait_id':'w','commands':[]})
    for worker in app._writers:worker.join(2)
    assert len(calls)==1
    assert calls[0]['threadId']=='root' and calls[0]['expectedTurnId']=='t'
    message=calls[0]['input'][0]['text']
    log=app._log_handle.getvalue()
    if member:
        assert 'collaboration.send_message' in message and 'child' in message
        assert 'foundry_process_wait_relay_delivered_to_parent' in log
        assert '"event": "foundry_process_wait_delivered"' not in log
    else:
        assert 'collaboration.send_message' not in message
        assert 'foundry_process_wait_delivered' in log


def test_observation_only_and_old_launch_never_send_completion_input():
    app=observer()
    assert app.process_completion_wait is False
    app.observation_only=True
    app.process_completion_wait=True
    app._observe(event('commandExecution','cmd',exitCode=0))
    app._observe(event('collabAgentToolCall','w',method='item/started',tool='wait'))
    assert not app._writers and not app._process_completed


def test_empty_process_interactions_and_unknown_exit_do_not_wake_models():
    app=observer()
    app._process_wait_notice(event('collabAgentToolCall','w',method='item/started',tool='wait'))
    for i in range(20):
        assert app._process_wait_notice(event('commandExecution',str(i),exitCode=None)) is None
        assert app._process_wait_notice(event('commandExecution',str(i),exitCode=0,source='unifiedExecInteraction')) is None
    assert not app._process_completed
    assert app._process_wait_notice(event('commandExecution','actual-exit',exitCode=1))['commands'][0]['exit_code']==1


@pytest.mark.parametrize('stop',[False,True])
def test_optional_delivery_timeout_does_not_change_normal_rpc_deadlines(stop):
    app=observer()
    app._send=lambda *args,**kwargs:None
    with pytest.raises(TimeoutError):
        app.request('turn/steer',{},timeout=.01,stop_on_timeout=stop)
    assert app._stopping.is_set() is stop
    assert not app._pending


def test_repeated_usage_projections_keep_one_wait_interval():
    from foundry_runtime._foundry_goal_usage import execution_efficiency
    interval={'call_id':'w','observed_seconds':30,'response_records_in_call_window':0}
    def call(identity,with_interval):
        return {'backend':'codex','phase':'coding','call_id':identity,'observation':{
            'session_id':'s','turn_id':'t','waiting_signals':{
                'calls':[{'call_id':'w','reason':'member_result','returned':with_interval}],
                'intervals':[interval] if with_interval else []}}}
    result=execution_efficiency({}, {'observations':[call('early',False),call('late',True),call('again',True)]})
    waits=[row for observation in result['waiting_observations'] for row in observation['calls']]
    assert len(waits)==1 and waits[0]['returned'] is True
    assert waits[0]['interval']==interval
    assert result['waiting_by_reason']['member_result']['calls']==1


@pytest.mark.parametrize('message', ['Wait timed out.',
    'Wait timed out.\n\nRequested timeout of 1000ms was clamped to the minimum of 60000ms.'])
def test_observed_native_v2_timeout_label_is_not_new_evidence(message):
    from foundry_runtime._context_signals import wait_result
    assert wait_result({'timed_out':True,'message':message})=={'state':'timeout'}
    assert wait_result({'timed_out':True,'message':message,'output':'new failure evidence'}) is None
    assert wait_result({'timed_out':True,'message':message+' New member result.'}) is None
    assert wait_result({'timed_out':False,'message':'Wait interrupted by new input.'}) is None
