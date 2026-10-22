"""Real native allocation/receipt replay, with owned local protocol subprocesses."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

from foundry_runtime import _foundry_manager as manager, _expert_consultation as experts
from foundry_runtime import _code_index, foundry_native as native
from foundry_runtime._foundry_response_contract import response_contract, validate_response_shape
from test_installed_runtime import external, offline


@pytest.fixture(params=['claude', 'codex'])
def consultation(request, tmp_path, monkeypatch):
    backend = request.param
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    monkeypatch.setattr(manager, 'resolve_executable', lambda _: Path(sys.executable))
    monkeypatch.setattr(_code_index, 'prepare', lambda *a, **k: None)
    calls, failures = [], set()
    spawn = native.OwnedProcess.spawn
    def local(argv, **kw):
        if argv[1:2] == ['-B']:
            return spawn(argv, **kw)  # The worker fixture has its own real child.
        source = kw['stdin']
        request = json.loads(source.read().rsplit(b'\n\n', 1)[-1]); source.seek(0)
        expert = request['packet'].get('stage') == 'expert_consultation'
        index = len(calls); calls.append(request)
        if expert:
            result = {'action': 'delegate', 'instruction': 'Decision: preserve the single ledger writer; see src/ledger.py:1.'}
        elif request['packet'].get('stage') == 'worker_returned':
            result = {'action': 'review', 'instruction': 'Review original worker result.', 'expert_request': None}
        elif request['packet'].get('expert_consultation'):
            result = {'action': 'delegate', 'instruction': 'Original worker implements the expert decision.', 'expert_request': None}
        else:
            result = {'action': 'consult_expert', 'instruction': 'Need ownership judgment.',
                      'expert_request': {'trigger': 'architecture', 'question': 'Which module owns ledger writes?',
                                         'evidence_refs': ['src/ledger.py:1']}}
        session = '00000000-0000-0000-0000-00000000000' + ('2' if expert else '1')
        if backend == 'claude':
            rows = [{'type': 'system', 'subtype': 'init', 'tools': ['StructuredOutput'],
                     'mcp_servers': [], 'skills': [], 'plugins': [], 'model': 'fixture', 'session_id': session}]
            if index not in failures:
                rows += [
                    {'type': 'assistant', 'session_id': session, 'message': {'role': 'assistant', 'content': [
                        {'type': 'tool_use', 'name': 'StructuredOutput', 'id': 'format-1', 'input': result}]}},
                    {'type': 'user', 'session_id': session, 'message': {'role': 'user', 'content': [
                        {'type': 'tool_result', 'tool_use_id': 'format-1', 'content': 'Structured output provided successfully'}]}},
                    {'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 2, 'session_id': session,
                     'result': json.dumps(result), 'structured_output': result,
                     'usage': {'input_tokens': 10, 'output_tokens': 20, 'iterations': [{'type': 'message'}]}, 'total_cost_usd': 0.01,
                     'modelUsage': {'claude-fable-5-1' if expert else 'claude-opus-5-5': {
                         'inputTokens': 10, 'outputTokens': 20, 'cacheReadInputTokens': 0,
                         'cacheCreationInputTokens': 0, 'costUSD': 0.01}}}]
            else:
                rows += [{'type': 'assistant', 'error': 'rate_limit', 'message': {'content': [
                    {'type': 'text', 'text': "You've hit your limit · resets 7pm"}]}}]
        else:
            rows = [{'type': 'thread.started', 'thread_id': session}, {'type': 'turn.started'}]
            if index not in failures:
                rows += [{'type': 'item.completed', 'item': {'id': 'final', 'type': 'agent_message', 'text': json.dumps(result)}},
                         {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'cached_input_tokens': 0, 'output_tokens': 5}}]
            else:
                rows += [{'type': 'turn.failed', 'error': {'message': 'Usage limit exceeded'}}]
        script = 'import sys;sys.stdin.buffer.read();sys.stdout.write(' + repr('\n'.join(map(json.dumps, rows))+'\n') + ');sys.exit(' + str(int(index in failures)) + ')'
        return spawn([sys.executable, '-B', '-c', script], **kw)
    monkeypatch.setattr(native.OwnedProcess, 'spawn', local)
    state = {'backend': backend, 'model': None, 'effort': None, 'last_call_id': None,
             'native_dir': str(tmp_path / 'manager'), 'expert': {'backend': backend,
                'model': 'claude-fable-5-1' if backend == 'claude' else 'gpt-6-astra', 'effort': 'high'}}
    return state, calls, failures


def test_manager_question_is_consulted_and_answer_reaches_worker_once(consultation, tmp_path):
    state, calls, _ = consultation
    packet = {'stage': 'assign', 'operator_answer': 'original'}
    result = manager.turn(state, tmp_path, packet)
    decision = result[0]
    assert decision['action'] == 'delegate'
    assert 'single ledger writer' in decision['expert_consultation']['answer']
    assert calls[-1]['packet']['expert_consultation']['evidence']
    assert len(calls) == 3
    assert manager.turn(state, tmp_path, packet)[0] == decision  # goal write lost
    assert len(calls) == 3
    assert experts.observe(state)[0]['status'] == 'answered'


@pytest.mark.parametrize('failure_index', [1, 2])
def test_interruption_replays_expert_or_uses_completed_answer(consultation, tmp_path, failure_index):
    state, calls, failures = consultation
    failures.add(failure_index)
    packet = {'stage': 'assign', 'operator_answer': 'original'}
    with pytest.raises(native.NativeError) as caught:
        manager.turn(state, tmp_path, packet)
    assert caught.value.manager_packet == packet
    assert experts.observe(state)[0]['status'] == ('blocked' if failure_index == 1 else 'answered')
    new = {**packet, 'operator_answer': 'updated instruction'}
    decision, _, _, accepted = manager.turn(state, tmp_path, new, operator_update=True)
    assert accepted['operator_answer'] == 'updated instruction'
    assert decision['expert_consultation']['answer']
    expert_calls = [c for c in calls if c['packet'].get('stage') == 'expert_consultation']
    assert len(expert_calls) == (2 if failure_index == 1 else 1)
    assert all(c['packet'] == expert_calls[0]['packet'] for c in expert_calls)


def test_reviewer_request_consults_before_manager_and_is_reusable(consultation, tmp_path):
    state, calls, _ = consultation
    path = tmp_path / 'review.json'
    path.write_text(json.dumps({'reviews': [{'correction_guidance': {
        'model': 'expert', 'cause': 'reasoning', 'reason': 'contradictory ledger ownership'}, 'findings': []}]}))
    ref = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    packet = {'stage': 'assign', 'prior_review': ref}
    first = manager.turn(state, tmp_path, packet)
    assert calls[0]['packet']['stage'] == 'expert_consultation' and len(calls) == 2
    assert manager.turn(state, tmp_path, packet)[0] == first[0] and len(calls) == 2
    assert first[0]['expert_consultation']['trigger'] == 'reviewer'


def test_legacy_contract_cannot_request_new_expert_action():
    value = {'action': 'consult_expert', 'instruction': 'question', 'expert_request': {
        'trigger': 'architecture', 'question': 'owner?', 'evidence_refs': ['src/a.py:1']}}
    validate_response_shape(value, response_contract('coordinate', expert_consultation=True))
    with pytest.raises(Exception):
        validate_response_shape(value, response_contract('coordinate'))
    with pytest.raises(Exception, match='needs its question'):
        validate_response_shape({**value, 'expert_request': None}, response_contract('coordinate', expert_consultation=True))


def test_highest_tier_failure_uses_native_expert_receipt_and_recovery(consultation, tmp_path):
    state, calls, failures = consultation
    packet = {'stage': 'worker_returned', 'worker_evidence': [{'path': 'fixture-test-output'}],
              'tier_escalation': {'action': 'expert_consultation_required', 'signals': ['test:ledger.py::test_balance']}}
    failures.add(0)
    with pytest.raises(native.NativeError):
        manager.turn(state, tmp_path, packet)
    decision = manager.turn(state, tmp_path, packet)[0]
    assert decision['action'] == 'review'
    assert decision['expert_consultation']['trigger'] == 'repeated_failure'
    assert len(calls) == 3  # Failed expert, recovered expert, original manager.
    assert manager.turn(state, tmp_path, packet)[0] == decision
    assert len(calls) == 3


def test_unsettled_expert_receipt_does_not_launch_a_duplicate(consultation, tmp_path):
    state, calls, _ = consultation
    packet = {'stage': 'assign'}
    manager.turn(state, tmp_path, packet)
    call = experts.observe(state)[0]['calls'][0]
    (Path(call['evidence']) / 'receipt.json').unlink()  # owned interrupted-journal fixture
    assert experts.observe(state)[0]['status'] == 'pending'
    with pytest.raises((native.NativeError, FileNotFoundError)):
        manager.turn(state, tmp_path, packet)
    assert len(calls) == 3


def test_failed_expert_does_not_admit_a_new_operator_packet(consultation, tmp_path):
    state, _, failures = consultation
    failures.update({1, 2})
    packet = {'stage': 'assign', 'operator_answer': '已准入'}
    with pytest.raises(native.NativeError):
        manager.turn(state, tmp_path, packet)
    with pytest.raises(native.NativeError) as caught:
        manager.turn(state, tmp_path, {**packet, 'operator_answer': '未准入补充'}, operator_update=True)
    assert caught.value.manager_packet == packet
    assert caught.value.analyst is None


def test_manager_transfer_keeps_pending_expert_and_its_receipts(consultation, external, tmp_path, monkeypatch):
    from foundry_runtime import foundry_goal as goal, _foundry_goal_transitions as transitions
    from test_goal_preflight_recovery import retained_goal
    state, calls, failures = consultation
    failures.add(1)
    packet = {'stage': 'assign'}
    with pytest.raises(native.NativeError):
        manager.turn(state, external[0], packet)
    _, folder, manifest = retained_goal(external, external[1], tmp_path)
    manifest.update(manager_state=state, manager_packet=packet)
    monkeypatch.setattr(transitions, 'resolve_executable', lambda _: Path(sys.executable))
    transferred = transitions.manager(goal, folder, manifest, provider=None, migrate=True, answer='继续')
    new = transferred['manager_state']
    assert new['expert_native_dir'] == str(experts.native_root(state))
    result, next_state, _, _ = manager.turn(new, external[0], transferred['manager_packet'])
    assert result['expert_consultation']['answer']
    assert 'expert_pending_request' not in next_state
    assert experts.observe(next_state)[0]['status'] == 'answered'
    assert len([c for c in calls if c['packet'].get('stage') == 'expert_consultation']) == 2


def test_expert_status_and_usage_are_separate_and_read_only(consultation, tmp_path):
    from foundry_runtime import _followup, _foundry_goal_usage as usage
    state, calls, failures = consultation
    folder = tmp_path / 'goal'; folder.mkdir()
    path = folder / 'manifest.json'
    path.write_text(json.dumps({'schema_version': 1, 'run_id': 'fixture', 'order': {}, 'manager_state': state}))
    failures.add(1)
    with pytest.raises(native.NativeError):
        manager.turn(state, tmp_path, {'stage': 'assign'})
    assert _followup.status(folder)['expert_consultations'][0]['status'] == 'blocked'
    manager.turn(state, tmp_path, {'stage': 'assign'})
    before = {p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    report = usage.task_report([folder, path])
    assert len(report['expert_consultations']) == 1
    assert report['expert_consultations'][0]['status'] == 'answered'
    assert report['manager_requested_model_usage'] == []
    assert report['coding_model_usage'] == []
    if state['backend'] == 'codex':
        assert report['expert_requested_model_usage'][0]['requested_model'] == state['expert']['model']
        assert any(r['role'] == 'expert' for r in report['api_price_equivalent']['rows'])
    else:
        assert report['claude_expert_session_usage']['sessions']
        assert any(r.get('roles') == ['expert'] for r in report['api_price_equivalent']['rows'])
    assert before == {p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_reviewer_expert_recovery_delivers_answer_to_unchanged_worker(consultation, external, tmp_path, monkeypatch):
    from foundry_runtime import foundry_goal as goal, _foundry_dispatch as dispatch
    from test_goal_preflight_recovery import retained_goal
    state, calls, failures = consultation
    root, folder, manifest = retained_goal(external, external[1], tmp_path)
    checkpoint = json.loads((folder / 'coding-checkpoint.json').read_bytes())
    for key in ('coding_checkpoint', 'manager_state', 'manager_packet', 'execution_decision'):
        manifest.pop(key)
    review = tmp_path / 'review.json'
    review.write_text(json.dumps({'reviews': [{'correction_guidance': {'model': 'expert', 'reason': 'ownership'}}]}))
    manifest.update(expert_state=state, expert_packet={'prior_review': goal._fact(review)},
        manager_phase='expert_pending', manager_start={'options': checkpoint['options'],
            'executable': sys.executable, 'coding_session': None})
    goal._write(folder / 'manifest.json', manifest)
    failures.add(0)
    with pytest.raises(native.NativeError):
        goal.recover_goal(external[2], root)
    assert not external[3]
    original = dispatch.coding_argv
    def coding(*args, **kwargs):
        argv = original(*args, **kwargs)
        argv[-1] += "; Path('candidate.txt').write_text('worker result')"
        return argv
    monkeypatch.setattr(dispatch, 'coding_argv', coding)
    outcome = goal.recover_goal(external[2], root, answer='保留单一账本写入者')
    assert outcome['status'] == 'project_handoff'
    assert len(external[3]) == 1 and len(calls) == 2
    saved = json.loads((folder / 'manifest.json').read_bytes())
    assert saved['execution_decision']['operator_answer'] == '保留单一账本写入者'
    assert saved['execution_decision']['expert_consultation']['answer']
    assert saved['manager_start']['options']['coding_model'] == 'recorded-codex'
    log = Path(json.loads((folder / 'coding-checkpoint.json').read_bytes())['evidence'][1]['path'])
    assert 'single ledger writer' in log.read_text(encoding='utf-8')
    assert '保留单一账本写入者' in log.read_text(encoding='utf-8')
    assert goal.recover_goal(external[2], root)['status'] == 'project_handoff'
    assert len(external[3]) == 1


def test_review_guided_worker_is_retained_only_for_new_consultation_contract(tmp_path):
    from foundry_runtime._foundry_goal_auto import correction_options
    path = tmp_path / 'review.json'
    path.write_text(json.dumps({'reviews': [{'findings': ['ownership'], 'correction_guidance': {'model': 'expert'}}]}))
    base = {'amend_from': str(path), 'compute_assignment': {'correction_policy': 'review-guided'},
            'coding_model': 'worker', 'coding_effort': 'medium'}
    correction = {'model': 'expensive', 'effort': 'high'}
    assert correction_options(base, correction)['coding_model'] == 'expensive'
    new = correction_options({**base, 'native_team': {'expert_consultation': True}}, correction)
    assert new['coding_model'] == 'worker' and new['coding_effort'] == 'medium'
