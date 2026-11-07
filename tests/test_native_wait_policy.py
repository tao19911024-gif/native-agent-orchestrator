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
    assert 'When no member owns outstanding work, use the retained command handle' in team['instructions']


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


def observe_process(app, message):
    return app.member_activity.observe(message, 1, thread_id=app.thread_id, turn_id=app.turn_id)


def event(item_type, identity, *, method='item/completed', thread='root',turn='t',**extra):
    return {'method':method,'params':{'threadId':thread,'turnId':turn,
        'item':{'id':identity,'type':item_type,**({'processId':'native-process'} if item_type=='commandExecution' else {}),**extra}}}


@pytest.mark.parametrize('thread',['root','child'])
def test_native_process_completion_wakes_only_its_waiting_owner(thread):
    app=observer()
    wait=event('collabAgentToolCall','w',method='item/started',thread=thread,tool='wait')
    start=event('commandExecution','cmd',method='item/started',thread=thread,status='inProgress')
    done=event('commandExecution','cmd',thread=thread,status='completed',exitCode=0)
    assert observe_process(app, start) is None
    assert observe_process(app, wait) is None
    notice=observe_process(app, done)
    assert notice=={'thread_id':thread,'turn_id':'t','wait_id':'w',
                    'commands':[{'item_id':'cmd','process_id':'native-process','status':'completed','exit_code':0}]}
    assert observe_process(app, done) is None
    assert observe_process(app, wait) is None
    assert observe_process(app, event('commandExecution','foreign',thread='unrelated')) is None


@pytest.mark.parametrize('after_operator_wake', [False, True])
def test_completed_command_is_not_replayed_on_a_later_wait(after_operator_wake):
    app=observer()
    start=event('commandExecution','cmd',method='item/started',status='inProgress',source='unifiedExecStartup')
    observe_process(app, start)
    if after_operator_wake:
        observe_process(app, event('collabAgentToolCall','w',method='item/started',tool='wait'))
        observe_process(app, event('collabAgentToolCall','w',tool='wait'))
    assert observe_process(app, event('commandExecution','cmd',exitCode=7,status='failed',
        source='unifiedExecStartup',aggregatedOutput='command result already returned')) is None
    assert observe_process(app, event('collabAgentToolCall','w2',method='item/started',tool='wait')) is None
    observe_process(app, {'method':'turn/completed','params':{'threadId':'root','turn':{'id':'t'}}})
    assert not app.member_activity.waiting


def test_quiet_team_wait_prompts_root_once_after_existing_progress_threshold():
    app = observer()
    app.process_completion_wait = True
    notices = []
    app._deliver_idle_wait_notice = notices.append
    app._notification(event('collabAgentToolCall', 'root-wait', method='item/started', tool='wait'), observed_at=1)
    app._notification(event('collabAgentToolCall', 'child-wait', method='item/started',
                            thread='child', tool='wait'), observed_at=60)
    app._notification(event('collabAgentToolCall', 'child-wait', method='item/started',
                            thread='child', tool='wait'), observed_at=60)
    app._publish_idle_wait(359)
    assert not notices
    app._publish_idle_wait(360)
    app._publish_idle_wait(500)
    assert len(notices) == 1 and notices[0]['members'] == ['child']
    assert notices[0]['wait_id'] == 'root-wait'


@pytest.mark.parametrize('kind', ['wait', 'command'])
def test_replayed_child_item_does_not_postpone_quiet_notification(kind):
    app = observer()
    app.process_completion_wait = True
    notices = []
    app._deliver_idle_wait_notice = notices.append
    for thread in ('root', 'child'):
        app._notification(event('collabAgentToolCall', thread + '-wait', method='item/started',
                                thread=thread, tool='wait'), observed_at=1)
    original = (event('collabAgentToolCall', 'child-wait', method='item/started', thread='child', tool='wait')
                if kind == 'wait' else event('commandExecution', 'result', thread='child',
                    command='pytest selected.py', aggregatedOutput='passed', exitCode=0, status='completed'))
    app._notification(original, observed_at=1)
    for now in (100, 200, 290):
        app._notification(original, observed_at=now)
    app._publish_idle_wait(300)
    assert not notices
    app._publish_idle_wait(301)
    assert len(notices) == 1
    assert app.member_activity.members['child']['last'] == 1


