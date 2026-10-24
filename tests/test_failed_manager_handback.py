"""Recover the precise failed assignment without repeating completed work."""
import copy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from test_installed_runtime import external, offline
from test_manager_input_wait import manager_io
from test_owned_goal_recovery import SERVER, SID
from foundry_runtime import foundry_goal as goal, app_server_transport as transport
from foundry_runtime import _dispatch_resume, _dispatch_package, _foundry_goal_continuation as continuation
from foundry_runtime._execution_plan_prompt import execution_plan_prompt


@pytest.mark.parametrize('legacy,publication_crash', [
    (False, None), (True, None), (True, 'before'), (True, 'after')])
def test_failed_assignment_returns_to_manager_once(external, manager_io, tmp_path, monkeypatch, legacy, publication_crash):
    tree, scope, order, _ = external
    requests, decisions = manager_io
    decisions.extend([{'action': 'delegate', 'instruction': 'Complete first slice', 'artifacts': []},
                      {'action': 'delegate', 'instruction': 'Continue second slice', 'artifacts': []}])
    monkeypatch.setattr(goal, '_session_from_log', lambda _: SID)
    launches = []
    run = transport.run_executor
    def execute(*args, **kwargs):
        launches.append(1)
        script = 'HANG=False\n' + SERVER
        script = script.replace("  if HANG: time.sleep(60)",
            "  send({'method':'item/started','params':{'threadId':sid,'turnId':'fixture-turn',"
            "'item':{'id':'input','type':'userMessage','content':r['params']['input']}}})\n"
            "  if HANG: time.sleep(60)")
        if len(launches) == 2:
            script = script.replace("'status':'completed','items':[],'error':None",
                "'status':'failed','items':[],'error':{'message':'Selected model is at capacity.',"
                "'codexErrorInfo':'serverOverloaded'}")
        def spawn(argv, **opts):
            return transport.OwnedProcess.spawn([sys.executable, '-B', '-c', script], **opts)
        return run(*args, process_factory=spawn, **kwargs)
    monkeypatch.setattr(transport, 'run_executor', execute)
    resume = _dispatch_resume.resume_order
    def retry(owner, session, worktree, tag, text, **kwargs):
        if text.startswith('Foundry provider-capacity continuation'):
            error = ValueError('fixture retry admission could not read unrelated history')
            error.dispatch_stop = _dispatch_package.DispatchNotStarted('not started',
                SimpleNamespace(path=tmp_path, frozen={'session_id': SID}))
            raise error
        return resume(owner, session, worktree, tag, text, **kwargs)
    monkeypatch.setattr(_dispatch_resume, 'resume_order', retry)
    folder = tmp_path/'goal'
    with pytest.raises(ValueError, match='fixture retry admission'):
        goal.run_goal(order, folder, Path(sys.executable), coding_launcher='app-server',
            project_record=scope.record(), manager_state={'backend': 'codex', 'model': None,
            'effort': None, 'native_dir': str(tmp_path/'manager'), 'artifact_delivery': True})
    manifest = json.loads((folder/'manifest.json').read_bytes())
    assert len(launches) == 2 and len(requests) == 2
    assert manifest['manager_phase'] == 'dispatch_pending'
    pending = Path(manifest['pending_dispatch']['path'])
    checkpoint = json.loads(goal.goal_usage._reference(manifest['coding_checkpoint']))
    assert checkpoint['returncode'] == 0
    assert all(f in checkpoint['evidence'] for f in manifest['manager_packet']['worker_evidence'])
    package = json.loads(pending.read_bytes())
    _, failed = _dispatch_package.read_intent(package, project_record=scope.record())
    assert failed['returncode'] == 1 and failed['state'] == 'completed'
    assert failed['evidence']['log'] not in checkpoint['evidence']
    if legacy:
        manifest.pop('pending_dispatch')
        goal._write(folder/'manifest.json', manifest)
        # Unrelated unresolved history shares the native session. Its input is
        # not this manager assignment, regardless of filename or completed state.
        unrelated = pending.with_name('dispatch-z-unrelated.package.json')
        goal._write(unrelated, {**package, 'fixture_unrelated': True})
        unrelated.with_suffix('').with_suffix('.log').write_text('')
        read = _dispatch_package.read_intent
        def read_intent(value, **kwargs):
            journal, record = read(value, **kwargs)
            if value.get('fixture_unrelated'):
                record = {**record, 'state': 'unknown', 'frozen': {**record['frozen'], 'order_sha256': '0'*64}}
            return journal, record
        monkeypatch.setattr(_dispatch_package, 'read_intent', read_intent)
    original = {p: p.read_bytes() for p in pending.parent.iterdir() if p.is_file()}
    selected = continuation.retained_dispatch(goal, folder, manifest)
    assert selected[0] == pending and selected[2]['returncode'] == 1
    from foundry_runtime._foundry_goal_delivery import coding_recovery
    advice = coding_recovery(folder, manifest)
    assert advice['retained_dispatch']['intent_id'] == failed['intent_id']
    assert advice['retained_dispatch']['returncode'] == 1
    assert advice['stop_cause'] == 'provider_capacity'
    assert advice['manager_recovery']['preferred']
    decisions.append({'action': 'await_input', 'instruction': 'Provider failed; retain second slice and completed first slice.', 'artifacts': []})
    monkeypatch.setattr(transport, 'run_executor', lambda *a, **k: pytest.fail('recovery repeated worker'))
    if publication_crash:
        write = goal._write
        def crash(path, value):
            if path == folder/'manifest.json' and value.get('manager_phase') == 'feedback_pending':
                if publication_crash == 'after':
                    write(path, value)
                raise RuntimeError('fixture checkpoint publication interruption')
            return write(path, value)
        with monkeypatch.context() as interrupted:
            interrupted.setattr(goal, '_write', crash)
            with pytest.raises(RuntimeError, match='checkpoint publication interruption'):
                goal.recover_goal(order, folder, answer='Review the failed second slice.')
        assert len(requests) == 2
    result = goal.recover_goal(order, folder, answer='Review the failed second slice without repeating completed work.')
    assert result['status'] == 'awaiting_input'
    assert len(requests) == 3
    assert requests[-1]['packet']['returncode'] == 1
    assert requests[-1]['packet']['operator_answer'] == 'Review the failed second slice without repeating completed work.'
    assert failed['evidence']['log'] in requests[-1]['packet']['worker_evidence']
    restored = json.loads((folder/'coding-checkpoint.json').read_bytes())
    assert restored['returncode'] == 1 and len(restored['coding_usage']['managed_turns']) == 2
    assert 'pending_dispatch' not in json.loads((folder/'manifest.json').read_bytes())
    assert all(p.read_bytes() == raw for p, raw in original.items())
    assert goal.recover_goal(order, folder) == result
    assert len(requests) == 3


