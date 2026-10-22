"""Retained manager questions use real native receipts, never business/model calls."""
import json
from pathlib import Path
import sys

import pytest

from test_installed_runtime import external, offline
from test_settled_failure_recovery import failed_goal
from foundry_runtime import foundry_goal as goal, foundry_native as native
from foundry_runtime import _foundry_manager as manager, _code_index, _native_session
from foundry_runtime._foundry_response_contract import response_contract, validate_response_shape
from foundry_runtime._followup import status


@pytest.fixture
def manager_io(external, tmp_path, monkeypatch):
    requests, decisions = [], []
    spawn = native.OwnedProcess.spawn
    monkeypatch.setattr(_code_index, 'prepare', lambda *a, **k: None)
    monkeypatch.setattr(manager, 'resolve_executable', lambda _: Path(sys.executable))
    monkeypatch.setattr(goal, '_session_from_log', lambda _: 'original-worker')
    monkeypatch.setattr(_native_session, 'read_session_worktree', lambda _: external[0])
    def run(argv, **kwargs):
        if argv[1:3] == ['-B', '-c']:
            return spawn(argv, **kwargs)
        source = kwargs['stdin']; request = json.loads(source.read().rsplit(b'\n\n', 1)[-1]); source.seek(0)
        requests.append(request)
        assert decisions, 'unexpected manager call while waiting'
        decision = decisions.pop(0)
        if '--output-schema' in argv:
            rows = [{'type': 'thread.started', 'thread_id': '00000000-0000-0000-0000-000000000001'},
                {'type': 'turn.started'}, {'type': 'item.completed', 'item': {'id': 'answer',
                'type': 'agent_message', 'text': json.dumps(decision)}}, {'type': 'turn.completed',
                'usage': {'input_tokens': 10, 'cached_input_tokens': 0, 'output_tokens': 10}}]
        else:
            rows = [{'type': 'system', 'subtype': 'init', 'session_id': 'original-manager',
                     'model': 'fixture', 'tools': ['StructuredOutput'], 'mcp_servers': [], 'skills': [], 'plugins': []},
                {'type': 'assistant', 'session_id': 'original-manager', 'message': {'role': 'assistant', 'content': [
                    {'type': 'tool_use', 'name': 'StructuredOutput', 'id': 'format-1', 'input': decision}]}},
                {'type': 'user', 'session_id': 'original-manager', 'message': {'role': 'user', 'content': [
                    {'type': 'tool_result', 'tool_use_id': 'format-1', 'content': 'Structured output provided successfully'}]}},
                {'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 2,
                 'result': json.dumps(decision), 'structured_output': decision,
                 'usage': {'input_tokens': 10, 'output_tokens': 10, 'iterations': [{'type': 'message'}]}, 'total_cost_usd': 0.0}]
        script = 'import sys;sys.stdin.buffer.read();sys.stdout.write(' + repr('\n'.join(map(json.dumps, rows))+'\n') + ')'
        return spawn([sys.executable, '-B', '-c', script], **kwargs)
    monkeypatch.setattr(native.OwnedProcess, 'spawn', run)
    return requests, decisions


def wait(question='请确认契约；已送达不等于已回答'):
    return {'action': 'await_input', 'instruction': question}


