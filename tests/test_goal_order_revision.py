"""Contract admission and native manager recovery; never a business task."""
import json
from pathlib import Path

import pytest

from foundry_runtime import foundry_goal as goal, cli
from foundry_runtime import _foundry_order_revision as revision
from test_installed_runtime import external, offline
from test_goal_preflight_recovery import retained_goal
from test_manager_recovery import conversation


def revision_case(external, tmp_path):
    root, folder, manifest = retained_goal(external, external[1], tmp_path)
    order = external[2]
    original = tmp_path/'original.md'
    original.write_bytes(order.read_bytes())
    order.write_bytes(order.read_bytes() + '\r\nApproved incremental contract 中文\r\n'.encode())
    revised = tmp_path/'revised.md'; revised.write_bytes(order.read_bytes())
    ruling = tmp_path/'ruling.md'; ruling.write_text('Operator approves only this exact revision; preserve old evidence.')
    decision = tmp_path/'decision.json'
    value = {'schema_version': 1, 'authorized_by': 'operator', 'goal': str(folder),
             'run_id': manifest['run_id'], 'order': manifest['order'],
             'previous': goal._fact(original), 'revised': goal._fact(revised),
             'authorization': goal._fact(ruling)}
    goal._write(decision, value)
    return folder, manifest, order, decision


def test_admission_keeps_original_identity_bytes_and_checkpoint(external, tmp_path):
    folder, manifest, order, decision = revision_case(external, tmp_path)
    before = (folder/'coding-checkpoint.json').read_bytes()
    with goal._run_lock(folder):
        saved = revision.admit(goal, folder, manifest, order, decision)
    assert saved['order'] == manifest['order']
    assert (folder/'coding-checkpoint.json').read_bytes() == before
    assert revision.matches(goal, order, saved)
    assert revision.pending(saved)
    packet, changed = revision.manager_input(goal, saved, {'operator_messages': ['existing-id']})
    assert changed and packet['operator_messages'] == ['existing-id']
    assert packet['order_revision']['revised_contract'].encode() == order.read_bytes()
    first = (folder/'manifest.json').read_bytes()
    revision.admit(goal, folder, saved, order, decision)
    assert (folder/'manifest.json').read_bytes() == first
    # Retained content does not depend on Git or authoring snapshots staying put.
    request = json.loads(decision.read_bytes())
    for key in ('previous', 'revised', 'authorization'):
        Path(request[key]['path']).unlink()
    assert revision.matches(goal, order, saved)
    saved['manager_order_revision'] = saved['order_revisions'][-1]
    assert revision.manager_input(goal, saved, {}) == ({}, False)


@pytest.mark.parametrize('fault', ['run', 'goal', 'old_bytes', 'new_bytes', 'ruling', 'path'])
def test_mismatched_revision_does_not_mutate_goal(external, tmp_path, fault):
    folder, manifest, order, decision = revision_case(external, tmp_path)
    data = json.loads(decision.read_bytes())
    if fault == 'run':
        data['run_id'] = 'foreign'
    elif fault == 'goal':
        data['goal'] = str(tmp_path)
    elif fault in ('old_bytes', 'new_bytes'):
        key = 'previous' if fault == 'old_bytes' else 'revised'
        Path(data[key]['path']).write_text('other')
        data[key] = goal._fact(data[key]['path'])
    elif fault == 'ruling':
        Path(data['authorization']['path']).write_text('')
        data['authorization'] = goal._fact(data['authorization']['path'])
    else:
        other = tmp_path/'TASK1_other.md'; other.write_bytes(order.read_bytes())
        order = other
    goal._write(decision, data)
    before = (folder/'manifest.json').read_bytes()
    with pytest.raises(ValueError):
        revision.admit(goal, folder, manifest, order, decision)
    assert (folder/'manifest.json').read_bytes() == before
    assert not list(folder.glob('order-revision-*.json'))


def test_new_file_change_requires_new_explicit_revision(external, tmp_path):
    folder, manifest, order, decision = revision_case(external, tmp_path)
    saved = revision.admit(goal, folder, manifest, order, decision)
    order.write_text('a further unapproved change')
    assert not revision.matches(goal, order, saved)
    with pytest.raises(ValueError, match='changed again'):
        revision.admit(goal, folder, saved, order, decision)
    with pytest.raises(ValueError, match='order differs'):
        goal.recover_goal(order, folder)


