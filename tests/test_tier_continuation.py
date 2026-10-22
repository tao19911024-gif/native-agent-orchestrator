import json

import pytest

from foundry_runtime import _compute_selection as compute, _tool_discipline as rules
from foundry_runtime import _tool_history as history
from foundry_runtime._foundry_goal_auto import correction_options
from test_installed_runtime import external, Analyst


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')


def native_log(path, backend, command, output, *, child=False):
    if backend == 'claude':
        rows = [{'isSidechain': child}, {'type': 'user', 'message': {'content': 'task'}},
                {'message': {'content': [{'type': 'tool_use', 'id': 'r', 'name': 'Read', 'input': command}]}},
                {'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'r', 'content': output}]}}]
    else:
        rows = [{'payload': {'source': {'subagent': child}}},
                {'type': 'event_msg', 'payload': {'type': 'task_started'}},
                {'payload': {'type': 'function_call', 'call_id': 'r', 'name': 'Read', 'arguments': json.dumps(command)}},
                {'payload': {'type': 'function_call_output', 'call_id': 'r', 'output': output}}]
    path.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows), encoding='utf-8')
    return rows


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_native_read_budget_override_children_and_index(tmp_path, backend):
    source = tmp_path / '示例应用.py'
    source.write_text('中文\n' * 151, encoding='utf-8')
    log = tmp_path / 'native.jsonl'
    args = {'file_path': str(source), 'limit': 100}
    native_log(log, backend, args, '中文\n' * 100)
    policy = {'version': 3, 'root_role': 'manager', 'reading_budget':
              {'unit': 'lines', 'single': 150, 'turn': 200, 'index_charge': 250}}
    event = {'tool_name': 'Read', 'tool_input': {'file_path': str(source)}, 'transcript_path': str(log)}
    assert 'budget' in history.reading_check(event, policy)
    event['tool_input']['description'] = '#decide: 核对事务边界'
    assert history.reading_check(event, policy) is None
    del event['tool_input']['description']
    native_log(log, backend, args, '中文\n' * 100, child=True)
    assert history.reading_check(event, policy) is None
    event['tool_name'] = 'mcp__codegraph_explore'
    event['tool_input'] = {'description': 'rg alternatives'}
    assert history.reading_check(event, policy) is None


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_reread_proof_changes_truncation_and_turn_reset(tmp_path, backend):
    source, log = tmp_path / 'source.py', tmp_path / 'native.jsonl'
    source.write_text('original', encoding='utf-8')
    args = {'file_path': str(source)}
    event = {'tool_name': 'Read', 'tool_input': args, 'transcript_path': str(log)}
    policy = {'version': 3, 'root_role': 'executor'}
    rows = native_log(log, backend, args, 'original')
    assert 'unchanged' in history.reading_check(event, policy)
    source.write_text('changed', encoding='utf-8')
    assert history.reading_check(event, policy) is None
    native_log(log, backend, args, 'changed\n[output truncated]')
    assert history.reading_check(event, policy) is None
    rows = native_log(log, backend, args, 'changed')
    rows.append({'type': 'user', 'message': {'content': 'next task'}} if backend == 'claude' else
                {'type': 'event_msg', 'payload': {'type': 'task_started'}})
    log.write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
    assert history.reading_check(event, policy) is None


def test_unknown_formats_and_new_activity_are_not_denied(tmp_path, monkeypatch):
    policy = {'version': 3, 'root_role': 'manager'}
    for tool, field in [('Agent', 'prompt'), ('spawn_agent', 'message')]:
        assert rules.check({'tool_name': tool, 'tool_input': {'model': 'future-custom', field: '[activity: new-task] do work'}}, policy) == {}
        assert rules.check({'tool_name': tool, 'tool_input': {'model': 'sonnet', field: 'implement'}}, policy) == {}
        assert rules.check({'tool_name': tool, 'tool_input': {'model': 'sonnet', field:
            '[activity: investigate] [decision: low-tier evidence leaves conflicting ledger ownership] resolve contradiction'}}, policy) == {}
    event = {'tool_name': 'Read', 'tool_input': {'file_path': 'missing', 'offset': 'unknown'},
             'transcript_path': str(tmp_path / 'missing')}
    assert rules.check(event, policy) == {}
    monkeypatch.setattr(history, 'reading_check', lambda *a: (_ for _ in ()).throw(ValueError('new format')))
    assert 'permissionDecision' not in rules.check(event, policy)['hookSpecificOutput']


