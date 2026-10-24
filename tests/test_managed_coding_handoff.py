"""Managed coding-provider transfer reuses manager feedback and native custody."""
import json
from pathlib import Path
import sys

import pytest

from test_installed_runtime import external, offline
from test_manager_input_wait import manager_io, wait
from test_owned_goal_recovery import SERVER, SID
from foundry_runtime import foundry_goal as goal, app_server_transport as transport
from foundry_runtime import _dispatch_claude, foundry_native_support


@pytest.mark.parametrize('source,interruption', [
    ('codex', None), ('codex', 'before_handoff'), ('codex', 'after_handoff'),
    ('claude', None), ('claude', 'before_checkpoint')])
def test_failed_provider_handoff_keeps_manager_and_resumes_new_worker(external, manager_io, tmp_path, monkeypatch, source, interruption):
    tree, scope, order, _ = external
    requests, decisions = manager_io
    decisions.append({'action': 'delegate', 'instruction': 'Complete the retained assignment'})
    original = transport.run_executor
    codex_calls, claude_calls = [], []

    def codex(*args, **kwargs):
        codex_calls.append(kwargs.get('resume_thread_id'))
        script = 'HANG=False\n' + SERVER
        if source == 'codex':
            script = script.replace("'status':'completed','items':[],'error':None",
                "'status':'failed','items':[],'error':{'message':'model_not_found: model unavailable', 'codexErrorInfo':'badRequest'}")
        def spawn(argv, **options):
            return transport.OwnedProcess.spawn([sys.executable, '-B', '-c', script], **options)
        return original(*args, process_factory=spawn, **kwargs)

    def claude(executable, session, **options):
        claude_calls.append((session, options.get('resume')))
        rows = [{'type': 'system', 'subtype': 'init', 'session_id': session},
                {'type': 'result', 'subtype': 'success', 'is_error': False,
                 'session_id': session, 'result': 'retained assignment completed'}]
        if source == 'claude':
            rows[-1].update(subtype='error_during_execution', is_error=True, result='model_not_found: model unavailable')
        script = 'import sys;sys.stdin.read();print(' + repr('\n'.join(map(json.dumps, rows))) + ')'
        if source == 'claude':
            script += ';sys.exit(1)'
        return [sys.executable, '-B', '-c', script]

    monkeypatch.setattr(transport, 'run_executor', codex)
    monkeypatch.setattr(_dispatch_claude, 'resolve_executable', lambda _: Path(sys.executable))
    monkeypatch.setattr(_dispatch_claude, 'argv', claude)
    monkeypatch.setattr(foundry_native_support, 'resolve_executable', lambda _: Path(sys.executable))
    monkeypatch.setattr(goal, '_session_from_log', lambda path:
        next((line.removeprefix('Claude session: ') for line in Path(path).read_text().splitlines()
              if line.startswith('Claude session: ')), SID))
    folder = tmp_path / 'goal'
    state = {'backend': 'claude', 'model': None, 'effort': None,
             'native_dir': str(tmp_path / 'manager')}
    stopped = goal.run_goal(order, folder, Path(sys.executable), coding_launcher='app-server' if source == 'codex' else 'claude',
                            project_record=scope.record(), manager_state=state)
    assert stopped['status'] == 'coding_incomplete'
    before = {p: p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    native_before = {p: p.read_bytes() for p in (tmp_path / 'manager').glob('call-*/*') if p.is_file()}
    old = json.loads((folder / 'manifest.json').read_bytes())
    decisions.extend([{'action': 'delegate', 'instruction': 'Use the replacement coding provider'},
                      {'action': 'delegate', 'instruction': 'Continue that same replacement session'}, wait('Ready')])
    target = 'claude' if source == 'codex' else 'codex'
    options = dict(answer=f'Model unavailable; continue with {target} and retain completed work.',
                   target_provider=target, handoff_role='coding')
    if interruption == 'before_checkpoint':
        publish = goal._publish_coding_checkpoint
        def interrupted_publication(destination, manifest, checkpoint, **kwargs):
            if destination != folder and checkpoint['returncode'] == 0:
                raise RuntimeError('fixture interrupted checkpoint publication')
            return publish(destination, manifest, checkpoint, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(goal, '_publish_coding_checkpoint', interrupted_publication)
            with pytest.raises(RuntimeError, match='fixture interrupted checkpoint publication'):
                goal.recover_goal(order, folder, **options)
    elif interruption:
        from foundry_runtime import _foundry_manager as manager
        turn, calls = manager.turn, []
        def interrupted(*args, **kwargs):
            calls.append(1)
            if len(calls) == (1 if interruption == 'before_handoff' else 2):
                raise RuntimeError('fixture interrupted manager feedback')
            return turn(*args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(manager, 'turn', interrupted)
            with pytest.raises(RuntimeError, match='fixture interrupted manager feedback'):
                goal.recover_goal(order, folder, **options)
    result = goal.recover_goal(order, folder, **options)
    assert result['status'] == 'awaiting_input'
    if source == 'codex':
        assert codex_calls == [None]
        assert len(claude_calls) == 2
        assert claude_calls[0][1] is False and claude_calls[1] == (claude_calls[0][0], True)
    else:
        assert codex_calls == [None, SID]
        assert len(claude_calls) == 1 and claude_calls[0][1] is False
    child = Path(result['continuation_evidence'])
    saved = json.loads((child / 'manifest.json').read_bytes())
    assert saved['manager_state']['native_dir'] == old['manager_state']['native_dir']
    assert not saved['manager_state'].get('handoff')
    assert saved['coding_launcher'] == ('claude' if target == 'claude' else 'app-server')
    assert requests[1]['packet']['returncode'] == 1
    assert requests[1]['packet']['worker_evidence'] == old['manager_packet']['worker_evidence']
    assert requests[1]['packet']['operator_answer'] == options['answer']
    assert len(requests) == 4
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert all(p.read_bytes() == raw for p, raw in native_before.items())
    assert goal.recover_goal(order, folder, **options) == result
    assert len(requests) == 4 and len(claude_calls) + len(codex_calls) == 3
