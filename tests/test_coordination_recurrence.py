"""Convergence uses work observations, with progressing native controls."""
import io
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from foundry_runtime import cli, foundry_goal as goal, _foundry_goal_auto as auto
from foundry_runtime import _expert_consultation as experts
from foundry_runtime._goal_intervention import LiveEvents, live_scope
from test_claude_live_events import assistant, bridge
from test_installed_runtime import external, offline, git
from test_manager_input_wait import manager_io, wait


def tool(events, identity, command, result, *, error=False):
    events.observe(assistant('request-' + identity, ''))
    events.observe({'type': 'assistant', 'uuid': 'call-' + identity, 'message': {'content': [
        {'type': 'tool_use', 'id': identity, 'name': 'Bash', 'input': {'command': command}}]}})
    events.observe({'type': 'user', 'message': {'content': [
        {'type': 'tool_result', 'tool_use_id': identity, 'content': result, 'is_error': error}]}})


@pytest.mark.parametrize('variation', ['status', 'format'])
def test_unchanged_live_handoff_closes(variation):
    calls = []
    live = bridge(lambda rows: calls.append(rows) or {'action': 'delegate', 'instruction': 'Confirm delivery.'})
    stream = io.StringIO()
    try:
        for index in range(6):
            if variation == 'status':
                tool(live, str(index), 'git status --porcelain=v1', '')
            live.observe(assistant('done-' + str(index),
                'FOUNDRY_DELIVERABLE_DONE Ready' + ('\n' * index if variation == 'format' else '')))
            live.live_events.drain(live)
            if live.live_events.pending is not None:
                live.live_events.pending.result(timeout=5)
                live.live_events.drain(live)
            live.result_ready = True
            live.advance(SimpleNamespace(stdin=stream))
            if live.closed:
                break
        assert live.closed and len(calls) == 1
    finally:
        live.close()


@pytest.mark.parametrize('variation', ['same', 'command', 'result', 'error', 'oscillation', 'unknown'])
def test_live_observation_preserves_new_evidence(variation):
    live = bridge()
    try:
        for index in range(3):
            command = 'git status' + (str(index) if variation == 'command' else '')
            result = None if variation == 'unknown' else str(index) if variation in ('result', 'error') else str(index % 2) if variation == 'oscillation' else ''
            tool(live, str(index), command, result, error=variation == 'error')
            live.observe(assistant('done-' + str(index), 'FOUNDRY_DELIVERABLE_DONE Ready'))
        assert live.live_events.events.qsize() == (1 if variation == 'same' else 3)
    finally:
        live.close()


@pytest.mark.parametrize('variation', ['source', 'question', 'operator', 'unknown'])
def test_live_work_can_continue(variation):
    source = {'head': 'same', 'contents': 1}
    with live_scope(lambda rows: 'continue', observe_source=lambda: source if variation != 'unknown' else None):
        events = LiveEvents()
    def message(index):
        events.observe({'method': 'item/completed', 'params': {'threadId': 'root', 'turnId': 'turn',
            'item': {'type': 'agentMessage', 'id': str(index), 'text': 'FOUNDRY_MANAGER_QUESTION ' +
                (str(index) if variation == 'question' else 'Same question')}}}, 'root')
    message(1)
    if variation == 'source':
        source['contents'] = 2
    if variation == 'operator':
        events.observe({'method': 'item/completed', 'params': {'threadId': 'root', 'turnId': 'turn',
            'item': {'type': 'userMessage', 'id': 'operator', 'content': 'Repeat this step explicitly.'}}}, 'root')
    message(2)
    assert events.events.qsize() == 2
    if variation == 'source':
        source['contents'] = 1
        message(3)
        source['contents'] = 2
        message(4)
        assert events.events.qsize() == 4


@pytest.mark.parametrize('variation', ['empty_commit', 'oscillation', 'progress', 'measurement', 'finding', 'closure'])
def test_auto_content_recurrence_preserves_progress(external, tmp_path, monkeypatch, variation):
    tree, _, order, _ = external
    source = tree / 'addition.py'
    calls = []
    def run(order, folder, executable, **options):
        index = len(calls)
        assert index < 8, 'auto owner failed to converge'
        suffix = str(index) if variation == 'progress' else str(index % 2) if variation == 'oscillation' else 'same'
        source.write_text('def add(a, b):\n    return a + b + 1\n# ' + suffix + '\n')
        git(tree, 'add', '.'); git(tree, 'commit', '--allow-empty', '-qm', 'correction ' + str(index))
        calls.append(options)
        folder.mkdir()
        goal._write(folder / 'manifest.json', {'worktree': str(tree), 'base_commit': 'base', 'tag': 'fixture'})
        result = {'status': 'candidate_verified' if index == 6 else 'review_blocked',
            'candidate_commit': git(tree, 'rev-parse', 'HEAD'), 'coding_session': 'fixture-session',
            'reviews': [{'findings': [{'file': 'addition.py', 'line': 2, 'severity': 'P1',
                'summary': 'Incorrect addition' + (str(index) if variation == 'finding' else '')}]}]}
        result['implementation_evidence'] = {'active_test_failures': [
            {'test_id': 'test_' + (str(index) if variation == 'measurement' else 'addition'),
             'call_id': 'new-call-' + str(index), 'session_id': 'new-session-' + str(index), 'model': 'fixture'}
            for _ in range(index + 1)]}
        result['candidate_closure'] = {'status': 'passed', 'candidate_commit': result['candidate_commit'],
            'checkout': 'checkout-' + str(index), 'elapsed_seconds': index,
            'stdout': {'path': 'stdout-' + str(index), 'sha256': str(index) if variation == 'closure' else 'same-output'}}
        goal._write(folder / 'outcome.json', result)
        return result
    monkeypatch.setattr(goal, '_run', run)
    result = auto.run(goal, order, tmp_path / 'auto', Path(sys.executable), {'model': 'gpt-6-sol'},
        {'coding_launcher': 'app-server', 'coding_model': 'gpt-6-sol', 'task_staffing': {'kind': 'small'},
         'compute_assignment': {'profile_version': 5, 'roles': {}}})
    if variation in ('empty_commit', 'oscillation'):
        assert result['status'] == 'review_blocked' and len(calls) < 7
        assert result['auto_amend_stop'] == 'unchanged candidate and blocking findings repeated'
    else:
        assert result['status'] == 'candidate_verified' and len(calls) == 7


