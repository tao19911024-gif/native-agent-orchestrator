"""Existing queue/packet/receipt owners; no paid services or business tasks."""
import hashlib
import json
from copy import deepcopy
from threading import Event
from types import SimpleNamespace

import pytest

from foundry_runtime import _expert_consultation as experts
from foundry_runtime import _foundry_manager as manager
from foundry_runtime._goal_intervention import LiveEvents
from foundry_runtime._foundry_native_request import build_prompt
from test_manager_recovery import conversation
from test_expert_consultation import consultation


def fact(path):
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_native_packet_keeps_new_input_and_one_copy_of_evidence(conversation, tmp_path, backend):
    state, calls, failures = conversation
    failures.clear()
    state.update(backend=backend, effective_coordination=1)
    event = {'kind': 'worker_message', 'text': 'question with substantial evidence ' * 100}
    new = {'kind': 'worker_message', 'text': 'different finding'}
    packet = {'stage': 'worker_running', 'events': [event, new],
              'unanswered_live_events': [event], 'operator_answer': 'new ruling'}
    original = deepcopy(packet)
    decision, _, _, accepted = manager.turn(state, tmp_path, packet)
    assert packet == original
    assert accepted['events'] == [new] and accepted['unanswered_live_events'] == [event]
    assert accepted['operator_answer'] == decision['instruction'] == 'new ruling'
    assert len(json.dumps(accepted)) < len(json.dumps(packet)) - 3000
    prompt = build_prompt(calls[0][0], backend, ValueError)
    assert b'Decision packet:' in prompt
    # Lost goal write: authoritative native receipt supplies the same result.
    assert manager.turn(state, tmp_path, packet)[0] == decision
    assert len(calls) == 1


@pytest.mark.parametrize('backend', ['claude', 'codex'])
@pytest.mark.parametrize('change', ['none', 'source', 'question', 'unknown', 'legacy', 'operator'])
def test_repeated_expert_question_reuses_only_unchanged_evidence(tmp_path, backend, change):
    source = tmp_path / '代码.py'
    source.write_text('original', encoding='utf-8')
    receipt = tmp_path / 'receipt.json'
    receipt.write_text('{}')
    question = {'trigger': 'architecture', 'question': 'Who owns writes?',
                'evidence_refs': [str(source) + ':1']}
    if change == 'unknown':
        question['evidence_refs'] = ['not-a-readable-artifact']
    state = {'native_dir': str(tmp_path / 'manager'), 'effective_coordination': 1,
             'expert': {'backend': backend, 'model': 'fixture', 'effort': 'high'}}
    if change == 'legacy':
        state.pop('effective_coordination')
    calls, packets = [], []
    def turn(current, workspace, packet, **kwargs):
        if packet['stage'] == 'expert_consultation':
            calls.append(deepcopy(packet))
            return {'instruction': 'single writer', 'evidence': fact(receipt)}, current, {}, packet
        packets.append(deepcopy(packet))
        if len(packets) == 2:
            if change == 'source':
                source.write_text('changed', encoding='utf-8')
            if change == 'question':
                question['question'] = 'What about a new dependency?'
        result = {'action': 'consult_expert', 'expert_request': deepcopy(question), 'evidence': fact(receipt)}
        if len(packets) == 3:
            result = {'action': 'delegate', 'instruction': 'implement', 'expert_request': None}
        return result, current, {}, packet
    result = experts.coordinate(turn, state, tmp_path, {'stage': 'assign'}, operator_update=change == 'operator')
    assert result[0]['instruction'] == 'implement'
    assert len(calls) == (1 if change == 'none' else 2)
    if change == 'none':
        assert packets[1]['expert_consultation'] == packets[2]['expert_consultation']
        assert calls[0]['evidence_snapshot'] == [fact(source)]


def test_changed_during_consultation_or_missing_receipt_is_not_reusable(tmp_path):
    source = tmp_path / 'source.py'; source.write_text('before')
    receipt = tmp_path / 'receipt.json'; receipt.write_text('{}')
    state = {'native_dir': str(tmp_path / 'manager'), 'effective_coordination': 1,
             'expert': {'backend': 'codex', 'model': 'fixture'}}
    question = {'trigger': 'architecture', 'question': 'owner?', 'evidence_refs': [str(source)]}
    def turn(current, workspace, packet):
        source.write_text('after')
        return {'instruction': 'advice', 'evidence': fact(receipt)}, current, {}, packet
    answer = experts.consult(turn, state, tmp_path, question, fact(receipt))
    assert 'evidence_snapshot' not in answer
    assert not experts.reusable_answer(answer, question, state, tmp_path)
    answer['evidence_snapshot'] = [fact(source)]
    receipt.unlink()
    assert not experts.reusable_answer(answer, question, state, tmp_path)


