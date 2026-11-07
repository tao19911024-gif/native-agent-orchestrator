"""Efficiency advice does not replace execution and review admission."""
import json

import pytest

from foundry_runtime import _launch_context as launch, _tool_discipline as rules


@pytest.mark.parametrize('name,field', [('Agent', 'prompt'), ('spawn_agent', 'message')])
@pytest.mark.parametrize('model,brief', [
    ('sol', 'find callers of X'),
    ('haiku', 'find callers of X'),
    ('astra', '[activity: execute] repair a cross-module race'),
])
def test_cost_and_brief_heuristics_advise_new_launches(name, field, model, brief):
    event = {'tool_name': name, 'tool_input': {'model': model, field: brief}}
    old = rules.check(event, {'version': 6})['hookSpecificOutput']
    assert old['permissionDecision'] == 'deny'
    new = rules.check(event, {'version': 7})['hookSpecificOutput']
    assert 'permissionDecision' not in new
    assert 'Efficiency guidance:' in new['additionalContext']


def test_index_and_read_advice_do_not_deny_scoped_work(tmp_path):
    (tmp_path / 'source.txt').write_text('line\n' * 400, encoding='utf-8')
    policy = {'version': 7, 'root_role': 'manager', 'index_available': True,
              'index_workspace': str(tmp_path),
              'reading_budget': {'unit': 'lines', 'single': 150, 'turn': 400}}
    for command in ('rg needle source.txt', 'head -300 source.txt'):
        event = {'tool_name': 'Bash', 'tool_input': {'command': command}, 'cwd': str(tmp_path)}
        assert rules.check(event, {**policy, 'version': 6})['hookSpecificOutput']['permissionDecision'] == 'deny'
        new = rules.check(event, policy)['hookSpecificOutput']
        assert 'permissionDecision' not in new and 'Efficiency guidance:' in new['additionalContext']


def test_duplicate_source_read_remains_visible_as_advice(tmp_path):
    source = tmp_path / 'source.txt'
    source.write_text('line\n' * 10, encoding='utf-8')
    transcript = tmp_path / 'native.jsonl'
    transcript.write_text('\n'.join(map(json.dumps, [
        {'type': 'user', 'message': {'content': 'task'}},
        {'message': {'content': [{'type': 'tool_use', 'id': 'r', 'name': 'Read',
                                 'input': {'file_path': str(source), 'offset': 1, 'limit': 10}}]}},
        {'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'r', 'content': 'line\n' * 10}]}}
    ])), encoding='utf-8')
    event = {'tool_name': 'Read', 'tool_input': {'file_path': str(source), 'offset': 1, 'limit': 10},
             'cwd': str(tmp_path), 'transcript_path': str(transcript)}
    assert rules.check(event, {'version': 6})['hookSpecificOutput']['permissionDecision'] == 'deny'
    new = rules.check(event, {'version': 7})['hookSpecificOutput']
    assert 'permissionDecision' not in new and 'Efficiency guidance:' in new['additionalContext']


@pytest.mark.parametrize('model,brief', [
    ('sol', '[activity: review] find callers of X'),
    ('astra', '[activity: review] inspect candidate correctness'),
])
def test_cost_advice_cannot_bypass_coding_model_review_exclusion(model, brief):
    event = {'tool_name': 'spawn_agent', 'tool_input': {'model': model, 'message': brief}}
    result = rules.check(event, {'version': 7, 'recorded_coding_model': model})['hookSpecificOutput']
    assert result['permissionDecision'] == 'deny'
    assert 'Reviewer matches' in result['permissionDecisionReason']


def test_explicit_model_and_storage_rules_remain_required(monkeypatch):
    monkeypatch.setenv('TEMP', 'C:/Users/Fixture/AppData/Local/Temp')
    for event in (
        {'tool_name': 'Agent', 'tool_input': {'prompt': 'Implement the assigned change'}},
        {'tool_name': 'Write', 'tool_input': {'file_path': '/tmp/unowned.txt'}},
    ):
        assert rules.check(event, {'version': 7})['hookSpecificOutput']['permissionDecision'] == 'deny'


@pytest.mark.parametrize('version,role,excluded', [(7, 'executor', True), (6, 'executor', False), (7, 'manager', False)])
def test_executor_and_member_coding_assignments_both_exclude_native_reviewers(tmp_path, version, role, excluded):
    transcript = tmp_path / 'native.jsonl'
    transcript.write_text('\n'.join(map(json.dumps, [
        {'type': 'event_msg', 'payload': {'type': 'task_started'}},
        {'payload': {'type': 'function_call', 'call_id': 'member', 'name': 'spawn_agent',
                     'arguments': json.dumps({'model': 'terra', 'message': '[activity: execute] implement one independent slice'})}},
        {'payload': {'type': 'function_call_output', 'call_id': 'member', 'output': 'member started'}}
    ])), encoding='utf-8')
    event = {'tool_name': 'spawn_agent', 'transcript_path': str(transcript),
             'tool_input': {'model': 'sol', 'message': '[activity: review] inspect the combined candidate'}}
    policy = {'version': version, 'root_role': role, 'recorded_coding_model': 'sol'}
    result = rules.check(event, policy).get('hookSpecificOutput', {})
    assert (result.get('permissionDecision') == 'deny') is excluded
    event['tool_input']['model'] = 'terra'
    assert rules.check(event, policy)['hookSpecificOutput']['permissionDecision'] == 'deny'
    event['tool_input']['model'] = 'sonnet'
    assert rules.check(event, policy) == {}


def test_capture_freezes_advisory_policy_without_rewriting_retained_policy():
    fresh = launch.capture(enable_index=False)
    assert fresh['tool_discipline']['version'] == 7
    retained = {'settings': fresh['settings'], 'server': None,
                'tool_discipline': {'version': 6, 'index_available': False}}
    original = json.dumps(retained, sort_keys=True)
    launch.codex_settings(launch=retained)
    launch.claude_args(launch=retained)
    assert json.dumps(retained, sort_keys=True) == original