@pytest.mark.parametrize('kind', ['new_item', 'delta'])
def test_new_child_activity_retains_its_quiet_interval(kind):
    app = observer()
    app.process_completion_wait = True
    notices = []
    app._deliver_idle_wait_notice = notices.append
    for thread in ('root', 'child'):
        app._notification(event('collabAgentToolCall', thread + '-wait', method='item/started',
                                thread=thread, tool='wait'), observed_at=1)
    message = (event('agentMessage', 'new-message', thread='child', text='New evidence')
               if kind == 'new_item' else {'method': 'item/reasoning/textDelta',
                   'params': {'threadId': 'child', 'turnId': 't', 'itemId': 'reasoning', 'delta': 'New analysis'}})
    app._notification(message, observed_at=290)
    app._publish_idle_wait(301)
    assert not notices
    app._publish_idle_wait(590)
    assert len(notices) == 1


def test_idle_wait_notice_uses_root_without_interrupting_native_turn():
    app = observer()
    calls = []
    def request(method, params, **kwargs):
        calls.append((method, params, kwargs))
        return {'turnId': 't'}
    app.request = request
    app._deliver_idle_wait_notice({'thread_id': 'root', 'turn_id': 't', 'wait_id': 'root-wait',
                                  'members': ['child'], 'quiet_seconds': 300})
    for writer in app._writers:
        writer.join(2)
    [(method, params, options)] = calls
    assert method == 'turn/steer' and params['threadId'] == 'root' and params['expectedTurnId'] == 't'
    assert options == {'stop_on_timeout': False}
    assert 'foundry_idle_wait_delivered_to_root' in app._log_handle.getvalue()
    assert not app._stopping.is_set()


@pytest.mark.parametrize('had_member', [False, True])
def test_root_wait_without_active_members_still_checks_retained_work(had_member):
    app = observer()
    app.process_completion_wait = True
    if had_member:
        app._notification(event('subAgentActivity', 'done', agentThreadId='child', kind='completed'), observed_at=1)
    else:
        app.member_activity.members.clear()
    idle, completed = [], []
    app._deliver_idle_wait_notice = idle.append
    app._deliver_process_wait_notice = completed.append
    app._notification(event('commandExecution', 'test-result', status='completed', exitCode=1), observed_at=2)
    app._notification(event('collabAgentToolCall', 'root-only', method='item/started', tool='wait'), observed_at=3)
    app._publish_idle_wait(3)
    assert len(idle) == 1 and idle[0]['quiet_seconds'] == 0
    app._publish_idle_wait(900)
    assert len(idle) == 1 and idle[0]['members'] == []
    assert idle[0]['wait_id'] == 'root-only' and not completed


def test_root_only_wait_does_not_publish_while_its_command_runs():
    app = observer()
    app.process_completion_wait = True
    app.member_activity.members.clear()
    idle = []
    app._deliver_idle_wait_notice = idle.append
    app._notification(event('commandExecution', 'background', method='item/started', status='inProgress'), observed_at=1)
    app._notification(event('collabAgentToolCall', 'root-only', method='item/started', tool='wait'), observed_at=2)
    app._publish_idle_wait(1802)
    assert not idle


def test_background_completion_does_not_also_send_an_empty_wait_notice():
    app = observer()
    app.process_completion_wait = True
    app.member_activity.members.clear()
    idle, completed = [], []
    app._deliver_idle_wait_notice = idle.append
    app._deliver_process_wait_notice = completed.append
    app._notification(event('commandExecution', 'background', method='item/started',
                            status='inProgress'), observed_at=1)
    app._notification(event('collabAgentToolCall', 'w', method='item/started', tool='wait'), observed_at=2)
    app._publish_idle_wait(2)
    assert not idle and not completed
    app._notification(event('commandExecution', 'background', status='completed', exitCode=0), observed_at=3)
    app._publish_idle_wait(3)
    app._publish_idle_wait(303)
    assert len(completed) == 1 and not idle
    app._notification(event('collabAgentToolCall', 'w', tool='wait'), observed_at=304)
    app._notification(event('collabAgentToolCall', 'next', method='item/started', tool='wait'), observed_at=305)
    app._publish_idle_wait(305)
    assert len(idle) == 1 and idle[0]['wait_id'] == 'next'
    assert len(completed) == 1


