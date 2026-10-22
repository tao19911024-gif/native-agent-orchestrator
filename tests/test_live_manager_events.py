import io
import pytest
from types import SimpleNamespace

from foundry_runtime.app_server_transport import AppServer
from foundry_runtime._goal_intervention import live_scope
from foundry_runtime import _foundry_manager as manager
from test_manager_recovery import conversation


def settle(app):
    app.live_events.drain(app)
    if app.live_events.pending is not None:
        try:
            app.live_events.pending.result(timeout=20)
        except Exception:
            pass
    app.live_events.drain(app)


def test_never_ending_root_calls_real_manager_and_receives_same_turn_steer(conversation, tmp_path):
    state, calls, failures = conversation
    failures.clear()
    replies = []
    call_ids = []
    def decide(events):
        nonlocal state
        decision, state, usage, packet = manager.turn(state, tmp_path, {'stage':'worker_running','events':events})
        call_ids.append(state['last_call_id'])
        return decision['instruction']
    with live_scope(decide):
        app = AppServer(SimpleNamespace(), io.StringIO(), deadline=None)
    app.thread_id, app.turn_id = 'root', 'running-turn'
    app.steer = lambda text, turn: replies.append((text, turn))
    for index, marker in enumerate(('FOUNDRY_MANAGER_QUESTION contract choice', 'FOUNDRY_DELIVERABLE_DONE evidence/path')):
        event = {'method':'item/completed','params':{'threadId':'root','turnId':'running-turn',
                 'item':{'type':'agentMessage','id':str(index),'phase':'commentary','text':marker}}}
        app._observe(event)
        app._observe(event)
        settle(app)
        assert not app.outcome_ready.is_set()  # Fake root never publishes turn/completed.
    assert len(calls) == 2 and len(replies) == 2
    assert all(turn == 'running-turn' for _, turn in replies)
    assert calls[1][0]['continue_from'] == call_ids[0]


def test_residual_and_manager_error_preserve_live_worker():
    failures=[]
    def decide(events):
        failures.extend(events)
        raise RuntimeError('manager temporary failure')
    with live_scope(decide):
        app=AppServer(SimpleNamespace(),io.StringIO(),deadline=None)
    app.thread_id,app.turn_id='root','turn'
    app.live_events.residual.threshold=120000
    for event in [
        {'method':'item/completed','params':{'threadId':'member','item':{'type':'contextCompaction','id':'c'}}},
        {'method':'thread/tokenUsage/updated','params':{'threadId':'member','tokenUsage':{'last':{'inputTokens':104000}}}}]:
        app.live_events.observe(event,'root')
    settle(app)
    assert failures[0]['kind']=='context_renewal'
    assert not app.outcome_ready.is_set()


from test_installed_runtime import external, offline


def test_renewal_storm_coalesces_until_measured_recovery_or_replacement():
    from foundry_runtime._goal_intervention import LiveEvents
    events = LiveEvents(lambda rows: None, threshold=120000)
    def sample(member, number, tokens):
        events.observe({'method':'item/completed','params':{'threadId':member,
            'item':{'type':'contextCompaction','id':str(number)}}}, 'root')
        events.observe({'method':'thread/tokenUsage/updated','params':{'threadId':member,
            'tokenUsage':{'last':{'inputTokens':tokens}}}}, 'root')
    for number in range(250):
        sample('old', number, 104000)
    assert events.events.qsize() == 1
    sample('replacement', 1, 104000)
    assert events.events.qsize() == 2
    sample('old', 251, 60000)
    sample('old', 252, 104000)
    assert events.events.qsize() == 3


