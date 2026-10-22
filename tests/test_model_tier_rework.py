import json

import pytest

from foundry_runtime import _tool_discipline as rules, _tool_history as history, _launch_context as launch
from test_manager_input_wait import manager_io, external


@pytest.mark.parametrize('tool,tier', [('Agent', 'sonnet'), ('Agent', 'opus'),
                                      ('spawn_agent', 'sol'), ('spawn_agent', 'astra')])
@pytest.mark.parametrize('brief', ['find callers of X', '[decision: expensive decision] find callers of X'])
def test_unmarked_lookup_cannot_buy_a_higher_tier(tool, tier, brief):
    result = rules.check({'tool_name': tool, 'tool_input': {'model': tier, 'prompt': brief}}, {'version': 4})
    assert 'Pure lookup' in result['hookSpecificOutput']['permissionDecisionReason']


def test_execution_and_unknown_work_continue_and_reader_can_repair_brief():
    assert rules.spawn_reason('sonnet', 'find callers and fix implementation', {'version': 4}) is None
    assert rules.spawn_reason('sonnet', '[activity: review] read diff', {'version': 4}) is None
    assert rules.spawn_reason('future-model', 'future work kind', {'version': 4}) is None
    assert rules.spawn_reason('sonnet', 'find callers', {'version': 3}) is None
    for tier in ('haiku', 'luna'):
        assert 'file:line' in rules.spawn_reason(tier, 'find callers', {'version': 4})
        assert rules.spawn_reason(tier, 'find callers; return file:line plus quotes', {'version': 4}) is None


@pytest.mark.parametrize('backend', ['claude', 'codex'])
@pytest.mark.parametrize('command', ['head -300 source.txt', 'tail -n 300 source.txt',
                                    'git show sha:source.txt', 'grep -rn needle .',
                                    "awk 'NR<500' source.txt", 'rg needle . | head -500',
                                    'git diff', 'tail -n +10 source.txt', 'git log -p -3',
                                    'Get-Content source.txt | Select-Object -Skip 0 -First 100'])