def test_pending_worker_is_not_relaunched_by_contract_admission(external, tmp_path, monkeypatch):
    folder, manifest, order, decision = revision_case(external, tmp_path)
    manifest['manager_phase'] = 'dispatch_pending'
    goal._write(folder/'manifest.json', manifest)
    def unsettled(*args, **kwargs):
        raise goal.GoalBlocked('existing worker needs terminal reconciliation')
    monkeypatch.setattr(goal.goal_continuation, 'restore_completed_feedback', unsettled)
    monkeypatch.setattr(goal.goal_execution, 'run_owned', lambda *a, **k: pytest.fail('worker replay'))
    with pytest.raises(ValueError, match='terminal reconciliation'):
        goal.recover_goal(order, folder, order_revision_file=decision)
    assert len(json.loads((folder/'manifest.json').read_bytes())['order_revisions']) == 1
    assert external[3] == []


def test_cli_forwards_revision_to_existing_recovery(external, tmp_path, monkeypatch):
    folder, manifest, order, decision = revision_case(external, tmp_path)
    called = []
    def recover(order, evidence, **kw):
        called.append((order, evidence, kw))
        return {'status': 'project_handoff'}
    monkeypatch.setattr(goal, 'recover_goal', recover)
    assert cli.main(['--project-config', str(external[1].path), '--order', str(order),
                     '--foundry-goal', '--recover-goal', str(folder),
                     '--order-revision', str(decision)]) == 0
    assert called[0][2]['order_revision_file'] == str(decision)


@pytest.mark.parametrize('backend', ['claude', 'codex'])
@pytest.mark.parametrize('phase', ['delegation_ready', 'review_pending', 'artifacts_pending'])
def test_revised_contract_reaches_manager_before_old_decision(external, tmp_path, monkeypatch, backend, phase):
    from foundry_runtime import _foundry_manager as manager
    folder, manifest, order, decision = revision_case(external, tmp_path)
    manifest['manager_phase'] = phase
    manifest['manager_state']['backend'] = backend
    goal._write(folder/'manifest.json', manifest)
    calls = []
    def turn(state, tree, packet, **kw):
        calls.append((packet, kw))
        assert kw['operator_update'] is True and kw['pending_only'] is False
        assert packet['order_revision']['revised_contract'].encode() == order.read_bytes()
        return ({'action': 'await_input', 'instruction': 'review the bounded revision', 'evidence': {}, 'call_id': 'fixture-decision'},
                state, {}, packet)
    monkeypatch.setattr(manager, 'turn', turn)
    monkeypatch.setattr(goal, '_record_usage', lambda *a, **k: None)
    monkeypatch.setattr(goal.goal_continuation, 'continue_coding', lambda *a, **k: pytest.fail('unexpected new worker'))
    goal.recover_goal(order, folder, order_revision_file=decision)
    saved = json.loads((folder/'manifest.json').read_bytes())
    assert len(calls) == 1
    assert saved['manager_order_revision'] == saved['order_revisions'][-1]
    assert saved['order'] == manifest['order']
    assert not external[3]


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_native_manager_receipt_replays_revision_without_duplicate_call(external, tmp_path, conversation, backend):
    from foundry_runtime import _foundry_manager as manager
    folder, manifest, order, decision = revision_case(external, tmp_path)
    saved = revision.admit(goal, folder, manifest, order, decision)
    packet, changed = revision.manager_input(goal, saved, {'stage': 'worker_returned'})
    state, requests, failures = conversation
    state['backend'] = backend
    failures.clear()
    result = manager.turn(state, external[0], packet, operator_update=changed)
    assert result[3]['order_revision'] == packet['order_revision']
    count = len(requests)
    replay = manager.turn(state, external[0], packet, operator_update=changed)
    assert replay[0] == result[0] and len(requests) == count


def test_second_revision_extends_effective_contract_not_original_hash(external, tmp_path):
    folder, manifest, order, decision = revision_case(external, tmp_path)
    saved = revision.admit(goal, folder, manifest, order, decision)
    request = json.loads(decision.read_bytes())
    request['previous'] = request['revised']
    order.write_bytes(order.read_bytes() + b'Further specifically approved change.\n')
    snapshot = tmp_path/'second.md'; snapshot.write_bytes(order.read_bytes())
    request['revised'] = goal._fact(snapshot)
    second = tmp_path/'second.json'; goal._write(second, request)
    twice = revision.admit(goal, folder, saved, order, second)
    assert len(twice['order_revisions']) == 2
    assert revision.matches(goal, order, twice)
    assert twice['order'] == manifest['order']


