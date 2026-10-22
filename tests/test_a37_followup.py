import json
from pathlib import Path
import sys

import pytest

from foundry_runtime import _tool_discipline as rules, _tool_history as history
from foundry_runtime import _launch_context as launch, _compute_selection as compute
from test_manager_input_wait import external, manager_io
from test_manager_recovery import conversation


@pytest.mark.parametrize('command,offset', [('tail -n +1 source.txt', 0),
                                         ('tail -n +201 source.txt', 200),
                                         ('tail -n+0 source.txt', 0)])
def test_tail_start_line_is_not_last_n_lines(tmp_path, command, offset):
    (tmp_path / 'source.txt').write_text('中文\n' * 400, encoding='utf-8')
    read = history.read_request('Bash', {'command': command}, tmp_path, expanded=True, followup=True)
    assert read['offset'] == offset and read['count'] is None
    assert read['lines'] == 400 - offset
    event = {'tool_name': 'Bash', 'tool_input': {'command': command}, 'cwd': str(tmp_path)}
    assert 'budget' in history.reading_check(event, {'version': 5, 'root_role': 'manager',
        'reading_budget': {'unit': 'lines', 'single': 150, 'turn': 400}})
    assert history.reading_check(event, {'version': 4, 'root_role': 'manager',
        'reading_budget': {'unit': 'lines', 'single': 150, 'turn': 400}}) is None


def test_git_log_meter_does_not_upgrade_frozen_v4(tmp_path):
    rows = [{}, {'type': 'event_msg', 'payload': {'type': 'task_started'}},
        {'payload': {'type': 'function_call', 'call_id': 'r', 'name': 'exec_command',
                     'arguments': json.dumps({'cmd': 'git log -p -3'})}},
        {'payload': {'type': 'function_call_output', 'call_id': 'r', 'output': 'line\n' * 450}}]
    log = tmp_path / 'native.jsonl'
    log.write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
    event = {'tool_name': 'Bash', 'tool_input': {'command': 'git diff'}, 'transcript_path': str(log)}
    policy = {'version': 5, 'root_role': 'manager', 'reading_budget': {'unit': 'lines', 'single': 150, 'turn': 400}}
    assert 'Observed 450' in history.reading_check(event, policy)
    assert history.reading_check(event, {**policy, 'version': 4}) is None


@pytest.mark.parametrize('tool,model,alternative', [('Agent', 'sonnet', 'sol'), ('spawn_agent', 'sol', 'sonnet')])
def test_missing_member_history_uses_frozen_coding_assignment(tool, model, alternative):
    policy = launch.capture(enable_index=False, coding_model=model)['tool_discipline']
    event = {'tool_name': tool, 'tool_input': {'model': model, 'prompt': '[activity: review] review changes'}}
    assert 'fallback' in rules.check(event, policy)['hookSpecificOutput']['permissionDecisionReason']
    event['tool_input']['model'] = alternative
    assert rules.check(event, policy) == {}
    event['tool_input']['model'] = model
    assert rules.check(event, {**policy, 'version': 4}) == {}
    assert rules.check(event, {'version': 5}) == {}  # Unknown does not strand execution.


@pytest.mark.parametrize('tool', ['Agent', 'spawn_agent'])
@pytest.mark.parametrize('total', [99999, 100000, 140000])
def test_planned_haiku_inputs_warn_without_refusing(tool, total):
    event = {'tool_name': tool, 'tool_input': {'model': 'haiku',
        'prompt': f'find callers; file:line plus quotes [planned_input_tokens: {total}]'}}
    result = rules.check(event, {'version': 5})
    if total >= 100000:
        output = result['hookSpecificOutput']
        assert str(total) in output['additionalContext'] and 'Split' in output['additionalContext']
        assert 'permissionDecision' not in output
    else:
        assert result == {}
    assert rules.check(event, {'version': 4}) == {}


def test_large_brief_warns_as_estimate_not_measured_context():
    result = rules.check({'tool_name': 'Agent', 'tool_input': {'model': 'haiku',
        'prompt': 'find callers; file:line plus quotes\n' + '中文资料' * 25000}}, {'version': 5})
    assert 'brief estimate' in result['hookSpecificOutput']['additionalContext']
    assert 'unknown inherited' in result['hookSpecificOutput']['additionalContext']


@pytest.mark.parametrize('phrase', ['Contradictory evidence', 'evidence conflict', '证据矛盾', '证据冲突'])
def test_contradiction_retains_exact_trigger(monkeypatch, phrase):
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    decision = compute.escalation(compute.model('sol'), {}, findings=[{
        'file': 'ledger.py', 'severity': 'P1', 'summary': phrase + ': two results disagree'}])
    assert decision['contradiction_matches'] == [{'file': 'ledger.py',
        'summary': phrase + ': two results disagree', 'matched_phrase': phrase}]


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_goal_manager_launch_retains_coding_model(external, manager_io, tmp_path, monkeypatch, backend):
    from foundry_runtime import foundry_goal as goal
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    _, scope, order, _ = external
    requests, decisions = manager_io
    decisions.extend([{'action': 'delegate', 'instruction': 'Implement once'},
                      {'action': 'await_input', 'instruction': 'Confirm fixture'}])
    state = {'backend': backend, 'model': None, 'effort': None, 'last_call_id': None,
             'native_dir': str(tmp_path / 'manager')}
    # The fixture worker is a local Python process, not a provider request.
    selected = compute.model('sol')
    result = goal.run_goal(order, tmp_path / 'goal', Path(sys.executable), manager_state=state,
                          coding_launcher='exec', coding_model=selected, coding_effort='high',
                          project_record=scope.record())
    assert result['status'] == 'awaiting_input'
    assert len(requests) == 2
    assert all('recorded_coding_model' not in r['packet'] for r in requests)
    records = list((tmp_path / 'manager').rglob('invocation.json'))
    assert len(records) == 2
    for path in records:
        policy = json.loads(path.read_bytes())['launch_context']['tool_discipline']
        assert policy['recorded_coding_model'] == selected


def test_changed_launch_metadata_does_not_replay_manager_provider_call(conversation, tmp_path):
    from foundry_runtime import _foundry_manager as manager
    state, requests, failures = conversation
    failures.clear()
    packet = {'stage': 'assign', 'run_id': 'legacy-run', 'coding_session': None}
    original = manager.turn(state, tmp_path, packet)[0]
    assert len(requests) == 1
    # Crash before the goal saved the decision; upgrade adds launch facts only.
    state['recorded_coding_model'] = 'sol'
    assert manager.turn(state, tmp_path, packet)[0] == original
    assert len(requests) == 1


def test_resumed_explicit_model_retains_prior_fallback_without_changing_policy():
    context = launch.capture(enable_index=False, coding_model='sonnet')
    budget = dict(context['tool_discipline']['reading_budget'])
    version = context['tool_discipline']['version']
    launch.bind_coding_model(context, 'opus')
    policy = context['tool_discipline']
    assert policy['recorded_coding_models'] == ['opus', 'sonnet']
    assert policy['reading_budget'] == budget and policy['version'] == version
    for model in ('opus', 'sonnet'):
        result = rules.check({'tool_name': 'Agent', 'tool_input': {'model': model,
            'prompt': '[activity: review] review [decision: critical review]'}}, policy)
        assert 'fallback' in result['hookSpecificOutput']['permissionDecisionReason']
    old = {'tool_discipline': {'version': 4}}
    launch.bind_coding_model(old, 'sol')
    assert old == {'tool_discipline': {'version': 4}}
