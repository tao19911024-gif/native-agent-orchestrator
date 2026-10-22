import json

import pytest

from foundry_runtime._goal_intervention import LiveEvents
from foundry_runtime._context_signals import tool_record, waiting_signals
from foundry_runtime._tool_discipline import check
from test_claude_live_events import bridge, assistant
from test_efficiency_observation import parse, message


@pytest.mark.parametrize('tool', ['Bash', 'PowerShell', 'exec_command'])
def test_compound_config_search_uses_index_database(tmp_path, tool):
    import sqlite3
    (tmp_path/'.codegraph').mkdir()
    (tmp_path/'justfile').write_text('python := "python"')
    with sqlite3.connect(tmp_path/'.codegraph/codegraph.db') as db:
        db.execute('create table files(path text)')
    command = "#decide: inspect configuration\ngrep -nE '^(python|venv)\\s*:?=' justfile | head; grep -nA8 '^ci-static' justfile"
    event = {'tool_name': tool, 'cwd': str(tmp_path), 'tool_input': {'command': command}}
    policy = {'index_available': True, 'index_workspace': str(tmp_path)}
    assert check(event, policy) == {}
    with sqlite3.connect(tmp_path/'.codegraph/codegraph.db') as db:
        db.execute("insert into files values ('justfile')")
    assert check(event, policy)  # Real indexed content still follows existing rule.
    event['tool_input']['command'] = 'rg pattern justfile; rg other src'
    assert check(event, policy)


def test_wait_timeouts_do_not_invalidate_pending_question_but_changed_results_do():
    live = LiveEvents(lambda rows: None)
    def event(identity, kind, **extra):
        live.observe({'method': 'item/completed', 'params': {'threadId': 'root', 'turnId': 't',
            'item': {'id': identity, 'type': kind, **extra}}}, 'root')
    event('q1', 'agentMessage', text='FOUNDRY_MANAGER_QUESTION owner?')
    for i in range(20):
        event(str(i), 'collabAgentToolCall', tool='wait', agentsStates={})
        event('q' + str(i+2), 'agentMessage', text='FOUNDRY_MANAGER_QUESTION owner?')
    assert live.events.qsize() == 1 and live.evidence_generation == 0
    event('new', 'collabAgentToolCall', tool='wait', agentsStates={'child': {'status': 'completed', 'message': 'done'}})
    event('q-new', 'agentMessage', text='FOUNDRY_MANAGER_QUESTION owner?')
    event('repeat', 'collabAgentToolCall', tool='wait', agentsStates={'child': {'status': 'completed', 'message': 'done'}})
    event('q-repeat', 'agentMessage', text='FOUNDRY_MANAGER_QUESTION owner?')
    assert live.events.qsize() == 2 and live.evidence_generation == 1
    event('future', 'unknownFutureTool', result='new fact')
    assert live.evidence_generation == 2
    event('failed-wait', 'collabAgentToolCall', tool='wait', status='failed', agentsStates={})
    event('unknown-wait', 'collabAgentToolCall', tool='wait')
    assert live.evidence_generation == 4


@pytest.mark.parametrize('error', [False, True])
def test_claude_wait_timeout_uses_shared_events_and_keeps_errors(error):
    live = bridge()
    live.observe(assistant('q1'))
    live.observe({'type': 'assistant', 'message': {'content': [
        {'type': 'tool_use', 'id': 'wait', 'name': 'TaskOutput'}]}})
    live.observe({'type': 'user', 'message': {'content': [
        {'type': 'tool_result', 'tool_use_id': 'wait', 'is_error': error,
         'content': json.dumps({'retrieval_status': 'timeout'})}]}})
    live.observe(assistant('q2'))
    assert live.live_events.events.qsize() == (2 if error else 1)


@pytest.mark.parametrize('field', ['task', 'result', 'message', 'errors'])
def test_timeout_with_nested_or_additional_evidence_remains_actionable(field):
    from foundry_runtime._context_signals import wait_result
    value = {'retrieval_status': 'timeout', field: {'output': 'new diagnostic evidence'}}
    assert wait_result(value) is None
    live = bridge()
    live.observe(assistant('q1'))
    live.observe({'type': 'assistant', 'message': {'content': [
        {'type': 'tool_use', 'id': 'wait', 'name': 'TaskOutput'}]}})
    live.observe({'type': 'user', 'message': {'content': [
        {'type': 'tool_result', 'tool_use_id': 'wait', 'content': json.dumps(value)}]}})
    live.observe(assistant('q2'))
    assert live.live_events.events.qsize() == 2


