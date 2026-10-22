import json
from pathlib import Path

import pytest

from foundry_runtime import _tool_history as history


@pytest.fixture(params=['claude', 'codex'])
def member(request, tmp_path):
    provider = request.param
    source = tmp_path / 'source.py'
    source.write_text('evidence\n' * 200)
    log = tmp_path / 'member.jsonl'
    rows = ([{'isSidechain': True}] if provider == 'claude' else
            [{'type': 'session_meta', 'payload': {'source': {'subagent': {'parent_thread_id': 'p'}}}}])

    def turn(text):
        if provider == 'claude':
            rows.append({'type': 'user', 'message': {'content': [{'type': 'text', 'text': text}]}})
        else:
            rows.extend([{'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': str(len(rows))}},
                         {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                          'content': [{'type': 'input_text', 'text': text}]}}])
        log.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')

    event = {'tool_name': 'Read', 'tool_input': {'file_path': str(source)},
             'cwd': str(tmp_path), 'transcript_path': str(log)}
    policy = {'version': 6, 'root_role': 'manager',
              'reading_budget': {'unit': 'lines', 'single': 100, 'turn': 300}}
    return turn, event, policy


def test_expert_identity_survives_new_turn_without_redeclaring(member):
    turn, event, policy = member
    turn('[activity: expert] decide ownership')
    assert 'Expert reading budget' in history.reading_check(event, policy)
    turn('Continue using the updated evidence.')
    assert 'Expert reading budget' in history.reading_check(event, policy)
    event['tool_input']['description'] = '#decide: verify the conflicting owner'
    assert history.reading_check(event, policy) is None


@pytest.mark.parametrize('assignment', ['[activity: review] review the diff',
                                      '[activity: expert]\n[activity: review] ambiguous reassignment'])
def test_new_role_declaration_supersedes_old_expert(member, assignment):
    turn, event, policy = member
    turn('[activity: expert] decide ownership')
    turn(assignment)
    assert history.reading_check(event, policy) is None


def test_native_reviewer_is_not_classified_from_old_expert_assignment(member):
    turn, event, policy = member
    turn('[activity: expert] decide ownership')
    event['agent_type'] = 'reviewer'
    assert history.reading_check(event, policy) is None


def test_truncated_missing_assignment_remains_unknown(member, monkeypatch):
    turn, event, policy = member
    turn('[activity: expert] decide ownership')
    turn('Continue.')
    header, rows, _ = history.transcript(event['transcript_path'])
    suffix, _ = history.current_turn(rows)
    monkeypatch.setattr(history, 'transcript', lambda _: (header, suffix, False))
    assert history.reading_check(event, policy) is None
    event['agent_type'] = 'expert'
    assert 'Expert reading budget' in history.reading_check(event, policy)


def test_manager_and_expert_get_role_appropriate_recovery(member):
    _, event, policy = member
    event.pop('transcript_path')
    manager = history.reading_check(event, policy)
    assert 'Delegate' in manager and 'through the manager' not in manager
    expert = history.reading_check(event, {**policy, 'root_role': 'expert'})
    assert 'Ask the manager' in expert


def test_prior_turn_reading_is_not_charged_again(member):
    turn, event, policy = member
    turn('[activity: expert] decide ownership')
    log = Path(event['transcript_path'])
    first = log.read_text()
    turn('Continue.')
    later = log.read_text()[len(first):]
    output = 'prior evidence\n' * 500
    if json.loads(first.splitlines()[0]).get('isSidechain'):
        reads = [{'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'r',
                  'name': 'Read', 'input': {'file_path': 'prior.py'}}]}},
                 {'type': 'user', 'message': {'content': [{'type': 'tool_result',
                  'tool_use_id': 'r', 'content': output}]}}]
    else:
        reads = [{'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'r',
                  'name': 'Read', 'arguments': json.dumps({'file_path': 'prior.py'})}},
                 {'type': 'response_item', 'payload': {'type': 'function_call_output',
                  'call_id': 'r', 'output': output}}]
    log.write_text(first + '\n'.join(json.dumps(r) for r in reads) + '\n')
    event['tool_input']['limit'] = 10
    assert 'Expert reading budget' in history.reading_check(event, policy)
    log.write_text(log.read_text() + later)
    assert history.reading_check(event, policy) is None