@pytest.mark.parametrize('backend', ['claude', 'codex'])
@pytest.mark.parametrize('before_worker', [False, True])
def test_wait_stops_calls_and_explicit_answer_is_deduplicated(external, manager_io, tmp_path, backend, before_worker):
    tree, scope, order, workers = external
    requests, decisions = manager_io
    state = {'backend': backend, 'model': None, 'effort': None, 'last_call_id': None,
             'native_dir': str(tmp_path/'manager')}
    if not before_worker:
        decisions.append({'action': 'delegate', 'instruction': 'Investigate once'})
    decisions.append(wait())
    folder = tmp_path/'goal'
    result = goal.run_goal(order, folder, Path(sys.executable), manager_state=state,
                           coding_launcher='exec', project_record=scope.record())
    assert result['status'] == 'awaiting_input'
    assert result['input_request']['question'] == wait()['instruction']
    assert len(workers) == (0 if before_worker else 1)
    calls = len(requests)
    frozen = {p: p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    native_frozen = {p: p.read_bytes() for p in Path(state['native_dir']).glob('call-*/*') if p.is_file()}
    for _ in range(2):
        assert goal.recover_goal(order, folder) == result
        observed = status(folder)
        assert observed['outcome_status'] == 'awaiting_input'
        assert observed['input_request'] == result['input_request']
    assert len(requests) == calls
    decisions.extend([{'action': 'delegate', 'instruction': 'Implement the actual answer'}, wait('下一项不同问题')])
    answered = goal.recover_goal(order, folder, answer='实际裁决：保持原契约')
    assert answered['status'] == 'awaiting_input'
    delivered = Path(answered['continuation_evidence']) / 'outcome.json'
    assert answered['result_outcome'] == goal._fact(delivered)
    assert json.loads(delivered.read_bytes())['status'] == answered['status']
    assert answered['input_request']['question'] == '下一项不同问题'
    assert len(workers) == (1 if before_worker else 2)
    assert requests[calls]['packet']['operator_answer'] == '实际裁决：保持原契约'
    calls = len(requests)
    assert goal.recover_goal(order, folder, answer='实际裁决：保持原契约') == answered
    assert len(requests) == calls and not decisions
    assert all(p.read_bytes() == raw for p, raw in frozen.items())
    assert all(p.read_bytes() == raw for p, raw in native_frozen.items())


def test_coordinate_contract_can_represent_wait_without_a_worker_turn():
    validate_response_shape(wait(), response_contract('coordinate', operator_input=True))


@pytest.mark.parametrize('before_worker', [False, True])
def test_actual_answer_can_follow_wait_manifest_before_outcome_publication(external, manager_io, tmp_path, monkeypatch, before_worker):
    tree, scope, order, workers = external
    requests, decisions = manager_io
    if not before_worker:
        decisions.append({'action': 'delegate', 'instruction': 'Investigate'})
    decisions.append(wait())
    state = {'backend': 'codex', 'model': None, 'effort': None, 'last_call_id': None, 'native_dir': str(tmp_path/'manager')}
    folder = tmp_path/'goal'
    original = goal._write
    def interrupted(path, value):
        if path.name == 'outcome.json' and value.get('input_request'):
            raise RuntimeError('waiting receipt not yet published')
        return original(path, value)
    with monkeypatch.context() as patch:
        patch.setattr(goal, '_write', interrupted)
        with pytest.raises(RuntimeError, match='waiting receipt not yet published'):
            goal.run_goal(order, folder, Path(sys.executable), manager_state=state, coding_launcher='exec', project_record=scope.record())
    assert not (folder/'outcome.json').exists()
    decisions.extend([{'action': 'delegate', 'instruction': 'Use actual answer'}, wait('新问题')])
    answer = goal.recover_goal(order, folder, answer='实际答复先于回执恢复到达')
    assert answer['input_request']['question'] == '新问题'
    assert len(workers) == (1 if before_worker else 2)
    assert goal.recover_goal(order, folder, answer='实际答复先于回执恢复到达') == answer


@pytest.mark.parametrize('crash', ['before_manifest', 'before_outcome', None])
def test_auto_goal_wait_survives_publication_crash_and_retains_worker_on_second_question(external, manager_io, tmp_path, monkeypatch, crash):
    tree, scope, order, workers = external
    requests, decisions = manager_io
    decisions.extend([{'action': 'delegate', 'instruction': 'Investigate'}, wait()])
    state = {'backend': 'codex', 'model': None, 'effort': None, 'last_call_id': None, 'native_dir': str(tmp_path/'manager')}
    folder = tmp_path/'goal'
    original = goal._write
    def interrupted(path, value):
        if crash == 'before_manifest' and path.name == 'manifest.json' and value.get('manager_phase') == 'awaiting_input':
            raise RuntimeError('fixture publication crash')
        if crash == 'before_outcome' and path.name == 'outcome.json' and value.get('input_request'):
            raise RuntimeError('fixture publication crash')
        return original(path, value)
    with monkeypatch.context() as patch:
        patch.setattr(goal, '_write', interrupted)
        kwargs = dict(manager_state=state, coding_launcher='exec', project_record=scope.record(),
                      auto_amend_model='fixture-correction', auto_amend_effort='high')
        if crash:
            with pytest.raises(RuntimeError, match='fixture publication crash'):
                goal.run_goal(order, folder, Path(sys.executable), **kwargs)
        else:
            assert goal.run_goal(order, folder, Path(sys.executable), **kwargs)['status'] == 'awaiting_input'
    result = goal.recover_goal(order, folder)
    assert result['status'] == 'awaiting_input' and len(requests) == 2 and len(workers) == 1
    assert status(folder)['input_request']['question'] == wait()['instruction']
    decisions.append(wait('仍需确认第二项'))
    again = goal.recover_goal(order, folder, answer='第一项的实际答案')
    assert again['status'] == 'awaiting_input' and len(workers) == 1
    child = Path(again['continuation_evidence'])
    checkpoint = json.loads((child/'coding-checkpoint.json').read_bytes())
    assert checkpoint['coding_session'] == 'original-worker' and checkpoint['returncode'] == 0
    decisions.extend([{'action': 'delegate', 'instruction': 'Implement'}, wait('实施后另一项')])
    assert goal.recover_goal(order, child, answer='第二项实际答案')['status'] == 'awaiting_input'
    assert len(workers) == 2 and not decisions


def test_answer_admitted_before_publication_crash_is_not_sent_twice(external, manager_io, tmp_path, monkeypatch):
    tree, scope, order, workers = external
    requests, decisions = manager_io
    decisions.append(wait())
    state = {'backend': 'codex', 'model': None, 'effort': None, 'last_call_id': None, 'native_dir': str(tmp_path/'manager')}
    folder = tmp_path/'goal'
    goal.run_goal(order, folder, Path(sys.executable), manager_state=state, coding_launcher='exec', project_record=scope.record())
    decisions.append(wait('另一项确实未回答的问题'))
    original = goal._write
    def interrupted(path, value):
        if path.name == 'manifest.json' and value.get('continuation') and value.get('manager_phase') == 'awaiting_input':
            raise RuntimeError('answer admitted before crash')
        return original(path, value)
    with monkeypatch.context() as patch:
        patch.setattr(goal, '_write', interrupted)
        with pytest.raises(RuntimeError, match='answer admitted before crash'):
            goal.recover_goal(order, folder, answer='实际答复')
    assert len(requests) == 2 and not workers
    recovered = goal.recover_goal(order, folder, answer='实际答复')
    assert recovered['input_request']['question'] == '另一项确实未回答的问题'
    assert len(requests) == 2 and not workers


def test_explicit_manager_transfer_after_settled_failed_worker_can_wait(failed_goal, manager_io, monkeypatch, tmp_path):
    tree, scope, order, folder, workers = failed_goal
    requests, decisions = manager_io
    decisions.append({'action': 'delegate', 'instruction': 'Original instruction'})
    state = {'backend': 'codex', 'model': None, 'effort': None, 'last_call_id': None, 'native_dir': str(tmp_path/'manager')}
    decision, state, _, packet = manager.turn(state, tree, {'stage': 'worker_returned', 'returncode': 1})
    manifest = json.loads((folder/'manifest.json').read_bytes())
    manifest.update(manager_state=state, manager_phase='feedback_pending', manager_packet=packet,
                    execution_decision={'manager': decision})
    checkpoint = json.loads((folder/'coding-checkpoint.json').read_bytes())
    checkpoint['coding_usage'] = {}
    goal._write(folder/'coding-checkpoint.json', checkpoint)
    manifest['coding_checkpoint'] = goal._fact(folder/'coding-checkpoint.json')
    goal._write(folder/'manifest.json', manifest)
    (folder/'outcome.json').unlink()  # Local a31-style failure fixture; no business history.
    with pytest.raises(goal.GoalBlocked, match='explicit operator instructions'):
        goal.recover_goal(order, folder)
    from foundry_runtime import _foundry_goal_transitions as transitions
    monkeypatch.setattr(transitions, 'resolve_executable', lambda _: Path(sys.executable))
    decisions.append(wait('需要实际业务裁决'))
    result = goal.recover_goal(order, folder, answer='迁移经理并核对待决事项', migrate_runtime=True, handoff_role='manager')
    assert result['status'] == 'awaiting_input' and len(requests) == 2
    assert workers == [None]