@pytest.mark.parametrize('pending', ['request', 'unanswered', 'manager_queued', 'manager_pending'])
def test_empty_wait_preserves_input_ownership_until_input_is_handled(pending):
    app = observer()
    app.process_completion_wait = True
    app.member_activity.members.clear()
    idle = []
    app._deliver_idle_wait_notice = idle.append
    if pending == 'request':
        app._input_requests['decision'] = {}
    elif pending == 'unanswered':
        app._unanswered_input = True
    elif pending == 'manager_queued':
        app.live_events.events.put({'kind': 'worker_message'})
    else:
        app.live_events.pending = {'kind': 'worker_message'}
    app._notification(event('collabAgentToolCall', 'w', method='item/started', tool='wait'), observed_at=1)
    app._publish_idle_wait(1)
    app._publish_idle_wait(1801)
    assert not idle and not app.member_activity.idle_notified
    app._input_requests.clear()
    app._unanswered_input = False
    app.live_events.pending = None
    if not app.live_events.events.empty():
        app.live_events.events.get_nowait()
    app._publish_idle_wait(1802)
    assert len(idle) == 1


@pytest.mark.parametrize('changed', [False, True])
def test_empty_wait_rearms_for_changed_work_without_repeating_unchanged_results(changed):
    app = observer()
    app.process_completion_wait = True
    app.member_activity.members.clear()
    idle = []
    app._deliver_idle_wait_notice = idle.append
    for index in range(2):
        app._notification(event('commandExecution', f'cmd{index}', status='completed', exitCode=0,
            command='pytest selected.py', aggregatedOutput='passed' if changed and index else 'failed'),
            observed_at=index * 10)
        app._notification(event('collabAgentToolCall', f'w{index}', method='item/started', tool='wait'),
                          observed_at=index * 10 + 1)
        app._publish_idle_wait(index * 10 + 1)
        app._notification(event('collabAgentToolCall', f'w{index}', tool='wait'), observed_at=index * 10 + 2)
    assert len(idle) == (2 if changed else 1)


def test_known_member_starts_another_turn_and_can_wake_the_root():
    app = observer()
    app.process_completion_wait = True
    notices = []
    app._deliver_idle_wait_notice = notices.append
    app._notification(event('subAgentActivity', 'spawn', agentThreadId='child', kind='started'), observed_at=1)
    app._notification(event('subAgentActivity', 'return', agentThreadId='child', kind='completed'), observed_at=2)
    assert not app.member_activity.members
    for thread in ('unrelated', 'child'):
        app._notification({'method': 'turn/started', 'params': {'threadId': thread,
                           'turn': {'id': 'new-turn', 'status': 'inProgress'}}}, observed_at=3)
    assert app.member_activity.members.keys() == {'child'}
    app._notification(event('collabAgentToolCall', 'root-wait', method='item/started', tool='wait'), observed_at=4)
    app._publish_idle_wait(304)
    assert not notices
    app._notification(event('collabAgentToolCall', 'child-wait', method='item/started',
                            thread='child', turn='new-turn', tool='wait'), observed_at=305)
    app._publish_idle_wait(605)
    assert [notice['wait_id'] for notice in notices] == ['root-wait']


