"""Repeated paid decisions terminate using source and result observations."""
from pathlib import Path
import sys

import pytest

from test_installed_runtime import external, offline
from test_manager_input_wait import manager_io, wait
from foundry_runtime import foundry_goal as goal, _foundry_dispatch as dispatch
from foundry_runtime import _repository_observation


def run(external, tmp_path, backend):
    _, scope, order, _ = external
    return goal.run_goal(order, tmp_path/'goal', Path(sys.executable), coding_launcher='exec',
        project_record=scope.record(), manager_state={'backend': backend, 'model': None,
        'effort': None, 'native_dir': str(tmp_path/'manager')})


@pytest.mark.parametrize('backend', ['claude', 'codex'])
@pytest.mark.parametrize('changed', [False, True])
def test_repeated_dirty_review_requires_changed_contents(external, manager_io, tmp_path, monkeypatch, backend, changed):
    tree, _, _, workers = external
    requests, decisions = manager_io
    original = dispatch.dispatch_order
    def dirty(*args, **kwargs):
        result = original(*args, **kwargs)
        (tree/'unfinished.txt').write_text('first')
        goal._git(tree, 'add', 'unfinished.txt')
        return result
    monkeypatch.setattr(dispatch, 'dispatch_order', dirty)
    snapshot = _repository_observation.snapshot
    observations = []
    def observe(*args, **kwargs):
        if changed and len(observations) == 2:
            # Same porcelain (staged addition); changed bytes are real progress.
            (tree/'unfinished.txt').write_text('second')
            goal._git(tree, 'add', 'unfinished.txt')
        result = snapshot(*args, **kwargs)
        observations.append(result)
        return result
    monkeypatch.setattr(_repository_observation, 'snapshot', observe)
    decisions.extend([{'action': 'delegate', 'instruction': 'Prepare candidate'},
        *[{'action': 'review', 'instruction': 'Review candidate'}]*2, wait('Changed candidate needs ruling')])
    result = run(external, tmp_path, backend)
    assert result['status'] == 'awaiting_input' and len(workers) == 1
    assert len(requests) == (4 if changed else 3)
    if not changed:
        assert result['input_request']['source'] == 'repeated_review_refusal'
        assert 'same dirty candidate' in result['input_request']['question']


@pytest.mark.parametrize('backend', ['claude', 'codex'])
@pytest.mark.parametrize('progress', ['none', 'report', 'source', 'unknown'])
def test_duplicate_worker_observation_stops_before_next_manager(external, manager_io, tmp_path, monkeypatch, backend, progress):
    tree, _, _, workers = external
    requests, decisions = manager_io
    def argv(*args, **kwargs):
        output = Path(args[args.index('-o') + 1])
        workers.append(1)
        report = 'result ' + str(len(workers) if progress == 'report' else 1)
        script = 'import sys; from pathlib import Path; sys.stdin.read(); '
        script += 'Path(' + repr(str(output)) + ').write_text(' + repr(report) + '); '
        if progress == 'source':
            script += 'Path("unfinished.txt").write_text(' + repr(str(len(workers))) + '); '
        script += 'print("fixture completed")'
        return [sys.executable, '-B', '-c', script]
    monkeypatch.setattr(dispatch, 'coding_argv', argv)
    if progress == 'unknown':
        def unavailable(*args, **kwargs):
            raise ValueError('unavailable observation')
        monkeypatch.setattr(_repository_observation, 'snapshot', unavailable)
    decisions.extend([{'action': 'delegate', 'instruction': 'Same assignment'}]*2 + [wait('New decision')])
    result = run(external, tmp_path, backend)
    assert result['status'] == 'awaiting_input' and len(workers) == 2
    assert len(requests) == (2 if progress == 'none' else 3)
    if progress == 'none':
        assert result['input_request']['source'] == 'repeated_worker_result'
        assert (tmp_path/'goal'/'coding-checkpoint.json').exists()
        assert result['input_request']['question'].startswith('The same worker assignment')
        calls = len(requests)
        assert goal.recover_goal(external[2], tmp_path/'goal') == result
        assert len(requests) == calls
        decisions.clear()
        decisions.extend([{'action': 'delegate', 'instruction': 'Same assignment'}, wait('After explicit answer')])
        answered = goal.recover_goal(external[2], tmp_path/'goal', answer='Repeat the assignment once')
        assert answered['status'] == 'awaiting_input'
        assert answered['input_request']['question'] == 'After explicit answer'
        assert len(workers) == 3 and len(requests) == calls + 2