@pytest.mark.parametrize('backend', ['claude', 'codex'])
@pytest.mark.parametrize('change', ['none', 'scope', 'cwd', 'failure'])
def test_scoped_index_reason_reuses_native_success_only(tmp_path, backend, change):
    cwd = str(tmp_path)
    args = {'command': 'rg first src\n#idx: 索引缺少未提交符号', 'workdir': cwd}
    if backend == 'claude':
        rows = [{'cwd': cwd, 'message': {'content': [{'type': 'tool_use', 'id': 'x', 'name': 'Bash', 'input': args}]}},
                {'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'x', 'is_error': change == 'failure', 'content': 'found'}]}}]
    else:
        rows = [{'payload': {'type': 'function_call', 'name': 'functions.exec_command', 'call_id': 'x', 'arguments': json.dumps(args)}},
                {'payload': {'type': 'function_call_output', 'call_id': 'x', 'output': 'blocked by policy' if change == 'failure' else 'found'}}]
    transcript = tmp_path/'native.jsonl'
    transcript.write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
    event = {'tool_name': 'Bash' if backend == 'claude' else 'exec_command',
        'tool_input': {'command': 'rg second ' + ('other' if change == 'scope' else 'src'),
                       'workdir': str(tmp_path/'other') if change == 'cwd' else cwd},
        'transcript_path': str(transcript)}
    assert bool(check(event, {'index_available': True})) == (change != 'none')


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_wait_duration_and_timeout_are_observed_without_token_estimation(tmp_path, backend):
    start, end = '2026-10-18T12:00:00Z', '2026-10-18T12:00:45Z'
    output = json.dumps({'timed_out': True, 'status': {}})
    if backend == 'codex':
        rows = [{'timestamp': start, 'payload': tool_record({'type': 'function_call', 'name': 'collaboration.wait_agent', 'call_id': 'w', 'arguments': '{}'})},
                {'timestamp': end, 'payload': tool_record({'type': 'function_call_output', 'call_id': 'w', 'output': output})}]
        result = waiting_signals(rows)
    else:
        result = parse(tmp_path, [{**message({}, content=[{'type': 'tool_use', 'id': 'w', 'name': 'TaskOutput'}]), 'timestamp': start},
            {'type': 'user', 'agent_id': 'reader', 'timestamp': end, 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'w', 'content': output}]}}])['waiting_signals']
    assert result['calls'][0]['observed_seconds'] == 45
    assert result['calls'][0]['result'] == 'timeout'
    assert result['elapsed_seconds'] is None  # Concurrent waits cannot be summed.


def test_stage_costs_reference_existing_rows_and_wait_summary_reconciles():
    from foundry_runtime._foundry_goal_usage import execution_efficiency
    row = {'role': 'manager', 'model': 'test', 'backend': 'claude',
           'token_breakdown': {'ordinary_input': 12}, 'usd_min': 1, 'usd_max': 2}
    def observation(returned, **extra):
        return {'backend': 'codex', 'phase': 'coding', 'call_id': 's/t', 'observation': {
            'session_id': 's', 'turn_id': 't', 'waiting_signals': {'calls': [
                {'call_id': 'w', 'reason': 'member_result', 'returned': returned, **extra}]}}}
    result = execution_efficiency({'api_price_equivalent': {'rows': [row]}}, {'observations': [
        observation(False), observation(True, result='timeout', observed_seconds=45)]})
    assert result['stage_costs'][0]['token_breakdown'] == row['token_breakdown']
    assert result['stage_costs'][0]['api_price_equivalent_usd'] == {'min': 1, 'max': 2}
    assert result['waiting_by_reason']['member_result'] == {
        'calls': 1, 'returned': 1, 'timeouts': 1, 'timed_calls': 1, 'max_observed_seconds': 45}
