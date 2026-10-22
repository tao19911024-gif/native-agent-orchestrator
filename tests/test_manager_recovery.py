"""Manager recovery using real native allocations/receipts and local child I/O."""
import json
import os
from pathlib import Path
import sys

import pytest

from foundry_runtime import _code_index, _foundry_manager as manager, foundry_native as native


@pytest.fixture
def conversation(tmp_path, monkeypatch, request):
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    monkeypatch.setattr(manager, 'resolve_executable', lambda _: Path(sys.executable))
    monkeypatch.setattr(_code_index, 'prepare', lambda _, **kw: None)
    requests, failures = [], {0}
    real_spawn = native.OwnedProcess.spawn
    def spawn(argv, **kwargs):
        if argv[1:3] == ['-B', '-c']:
            return real_spawn(argv, **kwargs)  # Local worker fixture, not a model call.
        source = kwargs['stdin']
        envelope = json.loads(source.read().rsplit(b'\n\n', 1)[-1]); source.seek(0)
        fail = len(requests) in failures
        requests.append((envelope, argv))
        result = {'action': 'delegate', 'instruction': envelope['packet'].get('operator_answer') or 'original'}
        rows = [{'type': 'system', 'subtype': 'init', 'tools': ['StructuredOutput'],
                 'mcp_servers': [], 'skills': [], 'plugins': [], 'model': 'fixture', 'session_id': 'original-manager'}]
        if fail and getattr(request, 'param', None):
            rows.append(request.param)
        if not fail:
            rows += [
                {'type': 'assistant', 'session_id': 'original-manager', 'message': {'role': 'assistant', 'content': [
                    {'type': 'tool_use', 'name': 'StructuredOutput', 'id': 'format-1', 'input': result}]}},
                {'type': 'user', 'session_id': 'original-manager', 'message': {'role': 'user', 'content': [
                    {'type': 'tool_result', 'tool_use_id': 'format-1', 'content': 'Structured output provided successfully'}]}},
                {'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 2,
                 'result': json.dumps(result), 'structured_output': result,
                 'usage': {'input_tokens': 10, 'output_tokens': 20, 'iterations': [{'type': 'message'}]}, 'total_cost_usd': 0.0}]
        if '--output-schema' in argv:
            rows = [{'type': 'thread.started', 'thread_id': '00000000-0000-0000-0000-000000000001'},
                {'type': 'turn.started'},
                {'type': 'item.completed', 'item': {'id': 'final', 'type': 'agent_message', 'text': json.dumps(result)}},
                {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'cached_input_tokens': 0, 'output_tokens': 5}}]
        script = 'import sys;sys.stdin.buffer.read();sys.stdout.write(' + repr('\n'.join(map(json.dumps, rows))+'\n') + ');sys.exit(' + str(1 if fail else 0) + ')'
        return real_spawn([sys.executable, '-B', '-c', script], **kwargs)
    monkeypatch.setattr(native.OwnedProcess, 'spawn', spawn)
    state = {'backend': 'claude', 'model': None, 'effort': None, 'last_call_id': None,
             'native_dir': str(tmp_path / 'manager')}
    return state, requests, failures


@pytest.mark.parametrize('second_failure', [False, True])
def test_interrupted_manager_accepts_updated_packet_in_original_conversation(conversation, tmp_path, second_failure):
    state, requests, failures = conversation
    old = {'stage': 'worker_returned', 'returncode': 0}
    with pytest.raises(native.NativeError):
        manager.turn(state, tmp_path, old)
    frozen = {str(p): p.read_bytes() for p in Path(state['native_dir']).glob('call-*/*') if p.is_file()}
    new = {**old, 'operator_answer': 'new ruling'}
    if second_failure:
        failures.add(2)
        with pytest.raises(native.NativeError):
            manager.turn(state, tmp_path, new)
        new = {**old, 'operator_answer': 'newer ruling'}
    decision, updated, usage, accepted = manager.turn(state, tmp_path, new)
    assert accepted == new
    assert decision['instruction'] == new['operator_answer']
    assert updated['session_id'] == 'original-manager'
    assert [r[0]['packet'] for r in requests] == ([old, old, {**old, 'operator_answer': 'new ruling'},
         {**old, 'operator_answer': 'new ruling'}, new] if second_failure else [old, old, new])
    for _, argv in requests[1:]:
        assert argv[argv.index('--resume') + 1] == 'original-manager'
    assert all(Path(p).read_bytes() == raw for p, raw in frozen.items())
    # Simulate goal not saving the last native result: replay reads receipts.
    count = len(requests)
    again, _, _, _ = manager.turn(state, tmp_path, new)
    assert again == decision and len(requests) == count


