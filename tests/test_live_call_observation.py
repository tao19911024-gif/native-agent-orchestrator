import json
import os
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from foundry_runtime import _member_activity as activity, _executor_lifecycle as lifecycle
from foundry_runtime._goal_intervention import LiveEvents
from foundry_runtime import _followup, foundry_goal as goal


def call(tmp_path, monkeypatch, events):
    folder = tmp_path/'call'; folder.mkdir()
    (folder/'reservation.json').write_text(json.dumps({'attempt_id':'attempt','call_id':'full-id'}))
    (folder/'process.json').write_text(json.dumps({'attempt_id':'attempt','root_pid':123,'root_creation_time':456}))
    (folder/'stdout.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
    os.utime(folder/'stdout.jsonl', (1000, 1000))
    monkeypatch.setattr(lifecycle,'process_identity_exited',lambda *a: False)
    return folder


@pytest.mark.parametrize('backend,events', [
    ('claude', [{'type':'assistant','message':{'content':[{'type':'tool_use','id':'tool','name':'Bash','input':{'command':'sensitive command'}}]}}]),
    ('codex', [{'type':'item.started','item':{'type':'command_execution','id':'tool','command':'sensitive command'}}])])
def test_silent_live_tool_is_visible_without_cancellation_or_input_disclosure(tmp_path,monkeypatch,backend,events):
    folder=call(tmp_path,monkeypatch,events)
    before={p:p.read_bytes() for p in folder.iterdir()}
    result=activity.native_call_status(folder,backend,now=1400)
    assert result['activity']=='tool_pending' and result['attention_required']
    assert result['no_output_seconds']==400
    assert result['active_tools'][0]['id']=='tool'
    assert 'sensitive command' not in json.dumps(result)
    assert before=={p:p.read_bytes() for p in folder.iterdir()}


def test_heartbeat_is_not_tool_completion_or_proof_of_progress(tmp_path,monkeypatch):
    folder=call(tmp_path,monkeypatch,[{'type':'tool_progress','tool_name':'Bash',
        'tool_use_id':'tool-heartbeat-10','parent_tool_use_id':'outer-agent',
        'heartbeat':True,'elapsed_time_seconds':570}])
    result=activity.native_call_status(folder,'claude',now=1001)
    assert result['attention_required'] and result['no_output_seconds']==1
    assert result['active_tools'][0]['heartbeat_only']
    assert result['active_tools'][0]['id']=='tool'


def test_terminal_and_receipt_override_stale_tool(tmp_path,monkeypatch):
    folder=call(tmp_path,monkeypatch,[{'type':'tool_progress','tool_use_id':'tool'}, {'type':'result'}])
    result=activity.native_call_status(folder,'claude',now=2000)
    assert result['activity']=='terminal_observed' and not result['attention_required']
    (folder/'receipt.json').write_text('{}')
    assert activity.native_call_status(folder,'claude')['activity']=='receipt_available'


@pytest.mark.parametrize('backend',['claude','codex'])
def test_team_guidance_reuses_scoped_evidence_and_quiet_waits(backend):
    from foundry_runtime._compute_selection import resolve_native_team
    for managed in (False,True):
        text=resolve_native_team('adaptive',backend=backend,managed=managed)['instructions']
        assert 'Locate the named manifest or artifact first' in text
        assert 'Do not send unchanged status' in text


def test_status_observes_expert_through_existing_consultation_owner(tmp_path,monkeypatch):
    from foundry_runtime import _expert_consultation
    folder=call(tmp_path,monkeypatch,[{'type':'item.started','item':{'type':'command_execution','id':'tool'}}])
    root=tmp_path/'goal';root.mkdir()
    goal._write(root/'manifest.json',{'run_id':'r'})
    monkeypatch.setattr(_expert_consultation,'observe',lambda state:[{'calls':[
        {'outcome':None,'evidence':str(folder),'backend':'codex'}]}])
    report=_followup.status(root)
    assert report['active_native_calls'][0]['role']=='expert'
    assert report['active_native_calls'][0]['activity']=='tool_pending'


@pytest.mark.parametrize('backend,end', [('claude',{'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'tool'}]}}),
    ('codex',{'type':'item.completed','item':{'type':'command_execution','id':'tool'}})])
def test_tool_result_clears_wait(backend,end,tmp_path,monkeypatch):
    begin=({'type':'assistant','message':{'content':[{'type':'tool_use','id':'tool','name':'Bash'}]}}
        if backend=='claude' else {'type':'item.started','item':{'type':'command_execution','id':'tool'}})
    result=activity.native_call_status(call(tmp_path,monkeypatch,[begin,end]),backend,now=2000)
    assert not result['active_tools'] and not result['attention_required']


def test_partial_tail_and_process_observation_failure_do_not_fail_status(tmp_path,monkeypatch):
    folder=call(tmp_path,monkeypatch,[{'type':'tool_progress','tool_use_id':'tool','tool_name':'Bash'}])
    def denied(*args): raise OSError('access denied')
    monkeypatch.setattr(lifecycle,'process_identity_exited',denied)
    with (folder/'stdout.jsonl').open('ab') as f:f.write(b'{"type":')
    result=activity.native_call_status(folder,'claude',now=2000)
    assert result['partial_event'] and result['activity']=='tool_pending'
    if os.name=='nt': assert result['process_state']=='unknown' and result['process_observation_error']
    result=activity.native_call_status(folder,'claude',now=2000,max_bytes=24)
    assert result['snapshot_bytes']<=24 and result['truncated_history']
    assert result['activity']=='unknown'


def test_status_projects_unanswered_age_and_active_call_read_only(tmp_path,monkeypatch):
    folder=call(tmp_path,monkeypatch,[])
    root=tmp_path/'goal';root.mkdir()
    native=tmp_path/'native';native.mkdir();folder.rename(native/'call-test')
    goal._write(root/'manifest.json',{'run_id':'r','manager_state':{'native_dir':str(native),'backend':'claude'},
        'unanswered_live_events':[{'item_id':'new','received_at':1000},{'item_id':'legacy'}]})
    before=(root/'manifest.json').read_bytes()
    report=_followup.status(root)
    assert report['unanswered_event_ages'][0]['waiting_seconds']>0
    assert report['unanswered_event_ages'][1]['waiting_seconds'] is None
    assert report['active_native_calls'][0]['role']=='manager'
    assert (root/'manifest.json').read_bytes()==before


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_reserved_call_and_process_elapsed_are_separate_from_coordination(tmp_path, monkeypatch, backend):
    folder = call(tmp_path, monkeypatch, [])
    identity = json.loads((folder/'process.json').read_bytes())
    (folder/'process.json').unlink()
    before = {p: p.read_bytes() for p in folder.iterdir()}
    reserved = activity.native_call_status(folder, backend, now=5000)
    assert reserved['activity'] == 'process_not_observed'
    assert reserved['call_id'] == 'full-id'
    assert reserved['process_state'] == 'unknown'
    assert {p: p.read_bytes() for p in folder.iterdir()} == before
    identity['observed_utc'] = '1970-01-01T01:23:00+00:00'
    (folder/'process.json').write_text(json.dumps(identity))
    running = activity.native_call_status(folder, backend, now=5000)
    assert running['process_elapsed_seconds'] == 20
    live = {'state': 'running', 'stage': 'coordinate', 'observed_elapsed_seconds': 1400}
    observed = _followup._manager_wait(live, [{'role': 'manager', **running}])
    assert observed['coordination_elapsed_seconds'] == 1400
    assert observed['native_calls'][0]['process_elapsed_seconds'] == 20
    assert observed['deadlock'] == 'not_established'


@pytest.mark.parametrize('rows,expected', [
    ([], 'coordination_without_active_native_evidence'),
    ([{'activity': 'process_not_observed'}], 'reserved_process_not_observed'),
    ([{'process_state': 'unknown'}], 'native_call_unconfirmed'),
    ([{'process_state': 'exists', 'no_output_seconds': 99999}], 'native_call_in_flight'),
    ([{'process_state': 'exists', 'activity': 'terminal_observed'}], 'native_terminal_receipt_pending'),
    ([{'process_state': 'exited'}], 'process_exited_receipt_pending'),
    ([{'activity': 'receipt_available'}], 'coordination_without_active_native_evidence'),
])
def test_manager_wait_does_not_turn_silence_or_terminal_into_deadlock(rows, expected):
    rows = [{'role': 'manager', **row} for row in rows]
    result = _followup._manager_wait({'state': 'running'}, rows)
    assert result['state'] == expected
    assert result['deadlock'] == 'not_established'
    assert _followup._manager_wait({'state': 'decision_available'}, [])['state'] == 'no_active_call_observed'


def test_goal_status_includes_wait_projection(tmp_path, monkeypatch):
    root = tmp_path/'goal'; root.mkdir()
    goal._write(root/'manifest.json', {'run_id': 'r', 'live_manager_call': {
        'state': 'running', 'started_at': 1000, 'stage': 'coordinate', 'soft_warning_seconds': 120}})
    before = (root/'manifest.json').read_bytes()
    report = _followup.status(root)
    assert report['manager_wait_observation']['state'] == 'coordination_without_active_native_evidence'
    assert report['live_manager_call']['soft_warning']
    assert (root/'manifest.json').read_bytes() == before


def test_coalesce_only_identical_unanswered_messages_then_allow_new_ruling():
    started,release=Event(),Event();seen=[]
    def decide(rows):
        seen.extend(rows);started.set();assert release.wait(5)
    events=LiveEvents(decide)
    app=SimpleNamespace(outcome_ready=Event(),turn_id='turn',steer=lambda *a:None,_log=lambda *a:None)
    def ask(ident,text='FOUNDRY_MANAGER_QUESTION decision?'):
        events.observe({'method':'item/completed','params':{'threadId':'root','turnId':'turn',
            'item':{'type':'agentMessage','id':ident,'text':text}}},'root')
    ask('1');ask('2');events.drain(app)
    assert started.wait(2)
    ask('3');ask('4','FOUNDRY_MANAGER_QUESTION decision? new evidence')
    assert events.events.qsize()==1
    release.set();events.pending.result(timeout=5);events.drain(app)
    events.pending.result(timeout=5);events.drain(app)
    ask('5');events.drain(app);events.close(app)
    assert len(seen)==3 and all(row['received_at']>0 for row in seen)
