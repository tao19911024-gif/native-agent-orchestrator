"""Whole-order completion and intermediate reviews, using offline native I/O."""
import json
from pathlib import Path
import sys

import pytest

from test_installed_runtime import external, offline
from test_manager_input_wait import manager_io
from foundry_runtime import foundry_goal as goal, _foundry_dispatch as dispatch
from foundry_runtime import _foundry_manager as manager
from foundry_runtime._foundry_native_request import build_prompt
from foundry_runtime._foundry_response_contract import response_contract_for_request, validate_response_shape


def decision(action, instruction, remaining=()):
    return {'action': action, 'instruction': instruction, 'remaining_work': list(remaining)}


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_new_completion_contract_preserves_historical_requests(backend):
    old = {'kind': 'coordinate', 'packet': {'stage': 'worker_returned'}}
    frozen = build_prompt(old, backend, ValueError)
    request = {**old, 'goal_completion': 1}
    contract = response_contract_for_request(request)
    assert 'remaining_work' in contract['required']
    validate_response_shape(decision('review', 'Design complete', ['Implement the feature']), contract)
    prompt = build_prompt(request, backend, ValueError)
    assert b'intermediate independent review' in prompt
    assert b'whole-goal handback' in prompt
    assert b'remaining_work' not in frozen
    assert build_prompt(old, backend, ValueError) == frozen
    assert manager.configuration('opus', Path('manager'))['goal_completion'] == 1


@pytest.mark.parametrize('backend', ['claude', 'codex'])
@pytest.mark.parametrize('handoff', ['review', 'deliver_artifacts'])
@pytest.mark.parametrize('interrupted', [False, True])
def test_design_review_does_not_end_implementation_goal(external, manager_io, tmp_path, monkeypatch, backend, handoff, interrupted):
    tree, scope, order, workers = external
    requests, decisions = manager_io
    order.write_text('Write a design, obtain independent design review, then implement. '
                     'Hand back the finished implementation, not the design commit.')
    pending = ['Independent design review', 'Implementation and checks']
    decisions.extend([
        decision('delegate', 'Write the design', pending),
        decision(handoff, 'Review the design commit', pending),
        decision('delegate', 'Obtain independent design review; return its evidence', ['Implementation and checks']),
        decision('delegate', 'Implement the reviewed design and run checks'),
        decision('review', 'The full order is implemented; final independent review'),
    ])
    if handoff == 'deliver_artifacts':
        for row in decisions:
            row['artifacts'] = ['design.md'] if row['action'] == handoff else []
    original = dispatch.dispatch_order
    resume = dispatch.resume_order
    sessions, reviews = [], []
    folder = tmp_path / 'goal'
    turn = manager.turn
    stopped = []

    def coordinate(*a, **kw):
        if interrupted and len(workers) == 2 and not stopped:
            stopped.append(True)
            raise RuntimeError('offline interruption after completed design review')
        return turn(*a, **kw)

    monkeypatch.setattr(manager, 'turn', coordinate)

    def worker(invoke, *args, **kwargs):
        if (folder / 'outcome.json').exists():
            assert json.loads((folder / 'outcome.json').read_bytes())['status'] != 'candidate_verified'
        sessions.append(args[0] if invoke is resume else None)
        result = invoke(*args, **kwargs)
        step = len(workers)
        if step == 1:
            (tree / 'design.md').write_text('Design of the requested implementation')
        elif step == 2:
            # Independent member output is retained evidence, not a second goal.
            (tmp_path / 'design-review.json').write_text(json.dumps({'reviewer': 'independent-member', 'verdict': 'PASS'}))
            return result
        else:
            assert (tmp_path / 'design-review.json').is_file()
            (tree / 'feature.py').write_text('def value(): return 42\n')
        goal._git(tree, 'add', '.')
        goal._git(tree, 'commit', '-qm', 'complete assigned stage')
        return result

    def finish(checkpoint, evidence):
        reviews.append(checkpoint)
        assert len(workers) == 3
        assert (tree / 'feature.py').is_file()
        assert checkpoint['context']['goal_completion'] == 1
        return {'status': 'candidate_verified'}

    monkeypatch.setattr(dispatch, 'dispatch_order', lambda *a, **kw: worker(original, *a, **kw))
    monkeypatch.setattr(dispatch, 'resume_order', lambda *a, **kw: worker(resume, *a, **kw))
    monkeypatch.setattr(goal, '_finish_review', finish)
    def run():
        return goal.run_goal(order, folder, Path(sys.executable), coding_launcher='exec',
            project_record=scope.record(), manager_state={'backend': backend, 'model': None, 'effort': None,
                'native_dir': str(tmp_path / 'manager'), 'goal_completion': 1,
                **({'artifact_delivery': True} if handoff == 'deliver_artifacts' else {})})
    if interrupted:
        with pytest.raises(RuntimeError, match='offline interruption'):
            run()
        frozen = {p: p.read_bytes() for p in (tmp_path / 'manager').glob('call-*/*') if p.is_file()}
        result = goal.recover_goal(order, folder)
        assert all(p.read_bytes() == raw for p, raw in frozen.items())
    else:
        result = run()
    assert result['status'] == 'candidate_verified'
    assert len(workers) == 3 and len(reviews) == 1 and len(requests) == 5
    assert requests[2]['packet']['review_refusal']['workspace']['remaining_work'] == pending
    assert requests[2]['packet']['order'] == goal._fact(order)
    assert requests[3]['packet']['order'] == goal._fact(order)
    assert not decisions
    assert sessions == [None, 'original-worker', 'original-worker']


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_final_review_keeps_full_order_over_manager_slice(backend):
    old = {'kind': 'review', 'packet': {'original_order': 'Implement feature'}}
    frozen = build_prompt(old, backend, ValueError)
    new = build_prompt({**old, 'packet': {**old['packet'], 'goal_completion': 1}}, backend, ValueError)
    assert b'missing required implementation' in new
    assert b'manager' in new
    assert build_prompt(old, backend, ValueError) == frozen