@pytest.mark.parametrize('initial_manager', [False, True])
def test_goal_admission_failure_does_not_replace_saved_packet(external, tmp_path, monkeypatch, initial_manager):
    from test_goal_preflight_recovery import retained_goal
    from foundry_runtime import foundry_goal as goal
    scope = external[1]
    root, folder, manifest = retained_goal(external, scope, tmp_path)
    if initial_manager:
        checkpoint = json.loads((folder / 'coding-checkpoint.json').read_bytes())
        manifest.pop('coding_checkpoint')
        manifest.update(manager_phase='assignment_pending', manager_start={
            'options': checkpoint['options'], 'executable': sys.executable, 'coding_session': None})
        goal._write(folder / 'manifest.json', manifest)
    def refused(*args, **kwargs):
        assert args[2]['operator_answer'] == 'not admitted'
        raise ValueError('native admission refused before a new call')
    monkeypatch.setattr(manager, 'turn', refused)
    monkeypatch.setattr(goal, '_record_usage', lambda *a, **k: None)
    with pytest.raises(ValueError, match='admission refused'):
        goal.recover_goal(external[2], root, answer='not admitted')
    current = json.loads((folder / 'manifest.json').read_bytes())
    assert current['manager_packet'] == manifest['manager_packet']
    assert current['manager_state'] == manifest['manager_state']
    assert current['manager_phase'] == manifest['manager_phase']
    with pytest.raises(ValueError, match='admission refused'):
        goal.recover_goal(external[2], root, answer='not admitted')


from test_installed_runtime import external, offline


def test_crash_after_admitted_supplement_never_returns_older_decision(conversation, tmp_path):
    state, requests, failures = conversation
    old = {'stage': 'worker_returned'}
    new = {**old, 'operator_answer': 'accepted before crash'}
    failures.add(2)
    with pytest.raises(native.NativeError):
        manager.turn(state, tmp_path, old)
    with pytest.raises(native.NativeError):
        manager.turn(state, tmp_path, new)
    # The goal still has the old packet/state; native evidence owns the successor.
    decision, _, _, accepted = manager.turn(state, tmp_path, old)
    assert decision['instruction'] == new['operator_answer'] and accepted == new
    assert len(requests) == 4 and requests[-1][0]['packet'] == new
    # Explicitly returning to an older ruling is a new operator instruction.
    decision, _, _, accepted = manager.turn(state, tmp_path, old, operator_update=True)
    assert decision['instruction'] == 'original' and accepted == old
    assert len(requests) == 5


def test_local_request_refusal_does_not_cancel_conversation(conversation, tmp_path, monkeypatch):
    state, requests, failures = conversation
    failures.clear()
    _, state, _, _ = manager.turn(state, tmp_path, {'stage': 'assign'})
    folder = Path(state['native_dir'])
    assert not list(folder.glob('cancellation*.json'))
    def refused(*args):
        raise ValueError('local request not admitted')
    with monkeypatch.context() as patch:
        patch.setattr(native.NativeAnalyst, 'manager_request', refused)
        with pytest.raises(ValueError, match='not admitted'):
            manager.turn(state, tmp_path, {'stage': 'worker_returned'})
    assert not list(folder.glob('cancellation*.json'))
    _, _, _, packet = manager.turn(state, tmp_path, {'stage': 'worker_returned'})
    assert packet['stage'] == 'worker_returned' and len(requests) == 2


