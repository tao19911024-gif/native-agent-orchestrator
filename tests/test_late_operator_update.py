"""Operator updates arriving during coordination precede its next action."""
from pathlib import Path
import sys
from threading import Event
from types import SimpleNamespace

import pytest

from foundry_runtime import foundry_goal as goal, _foundry_manager as manager
from foundry_runtime import _foundry_dispatch as dispatch
from foundry_runtime import _goal_intervention as intervention
from test_installed_runtime import external, offline
from test_manager_input_wait import manager_io, wait


@pytest.mark.parametrize('backend', ['claude', 'codex'])
@pytest.mark.parametrize('action', ['delegate', 'review', 'await_input', 'deliver_artifacts'])
@pytest.mark.parametrize('publication_crash', [False, True])
def test_update_arriving_during_ruling_precedes_next_stage(external, manager_io, tmp_path, monkeypatch, backend, action, publication_crash):
    _, scope, order, workers = external
    requests, decisions = manager_io
    delivery = {'artifacts': []} if action == 'deliver_artifacts' else {}
    decisions.extend([{'action': action, 'instruction': 'Prior instructions', **delivery},
                      {**wait('New pause request received'), **delivery}])
    directory = scope.evidence('TASK1') / 'goal'
    turn = manager.turn
    submitted = []
    def coordinate(*args, **kwargs):
        result = turn(*args, **kwargs)
        if not submitted:
            submitted.append(intervention.submit(directory, 'TASK1', 'Pause before starting more work.', scope))
        return result
    monkeypatch.setattr(manager, 'turn', coordinate)
    write = goal._write
    failures = []
    def publish(path, value):
        write(path, value)
        if (publication_crash and not failures and Path(path) == directory / 'manifest.json'
                and value.get('manager_phase') == 'feedback_pending'
                and value.get('manager_packet', {}).get('operator_message_ids')):
            failures.append(1)
            raise RuntimeError('fixture interrupted late-input publication')
    monkeypatch.setattr(goal, '_write', publish)
    def run():
        return goal.run_goal(order, directory, Path(sys.executable), coding_launcher='exec',
            project_record=scope.record(), manager_state={'backend': backend, 'model': None,
                'effort': None, 'native_dir': str(tmp_path / 'manager'),
                **({'artifact_delivery': True} if delivery else {})})
    if publication_crash:
        with pytest.raises(RuntimeError, match='fixture interrupted late-input publication'):
            run()
        assert len(failures) == 1 and not workers
        result = goal.recover_goal(order, directory)
    else:
        result = run()
    assert result['status'] == 'awaiting_input'
    assert result['input_request']['question'] == 'New pause request received'
    assert not workers and len(requests) == 2
    assert submitted[0]['message_id'] in requests[1]['packet']['operator_message_ids']
    assert intervention.observe(directory)[0]['status'] == 'responded'
    assert goal.recover_goal(order, directory) == result and len(requests) == 2


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_late_update_can_authorize_useful_work(external, manager_io, tmp_path, monkeypatch, backend):
    _, scope, order, workers = external
    requests, decisions = manager_io
    decisions.extend([{'action': 'delegate', 'instruction': 'Obsolete assignment.'},
        {'action': 'delegate', 'instruction': 'New approved assignment.'}, wait('Work returned')])
    directory = scope.evidence('TASK1') / 'goal'
    turn = manager.turn
    def coordinate(*args, **kwargs):
        result = turn(*args, **kwargs)
        if len(requests) == 1:
            intervention.submit(directory, 'TASK1', 'Use the revised assignment.', scope)
        return result
    monkeypatch.setattr(manager, 'turn', coordinate)
    result = goal.run_goal(order, directory, Path(sys.executable), coding_launcher='exec',
        project_record=scope.record(), manager_state={'backend': backend, 'model': None,
            'effort': None, 'native_dir': str(tmp_path / 'manager')})
    assert result['status'] == 'awaiting_input' and len(workers) == 1 and len(requests) == 3
    text = (scope.evidence('TASK1') / 'last_message.md').read_text(encoding='utf-8')
    assert 'New approved assignment.' in text and 'Obsolete assignment.' not in text


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_late_live_update_is_consumed_before_steering(external, manager_io, tmp_path, monkeypatch, backend):
    _, scope, order, workers = external
    requests, decisions = manager_io
    decisions.extend([{'action': 'delegate', 'instruction': 'Initial assignment'},
        {'action': 'delegate', 'instruction': 'Stale live continuation'}, wait('Live pause received')])
    directory = scope.evidence('TASK1') / 'goal'
    turn = manager.turn
    def coordinate(state, tree, packet, **kwargs):
        result = turn(state, tree, packet, **kwargs)
        if packet.get('stage') == 'worker_running' and len(requests) == 2:
            intervention.submit(directory, 'TASK1', 'Pause before further live instructions.', scope)
        return result
    monkeypatch.setattr(manager, 'turn', coordinate)
    launch = dispatch.dispatch_order
    steers = []
    def execute(*args, **kwargs):
        result = launch(*args, **kwargs)
        events = intervention.LiveEvents()
        app = SimpleNamespace(outcome_ready=Event(), turn_id='turn', _log=lambda value: None,
                              steer=lambda text, turn: steers.append(text))
        events.events.put({'kind': 'worker_message', 'text': 'FOUNDRY_DELIVERABLE_DONE Ready'})
        events.drain(app)
        events.pending.result(timeout=15)
        events.drain(app)
        assert events.finished and not steers
        events.close(app)
        return result
    monkeypatch.setattr(dispatch, 'dispatch_order', execute)
    result = goal.run_goal(order, directory, Path(sys.executable), coding_launcher='exec',
        project_record=scope.record(), manager_state={'backend': backend, 'model': None,
            'effort': None, 'native_dir': str(tmp_path / 'manager'), 'live_coordination': 1})
    assert result['status'] == 'awaiting_input' and result['input_request']['question'] == 'Live pause received'
    assert len(workers) == 1 and len(requests) == 3