def test_code_mode_exact_wrapper_and_unknown_mixed_script(tmp_path):
    source = tmp_path / '中文.py'
    source.write_text('a\nb', encoding='utf-8')
    script = 'text(await tools.exec_command(' + json.dumps({'cmd': "Get-Content '中文.py'", 'workdir': str(tmp_path)}) + '));'
    rows = [{'payload': {'type': 'custom_tool_call', 'call_id': 'c', 'input': script}},
            {'payload': {'type': 'custom_tool_call_output', 'call_id': 'c', 'output': json.dumps({'output': 'a\nb', 'exit_code': 0})}}]
    name, args, output = history.calls(rows)[0]
    assert history.read_request(name, args, '.')['text'] == output
    rows[0]['payload']['input'] = 'await tools.exec_command({cmd: dynamic}); text(mixed);'
    assert history.calls(rows) == []


def test_shell_output_is_not_mistaken_for_numbered_native_read(tmp_path):
    source, log = tmp_path / 'source', tmp_path / 'log'
    source.write_text('old', encoding='utf-8')
    rows = [{'type': 'event_msg', 'payload': {'type': 'task_started'}},
            {'payload': {'type': 'function_call', 'call_id': 'c', 'name': 'exec_command',
                         'arguments': json.dumps({'cmd': 'cat source', 'workdir': str(tmp_path)})}},
            {'payload': {'type': 'function_call_output', 'call_id': 'c', 'output': '1:old'}}]
    log.write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
    assert history.reading_check({'tool_name': 'Read', 'tool_input': {'file_path': str(source)},
                                 'transcript_path': str(log)}, {'root_role': 'executor'}) is None


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_source_json_remains_opaque_for_reread_proof(tmp_path, backend):
    source, log = tmp_path / 'source.json', tmp_path / 'log'
    args = {'file_path': str(source)}
    native_log(log, backend, args, '{"output":"changed"}')
    source.write_text('changed', encoding='utf-8')
    event = {'tool_name': 'Read', 'tool_input': args, 'transcript_path': str(log)}
    assert history.reading_check(event, {'root_role': 'executor'}) is None
    source.write_text('{"output":"changed"}', encoding='utf-8')
    assert 'unchanged' in history.reading_check(event, {'root_role': 'executor'})


def test_mixed_shell_test_output_does_not_consume_read_budget(tmp_path):
    log = tmp_path / 'log'
    rows = [{'type': 'event_msg', 'payload': {'type': 'task_started'}},
            {'payload': {'type': 'function_call', 'call_id': 't', 'name': 'exec_command',
                         'arguments': json.dumps({'cmd': 'echo rg; pytest'})}},
            {'payload': {'type': 'function_call_output', 'call_id': 't',
                         'output': json.dumps({'output': 'test passed\n' * 500, 'exit_code': 0})}}]
    log.write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
    event = {'tool_name': 'exec_command', 'tool_input': {'cmd': 'rg symbol src'}, 'transcript_path': str(log)}
    assert history.reading_check(event, {'root_role': 'manager', 'reading_budget': {'unit': 'lines', 'turn': 400}}) is None


@pytest.mark.parametrize('output', [' changed ', '1\tchanged ', '1→ changed'])
def test_source_whitespace_changes_are_not_unchanged(tmp_path, output):
    source, log = tmp_path / 'source', tmp_path / 'log'
    source.write_text('changed', encoding='utf-8')
    args = {'file_path': str(source)}
    native_log(log, 'claude', args, output)
    assert history.reading_check({'tool_name': 'Read', 'tool_input': args, 'transcript_path': str(log)},
                                 {'root_role': 'executor'}) is None


@pytest.mark.parametrize('current,next_tier', [('claude-haiku-99', 'sonnet'), ('claude-sonnet-99', 'opus'),
                                             ('gpt-99-luna', 'sol'), ('gpt-99-sol', 'astra')])
def test_repeated_assertions_upgrade_once_and_unknown_errors_continue(current, next_tier, monkeypatch):
    from foundry_runtime._context_signals import test_failures
    monkeypatch.setattr(compute, 'model', lambda tier: tier + '-latest')
    failure = {'test_id': 'a.py::test_balance', 'call_id': 'one', 'session_id': 's', 'model': current}
    assert compute.escalation(current, {'test_failures': [failure, failure]}) is None
    two = {**failure, 'call_id': 'two'}
    decision = compute.escalation(current, {'test_failures': [failure, two]})
    assert decision['model'] == next_tier + '-latest'
    assert compute.escalation('future-model', {'test_failures': [failure, two]}) is None
    assert compute.escalation('gpt-99-astra', {'test_failures': [failure, two]}) is None
    assert test_failures('503 timeout\nERROR a.py::test_balance - ImportError') == []
    assert test_failures('FAILED a.py::test_balance - AssertionError: mismatch') == ['a.py::test_balance']