def test_quiet_wait_rearms_after_edit_but_not_after_wait_or_duplicate_edit():
    app = observer()
    app.process_completion_wait = True
    notices = []
    app._deliver_idle_wait_notice = notices.append

    def wait(identity, start):
        for thread in ('root', 'child'):
            app._notification(event('collabAgentToolCall', identity + thread, method='item/started',
                                    thread=thread, tool='wait'), observed_at=start)
        app._publish_idle_wait(start + 301)
        for thread in ('root', 'child'):
            app._notification(event('collabAgentToolCall', identity + thread,
                                    thread=thread, tool='wait'), observed_at=start + 302)

    wait('first', 1)
    wait('repeat', 310)
    assert [notice['wait_id'] for notice in notices] == ['firstroot']
    edit = event('fileChange', 'edit', thread='child', status='completed')
    app._notification(edit, observed_at=620)
    wait('after-edit', 621)
    app._notification(edit, observed_at=930)
    wait('after-replay', 931)
    assert [notice['wait_id'] for notice in notices] == ['firstroot', 'after-editroot']


def test_resumed_member_ignores_old_terminal_and_clears_parent_reported_work():
    app = observer()
    app.process_completion_wait = True
    app._notification(event('subAgentActivity', 'spawn', agentThreadId='child', kind='started'), observed_at=1)
    for turn, now in [('old', 2), ('new', 4)]:
        app._notification({'method': 'turn/started', 'params': {'threadId': 'child',
                          'turn': {'id': turn, 'status': 'inProgress'}}}, observed_at=now)
        app._notification(event('commandExecution', turn, method='item/started',
                                thread='child', turn=turn, status='inProgress'), observed_at=now + 1)
    app._notification({'method': 'turn/completed', 'params': {'threadId': 'child',
                      'turn': {'id': 'old', 'status': 'completed'}}}, observed_at=6)
    assert app.member_activity.members.keys() == {'child'}
    assert app.member_activity.running == {('child', 'new', 'new')}
    app._notification(event('subAgentActivity', 'returned', agentThreadId='child', kind='completed'), observed_at=7)
    assert not app.member_activity.members and not app.member_activity.running


def test_changed_command_result_rearms_wait_without_rearming_repeated_result():
    app = observer()
    app.process_completion_wait = True
    notices = []
    app._deliver_idle_wait_notice = notices.append
    for index, output in enumerate(('failed test A', 'failed test A', 'passed test A')):
        now = index * 1000
        app._notification(event('commandExecution', 'test-' + str(index), thread='child',
                                command='pytest selected.py', aggregatedOutput=output,
                                exitCode=0, status='completed'), observed_at=now)
        for thread in ('root', 'child'):
            app._notification(event('collabAgentToolCall', str(index) + thread, method='item/started',
                                    thread=thread, tool='wait'), observed_at=now + 1)
        app._publish_idle_wait(now + 301)
    assert [notice['wait_id'] for notice in notices] == ['0root', '2root']


@pytest.mark.parametrize('work', ['command', 'member', 'operator', 'manager'])
def test_team_wait_leaves_unfinished_work_with_its_existing_owner(work):
    app = observer()
    app.process_completion_wait = True
    notices = []
    app._deliver_idle_wait_notice = notices.append
    if work == 'command':
        app._notification(event('commandExecution', 'running', method='item/started',
                                thread='child', status='inProgress'), observed_at=1)
    elif work == 'member':
        app.member_activity.members['active-member'] = {'status': 'running'}
    elif work == 'operator':
        app._input_requests['ruling'] = {}
    else:
        app.live_events.events.put({'kind': 'worker_message'})
    for thread in ('root', 'child'):
        app._notification(event('collabAgentToolCall', thread + '-wait', method='item/started',
                                thread=thread, tool='wait'), observed_at=2)
    app._publish_idle_wait(1801)
    assert not notices


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
    assert not app._writers and not app.member_activity.waiting


def test_empty_process_interactions_and_unknown_exit_do_not_wake_models():
    app=observer()
    observe_process(app, event('collabAgentToolCall','w',method='item/started',tool='wait'))
    for i in range(20):
        assert observe_process(app, event('commandExecution',str(i),exitCode=None)) is None
        assert observe_process(app, event('commandExecution',str(i),exitCode=0,source='unifiedExecInteraction')) is None
    observe_process(app, event('commandExecution','actual-exit',method='item/started',status='inProgress'))
    assert observe_process(app, event('commandExecution','actual-exit',exitCode=1))['commands'][0]['exit_code']==1


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