def test_slow_manager_leaves_supervision_available_and_no_late_steer():
    from threading import Event
    started, release = Event(), Event()
    def decide(rows):
        started.set()
        assert release.wait(5)
        return 'late response'
    with live_scope(decide):
        app = AppServer(SimpleNamespace(), io.StringIO(), deadline=None)
    app.live_events.events.put({'kind':'worker_message'})
    steers = []
    app.steer = lambda *args: steers.append(args)
    app.live_events.drain(app)
    assert started.wait(2)
    # The supervisor can keep publishing activity and input requests while busy.
    for _ in range(10):
        app.live_events.drain(app)
        assert app.input_requests() == []
    app.outcome_ready.set()
    release.set()
    app.live_events.close(app)
    assert not steers


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_live_timeout_cleans_native_child_and_retains_recoverable_request(conversation, tmp_path, monkeypatch, backend):
    import json
    import sys
    from foundry_runtime import foundry_native as native
    state, calls, failures = conversation
    state['backend'] = backend
    failures.clear()
    fixture_spawn = native.OwnedProcess.spawn
    # This case proves cancellation of an admitted, running native child.
    # Real host startup can exceed the two-second test budget before admission,
    # which exercises a different contract and cannot produce a call receipt.
    # Advance the logical clock only after a real child emits its native init;
    # retain the original budgets and the real cancellation/cleanup/recovery.
    clock, timers = [0.0], []
    class Timer:
        def __init__(self, delay, callback):
            self.delay, self.callback = delay, callback
            self.fired = False
            timers.append(self)
        def start(self): pass
        def cancel(self): pass
        def join(self): pass
    fixture_protocol = native._protocol
    class ReadyProtocol:
        def __init__(self, protocol): self.protocol = protocol
        def __getattr__(self, name): return getattr(self.protocol, name)
        def accept(self, event):
            result = self.protocol.accept(event)
            if timers and not timers[0].fired:
                timer = timers[0]
                timer.fired = True
                clock[0] += timer.delay
                timer.callback()
            return result
    monkeypatch.setattr(manager, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(manager, 'Timer', Timer)
    monkeypatch.setattr(native, '_protocol', lambda *args: ReadyProtocol(fixture_protocol(*args)))
    def stall(argv, **kwargs):
        init = json.dumps({'type':'system','subtype':'init','session_id':'original-manager',
                          'tools':['StructuredOutput'],'mcp_servers':[],'skills':[],'plugins':[], 'model':'fixture'})
        if backend == 'codex':
            init = json.dumps({'type':'thread.started','thread_id':'00000000-0000-0000-0000-000000000001'})
        return fixture_spawn([sys.executable, '-B', '-c',
            'import sys,time;sys.stdin.buffer.read();print(' + repr(init) + ',flush=True);time.sleep(60)'], **kwargs)
    monkeypatch.setattr(native.OwnedProcess, 'spawn', stall)
    with pytest.raises(TimeoutError, match='live manager'):
        with manager.live_budget(ordinary_seconds=.5, expert_seconds=1, total_seconds=2):
            manager.turn(state, tmp_path, {'stage':'worker_running'})
    receipts = list((tmp_path/'manager').glob('call-*/receipt.json'))
    assert timers[0].delay == .5 and timers[0].fired
    assert receipts
    receipt = json.loads(receipts[0].read_bytes())
    assert receipt['outcome'] == 'cancelled'
    assert receipt['cleanup']['cleanup_verified']
    monkeypatch.setattr(native.OwnedProcess, 'spawn', fixture_spawn)
    decision, updated, _, _ = manager.turn(state, tmp_path, {'stage':'worker_running'})
    assert decision and updated['last_call_id']


def test_live_budget_uses_longer_expert_window(conversation, tmp_path, monkeypatch):
    state, _, failures = conversation
    failures.clear()
    delays = []
    class Timer:
        def __init__(self, delay, callback, args=()):
            delays.append(delay)
        def start(self): pass
        def cancel(self): pass
        def join(self): pass
    monkeypatch.setattr(manager, 'Timer', Timer)
    with manager.live_budget(ordinary_seconds=3, expert_seconds=9, total_seconds=12):
        manager.turn(state, tmp_path, {'stage':'expert_consultation'})
    assert 8.5 < delays[0] <= 9


def test_live_budget_expired_before_admission_has_no_call_to_cancel(conversation, tmp_path, monkeypatch):
    state, calls, failures = conversation
    failures.clear()
    clock = [0.0]
    factory = manager.NativeAnalyst
    def slow_initialization(*args, **kwargs):
        analyst = factory(*args, **kwargs)
        clock[0] = 3.0
        return analyst
    monkeypatch.setattr(manager, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(manager, 'NativeAnalyst', slow_initialization)
    with pytest.raises(TimeoutError, match='live manager'):
        with manager.live_budget(ordinary_seconds=.5, expert_seconds=1, total_seconds=2):
            manager.turn(state, tmp_path, {'stage': 'worker_running'})
    assert not calls
    assert not list((tmp_path/'manager').glob('call-*/receipt.json'))
    decision, updated, _, _ = manager.turn(state, tmp_path, {'stage': 'worker_running'})
    assert decision and updated['last_call_id']


@pytest.mark.parametrize('fail_first', [False, True])
def test_goal_owner_services_events_before_worker_returns(conversation, external, tmp_path, monkeypatch, fail_first):
    import json
    import pytest
    from test_goal_preflight_recovery import retained_goal
    from foundry_runtime import foundry_goal as goal, _foundry_dispatch as dispatch
    state, calls, failures = conversation
    failures.clear()
    state['live_coordination']=1
    tree, scope, order, _ = external
    decision,state,_,_=manager.turn(state,tree,{'stage':'worker_returned'})
    root,folder,manifest=retained_goal(external,scope,tmp_path)
    manifest.update(manager_state=state,manager_packet={'stage':'worker_returned'},execution_decision={'manager':decision})
    goal._write(folder/'manifest.json',manifest)
    steers=[]
    def worker(*args,**kwargs):
        app=AppServer(SimpleNamespace(),io.StringIO(),deadline=None)
        app.thread_id,app.turn_id='root','never-ended'
        app.steer=lambda text,turn:steers.append((text,turn))
        for number,marker in enumerate(('FOUNDRY_MANAGER_QUESTION design', 'FOUNDRY_DELIVERABLE_DONE slice')):
            app._observe({'method':'item/completed','params':{'threadId':'root','turnId':'never-ended',
                'item':{'type':'agentMessage','id':str(number),'phase':'commentary','text':marker}}})
            with monkeypatch.context() as scoped:
                if fail_first and number == 0:
                    def timeout(*args, **kwargs):
                        raise TimeoutError('fixture live manager timeout')
                    scoped.setattr(manager, 'turn', timeout)
                settle(app)
            if fail_first and number == 0:
                failed=json.loads((folder/'manifest.json').read_bytes())
                assert failed['live_manager_error']['message']=='fixture live manager timeout'
                assert not app.outcome_ready.is_set() and not steers
                assert len(failed['unanswered_live_events']) == 1
        saved=json.loads((folder/'manifest.json').read_bytes())
        assert saved['coordination']['during_worker_turn']
        assert saved['live_manager_decision']['evidence']['path']
        assert 'live_manager_error' not in saved
        assert saved['unanswered_live_events'] == []
        assert saved['live_manager_call']['automatic_timeout_seconds'] == 28800
        assert saved['live_manager_call']['ordinary_timeout_seconds'] == 7200
        assert saved['live_manager_call']['expert_timeout_seconds'] == 14400
        assert saved['live_manager_call']['soft_warning_seconds'] == 120
        assert saved['live_manager_call']['stage_seconds']['coordinate'] >= 0
        assert len(steers)==(1 if fail_first else 2) and not app.outcome_ready.is_set()
        app.live_events.close(app)
        raise RuntimeError('fixture ends only after live manager proof')
    monkeypatch.setattr(dispatch,'resume_order',worker)
    with pytest.raises(RuntimeError,match='fixture ends only after live manager proof'):
        goal.recover_goal(order,root)
    assert len(calls)==(2 if fail_first else 3)