def test_document_only_order_can_finish(external, manager_io, tmp_path, monkeypatch):
    tree, scope, order, workers = external
    _, decisions = manager_io
    order.write_text('Deliver a reviewed design document; implementation is outside this order.')
    decisions.extend([decision('delegate', 'Write the document'), decision('review', 'Review the complete document order')])
    original = dispatch.dispatch_order
    def worker(*a, **kw):
        result = original(*a, **kw)
        (tree / 'design.md').write_text('Complete requested design')
        goal._git(tree, 'add', '.'); goal._git(tree, 'commit', '-qm', 'design')
        return result
    monkeypatch.setattr(dispatch, 'dispatch_order', worker)
    monkeypatch.setattr(goal, '_finish_review', lambda *a: {'status': 'candidate_verified'})
    result = goal.run_goal(order, tmp_path / 'goal', Path(sys.executable), coding_launcher='exec',
        project_record=scope.record(), manager_state={'backend': 'claude', 'model': None, 'effort': None,
            'native_dir': str(tmp_path / 'manager'), 'goal_completion': 1})
    assert result['status'] == 'candidate_verified' and len(workers) == 1


def test_repeating_incomplete_handoff_uses_existing_no_progress_feedback(external, manager_io, tmp_path, monkeypatch):
    _, scope, order, workers = external
    requests, decisions = manager_io
    decisions.extend([decision('delegate', 'Write design', ['Implement']),
                      decision('review', 'Design complete', ['Implement']),
                      decision('review', 'Design complete', ['Implement'])])
    monkeypatch.setattr(goal, '_finish_review', lambda *a: pytest.fail('incomplete goal reached final review'))
    result = goal.run_goal(order, tmp_path / 'goal', Path(sys.executable), coding_launcher='exec',
        project_record=scope.record(), manager_state={'backend': 'claude', 'model': None, 'effort': None,
            'native_dir': str(tmp_path / 'manager'), 'goal_completion': 1})
    assert result['status'] == 'awaiting_input'
    assert result['input_request']['source'] == 'repeated_review_refusal'
    assert len(workers) == 1 and len(requests) == 3


