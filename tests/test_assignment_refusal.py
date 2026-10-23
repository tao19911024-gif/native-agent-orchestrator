import json
from pathlib import Path
import sys

import pytest

from test_installed_runtime import external, offline
from test_manager_input_wait import manager_io, wait
from foundry_runtime import foundry_goal as goal
from foundry_runtime._project_execution import ProjectExecution, AdmissionRefused


@pytest.mark.parametrize('backend', ['claude', 'codex'])
@pytest.mark.parametrize('repeat', [False, True])
def test_refused_assignment_returns_to_same_manager(external, manager_io, tmp_path, monkeypatch, backend, repeat):
    tree, scope, order, workers = external
    requests, decisions = manager_io
    decisions.extend([{'action': 'delegate', 'instruction': text} for text in
                      ['first valid task', 'false premise', 'false premise' if repeat else 'revised task']])
    if not repeat:
        decisions.append(wait('Finished fixture'))
    original = ProjectExecution.preflight
    refusals = []
    def preflight(self, tag, text, **kwargs):
        if 'false premise' in text:
            receipt = tmp_path / ('refusal-%d.json' % len(refusals))
            receipt.write_text(json.dumps({'premise': 'false', 'reason': 'already excluded'}))
            refusals.append(receipt)
            raise AdmissionRefused('False premise: already excluded', receipt=receipt, instructions=text)
        return original(self, tag, text, **kwargs)
    monkeypatch.setattr(ProjectExecution, 'preflight', preflight)
    state = {'backend': backend, 'model': None, 'effort': None, 'last_call_id': None,
             'native_dir': str(tmp_path/'manager')}
    folder = tmp_path/'goal'
    result = goal.run_goal(order, folder, Path(sys.executable), manager_state=state,
                           coding_launcher='exec', project_record=scope.record())
    assert result['status'] == 'awaiting_input'
    event = requests[2]['packet']['unanswered_live_events'][0]
    assert event['kind'] == 'assignment_refused'
    assert event['receipt']['path'] == str(refusals[0].resolve())
    assert 'already excluded' in event['judgement']
    assert len(workers) == (1 if repeat else 2)
    assert not decisions
    if repeat:
        assert 'identical refused assignment' in result['input_request']['question']
        assert len(refusals) == 2
        assert goal.recover_goal(order, folder) == result
        decisions.extend([{'action': 'delegate', 'instruction': 'commander corrected premise'}, wait('After answer')])
        answered = goal.recover_goal(order, folder, answer='Use the corrected premise')
        assert answered['status'] == 'awaiting_input'
        assert answered['input_request']['question'] == 'After answer'
        assert len(workers) == 2


def test_retained_unknown_call_diagnostic_is_explicit_and_read_only(tmp_path):
    from foundry_runtime._foundry_cli_help import unresolved_native_calls
    path = tmp_path/'manager-usage.json'
    path.write_text(json.dumps({'calls': [{'state': 'unknown', 'call_id': 'call-identity',
        'kind': 'coordinate', 'evidence': 'exact/native/call', 'error': 'identity changed',
        'root_pid': 42, 'root_creation_time': 99, 'cleanup': None}]}))
    before = path.read_bytes()
    row, = unresolved_native_calls(tmp_path)
    assert row['call_id'] == 'call-identity' and row['role'] == 'manager'
    assert row['evidence'] == 'exact/native/call' and row['error'] == 'identity changed'
    assert 'not a current process' in row['observation']
    assert path.read_bytes() == before


@pytest.mark.parametrize('backend', ['claude', 'codex'])
@pytest.mark.parametrize('repeat', [False, True])
def test_recovered_manager_can_correct_stale_hash(external, manager_io, tmp_path, monkeypatch, backend, repeat):
    """Replay the retained judgement at admission, never touch the business goal.

    Native managers/workers are local protocol fixtures. This verifies the goal
    control flow after a completed refusal, not the model's factual judgement.
    """
    import hashlib
    fixture_path = Path(__file__).parent / 'fixtures' / 'stale_hash_refusal.json'
    specimen = json.loads(fixture_path.read_bytes())
    tree, scope, order, workers = external
    requests, decisions = manager_io
    decisions.extend([{'action': 'delegate', 'instruction': 'Complete initial work'}, wait('Checkpoint')])
    state = {'backend': backend, 'model': None, 'effort': None, 'last_call_id': None,
             'native_dir': str(tmp_path / 'manager')}
    folder = tmp_path / 'goal'
    result = goal.run_goal(order, folder, Path(sys.executable), manager_state=state,
                           coding_launcher='exec', project_record=scope.record())
    assert result['status'] == 'awaiting_input' and len(workers) == 1
    checkpoint_outcome = (folder / 'outcome.json').read_bytes()
    original = ProjectExecution.preflight
    refused, admitted = [], []
    def preflight(self, tag, text, **kwargs):
        if specimen['stale_hash'] in text:
            assert kwargs['operation'] == 'resume'
            receipt = tmp_path / ('stale-hash-refusal-%d.json' % len(refused))
            receipt.write_text(json.dumps(specimen['result'], ensure_ascii=False), encoding='utf-8')
            refused.append(receipt)
            raise AdmissionRefused(json.dumps(specimen['result'], ensure_ascii=False),
                                   receipt=receipt, instructions=text)
        admitted.append(text)
        return original(self, tag, text, **kwargs)
    monkeypatch.setattr(ProjectExecution, 'preflight', preflight)
    stale = 'Continue using D1_SOURCE_AUDIT.md SHA256 ' + specimen['stale_hash']
    revised = stale if repeat else 'Continue using D1_SOURCE_AUDIT.md SHA256 ' + specimen['current_hash']
    decisions.extend([{'action': 'delegate', 'instruction': text} for text in (stale, revised)])
    if not repeat:
        decisions.append(wait('Revised assignment completed'))
    result = goal.recover_goal(order, folder, answer='Continue from the checkpoint')
    assert result['status'] == 'awaiting_input' and not decisions
    # An explicit answer creates the established continuation goal. Observe that
    # returned owner, not the immutable predecessor's earlier question.
    continued = Path(result['continuation_evidence'])
    assert Path(result['task_usage']['path']).parent == continued
    assert continued != folder
    assert (folder / 'outcome.json').read_bytes() == checkpoint_outcome
    feedback = requests[3]['packet']
    assert feedback['stage'] == 'worker_returned' and feedback['worker_started'] is False
    event, = feedback['unanswered_live_events']
    assert event['kind'] == 'assignment_refused'
    assert json.loads(event['judgement']) == specimen['result']
    assert event['receipt'] == {'path': str(refused[0].resolve()),
                                'sha256': hashlib.sha256(refused[0].read_bytes()).hexdigest()}
    assert event['assignment_sha256'] == hashlib.sha256(stale.encode('utf-8')).hexdigest()
    assert len(workers) == (1 if repeat else 2)
    if repeat:
        assert len(refused) == 2 and not admitted
        assert 'identical refused assignment' in result['input_request']['question']
        calls = len(requests)
        observed = goal.recover_goal(order, continued)
        # Only the parent continuation API adds its returned directory locator.
        assert {**observed, 'continuation_evidence': str(continued),
                'result_outcome': goal._fact(continued / 'outcome.json')} == result
        assert len(requests) == calls  # Observing the question does not retry a model.
    else:
        assert len(refused) == 1 and any(specimen['current_hash'] in text for text in admitted)
        assert result['input_request']['question'] == 'Revised assignment completed'