def test_expanded_read_output_meter_and_continuation(tmp_path, backend, command):
    # Actual output is used for opaque historical/git/search reads. No shell is run.
    output = '中文证据\n' * 450
    if backend == 'claude':
        rows = [{}, {'type': 'user', 'message': {'content': 'task'}},
                {'message': {'content': [{'type': 'tool_use', 'id': 'r', 'name': 'Bash', 'input': {'command': command}}]}},
                {'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'r', 'content': output}]}}]
    else:
        rows = [{}, {'type': 'event_msg', 'payload': {'type': 'task_started'}},
                {'payload': {'type': 'function_call', 'call_id': 'r', 'name': 'exec_command', 'arguments': json.dumps({'cmd': command})}},
                {'payload': {'type': 'function_call_output', 'call_id': 'r', 'output': output}}]
    log = tmp_path / 'native.jsonl'
    log.write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
    policy = {'version': 5, 'root_role': 'manager', 'reading_budget': {'unit': 'lines', 'single': 150, 'turn': 400}}
    event = {'tool_name': 'Bash', 'tool_input': {'command': 'git diff HEAD'}, 'transcript_path': str(log)}
    assert 'Observed 450' in history.reading_check(event, policy)
    event['tool_input']['command'] += '\n#decide: 核验关键修改'
    assert history.reading_check(event, policy) is None
    event['tool_input'] = {'command': 'git diff HEAD'}
    event['agent_id'] = 'member'
    assert history.reading_check(event, policy) is None


def test_head_predicted_size_and_mixed_commands(tmp_path):
    (tmp_path / 'source.txt').write_text('line\n' * 400, encoding='utf-8')
    event = {'tool_name': 'Bash', 'tool_input': {'command': 'head -300 source.txt'}, 'cwd': str(tmp_path)}
    assert 'budget' in history.reading_check(event, {'version': 4, 'root_role': 'manager',
        'reading_budget': {'unit': 'lines', 'single': 150, 'turn': 400}})
    assert not history.observed_read('Bash', {'command': 'echo rg; pytest'})
    assert not history.observed_read('Bash', {'command': 'git diff; pytest'})


def test_read_calibration_is_configured_then_frozen(monkeypatch):
    monkeypatch.setenv('FOUNDRY_CLAUDE_READ_SINGLE', '190')
    monkeypatch.setenv('FOUNDRY_CODEX_READ_TURN', '55000')
    launch.validate_project_launch({'FOUNDRY_CLAUDE_READ_SINGLE': 190, 'FOUNDRY_CODEX_READ_TURN': 55000})
    claude = launch.capture(enable_index=False, backend='claude')
    codex = launch.capture(enable_index=False, backend='codex')
    monkeypatch.setenv('FOUNDRY_CLAUDE_READ_SINGLE', 'broken')
    assert claude['tool_discipline']['reading_budget']['single'] == 190
    assert launch.capture(enable_index=False, backend='claude')['tool_discipline']['reading_budget']['single'] == 150
    assert codex['tool_discipline']['reading_budget']['turn'] == 55000


@pytest.mark.parametrize('backend,initial', [('claude', 'sonnet'), ('codex', 'sol')])
def test_contradiction_escalation_returns_to_recorded_baseline(monkeypatch, backend, initial):
    from foundry_runtime import _compute_selection as compute
    from foundry_runtime._foundry_goal_auto import correction_options
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    baseline = {'model': compute.model(initial), 'effort': 'medium'}
    decision = compute.escalation(baseline['model'], {}, findings=[
        {'severity': 'P1', 'file': 'ledger.py', 'summary': '证据冲突: contract and stored output disagree'}])
    assert decision['action'] == 'escalate'
    state = compute.tier_state({}, baseline, decision)
    cleared = compute.tier_state(state, {'model': decision['model'], 'effort': 'high'}, None)
    options = correction_options({'compute_assignment': {'profile_version': 5},
        'execution_decision': cleared, 'coding_model': decision['model']}, {})
    assert options['coding_model'] == baseline['model']
    assert options['coding_effort'] == 'medium'
    assert 'tier_escalation' not in cleared


def test_claude_member_ledger_deduplicates_responses_and_keeps_unknowns(tmp_path):
    from foundry_runtime._context_signals import claude_stream
    from foundry_runtime._foundry_goal_usage import team_execution
    path = tmp_path / 'stream.jsonl'
    usage = {'input_tokens': 11, 'cache_creation_input_tokens': 3, 'cache_read_input_tokens': 100, 'output_tokens': 7}
    spawn = {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'spawn-1', 'name': 'Agent',
        'input': {'prompt': '[activity: investigate] find callers; file:line plus quotes'}}]}}
    member = {'type': 'assistant', 'parent_tool_use_id': 'spawn-1', 'agent_id': 'reader-1',
        'message': {'id': 'response-1', 'model': 'claude-haiku-5-5', 'usage': usage}}
    path.write_text('\n'.join(map(json.dumps, [spawn, member, member])), encoding='utf-8')
    observation = {'session_id': 's', 'context_signals': claude_stream(path)}
    coding = {'observations': [{'backend': 'claude', 'observation': observation}] * 2}
    result = team_execution(coding, {'backend': 'claude'})
    row = result['model_usage'][0]
    assert row['member_id'] == 'reader-1' and row['tier'] == 'haiku'
    assert row['initial_assignment_activity'] == 'investigate'
    assert row['usage']['input_tokens'] == 11 and row['response_count'] == 1
    assert result['member_turns_observed'] is None
    member['message']['usage'] = {**usage, 'input_tokens': 99}
    path.write_text(json.dumps(member), encoding='utf-8')
    coding['observations'].append({'backend': 'claude', 'observation': {'session_id': 's', 'context_signals': claude_stream(path)}})
    assert team_execution(coding, {'backend': 'claude'})['model_usage'] == []


@pytest.mark.parametrize('binding', ['session_id', 'turn_binding', 'startup_header'])
def test_resume_reuses_session_bound_launch_without_policy_upgrade(tmp_path, monkeypatch, binding):
    from foundry_runtime import _dispatch_package as package
    frozen = {'settings': {'tool_tokens': 123}, 'server': None, 'tool_discipline': {'version': 2}}
    row = {'tag': 'TASK', 'intent_id': 'id', 'attempt_id': 'attempt', 'written_utc': '2026-10-10T00:00:00Z', 'launch_context': frozen}
    (tmp_path / 'dispatch.package.json').write_text(json.dumps(row), encoding='utf-8')
    record = {'attempt_id': 'attempt'}
    if binding == 'session_id':
        record['session_id'] = 'original'
    elif binding == 'turn_binding':
        record['turn_binding'] = {'thread_id': 'original'}
    else:
        record['frozen'] = {'launcher': 'exec'}
        (tmp_path / 'dispatch.log').write_text('OpenAI Codex vfixture\n--------\nsession id: original\n--------\n', encoding='utf-8')
    monkeypatch.setattr(package.ExecutionIntent, 'read_history', lambda *a, **k: (record, None, None))
    retained = package.retained_launch(tmp_path, tmp_path, 'TASK', 'original')
    assert retained == frozen
    assert package.retained_launch(tmp_path, tmp_path, 'TASK', 'other') is None
    retained['tool_discipline']['version'] = 4
    assert package.retained_launch(tmp_path, tmp_path, 'TASK', 'original')['tool_discipline']['version'] == 2