@pytest.mark.parametrize('publication_crash', [False, True])
def test_live_incomplete_review_returns_to_same_worker(external, manager_io, tmp_path, monkeypatch, publication_crash):
    from foundry_runtime import _dispatch_claude as claude
    tree, scope, order, _ = external
    requests, decisions = manager_io
    decisions.extend([decision('delegate', 'Write the design', ['Implement']),
                      decision('review', 'Design ready', ['Implement']),
                      decision('delegate', 'Implement after the design review'),
                      decision('review', 'Full implementation complete')])
    calls = tmp_path / 'worker-inputs.jsonl'
    monkeypatch.setattr(claude, 'resolve_executable', lambda _: Path(sys.executable))
    monkeypatch.setattr(goal, '_session_from_log', lambda path:
        next(line.removeprefix('Claude session: ') for line in Path(path).read_text().splitlines()
             if line.startswith('Claude session: ')))

    def argv(executable, session, **kwargs):
        script = '''
import sys,json,subprocess
from pathlib import Path
def emit(row):
 row['session_id']=SESSION
 print(json.dumps(row),flush=True)
emit({'type':'system','subtype':'init'})
for line in sys.stdin:
 count=len(Path(CALLS).read_text().splitlines()) if Path(CALLS).exists() else 0
 with Path(CALLS).open('a') as out: out.write(line)
 filename='design.md' if count==0 else 'feature.py'
 Path(filename).write_text('design' if count==0 else 'value=42')
 subprocess.run(['git','add',filename],check=True,stdout=sys.stderr)
 subprocess.run(['git','commit','-qm',filename],check=True,stdout=sys.stderr)
 emit(json.loads(line))
 emit({'type':'assistant','uuid':str(count),'message':{'content':[{'type':'text','text':'FOUNDRY_DELIVERABLE_DONE Assigned stage complete.'}]}})
 emit({'type':'result','subtype':'success','is_error':False,'result':'Assigned stage complete'})
'''.replace('SESSION', repr(session)).replace('CALLS', repr(str(calls)))
        return [executable, '-B', '-c', script]

    monkeypatch.setattr(claude, 'argv', argv)
    reviews = []
    def finish(checkpoint, folder):
        assert (tree / 'feature.py').is_file()
        reviews.append(checkpoint)
        return {'status': 'candidate_verified'}
    monkeypatch.setattr(goal, '_finish_review', finish)
    folder = tmp_path / 'goal'
    def run():
        return goal.run_goal(order, folder, Path(sys.executable), coding_launcher='claude',
            project_record=scope.record(), manager_state={'backend': 'claude', 'model': None, 'effort': None,
                'native_dir': str(tmp_path / 'manager'), 'goal_completion': 1,
                'live_coordination': 1, 'claude_live_events': 1})
    if publication_crash:
        with monkeypatch.context() as patch:
            patch.setattr(goal, '_publish_coding_checkpoint', lambda *a, **kw:
                (_ for _ in ()).throw(RuntimeError('offline checkpoint interruption')))
            with pytest.raises(RuntimeError, match='offline checkpoint interruption'):
                run()
        result = goal.recover_goal(order, folder)
    else:
        result = run()
    assert result['status'] == 'candidate_verified'
    assert len(reviews) == 1 and len(requests) == 4 and not decisions
    assert requests[2]['packet']['review_refusal']['workspace']['remaining_work'] == ['Implement']
    inputs = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(inputs) == 2
    assert len({row['session_id'] for row in inputs}) == 1