@pytest.mark.parametrize('mismatch', [None, 'decision', 'digest', 'turn', 'historical_response'])
def test_legacy_selection_requires_bound_input_digest_and_decision(tmp_path, mismatch):
    decision = {'manager': {'instruction': 'Second slice', 'call_id': 'current', 'evidence': {'path': 'receipt', 'sha256': 'abc'}}}
    source = execution_plan_prompt('Continue the same task from the retained worker result.', decision)
    record = {'turn_binding': {'thread_id': 'session', 'turn_id': 'current-turn'},
              'frozen': {'order_sha256': hashlib.sha256(source.encode()).hexdigest()}}
    row = {'method': 'item/started', 'params': {'threadId': 'session', 'turnId': 'current-turn',
        'item': {'type': 'userMessage', 'content': [{'type': 'text', 'text': 'team\nTask:\n'+source+'\nextra guidance'}]}}}
    expected = copy.deepcopy(decision)
    if mismatch == 'decision': expected['manager']['call_id'] = 'other'
    if mismatch == 'digest': record['frozen']['order_sha256'] = '0'*64
    if mismatch == 'turn': row['params']['turnId'] = 'old-turn'
    if mismatch == 'historical_response': row = {'id': 3, 'result': {'thread': {'turns': [row]}}}
    package = tmp_path/'dispatch.package.json'
    package.with_suffix('').with_suffix('.log').write_text('stdout: '+json.dumps(row)+'\n', encoding='utf-8')
    assert continuation._matches_manager_assignment(package, record, expected) == (mismatch is None)
