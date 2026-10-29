import pytest

from foundry_runtime import _compute_selection as compute, _tool_discipline as rules
from foundry_runtime._execution_usage import _team_arguments
from foundry_runtime._foundry_cost import estimate
from foundry_runtime._model_catalogue import tier_for_model
import json


@pytest.mark.parametrize('tool,field', [('Agent', 'prompt'), ('spawn_agent', 'message')])
def test_declared_deliverable_uses_same_rules_for_both_providers(tool, field):
    def check(model, text, version=2):
        return rules.check({'tool_name': tool, 'tool_input': {'model': model, field: text}}, {'version': version})
    for model in ('sonnet', 'gpt-99-sol', 'opus', 'gpt-99-astra'):
        assert check(model, '[activity: investigate] 查找符号，不要实现功能')
    for model in ('haiku', 'claude-haiku-99', 'gpt-99-luna'):
        assert not check(model, '[activity: investigate] 查找符号，返回 file:line 和摘录')
    assert not check('sonnet', '[activity: execute] 阅读调用链，然后修复实现')
    assert not check('opus', '[activity: execute] [decision: 修复资金事务所有权] 阅读调用链并修改')
    assert check('fable', '[activity: expert] [decision: 事务所有权]')
    assert not check('fable', '[activity: expert] [expert: 事务所有权冲突如何解决]')
    assert check('sonnet', '[activity: execute]\n[activity: investigate] ambiguous')
    assert not check('sonnet', '[activity: execute] Implement this. Handoff examples include [activity: investigate].')
    assert not check('fable', '[decision: historical expert question]', version=1)
    assert not check('sonnet', 'legacy lookup', version=1)


@pytest.mark.parametrize('team,kind,wanted', [(None, 'small', 'sonnet'), ('adaptive', 'small', 'sonnet'),
                                          (None, 'cross-module', 'sonnet'), (None, 'ui-live', 'sonnet')])
def test_implementation_stays_sonnet_with_separate_coordination(monkeypatch, team, kind, wanted):
    monkeypatch.setattr(compute, 'model', lambda tier: 'claude-' + tier + '-99')
    options, record = compute.resolve_goal_profile('claude', backend='claude', coding_backend='claude',
                                                 team_profile=team, task_kind=kind)
    assert options['coding_model'] == 'claude-' + wanted + '-99'
    assert options['auto_amend_model'] == options['coding_model']
    options, _ = compute.resolve_goal_profile('claude', backend='claude', coding_backend='claude',
        coding_model='claude-opus-5-5', coding_effort='high', team_profile=team, task_kind=kind)
    assert options['coding_model'] == 'claude-opus-5-5'


def test_unknown_model_and_undeclared_activity_remain_unknown():
    assert tier_for_model('gpt-99.2-luna') == 'luna'
    assert tier_for_model('claude-opus-5-5[1m]') == 'opus'
    assert tier_for_model('custom-opus-like') is None
    assert 'declared_activity' not in _team_arguments(json.dumps({'message': 'encrypted-or-legacy'}))
    row = _team_arguments(json.dumps({'message': '[activity: review] private brief'}))
    assert row['declared_activity'] == 'review'
    assert 'private brief' not in json.dumps(row)


def test_new_price_bounds_do_not_treat_cumulative_tokens_as_request_sizes():
    metrics = {k: {'observed_sum': v} for k, v in dict(input_tokens=200000, output_tokens=1000,
        cache_read_input_tokens=1000000, cache_creation_input_tokens=2000).items()}
    haiku = estimate('claude-haiku-5-5', metrics, backend='claude')
    assert haiku['usd_min'] == pytest.approx(.03075)
    assert haiku['usd_max'] == pytest.approx(.1545)
    assert estimate('claude-fable-5-1', metrics, backend='claude')['usd_min'] == pytest.approx(2.325)
    assert estimate('future-unknown', metrics, backend='claude')['usd_min'] is None


def test_code_mode_reads_are_compact_and_blocked_outputs_are_not_reads():
    from foundry_runtime._context_signals import tool_record, reading_signals
    records = []
    for identity, output in [('one', '中文\nsource'), ('two', 'Command blocked by PreToolUse hook'),
                             ('three', '中文\nsource')]:
        call = tool_record({'type': 'custom_tool_call', 'name': 'exec', 'call_id': identity,
            'input': 'const r=await tools.exec_command({cmd:"rg 示例应用 src"});text(r.output);'})
        result = tool_record({'type': 'custom_tool_call_output', 'call_id': identity,
            'output': [{'type': 'input_text', 'text': output}]})
        records += [{'type': 'tool_observation', 'payload': {**r, 'turn_id': 't'}} for r in (call, result)]
    stats = reading_signals(records, 't')
    assert stats['code_mode_calls'] == 3
    assert stats['observed_read_outputs'] == 2
    assert stats['blocked_outputs'] == 1
    assert stats['identical_call_output_repeats'] == 1
    assert '示例应用' not in json.dumps(records, ensure_ascii=False)
    assert reading_signals(records, 'another')['tool_calls'] == 0


def test_dynamic_code_mode_commands_remain_unclassified():
    from foundry_runtime._context_signals import tool_record
    row = tool_record({'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'x',
                      'input': 'await tools.exec_command({cmd:variable});'})
    assert not row['read_candidate'] and row['literal_commands'] == 0


def test_nested_output_envelope_counts_decoded_lines():
    from foundry_runtime._context_signals import tool_record
    row = tool_record({'type': 'custom_tool_call_output', 'call_id': 'x', 'output': [
        {'type': 'input_text', 'text': json.dumps({'result': {'status': 'fulfilled', 'value': {
            'output': '中文\nline2\nline3', 'exit_code': 0}}})}]})
    assert row['lines'] == 3
    assert not row['truncated']


def test_native_snapshot_attributes_tools_without_retaining_source(tmp_path):
    from datetime import datetime, timezone
    from foundry_runtime._execution_usage import _snapshot
    from foundry_runtime._context_signals import reading_signals
    stamp = '2026-10-10T00:00:00Z'
    rows = [dict(type='session_meta', timestamp=stamp, payload={'id': 'fixture'}),
        dict(type='event_msg', timestamp=stamp, payload={'type': 'task_started', 'turn_id': 't'}),
        dict(type='response_item', timestamp=stamp, payload={'type': 'function_call', 'name': 'exec_command',
            'call_id': 'read', 'arguments': json.dumps({'cmd': 'Get-Content 中文.py'})}),
        dict(type='response_item', timestamp=stamp, payload={'type': 'function_call_output', 'call_id': 'read',
            'output': json.dumps({'output': 'private source\nsecond line'})}),
        dict(type='event_msg', timestamp=stamp, payload={'type': 'task_complete', 'turn_id': 't'})]
    path = tmp_path / 'rollout.jsonl'
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
    _, observed, source = _snapshot(path, datetime(2026, 10, 9, tzinfo=timezone.utc),
                                   datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert 'private source' not in json.dumps(observed)
    assert source['snapshot_bytes'] == path.stat().st_size
    assert reading_signals(observed, 't')['observed_read_outputs'] == 1
    assert reading_signals(observed, 't')['read_output_lines'] == 2
