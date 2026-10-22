import io
import json
import sys
from types import SimpleNamespace
from threading import Event

import pytest

from foundry_runtime import _dispatch_claude as transport
from foundry_runtime._goal_intervention import live_scope, LiveEvents
from foundry_runtime._executor_lifecycle import OwnedProcess, SpawnFailure
from foundry_runtime._member_activity import MemberActivity
from test_manager_recovery import conversation
from foundry_runtime import _foundry_manager as manager


def assistant(identity='question', text='FOUNDRY_MANAGER_QUESTION 中文证据?', **extra):
    return {'type': 'assistant', 'uuid': identity, 'session_id': 'session',
            'message': {'id': identity, 'content': [{'type': 'text', 'text': text}], **extra}}


def bridge(decide=lambda rows: '经理答复', capture=None):
    with live_scope(decide, capture=capture, claude=True):
        return transport.ClaudeLive('session', io.StringIO(), {'settings': {'compact_tokens': 100000}})


def test_ordinary_heartbeats_and_child_text_do_not_call_manager():
    live = bridge()
    for i in range(100):
        live.observe({'type': 'tool_progress', 'heartbeat': True, 'elapsed_time_seconds': i})
        live.observe(assistant(str(i), 'still waiting'))
        live.observe({**assistant(str(i)), 'parent_tool_use_id': 'child'})
    assert live.live_events.events.empty()
    live.observe(assistant())
    live.observe(assistant())
    live.observe(assistant('another-id'))
    assert live.live_events.events.qsize() == 1
    live.observe({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'tool'}]}})
    live.observe(assistant('new-evidence'))
    assert live.live_events.events.qsize() == 2


def test_compaction_uses_shared_residual_rearm_and_skips_recovered_alert():
    live = bridge()
    live.observe({'type': 'system', 'subtype': 'compact_boundary', 'uuid': 'compact'})
    live.observe(assistant('usage', 'ordinary output', usage={
        'input_tokens': 10000, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 75000}))
    assert live.live_events.events.qsize() == 1
    live.observe(assistant('lower', 'ordinary output', usage={
        'input_tokens': 10000, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 10000}))
    live.live_events.drain(live)
    assert live.live_events.pending is None
    assert 'measured_context_recovery' in live.human.getvalue()


def test_answer_awaiting_turn_boundary_still_coalesces_identical_questions():
    live = bridge()
    live.observe(assistant())
    live.live_events.drain(live)
    live.live_events.pending.result(timeout=5)
    live.live_events.drain(live)
    assert len(live.outbox) == 1
    live.observe(assistant('same-before-delivery'))
    assert live.live_events.events.empty()
    live.observe({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'changed'}]}})
    live.observe(assistant('changed-before-delivery'))
    assert live.live_events.events.qsize() == 1
    live.close()


@pytest.mark.parametrize('failure', ['manager', 'delivery', 'terminal'])
def test_failures_and_terminal_race_retain_unanswered_rows(failure):
    retained = []
    def decide(rows):
        if failure == 'manager':
            raise RuntimeError('quota or unknown failure')
        return 'reply'
    live = bridge(decide, retained.extend)
    live.observe(assistant())
    live.live_events.drain(live)
    try:
        live.live_events.pending.result(timeout=5)
    except RuntimeError:
        pass
    if failure == 'terminal':
        live.close()
    else:
        live.live_events.drain(live)
        if failure == 'delivery':
            class Broken:
                def write(self, text): raise BrokenPipeError('closed')
                def close(self): pass
            live.result_ready = True
            live.advance(SimpleNamespace(stdin=Broken()))
        live.close()
    assert retained and all(row['text'] == 'FOUNDRY_MANAGER_QUESTION 中文证据?' for row in retained)


def test_observation_failure_retains_native_bytes_and_continues(tmp_path):
    path = tmp_path / 'stdout.jsonl'
    path.write_text(json.dumps(assistant()) + '\n', encoding='utf-8')
    def broken(row): raise ValueError('unknown shape')
    rows = MemberActivity().read(path, 0, on_event=broken)
    assert rows[0]['signal'] == 'live_event_observation_unavailable'
    assert json.loads(path.read_text()) == assistant()