def test_outer_goal_uses_one_active_round_revision_authority(external, tmp_path, monkeypatch):
    folder, manifest, order, decision = revision_case(external, tmp_path)
    called = []
    def feedback(order, folder, retained, **kwargs):
        called.append(retained)
        return {'continuation_evidence': 'fixture', 'status': 'awaiting_input'}
    monkeypatch.setattr(goal, '_recover_manager_feedback', feedback)
    goal.recover_goal(order, folder.parent, order_revision_file=decision)
    goal.recover_goal(order, folder.parent)
    parent = json.loads((folder.parent/'manifest.json').read_bytes())
    assert 'order_revisions' not in parent
    assert revision.matches(goal, order, parent)
    assert len(called) == 2 and len(called[-1]['order_revisions']) == 1


@pytest.mark.parametrize('backend', ['claude', 'codex'])
@pytest.mark.parametrize('answer', [None, 'Proceed with the approved contract.'])
@pytest.mark.parametrize('migrate', [False, True])
def test_nested_awaiting_revision_uses_answer_successor(external, tmp_path, monkeypatch, backend, answer, migrate):
    from foundry_runtime import _foundry_manager as manager
    folder, manifest, order, decision = revision_case(external, tmp_path)
    manifest.update(manager_phase='awaiting_input')
    manifest['manager_state']['backend'] = backend
    goal._write(folder/'manifest.json', manifest)
    waiting = {'status': 'awaiting_input', 'reason': 'Need a contract ruling'}
    goal._write(folder/'outcome.json', waiting)
    checkpoint_before = (folder/'coding-checkpoint.json').read_bytes()
    parent = json.loads((folder.parent/'manifest.json').read_bytes())
    parent['state'] = 'completed'
    goal._write(folder.parent/'manifest.json', parent)
    goal._write(folder.parent/'outcome.json', waiting)
    outer = tmp_path
    goal._write(outer/'manifest.json', {**parent, 'active_round': str(folder.parent)})
    goal._write(outer/'outcome.json', waiting)
    calls = []
    transfers = []
    if migrate:
        from foundry_runtime import _foundry_goal_transitions as transitions
        monkeypatch.setattr(transitions, 'manager_history', lambda *a, **kw: ({}, None, []))
        def transfer(owner, target, saved, **kwargs):
            transfers.append((target, kwargs))
            assert kwargs['worker_feedback']['order_revision']['revised_contract'].encode() == order.read_bytes()
            saved = {**saved, 'manager_state': {**saved['manager_state'], 'native_dir': str(target/'new-manager')}}
            owner._write(target/'manifest.json', saved)
            return saved
        monkeypatch.setattr(transitions, 'manager', transfer)
    def turn(state, tree, packet, **kwargs):
        calls.append(packet)
        assert packet['order_revision']['revised_contract'].encode() == order.read_bytes()
        assert packet['order'] == goal._fact(order)
        assert packet['operator_answer'] == (answer or 'Operator approves only this exact revision; preserve old evidence.')
        return ({'action': 'await_input', 'instruction': 'New bounded question', 'evidence': {}, 'call_id': 'new-question'},
                state, {}, packet)
    monkeypatch.setattr(manager, 'turn', turn)
    monkeypatch.setattr(goal, '_record_usage', lambda *a, **k: None)
    result = goal.recover_goal(order, outer, answer=answer, order_revision_file=decision, migrate_runtime=migrate)
    assert len(calls) == 1
    assert len(transfers) == int(migrate)
    successor = Path(result['continuation_evidence'])
    assert successor != folder and result['reason'] == 'New bounded question'
    for path in (outer, folder.parent, folder):
        assert json.loads((path/'outcome.json').read_bytes()) == waiting
    assert (folder/'coding-checkpoint.json').read_bytes() == checkpoint_before
    saved = json.loads((successor/'manifest.json').read_bytes())
    checkpoint = json.loads((successor/'coding-checkpoint.json').read_bytes())
    assert checkpoint['order'] == saved['order'] == goal._fact(order)
    replay = goal.recover_goal(order, outer, answer=answer, order_revision_file=decision, migrate_runtime=migrate)
    assert replay == result and len(calls) == 1
    assert not external[3]


@pytest.mark.parametrize('status', ['candidate_verified', 'review_blocked', 'project_handoff'])
def test_delivered_non_question_still_uses_amendment_boundary(external, tmp_path, status):
    folder, manifest, order, decision = revision_case(external, tmp_path)
    manifest['manager_phase'] = 'awaiting_input'
    goal._write(folder/'manifest.json', manifest)
    goal._write(folder/'outcome.json', {'status': status})
    before = (folder/'manifest.json').read_bytes()
    with pytest.raises(goal.GoalBlocked, match='amendment boundary'):
        revision.admit(goal, folder, manifest, order, decision)
    assert (folder/'manifest.json').read_bytes() == before