@pytest.mark.parametrize('current', ['claude-opus-5-5', 'gpt-6-astra'])
def test_highest_tier_uses_consultation_and_existing_correction_options(current):
    finding = {'severity': 'P1', 'file': 'ledger.py', 'summary': 'Duplicate debit'}
    decision = compute.escalation(current, {}, prior_findings=[finding], findings=[finding])
    assert decision['action'] == 'expert_consultation_required'
    base = {'compute_assignment': {'profile_version': 5}, 'execution_decision': {'tier_escalation': decision},
            'coding_model': current}
    assert correction_options(base, {'model': 'should-not-downgrade'})['coding_model'] == decision['model']
    base.pop('coding_model')
    base['role_compute'] = {'roles': {'coding': {'backend': 'codex', 'model': current}}}
    result = correction_options(base, {'model': 'ignored'})
    assert 'coding_model' not in result and result['role_compute']['roles']['coding']['model'] == decision['model']


def test_review_excludes_observed_workers_not_coordinator_and_unknown_continues():
    coding = {'managed_turns': [{'session_id': 'leader', 'model_observation': {'declarations': [{'model': 'gpt-6-astra'}]},
        'reading_signals': {'implementation_calls': 0}, 'child_agents': {'observations': [
            {'session_id': 'worker', 'model_observation': {'declarations': [{'model': 'gpt-6.1-sol'}]},
             'reading_signals': {'implementation_calls': 1}}]}}]}
    observed = compute.execution_evidence(coding)['implementation_models']
    assert observed == ['gpt-6.1-sol']
    rows = [{'backend': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high'}] * 2
    assert compute.independent_reviewers(rows, []) == rows
    chosen = compute.independent_reviewers(rows, observed)
    assert all(r['backend'] == 'claude' and r['model'] not in observed for r in chosen)
    with pytest.raises(ValueError, match='matches'):
        compute.independent_reviewers(rows, observed, explicit=True)
    chosen = compute.independent_reviewers(rows, observed, available_backends={'claude'})
    assert all(r['backend'] == 'claude' for r in chosen)


def test_claude_participant_and_context_alias_are_excluded():
    rows = [{'backend': 'claude', 'model': 'claude-sonnet-5-5[1m]', 'effort': 'high'}] * 2
    authors = compute.execution_evidence({'work_observation': {'models': ['claude-sonnet-5-5']}})['implementation_models']
    assert all(r['backend'] == 'codex' for r in compute.independent_reviewers(rows, authors))
    with pytest.raises(ValueError, match='matches'):
        compute.independent_reviewers(rows, authors, explicit=True)


def test_legacy_unknown_activity_tier_rule_is_retained():
    event = {'tool_name': 'Agent', 'tool_input': {'model': 'future-custom', 'prompt': '[activity: investigate] retrieve'}}
    assert rules.check(event, {'version': 2})
    assert not rules.check(event, {'version': 3})


def test_claude_model_attribution_and_optional_observation_gap(tmp_path):
    from foundry_runtime._context_signals import observe_work
    path = tmp_path / 'stream.jsonl'
    path.write_text('\n'.join(map(json.dumps, [
        {'message': {'model': 'claude-sonnet-5-5', 'content': [{'type': 'tool_use', 'id': 'c', 'name': 'Bash', 'input': {'command': 'pytest'}}]}},
        {'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'c', 'content': 'FAILED x.py::test_x - assert False'}]}}])), encoding='utf-8')
    assert observe_work(path)['test_failures'][0]['model'] == 'claude-sonnet-5-5'
    path.write_text('new non-json format', encoding='utf-8')
    assert observe_work(path)['coverage'] == 'uncovered'
    observed = compute.execution_evidence({'managed_turns': [None, {'work_observation': {'models': ['worker']}}]})
    assert observed['implementation_models'] == ['worker'] and observed['gaps']


def test_write_keywords_in_reads_are_not_author_evidence():
    from foundry_runtime._context_signals import tool_record
    read = tool_record({'type': 'function_call', 'call_id': 'r', 'name': 'exec_command',
                       'arguments': json.dumps({'cmd': 'rg Set-Content src'})})
    assert not read['implementation']
    patch = tool_record({'type': 'custom_tool_call', 'call_id': 'w', 'name': 'exec',
                        'input': 'text(await tools.apply_patch("*** Begin Patch\\n*** End Patch"));'})
    assert patch['implementation']


@pytest.mark.parametrize('backend,initial,top', [('claude', 'sonnet', 'opus'), ('codex', 'sol', 'astra')])
def test_auto_amend_upgrades_consults_then_preserves_existing_no_progress_exit(tmp_path, monkeypatch, backend, initial, top):
    import sys
    from foundry_runtime import foundry_goal as goal, _foundry_goal_auto as auto
    from foundry_runtime._model_catalogue import model
    calls = []
    order = tmp_path / 'TASK_fixture.md'
    order.write_text('fixture')
    def run(order, folder, executable, **options):
        calls.append(options)
        folder.mkdir()
        result = {'status': 'review_blocked', 'candidate_commit': 'same', 'coding_session': 'fixture',
                  'reviews': [{'verdict': 'BLOCK', 'findings': [{'severity': 'P1', 'file': 'a.py', 'summary': 'same failure'}]}]}
        goal._write(folder / 'manifest.json', {'requested_compute': {'coding': {'model': options['coding_model']}}})
        goal._write(folder / 'outcome.json', result)
        return result
    monkeypatch.setattr(goal, '_run', run)
    result = auto.run_owned(goal, order, tmp_path / 'auto', sys.executable, {'model': model(initial), 'effort': 'high'},
        {'coding_model': model(initial), 'coding_launcher': 'claude' if backend == 'claude' else 'exec',
         'compute_assignment': {'profile_version': 5}})
    assert [c['coding_model'] for c in calls] == [model(initial), model(initial), model(top), model(top)]
    assert calls[-1]['execution_decision']['tier_escalation']['action'] == 'expert_consultation_required'
    assert result['auto_amend_stop'] == 'unchanged candidate and blocking findings repeated'


@pytest.mark.parametrize('known_author', [False, True, 'recorded_assignment'])
def test_goal_review_conflict_retains_checkpoint_and_worker_is_not_replayed(external, tmp_path, monkeypatch, known_author):
    import sys
    from pathlib import Path
    from foundry_runtime import foundry_goal as goal, _foundry_dispatch as dispatch
    from foundry_runtime import _foundry_goal_execution as execution, _foundry_goal_transitions as transitions
    from foundry_runtime import _foundry_goal_usage as usage
    tree, scope, order, workers = external
    original = dispatch.coding_argv
    def coding(*args, **kwargs):
        argv = original(*args, **kwargs)
        argv[-1] += "; import subprocess; Path('candidate.txt').write_text('done'); subprocess.run(['git','add','candidate.txt'],check=True); subprocess.run(['git','commit','-qm','candidate'],check=True); print('session id: fixture-session')"
        return argv
    monkeypatch.setattr(dispatch, 'coding_argv', coding)
    monkeypatch.setattr(goal, 'NativeAnalyst', Analyst)
    monkeypatch.setattr(goal, 'resolve_executable', lambda _: Path(sys.executable))
    monkeypatch.setattr(transitions, 'assignment', lambda backend, **kw: {'backend': backend,
        'model': 'claude-opus-5-5', 'effort': 'high', 'executable': sys.executable})
    if known_author is True:
        monkeypatch.setattr(usage, 'capture', lambda *a: {'work_observation': {'models': ['gpt-6.1-sol']}})
    rows = [{'backend': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high', 'executable': sys.executable}] * 2
    folder = tmp_path / 'goal'
    arguments = dict(coding_launcher='exec', project_record=scope.record(), reviewers=rows,
                     compute_assignment={'profile_version': 5, 'selection': {'source': 'explicit'}})
    if known_author == 'recorded_assignment':
        arguments.update(coding_model='gpt-6.1-sol', coding_effort='high')
    if known_author:
        with pytest.raises(goal.GoalBlocked, match='matches'):
            execution.run_owned(order, folder, Path(sys.executable), **arguments)
        checkpoint = (folder / 'review-checkpoint.json').read_bytes()
        # Transfer only the first slot: the other known conflict remains visible.
        with pytest.raises(goal.GoalBlocked, match='matches'):
            goal.recover_goal(order, folder, handoff_role='review-0', target_provider='claude')
        result = goal.recover_goal(order, folder, handoff_role='review-1', target_provider='claude')
        assert (folder / 'review-checkpoint.json').read_bytes() == checkpoint
    else:
        result = execution.run_owned(order, folder, Path(sys.executable), **arguments)
        assert result['model_independence'] == 'unverified_missing_participant_evidence'
    assert result['status'] == 'candidate_verified'
    assert len(workers) == 1
