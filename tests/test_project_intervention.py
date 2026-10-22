"""Local socket and native child fixtures; never steer business tasks."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from foundry_runtime import cli, _foundry_dispatch as dispatch, app_server_transport as transport
from foundry_runtime import _goal_intervention as intervention, _foundry_manager as manager, foundry_native as native
from test_installed_runtime import external, offline
from test_manager_recovery import conversation


def manager_turn(state, workspace, packet, *, intervention_root, pending_only=False):
    packet, changed = intervention.incorporate(intervention_root, packet)
    return manager.turn(state, workspace, packet, operator_update=changed, pending_only=pending_only and not changed)


@pytest.mark.parametrize('answer', [False, True])
def test_descriptor_cli_reaches_existing_worker_socket(external, tmp_path, answer, capsys):
    tree, scope, _, launches = external
    (tree / 'in-progress.txt').write_text('worker edits are expected')
    messages = []
    app = SimpleNamespace(thread_id='worker-thread', turn_id='worker-turn',
        process=SimpleNamespace(pid=123, creation_time=456, contains=lambda *args: True),
        steer=lambda text, turn: messages.append((text, turn)) or {'turnId': turn},
        answer_input=lambda key, value: messages.append((key, value)) or {})
    server = transport.CorrectionServer(app, attempt_id='attempt')
    handle = {'schema_version': 'pdcl-active-turn-2', 'launcher': 'app-server', 'tag': 'TASK1',
        'worktree': str(tree), 'thread_id': app.thread_id, 'turn_id': app.turn_id,
        'attempt_id': 'attempt', 'root_pid': 123, 'root_creation_time': 456,
        'owner_pid': 12, 'owner_creation_time': 45,
        'control': {'host': server.host, 'port': server.port}}
    scope.evidence('TASK1').mkdir(parents=True)
    (scope.evidence('TASK1') / 'active_turn.json').write_text(json.dumps(handle))
    (tree / dispatch.LOCK_NAME).write_text(json.dumps({**handle, 'schema_version': 'pdcl-executor-lock-2', 'state': 'running'}))
    prompt = tmp_path / '中文.txt'
    text = json.dumps({'answer': '示例应用 继续'}, ensure_ascii=False) if answer else '请核对示例应用，保留未提交修改。'
    prompt.write_text(text, encoding='utf-8-sig')
    server.start()
    try:
        argv = ['--project-config', str(scope.path), '--steer', 'TASK1', '--prompt-file', str(prompt)]
        if answer:
            argv += ['--answer-request', '7']
        assert cli.main(argv) == 0
        assert messages == ([(7, {'answer': '示例应用 继续'})] if answer else [(text, 'worker-turn')])
        assert 'team compliance not observed' in capsys.readouterr().out
        handle['attempt_id'] = 'stale'
        (scope.evidence('TASK1') / 'active_turn.json').write_text(json.dumps(handle))
        assert cli.main(argv) == 1
        assert len(messages) == 1
        assert not launches
    finally:
        server.close()


def goal_fixture(external, state):
    scope = external[1]
    root = scope.evidence('TASK1') / 'goal-fixture'
    root.mkdir(parents=True)
    state['native_dir'] = str(root.with_name(root.name + '-manager'))
    manifest = {'run_id': 'fixture-run', 'tag': 'TASK1', 'worktree': str(external[0]),
        'manager_state': state, 'intervention': {'schema_version': 1, 'root': str(root)}}
    (root / 'manifest.json').write_text(json.dumps(manifest))
    return root, manifest


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_manager_queue_consumed_by_existing_native_owner(external, conversation, backend):
    state, requests, failures = conversation
    state['backend'] = backend
    failures.clear()
    root, _ = goal_fixture(external, state)
    first = intervention.submit(root, 'TASK1', '示例应用：先核对，不要再启动团队。', external[1])
    assert first['status'] == 'queued' and not requests
    assert intervention.submit(root, 'TASK1', '示例应用：先核对，不要再启动团队。', external[1])['message_id'] == first['message_id']
    packet = {'stage': 'worker_returned'}
    result = manager_turn(state, external[0], packet, pending_only=True, intervention_root=root)
    decision, next_state, _, accepted = result
    assert first['message_id'] in decision['operator_answer']
    assert accepted['operator_message_ids'] == [first['message_id']]
    assert accepted['operator_answer'].count('示例应用') == 1
    assert intervention.observe(root)[0]['status'] == 'responded'
    # Lost goal write: native reservations/receipts reconstruct the same decision.
    replay = manager_turn(state, external[0], packet, pending_only=True, intervention_root=root)
    assert replay[0] == decision and len(requests) == 1
    assert manager_turn(next_state, external[0], accepted, pending_only=True, intervention_root=root) is None
    second = intervention.submit(root, 'TASK1', '新增补充', external[1])
    assert second['status'] == 'queued'
    manager_turn(next_state, external[0], {'stage': 'worker_returned'}, intervention_root=root)
    assert len(requests) == 2
    assert requests[-1][0]['packet']['operator_message_ids'] == [second['message_id']]


def test_quota_retry_retains_old_and_new_manager_inputs(external, conversation):
    state, requests, failures = conversation
    root, _ = goal_fixture(external, state)
    intervention.submit(root, 'TASK1', '第一条', external[1])
    packet = {'stage': 'worker_returned'}
    with pytest.raises(native.NativeError):
        manager_turn(state, external[0], packet, intervention_root=root)
    assert intervention.observe(root)[0]['status'] == 'blocked'
    intervention.submit(root, 'TASK1', '第二条', external[1])
    result = manager_turn(state, external[0], packet, intervention_root=root)
    assert '第二条' in result[0]['operator_answer']
    assert len(requests) == 3  # failed old, original replay, new supplement
    assert [r['status'] for r in intervention.observe(root)] == ['responded', 'responded']


def test_rejected_native_admission_leaves_message_queued(external, conversation, monkeypatch):
    state, requests, failures = conversation
    root, _ = goal_fixture(external, state)
    intervention.submit(root, 'TASK1', '不丢失补充', external[1])
    monkeypatch.setattr(native.NativeAnalyst, 'manager_request', lambda *args: (_ for _ in ()).throw(ValueError('local refusal')))
    with pytest.raises(ValueError, match='local refusal'):
        manager_turn(state, external[0], {'stage': 'assign'}, intervention_root=root)
    assert intervention.observe(root)[0]['status'] == 'queued' and not requests


def test_cli_manager_receipt_and_legacy_boundary(external, tmp_path, capsys):
    root, manifest = goal_fixture(external, {'native_dir': 'unused'})
    prompt = tmp_path / 'update.txt'; prompt.write_text('经理补充', encoding='utf-8')
    argv = ['--project-config', str(external[1].path), '--steer', 'TASK1',
            '--steer-goal', str(root), '--prompt-file', str(prompt)]
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'queued'
    (root / 'outcome.json').write_text('{"status":"completed"}')
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'undelivered'
    prompt.write_text('different', encoding='utf-8')
    assert cli.main(argv) == 1
    capsys.readouterr()
    del manifest['intervention']
    (root / 'manifest.json').write_text(json.dumps(manifest))
    assert cli.main(argv) == 1
    assert 'does not support queued manager input' in capsys.readouterr().out


@pytest.mark.parametrize('has_session', [True, False])
def test_running_goal_consumes_input_after_worker_returns(external, conversation, monkeypatch, has_session):
    import sys
    from foundry_runtime import foundry_goal as goal
    state, requests, failures = conversation
    failures.clear()
    tree, scope, order, _ = external
    root = scope.evidence('TASK1') / 'goal-live'
    state['native_dir'] = str(root.with_name(root.name + '-manager'))
    original = dispatch.dispatch_order
    coding_argv = dispatch.coding_argv
    session_id = '00000000-0000-0000-0000-000000000001'
    def argv(*args, **kwargs):
        result = coding_argv(*args, **kwargs)
        if has_session:
            header = 'OpenAI Codex vfixture\n--------\nsession id: ' + session_id + '\n--------'
            result[-1] = 'print(' + repr(header) + '); ' + result[-1]
        return result
    monkeypatch.setattr(dispatch, 'coding_argv', argv)
    def worker(*args, **kwargs):
        assert intervention.submit(root, 'TASK1', '任务中新增中文补充', scope)['status'] == 'queued'
        return original(*args, **kwargs)
    def resumed(session, workspace, tag, text, **kwargs):
        assert session == session_id
        assert '任务中新增中文补充' in text
        assert intervention.observe(root)[0]['status'] == 'responded'
        raise RuntimeError('updated decision reached original worker')
    monkeypatch.setattr(dispatch, 'dispatch_order', worker)
    monkeypatch.setattr(dispatch, 'resume_order', resumed)
    monkeypatch.setattr(goal, '_record_usage', lambda *a, **k: None)
    if has_session:
        with pytest.raises(RuntimeError, match='updated decision reached original worker'):
            goal.run_goal(order, root, Path(sys.executable), manager_state=state,
                          coding_launcher='exec', project_record=scope.record())
    else:
        result = goal.run_goal(order, root, Path(sys.executable), manager_state=state,
                              coding_launcher='exec', project_record=scope.record())
        assert result['status'] == 'coding_incomplete'
        assert result['reason'] == 'manager continuation needs the original coding session'
        assert result.get('project_action') != 'prepare_candidate'
    assert len(requests) == 2


def test_legacy_round_uses_outer_goal_input_owner(external):
    root, manifest = goal_fixture(external, {'native_dir': 'unused'})
    child = root / 'round-0'; child.mkdir()
    (child / 'manifest.json').write_text(json.dumps(manifest))
    (root / 'manifest.json').write_text(json.dumps({'mode': 'auto-amend', 'run_id': 'fixture-run',
                                                  'active_round': str(child)}))
    assert intervention.input_root(child, manifest) == root
    assert intervention.submit(child, 'TASK1', '旧任务恢复后的补充', external[1])['status'] == 'queued'
    assert intervention.observe(root)[0]['status'] == 'queued'


@pytest.mark.parametrize('backend', ['claude', 'codex'])
@pytest.mark.parametrize('target', ['root', 'leaf'])
def test_upgraded_nested_manager_ignores_historical_outcome_and_reads_receipt(
        external, conversation, backend, target):
    state, requests, failures = conversation
    state['backend'] = backend
    failures.clear()
    root, original = goal_fixture(external, {'native_dir': 'unused'})
    upgraded = root/'round-upgrade-fixture'; upgraded.mkdir()
    leaf = upgraded/'round-0'; leaf.mkdir()
    state['native_dir'] = str(upgraded.with_name(upgraded.name+'-manager'))
    (root/'manifest.json').write_text(json.dumps({**original, 'mode': 'auto-amend',
        'runtime_upgrades': [{'round': str(upgraded)}], 'active_round': str(upgraded)}))
    historical = b'{"status":"awaiting_input","reason":"old boundary"}'
    (root/'outcome.json').write_bytes(historical)
    (upgraded/'manifest.json').write_text(json.dumps({'run_id': 'new-controller',
        'mode': 'auto-amend', 'active_round': str(leaf), 'follow_up_goal': str(root)}))
    (leaf/'manifest.json').write_text(json.dumps({**original, 'run_id': 'new-worker',
        'manager_state': state, 'follow_up_goal': str(root)}))
    address = root if target == 'root' else leaf
    first = intervention.submit(address, 'TASK1', '升级后核验回执', external[1])
    assert first['goal'] == str(root) and first['status'] == 'queued'
    decision, _, _, _ = manager_turn(state, external[0], {'stage': 'worker_returned'},
                                     intervention_root=root)
    assert first['message_id'] in decision['operator_answer']
    assert intervention.observe(root)[0]['status'] == 'responded'
    assert intervention.incorporate(root, {'stage': 'worker_returned'}) == ({'stage': 'worker_returned'}, False)
    assert intervention.submit(address, 'TASK1', '升级后核验回执', external[1])['status'] == 'responded'
    assert len(requests) == 1
    pending = intervention.submit(address, 'TASK1', '另一条待处理信息', external[1])
    assert pending['status'] == 'queued'
    (upgraded/'outcome.json').write_text('{"status":"awaiting_input"}')
    with pytest.raises(ValueError, match='goal has an outcome'):
        intervention.submit(address, 'TASK1', '已结束后的新指令', external[1])
    assert {r['message_id']: r['status'] for r in intervention.observe(root)} == {
        first['message_id']: 'responded', pending['message_id']: 'undelivered'}
    assert (root/'outcome.json').read_bytes() == historical
