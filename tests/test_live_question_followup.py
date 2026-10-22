import io
import json
from threading import Event, Thread
from types import SimpleNamespace

from foundry_runtime import foundry_goal as goal
from foundry_runtime._goal_intervention import LiveEvents, live_scope, retain_events, carry_events, answer_events
from foundry_runtime.app_server_transport import AppServer
from test_installed_runtime import external, offline
import pytest


def test_threshold_oscillation_does_not_repeat_paid_signal_and_records_residual_fall():
    events = LiveEvents(lambda rows: None, threshold=120000)
    def sample(number, value):
        events.observe({'method':'item/completed','params':{'threadId':'m',
            'item':{'type':'contextCompaction','id':str(number)}}}, 'root')
        return events.observe({'method':'thread/tokenUsage/updated','params':{'threadId':'m',
            'tokenUsage':{'last':{'inputTokens':value}}}}, 'root')
    sample(0, 104000)
    for n in range(1, 251):
        observed = sample(n, 95500 if n%2 else 96500)
    assert events.events.qsize() == 1
    assert observed['renewal_followup']['residual_fell']
    assert not observed['renewal_followup']['rearmed']
    assert sample(251, 70000)['renewal_followup']['rearmed']
    sample(252, 104000)
    assert events.events.qsize() == 2


def test_terminal_race_retains_questions_without_another_model_call():
    captured = []
    with live_scope(lambda rows: (_ for _ in ()).throw(AssertionError('no paid call')), capture=captured.extend):
        app = AppServer(SimpleNamespace(), io.StringIO(), deadline=None)
    question = {'kind':'worker_message', 'text':'design question'}
    app.live_events.events.put(question)
    app.outcome_ready.set()
    app.live_events.drain(app)
    app.live_events.close(app)
    assert captured == [question]


def test_unanswered_questions_survive_failure_await_input_and_outcome(tmp_path, monkeypatch):
    old, new = {'item_id':'old','text':'contract?'}, {'item_id':'new','text':'ownership?'}
    manifest = {'live_manager_error': {'events':[old], 'message':'transport failed'}}
    retain_events(manifest, [new, old])
    packet = carry_events(manifest, {'stage':'worker_returned'})
    assert packet['unanswered_live_events'] == [old, new]
    answer_events(manifest, packet, {'action':'await_input'})
    goal._write(tmp_path/'manifest.json', manifest)
    monkeypatch.setattr(goal.goal_usage, 'task_usage', lambda *a, **k: (_ for _ in ()).throw(ValueError('usage unavailable')))
    outcome = {'status':'execution_stopped'}
    goal._record_usage(tmp_path, {}, None, outcome)
    assert outcome['unanswered_live_events'] == [old, new]
    answer_events(manifest, {'unanswered_live_events':[old]}, {'action':'delegate'})
    assert manifest['unanswered_live_events'] == [new]
    assert 'live_manager_error' not in manifest
    answer_events(manifest, {'unanswered_live_events':[new]}, {'action':'delegate'})
    assert 'unanswered_live_events' not in carry_events(manifest, packet)


def test_close_joins_manifest_writer_before_returning_owner(tmp_path):
    started, release, closed = Event(), Event(), Event()
    path = tmp_path/'manifest.json'
    goal._write(path, {'owner':'initial'})
    def decide(rows):
        started.set()
        assert release.wait(5)
        goal._write(path, {'owner':'manager'})
    events = LiveEvents(decide)
    app = SimpleNamespace(outcome_ready=Event(), _log=lambda text: None)
    events.events.put({'kind':'worker_message'})
    events.drain(app)
    assert started.wait(2)
    def finish():
        events.close(app)
        goal._write(path, {'owner':'returned-controller'})
        closed.set()
    thread = Thread(target=finish)
    thread.start()
    try:
        assert not closed.wait(.05)
        assert json.loads(path.read_bytes()) == {'owner':'initial'}
    finally:
        release.set()
        thread.join(5)
    assert closed.is_set()
    assert json.loads(path.read_bytes()) == {'owner':'returned-controller'}


@pytest.mark.parametrize('phase', ['feedback_pending', 'delegation_ready'])
def test_recovered_worker_return_carries_unanswered_question_to_manager(external, tmp_path, monkeypatch, phase):
    from test_goal_preflight_recovery import retained_goal
    from foundry_runtime import _foundry_manager as manager
    tree, scope, order, _ = external
    root, folder, manifest = retained_goal(external, scope, tmp_path)
    question = {'kind':'worker_message','item_id':'unanswered-design','text':'which owner?'}
    manifest.update(manager_phase=phase, unanswered_live_events=[question])
    goal._write(folder/'manifest.json', manifest)
    observed = []
    def receive(state, workspace, packet, **kwargs):
        observed.append(packet)
        assert packet['stage']=='worker_returned'
        assert packet['unanswered_live_events']==[question]
        assert not kwargs['pending_only']
        raise RuntimeError('fixture stops after observed carry')
    monkeypatch.setattr(manager, 'turn', receive)
    with pytest.raises(RuntimeError, match='fixture stops after observed carry'):
        goal.recover_goal(order, root)
    assert len(observed)==1
    assert json.loads((folder/'manifest.json').read_bytes())['unanswered_live_events']==[question]