def test_stable_call_uses_short_physical_folder_and_full_recorded_identity(conversation, tmp_path):
    state, requests, failures = conversation
    failures.clear()
    decision, _, _, _ = manager.turn(state, tmp_path, {'stage': 'worker_returned'})
    folders = list(Path(state['native_dir']).glob('call-*'))
    assert len(folders) == 1
    assert folders[0].name == 'call-' + decision['call_id'][:16]
    allocation = json.loads((folders[0] / 'reservation.json').read_bytes())
    assert allocation['call_id'] == decision['call_id']
    assert len(allocation['call_id']) == 64


@pytest.mark.skipif(os.name != 'nt', reason='Windows path budget')
def test_call_path_budget_fails_before_partial_evidence(tmp_path):
    folder = tmp_path / ('x' * 180) / ('call-' + 'a' * 16)
    with pytest.raises(native.NativeError, match='evidence path is too long'):
        native._check_call_path_budget(folder)
    assert not folder.exists()


def test_goal_ready_recovery_settles_admitted_successor_before_worker(conversation, external, tmp_path, monkeypatch):
    from test_goal_preflight_recovery import retained_goal
    from foundry_runtime import foundry_goal as goal, _foundry_dispatch as dispatch
    state, requests, failures = conversation
    failures.clear()
    tree, scope, order, _ = external
    old = {'stage': 'worker_returned'}
    decision, state, _, _ = manager.turn(state, tree, old)
    assert manager.turn(state, tree, old, pending_only=True) is None
    assert len(requests) == 1
    root, folder, manifest = retained_goal(external, scope, tmp_path)
    manifest.update(manager_state=state, manager_packet=old, execution_decision={'manager': decision})
    goal._write(folder / 'manifest.json', manifest)
    failures.add(1)
    new = {**old, 'operator_answer': 'use newly admitted ruling'}
    with pytest.raises(native.NativeError):
        manager.turn(state, tree, new, operator_update=True)
    # Crash before goal receives the exception: its disk state is still ready/A.
    def worker(session, workspace, tag, text, **kwargs):
        assert new['operator_answer'] in text
        assert session == 'original-session'
        assert json.loads((folder / 'manifest.json').read_bytes())['manager_packet'] == new
        raise RuntimeError('correct decision reached worker')
    monkeypatch.setattr(dispatch, 'resume_order', worker)
    monkeypatch.setattr(goal, '_record_usage', lambda *a, **k: None)
    with pytest.raises(RuntimeError, match='correct decision reached worker'):
        goal.recover_goal(order, root)
    assert len(requests) == 3 and requests[-1][0]['packet'] == new


@pytest.mark.parametrize('backend', ['claude', 'codex'])
@pytest.mark.parametrize('interruption', ['exit', 'cancel'])
def test_manager_recovery_before_session_init_reissues_retained_packet(conversation, tmp_path, monkeypatch, backend, interruption):
    state, requests, failures = conversation
    state['backend'] = backend
    failures.clear()
    packet = {'stage': 'worker_returned', 'worker_evidence': {'handoff': 'retained handoff'},
              'unanswered_live_events': [{'text': 'design ruling needed'}]}
    fixture_spawn = native.OwnedProcess.spawn
    def before_init(argv, **kwargs):
        return fixture_spawn([sys.executable, '-B', '-c',
            'import sys,time; sys.stdin.buffer.read(); ' + ('time.sleep(60)' if interruption == 'cancel' else 'sys.exit(1)')], **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(native.OwnedProcess, 'spawn', before_init)
        with pytest.raises((native.NativeError, TimeoutError)):
            with manager.live_budget(ordinary_seconds=.25, expert_seconds=.5, total_seconds=1):
                manager.turn(state, tmp_path, packet)
    folder = Path(state['native_dir'])
    frozen = {str(p): p.read_bytes() for p in folder.glob('call-*/*') if p.is_file()}
    decision, updated, _, accepted = manager.turn(state, tmp_path, packet)
    assert decision and accepted == packet and updated['session_id']
    assert len(requests) == 1 and requests[0][0]['packet'] == packet
    assert '--resume' not in requests[0][1] and 'resume' not in requests[0][1]
    assert all(Path(p).read_bytes() == raw for p, raw in frozen.items())
    assert manager.turn(state, tmp_path, packet)[0] == decision
    assert len(requests) == 1  # Replay reads the successful receipt, no duplicate paid call.
