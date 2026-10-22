import json
import pytest

from foundry_runtime._context_signals import claude_stream, waiting_signals
from foundry_runtime._foundry_goal_usage import execution_efficiency, team_execution


def message(usage, member='reader', **extra):
    return {'type': 'assistant', 'agent_id': member, 'timestamp': '2026-10-17T12:00:00Z',
            'message': {'id': 'response', 'model': 'claude-test', 'usage': usage, **extra}}


def parse(tmp_path, rows):
    path = tmp_path/'stream.jsonl'
    path.write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
    before = path.read_bytes()
    result = claude_stream(path)
    assert path.read_bytes() == before
    return result


def test_claude_segmented_response_is_one_request_with_late_output(tmp_path):
    usage = dict(input_tokens=100, cache_read_input_tokens=400,
                 cache_creation_input_tokens=30, output_tokens=2)
    first = message(usage)
    last = message({**usage, 'output_tokens': 17})
    result = parse(tmp_path, [first, last, first, last])
    assert len(result['response_usage']) == 1
    assert result['response_usage'][0]['usage'] == {**usage, 'output_tokens': 17}
    assert result['members']['reader']['responses'] == 1
    assert result['members']['reader']['max_context_tokens'] == 530
    coding = {'observations': [{'backend': 'claude', 'observation': {
        'session_id': 's', 'context_signals': result}}]}
    assert team_execution(coding, {'backend': 'claude'})['model_usage'][0]['usage']['output_tokens'] == 17


def test_raw_stream_deltas_and_complete_blocks_do_not_double_count(tmp_path):
    usage = dict(input_tokens=9, cache_read_input_tokens=3, cache_creation_input_tokens=0, output_tokens=1)
    rows = [{'type': 'stream_event', 'event': {'type': 'message_start', 'message': message(usage)['message']}},
            {'type': 'stream_event', 'event': {'type': 'message_delta', 'usage': {'output_tokens': 21}}},
            message({**usage, 'output_tokens': 21}, member='leader'),
            {'type': 'stream_event', 'event': {'type': 'message_stop'}}]
    result = parse(tmp_path, rows)
    assert len(result['response_usage']) == 1
    assert result['response_usage'][0]['usage']['output_tokens'] == 21
    assert result['response_usage'][0]['usage']['input_tokens'] == 9


def test_conflicting_request_inputs_remain_unknown_after_later_duplicate(tmp_path):
    result = parse(tmp_path, [message({'input_tokens': 3}), message({'input_tokens': 4}),
                              message({'input_tokens': 3})])
    row = result['response_usage'][0]
    assert row['usage']['input_tokens'] is None
    assert row['conflicting_fields'] == ['input_tokens']
    assert row['usage']['output_tokens'] is None


def test_member_identity_scopes_same_response_id(tmp_path):
    result = parse(tmp_path, [message({'output_tokens': 7}, 'a'), message({'output_tokens': 8}, 'b')])
    coding = {'observations': [{'backend': 'claude', 'observation': {'session_id': 's', 'context_signals': result}}]}
    assert sorted(r['usage']['output_tokens'] for r in team_execution(coding, {'backend': 'claude'})['model_usage']) == [7, 8]


def test_late_native_agent_identity_reconciles_parent_tool_identity(tmp_path):
    early = message({'input_tokens': 10, 'output_tokens': 2})
    early.pop('agent_id')
    early['parent_tool_use_id'] = 'spawn'
    late = {**message({'input_tokens': 10, 'output_tokens': 9}), 'parent_tool_use_id': 'spawn'}
    result = parse(tmp_path, [early, late])
    assert len(result['response_usage']) == 1
    assert result['response_usage'][0]['member_id'] == 'reader'
    assert result['response_usage'][0]['usage']['output_tokens'] == 9


def test_identity_reconciliation_preserves_conflicting_counter(tmp_path):
    early = message({'input_tokens': 10})
    early.pop('agent_id')
    early['parent_tool_use_id'] = 'spawn'
    late = {**message({'input_tokens': 11}), 'parent_tool_use_id': 'spawn'}
    conflict = {**message({'input_tokens': 12}), 'parent_tool_use_id': 'spawn'}
    result = parse(tmp_path, [early, late, conflict])
    assert result['response_usage'][0]['usage']['input_tokens'] is None


@pytest.mark.parametrize('tool,reason', [('wait_agent', 'member_result'), ('TaskOutput', 'background_tool_result'),
                                      ('write_stdin', 'process_io'), ('AskUserQuestion', 'operator_input')])
def test_wait_purpose_is_not_elapsed_time_or_completion(tool, reason):
    call = {'payload': {'kind': 'call', 'call_id': 'x', 'tool': tool, 'turn_id': 't'}}
    rows = [call, call, {'payload': {'kind': 'output', 'call_id': 'x', 'turn_id': 't'}}]
    result = waiting_signals(rows, 't')
    assert result['calls'] == [{'call_id': 'x', 'tool': tool, 'reason': reason, 'returned': True}]
    assert result['elapsed_seconds'] is None
    assert not waiting_signals(rows, 'other')['calls']


def test_claude_user_tool_result_matches_wait(tmp_path):
    result = parse(tmp_path, [message({}, content=[{'type': 'tool_use', 'id': 'tool', 'name': 'TaskOutput'}]),
        {'type': 'user', 'agent_id': 'reader', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'tool'}]}}])
    assert result['waiting_signals']['calls'][0]['returned'] is True


def test_late_agent_id_also_reconciles_wait_result(tmp_path):
    call = message({}, content=[{'type': 'tool_use', 'id': 'wait', 'name': 'TaskOutput'}])
    call.pop('agent_id')
    call['parent_tool_use_id'] = 'spawn'
    result = parse(tmp_path, [call, {'type': 'user', 'agent_id': 'reader', 'parent_tool_use_id': 'spawn',
        'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'wait'}]}}])
    assert result['waiting_signals']['calls'] == [{'call_id': 'reader/wait', 'tool': 'TaskOutput',
        'reason': 'background_tool_result', 'member': 'reader', 'returned': True}]


def test_report_reuses_cost_rows_and_exposes_missing_legacy_waits():
    report = {'api_price_equivalent': {'rows': [{'role': 'claude coding/manager sessions',
              'roles': ['coding', 'manager'], 'backend': 'claude', 'model': 'test'}]}}
    coding = {'observations': [{'backend': 'codex', 'phase': 'coding', 'call_id': 's/t',
              'observation': {'session_id': 's', 'turn_id': 't', 'usage': {'input_tokens': 10}}}]}
    result = execution_efficiency(report, coding)
    assert result['stage_costs'][0]['attribution'] == 'shared_session'
    assert result['stage_costs'][0]['cost_row_index'] == 0
    assert result['member_usage'][0]['usage']['input_tokens'] == 10
    assert result['gaps'] == [{'kind': 'waiting_unobserved', 'call_id': 's/t'}]
    assert result['coverage'] == 'partial'


def test_later_wait_result_enriches_one_retained_call():
    def call(returned):
        return {'backend': 'claude', 'phase': 'coding', 'call_id': 's/a', 'observation': {
            'session_id': 's', 'context_signals': {'waiting_signals': {'calls': [
                {'call_id': 'reader/wait', 'tool': 'TaskOutput', 'reason': 'background_tool_result',
                 'returned': returned}]}}}}
    result = execution_efficiency({}, {'observations': [call(False), call(True)]})
    waits = [w for row in result['waiting_observations'] for w in row['calls']]
    assert len(waits) == 1 and waits[0]['returned'] is True