def test_member_review_excludes_recorded_coder_and_allows_another(tmp_path):
    log = tmp_path / 'session.jsonl'
    rows = [{}, {'payload': {'type': 'function_call', 'call_id': 'spawn', 'name': 'spawn_agent',
        'arguments': json.dumps({'model': 'sol', 'message': '[activity: execute] implement ledger'})}},
        {'payload': {'type': 'function_call_output', 'call_id': 'spawn', 'output': 'spawned'}}]
    log.write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
    event = {'tool_name': 'spawn_agent', 'tool_input': {'model': 'sol', 'message': '[activity: review] review ledger'},
             'transcript_path': str(log)}
    assert 'Reviewer matches' in rules.check(event, {'version': 4})['hookSpecificOutput']['permissionDecisionReason']
    event['tool_input']['model'] = 'sonnet'
    assert rules.check(event, {'version': 4}) == {}
    event['tool_input'] = {'model': 'sol', 'message': '[activity: execute] Fix the review findings in ledger.py'}
    assert rules.check(event, {'version': 4}) == {}
    event['tool_input']['message'] = 'Fix the review findings in ledger.py'
    assert rules.check(event, {'version': 4}) == {}


@pytest.mark.parametrize('brief', ['[activity: review] Review the fix; do not edit any files.',
                                 'Review the fix; do not edit any files.'])
def test_review_only_assignment_does_not_claim_authorship(tmp_path, brief):
    log = tmp_path / 'session.jsonl'
    rows = [{}, {'payload': {'type': 'function_call', 'call_id': 'spawn', 'name': 'spawn_agent',
        'arguments': json.dumps({'model': 'sol', 'message': brief})}},
        {'payload': {'type': 'function_call_output', 'call_id': 'spawn', 'output': 'spawned'}}]
    log.write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
    assert history.member_review_models(log) == set()


@pytest.mark.parametrize('tier', ['sonnet', 'sol'])
def test_recurrence_survives_turns_but_cleared_failure_resets_tier(monkeypatch, tier):
    from foundry_runtime import _compute_selection as compute
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    baseline = {'model': compute.model(tier), 'effort': 'medium'}
    def turn(n, failures, current=baseline['model']):
        return {'session_id': 's', 'turn_id': str(n), 'work_observation': {'test_failures': [
            {'test_id': f, 'call_id': str(n), 'model': current} for f in failures]}}
    turns = [turn(1, ['assertion']), turn(2, ['assertion'])]
    evidence = compute.execution_evidence({'managed_turns': turns})
    decision = compute.escalation(baseline['model'], evidence)
    assert decision and decision['action'] == 'escalate'
    state = compute.tier_state({}, baseline, decision)
    turns.append(turn(3, ['assertion'], decision['model']))
    evidence = compute.execution_evidence({'managed_turns': turns})
    assert compute.escalation(decision['model'], evidence) is None
    assert compute.tier_state(state, {}, None, evidence=evidence) == state
    turns.append(turn(4, []))
    evidence = compute.execution_evidence({'managed_turns': turns})
    assert len(evidence['test_failures']) == 3  # Retain audit history.
    assert compute.escalation(baseline['model'], evidence) is None
    assert compute.tier_state(state, {}, None, evidence=evidence)['tier_reset']['model'] == baseline['model']
    turns.append(turn(5, ['assertion']))
    evidence = compute.execution_evidence({'managed_turns': turns})
    assert len(evidence['active_test_failures']) == 1
    assert compute.escalation(baseline['model'], evidence) is None


def test_manager_tier_decisions_are_in_frozen_response_contract():
    from foundry_runtime._foundry_response_contract import response_contract_for_request, allocation_contract
    request = {'kind': 'coordinate', 'member_tier_decisions': ['failure-1'], 'operator_input': True}
    contract = response_contract_for_request(request)
    assert 'member_tier_decisions' in contract['required']
    assert contract['properties']['member_tier_decisions']['required'] == ['failure-1']
    assert allocation_contract({**request, 'message_recipients': None}) == contract


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_manager_records_tier_answer_and_replays_without_model_call(external, manager_io, tmp_path, backend):
    from foundry_runtime import _foundry_manager as manager
    requests, decisions = manager_io
    decisions.append({'action': 'delegate', 'instruction': 'Apply the changed assignment using existing team controls.',
        'member_tier_decisions': {'failure-1': {'action': 'escalate', 'reason': 'same assertion failed twice'}}})
    state = {'native_dir': str(tmp_path / 'manager'), 'backend': backend, 'model': 'fixture', 'effort': 'high'}
    packet = {'stage': 'worker_returned', 'member_tier_decisions': [{'decision_id': 'failure-1', 'model': 'next'}]}
    result = manager.turn(state, external[0], packet)
    assert result[0]['member_tier_decisions']['failure-1']['action'] == 'escalate'
    assert manager.turn(state, external[0], packet)[0] == result[0]
    assert len(requests) == 1