@pytest.mark.parametrize('manager_backend', ['claude', 'codex'])
def test_owned_claude_stream_roundtrip_and_cleanup(conversation, tmp_path, monkeypatch, manager_backend):
    state, native_calls, failures = conversation
    failures.clear()
    state['backend'] = manager_backend
    calls, cleanups = [], []
    def decide(rows):
        calls.append(rows)
        decision, _, _, _ = manager.turn(state, tmp_path, {'stage': 'worker_running', 'events': rows})
        return decision['instruction'] + ' 经理：继续，中文答复'
    monkeypatch.setattr(transport, 'resolve_executable', lambda _: sys.executable)
    def argv(executable, session, **kwargs):
        script = '''
import sys,json
def emit(row):
    row['session_id']=SESSION
    print(json.dumps(row,ensure_ascii=False),flush=True)
emit({'type':'system','subtype':'init'})
for index,line in enumerate(sys.stdin):
    message=json.loads(line)
    emit(message)
    if index==0:
        emit({'type':'assistant','uuid':'question','message':{'content':[{'type':'text','text':'FOUNDRY_MANAGER_QUESTION 中文证据?'}]}})
        emit({'type':'assistant','uuid':'duplicate','message':{'content':[{'type':'text','text':'FOUNDRY_MANAGER_QUESTION 中文证据?'}]}})
        for n in range(100): emit({'type':'tool_progress','heartbeat':True})
    else:
        assert '经理：继续，中文答复' in message['message']['content']
    emit({'type':'result','result_index':index,'subtype':'success','is_error':False,'result':'question' if index==0 else '中文已处理'})
'''.replace('SESSION', repr(session))
        return [executable, '-B', '-c', script]
    monkeypatch.setattr(transport, 'argv', argv)
    owner = SimpleNamespace(OwnedProcess=OwnedProcess, SpawnFailure=SpawnFailure,
                            _record_cleanup=lambda lease, receipt, path: cleanups.append(receipt))
    lease = SimpleNamespace(attempt_id='claude-stream-fixture', require_reserved=lambda *a: None,
                            bind_process=lambda *a: None)
    log, out = tmp_path / 'dispatch.log', tmp_path / 'output.txt'
    with live_scope(decide, claude=True):
        assert transport.run(owner, '中文任务', tmp_path, log, out, 'FIXTURE',
                             lease=lease, on_started=lambda sid: None) == 0
    assert len(calls) == 1
    assert len(native_calls) == 1
    assert out.read_text(encoding='utf-8') == '中文已处理'
    assert cleanups[0].cleanup_verified and cleanups[0].job_active_processes == 0
    proof = json.loads(log.with_suffix('.terminal.json').read_bytes())
    assert proof['input_format'] == 'stream-json'
    result = transport.verify_terminal(proof, session=proof['session_id'], returncode=0, attempt_id=lease.attempt_id)
    assert result['result_index'] == 1
    with pytest.raises(ValueError, match='session evidence'):
        transport.read_result(log.with_suffix('.claude.jsonl'))  # Legacy contract is unchanged.


def test_late_codex_delivery_is_retained_by_same_queue():
    rows = []
    events = LiveEvents(lambda batch: 'reply')
    events.capture = rows.extend
    events.events.put({'kind': 'worker_message', 'text': 'question'})
    app = SimpleNamespace(outcome_ready=Event(), turn_id='turn', steer=lambda *a: None, _log=lambda *a: None)
    events.drain(app)
    events.pending.result(timeout=5)
    app.outcome_ready.set()
    events.close(app)
    assert rows == [{'kind': 'worker_message', 'text': 'question'}]


def test_recorded_protocol_guidance_and_legacy_transport(monkeypatch):
    from foundry_runtime._foundry_native_request import build_prompt
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    launch = {'settings': {'context_tokens': 100000, 'compact_tokens': 80000, 'compact_percent': 90,
                          'bash_chars': 1000, 'tool_tokens': 1000}, 'server': None}
    plain = transport.argv(sys.executable, 'session', launch_context=launch)
    live = transport.argv(sys.executable, 'session', launch_context=launch, live=True)
    assert '--input-format' not in plain and '--replay-user-messages' not in plain
    assert live[live.index('--input-format') + 1] == 'stream-json'
    assert 'after your current native turn returns' in live[live.index('--append-system-prompt') + 1]
    request = {'kind': 'coordinate', 'live_coordination': 1, 'packet': {'stage': 'worker_running'}}
    assert b'existing same-turn steer channel.' in build_prompt(request, 'claude', ValueError)
    request['claude_live_events'] = 1
    for backend in ('claude', 'codex'):
        assert b'Claude after its current native turn returns' in build_prompt(request, backend, ValueError)


def test_mismatched_terminal_closes_input_for_existing_validation():
    live = bridge()
    stream = io.StringIO()
    live.observe({'type': 'result', 'session_id': 'different'})
    live.advance(SimpleNamespace(stdin=stream))
    assert stream.closed and live.closed