def test_native_expert_snapshot_replay_and_changed_source(consultation, tmp_path):
    state, calls, _ = consultation
    state['effective_coordination'] = 1
    source = tmp_path / 'source.py'; source.write_text('original')
    question = {'trigger': 'architecture', 'question': 'owner?', 'evidence_refs': [str(source) + ':1']}
    answer = experts.consult(manager._turn, state, tmp_path, question, {'manager_call': 'fixture'})
    assert experts.reusable_answer(answer, question, state, tmp_path)
    again = experts.consult(manager._turn, state, tmp_path, question, {'manager_call': 'fixture'})
    assert again == answer and len(calls) == 1
    source.write_text('changed')
    assert not experts.reusable_answer(answer, question, state, tmp_path)
    updated = experts.consult(manager._turn, state, tmp_path, question, {'manager_call': 'fixture'})
    assert updated['consultation_id'] == answer['consultation_id'] and len(calls) == 2
    assert updated['evidence'] != answer['evidence']
    assert experts.reusable_answer(updated, question, state, tmp_path)


def test_changed_source_after_failed_expert_settles_original_native_call(consultation, tmp_path):
    from foundry_runtime.foundry_native import NativeError
    state, calls, failures = consultation
    state['effective_coordination'] = 1
    source = tmp_path / 'source.py'; source.write_text('original')
    question = {'trigger': 'architecture', 'question': 'owner?', 'evidence_refs': [str(source)]}
    original = fact(source)
    failures.add(0)
    with pytest.raises(NativeError):
        experts.consult(manager._turn, state, tmp_path, question, {'manager_call': 'fixture'})
    source.write_text('changed')
    answer = experts.consult(manager._turn, state, tmp_path, question, {'manager_call': 'fixture'})
    assert len(calls) == 3
    assert calls[1]['packet']['evidence_snapshot'] == [original]
    assert calls[2]['packet']['evidence_snapshot'] == [fact(source)]
    assert experts.reusable_answer(answer, question, state, tmp_path)


def test_identical_question_after_new_tool_result_survives_pending_dedup():
    events = LiveEvents(lambda rows: None)
    def item(ident, kind='agentMessage', **extra):
        return {'method': 'item/completed', 'params': {'threadId': 'root', 'turnId': 'turn',
                'item': {'id': ident, 'type': kind, **extra}}}
    def ask(ident):
        events.observe(item(ident, text='FOUNDRY_MANAGER_QUESTION owner?'), 'root')
    ask('1'); ask('2')
    assert events.events.qsize() == 1
    events.observe({'method': 'heartbeat'}, 'root')
    events.observe(item('reason', 'reasoning'), 'root')
    ask('3')
    assert events.events.qsize() == 1
    tool = item('tool', 'unknownFutureTool', result='new evidence')
    events.observe(tool, 'root'); events.observe(tool, 'root')
    ask('4')
    rows = [events.events.get_nowait(), events.events.get_nowait()]
    assert [row['evidence_generation'] for row in rows] == [0, 1]
    assert rows[0]['text'] == rows[1]['text']


def test_recovered_context_alert_does_not_call_manager_and_can_rearm():
    calls, logs = [], []
    events = LiveEvents(lambda rows: calls.append(rows), threshold=100000)
    app = SimpleNamespace(outcome_ready=Event(), turn_id='turn', steer=lambda *a: None, _log=logs.append)
    def usage(value):
        events.observe({'method': 'thread/tokenUsage/updated', 'params': {
            'threadId': 'member', 'tokenUsage': {'last': {'inputTokens': value}}}}, 'root')
    def compact(ident):
        events.observe({'method': 'item/completed', 'params': {'threadId': 'member',
            'item': {'type': 'contextCompaction', 'id': ident}}}, 'root')
    compact('1'); usage(85000)
    assert events.events.qsize() == 1
    usage(50000)  # Measured progress before the manager consumes the queued alert.
    events.drain(app)
    assert not calls and events.pending is None
    assert json.loads(logs[0])['reason'] == 'measured_context_recovery'
    compact('2'); usage(85000)
    events.drain(app); events.close(app)
    assert len(calls) == 1 and calls[0][0]['compaction_id'] == '2'