def test_admission_crash_reuses_retained_bytes(external, tmp_path, monkeypatch):
    folder, manifest, order, decision = revision_case(external, tmp_path)
    original = goal._write
    def crash(path, value):
        if Path(path) == folder/'manifest.json':
            raise OSError('fixture interruption before pointer publication')
        original(path, value)
    monkeypatch.setattr(goal, '_write', crash)
    with pytest.raises(OSError, match='fixture interruption'):
        revision.admit(goal, folder, manifest, order, decision)
    assert 'order_revisions' not in json.loads((folder/'manifest.json').read_bytes())
    monkeypatch.setattr(goal, '_write', original)
    saved = revision.admit(goal, folder, manifest, order, decision)
    assert len(saved['order_revisions']) == 1
    assert len(list(folder.glob('order-revision-*.json'))) == 1


def test_recovery_advice_and_retention_keep_revision_reader(external, tmp_path):
    from foundry_runtime._foundry_cli_help import recovery_invocation
    from foundry_runtime._release_retention import _references
    import sys
    folder, manifest, order, decision = revision_case(external, tmp_path)
    manifest['runtime_provenance'] = {'python': 'original-runtime'}
    saved = revision.admit(goal, folder, manifest, order, decision)
    advice = recovery_invocation(order, folder, saved, python='old-native-session-runtime')
    assert advice['argv'][0] == sys.executable
    assert sys.executable in list(_references(saved))
    assert saved['runtime_provenance'] == manifest['runtime_provenance']
    from foundry_runtime._followup import status
    observed = status(folder)
    assert observed['order_revision']['manager_reconciliation_pending'] is True
    assert observed['order_revision']['effective_order'] == goal._fact(order)
    upgraded = recovery_invocation(order, folder, saved, python='qualified-next-runtime',
                                   extra_args=['--upgrade-runtime', 'current'])
    assert upgraded['argv'][0] == 'qualified-next-runtime'


def test_outer_goal_rejects_foreign_order_without_boundary_record(external, tmp_path):
    folder, manifest, order, decision = revision_case(external, tmp_path)
    manifest['order'] = goal._fact(order)
    goal._write(folder/'manifest.json', manifest)
    parent = json.loads((folder.parent/'manifest.json').read_bytes())
    with pytest.raises(ValueError, match='without its recorded boundary'):
        revision.records(goal, parent)


def test_boundary_input_authorizes_new_round_contract_without_rewriting_root(external, tmp_path):
    folder, manifest, order, decision = revision_case(external, tmp_path)
    saved = revision.admit(goal, folder, manifest, order, decision)
    parent = json.loads((folder.parent/'manifest.json').read_bytes())
    prepared = folder.parent/'upgrade-input.json'; goal._write(prepared, saved)
    next_round = folder.parent/'round-upgrade'; next_round.mkdir()
    next_manifest = {**manifest, 'run_id': 'new-round', 'order': goal._fact(order)}
    goal._write(next_round/'manifest.json', next_manifest)
    parent.update(active_round=str(next_round),
                  runtime_upgrades=[{'round': str(next_round), 'input': goal._fact(prepared)}])
    assert revision.records(goal, parent)[0] == goal._fact(order)
    assert parent['order'] == manifest['order']

@pytest.mark.parametrize('settled', [True, False])
def test_explicit_revision_after_failed_worker_uses_existing_completion_verifier(external, tmp_path, monkeypatch, settled):
    from foundry_runtime import _recovery_reconciliation as reconciliation
    folder, manifest, order, decision = revision_case(external, tmp_path)
    manifest['manager_phase'] = 'feedback_pending'
    path = folder/'coding-checkpoint.json'
    checkpoint = json.loads(path.read_bytes()); checkpoint['returncode'] = 1
    goal._write(path, checkpoint); manifest['coding_checkpoint'] = goal._fact(path)
    checkpoint_before = path.read_bytes()
    goal._write(folder/'manifest.json', manifest)
    goal._write(folder/'outcome.json', {'status':'coding_incomplete'})
    before = (folder/'manifest.json').read_bytes()
    calls = []
    def verify(value):
        calls.append(value)
        if not settled:
            raise goal.GoalBlocked('original worker cleanup is unsettled')
        return {'state':'completed','cleanup_verified':True}
    monkeypatch.setattr(reconciliation, 'completed_coding', verify)
    if settled:
        saved = revision.admit(goal, folder, manifest, order, decision)
        assert revision.matches(goal, order, saved) and revision.pending(saved)
        assert path.read_bytes() == checkpoint_before
    else:
        with pytest.raises(ValueError, match='cleanup is unsettled'):
            revision.admit(goal, folder, manifest, order, decision)
        assert (folder/'manifest.json').read_bytes() == before
    assert calls == [checkpoint]
    assert not external[3]