@pytest.mark.parametrize('variation', ['same', 'source', 'question', 'unknown', 'operator'])
def test_expert_answer_recurrence_preserves_new_work(tmp_path, variation):
    source = tmp_path / 'source.py'; source.write_text('original')
    receipt = tmp_path / 'receipt.json'; receipt.write_text('{}')
    question = {'trigger': 'architecture', 'question': 'Who owns writes?',
        'evidence_refs': [str(source)] if variation != 'unknown' else ['unknown-evidence']}
    state = {'native_dir': str(tmp_path / 'manager'), 'effective_coordination': 1,
             'expert': {'backend': 'codex', 'model': 'fixture', 'effort': 'high'}}
    manager_calls, expert_calls = [], []
    def turn(current, workspace, packet, **kwargs):
        if packet['stage'] == 'expert_consultation':
            expert_calls.append(deepcopy(packet))
            return {'instruction': 'Use the ledger.', 'evidence': goal._fact(receipt)}, current, {}, packet
        manager_calls.append(deepcopy(packet))
        if len(manager_calls) == 2:
            if variation == 'source': source.write_text('new evidence')
            if variation == 'question': question['question'] = 'What about dependency direction?'
        decision = {'action': 'consult_expert', 'instruction': 'Resolve ownership.',
                    'expert_request': deepcopy(question), 'evidence': goal._fact(receipt)}
        if len(manager_calls) == 3:
            decision = {'action': 'delegate', 'instruction': 'Implement the answer.'}
        return decision, current, {}, packet
    result = experts.coordinate(turn, state, tmp_path, {'stage': 'assign'}, operator_update=variation == 'operator')
    if variation in ('same', 'operator'):
        assert result[0]['action'] == 'await_input' and len(manager_calls) == 2 and len(expert_calls) == 1
    else:
        assert result[0]['action'] == 'delegate' and len(manager_calls) == 3 and len(expert_calls) == 2


@pytest.mark.parametrize('retained_only', [False, True])
def test_recovery_mode_is_selected_before_dispatch(external, manager_io, tmp_path, retained_only):
    _, scope, order, workers = external
    requests, decisions = manager_io
    decisions.append(wait('Original question'))
    folder = tmp_path / 'goal'
    goal.run_goal(order, folder, Path(sys.executable), coding_launcher='exec', project_record=scope.record(),
        manager_state={'backend': 'codex', 'model': None, 'effort': None, 'native_dir': str(tmp_path / 'manager')})
    argv = ['--project-config', str(scope.path), '--order', str(order), '--recover-goal', str(folder), '--launcher', 'exec']
    if retained_only:
        argv += ['--retained-only']
    else:
        answer = tmp_path / 'answer.txt'; answer.write_text('Answer the original question.')
        decisions.append(wait('Next question'))
        argv += ['--prompt-file', str(answer)]
    assert cli.main(argv) == 2
    assert not workers and len(requests) == (1 if retained_only else 2)


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_repeated_consultation_publishes_recoverable_question(external, manager_io, tmp_path, backend):
    tree, scope, order, workers = external
    requests, decisions = manager_io
    question = {'trigger': 'architecture', 'question': 'Who owns writes?',
                'evidence_refs': [str(tree / 'AGENTS.md')]}
    consult = {'action': 'consult_expert', 'instruction': 'Resolve ownership.', 'expert_request': question}
    decisions.extend([consult, {'action': 'delegate', 'instruction': 'The ledger owns writes.'}, consult])
    folder = tmp_path / 'goal'
    state = {'backend': backend, 'model': None, 'effort': None, 'native_dir': str(tmp_path / 'manager'),
             'effective_coordination': 1, 'expert': {'backend': backend, 'model': None, 'effort': None}}
    result = goal.run_goal(order, folder, Path(sys.executable), coding_launcher='exec',
                          project_record=scope.record(), manager_state=state)
    assert result['status'] == 'awaiting_input' and len(requests) == 3 and not workers
    assert 'same expert question' in result['input_request']['question']
    assert goal.recover_goal(order, folder) == result and len(requests) == 3
    decisions.append({**wait('New operator decision received.'), 'expert_request': None})
    answered = goal.recover_goal(order, folder, answer='Use the existing expert answer and continue.')
    assert answered['input_request']['question'] == 'New operator decision received.'
    assert len(requests) == 4 and not workers


def test_implicit_recovery_mode_reaches_recorded_runtime(monkeypatch):
    from foundry_runtime import _foundry_recovery_preflight as preflight
    monkeypatch.setattr(preflight, 'prepare', lambda *a, **k:
        {'status': 'ready', 'runtime_python': str(Path(sys.executable).with_name('recorded.exe'))})
    forwarded = []
    monkeypatch.setattr(preflight, 'execute_recorded', lambda plan, argv, **kwargs: forwarded.extend(argv) or 0)
    assert cli.main(['--recover-goal', 'retained-goal']) == 0
    assert forwarded == ['--recover-goal', 'retained-goal', '--foundry-goal']
