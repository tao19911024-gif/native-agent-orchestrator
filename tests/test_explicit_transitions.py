"""Explicit transitions preserve old evidence and use new native ownership."""
import json
import os
from pathlib import Path
import sys
import subprocess

import pytest

from foundry_runtime import foundry_goal as goal, _foundry_manager as manager
from foundry_runtime import _foundry_goal_transitions as transitions
from foundry_runtime import foundry_native as native
from test_manager_recovery import conversation
from test_installed_runtime import external, offline
from test_goal_preflight_recovery import retained_goal


@pytest.mark.skipif(not os.environ.get('FOUNDRY_TEST_OLD_PYTHON'), reason='retained release interpreter required')
def test_actual_retained_release_manager_migrates_read_only(external, tmp_path, monkeypatch):
    tree, scope, order, _ = external
    old = tmp_path/'old-manager'
    script = r'''
import json, sys
from pathlib import Path
from foundry_runtime import _code_index, _foundry_manager as manager, foundry_native as native
manager.resolve_executable = lambda _: Path(sys.executable)
_code_index.prepare = lambda _, **kw: None
spawn = native.OwnedProcess.spawn
rows = [{'type':'system','subtype':'init','tools':['StructuredOutput'],'mcp_servers':[],
         'skills':[],'plugins':[],'model':'fixture','session_id':'old-release-manager'}]
def child(argv, **kwargs):
    code = 'import sys;sys.stdin.buffer.read();print('+repr(json.dumps(rows[0]))+');sys.exit(1)'
    return spawn([sys.executable,'-B','-c',code], **kwargs)
native.OwnedProcess.spawn = child
state = {'backend':'claude','model':None,'effort':None,'last_call_id':None,'native_dir':sys.argv[2]}
try:
    manager.turn(state, Path(sys.argv[1]), {'stage':'worker_returned','operator_answer':'retained instructions'})
except native.NativeError:
    pass
print(json.dumps(state))
'''
    result = subprocess.run([os.environ['FOUNDRY_TEST_OLD_PYTHON'], '-I', '-B', '-c', script, str(tree), str(old)],
        capture_output=True, encoding='utf-8', timeout=30, env={**os.environ, 'PDCL_MODEL_CATALOGUE':'snapshot',
                                                               'FOUNDRY_COMPACT_TOKENS': '240000'})
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    root, folder, manifest = retained_goal(external, scope, tmp_path)
    manifest['manager_state'] = state
    goal._write(folder/'manifest.json', manifest)
    frozen = {p:p.read_bytes() for p in old.rglob('*') if p.is_file()}
    from foundry_runtime import _foundry_recovery_preflight as preflight, cli
    monkeypatch.setenv('FOUNDRY_COMPACT_TOKENS', '0')
    plan = preflight.inspect_recovery(folder, manifest, order)
    assert plan['status'] == 'ready', plan
    assert plan['runtime_python'] == os.environ['FOUNDRY_TEST_OLD_PYTHON']
    assert plan['environment']['FOUNDRY_COMPACT_TOKENS'] == '240000'
    routed = []
    monkeypatch.setattr(preflight, 'execute_recorded', lambda plan, argv, **kw: routed.append((plan, argv)) or 0)
    argv = ['--project-config', str(scope.path), '--order', str(order), '--foundry-goal', '--recover-goal', str(root)]
    assert cli.main(argv) == 0
    assert len(routed) == 1 and routed[0][1] == argv
    monkeypatch.setattr(transitions, 'resolve_executable', lambda _: Path(sys.executable))
    migrated = transitions.manager(goal, folder, manifest, provider=None, migrate=True, answer=None)
    assert migrated['manager_packet']['operator_answer'] == 'retained instructions'
    assert migrated['manager_state']['native_dir'] != str(old)
    assert all(p.read_bytes() == raw for p,raw in frozen.items())


@pytest.mark.parametrize('target', ['claude', 'codex'])
def test_manager_transfer_reads_retry_history_without_replaying_old_calls(conversation, external, tmp_path, monkeypatch, target):
    state, requests, failures = conversation
    state.update(context_reuse=1, quota_recovery=1, design_convergence=1, review_convergence=1)
    tree, scope, order, _ = external
    with pytest.raises(native.NativeError):
        manager.turn(state, tree, {'stage': 'assign'})
    _, state, _, _ = manager.turn(state, tree, {'stage': 'assign'})
    # A new admitted request was not yet saved by goal.
    failures.add(len(requests))
    latest = {'stage': 'worker_returned', 'operator_answer': '已批准的中文补充'}
    with pytest.raises(native.NativeError):
        manager.turn(state, tree, latest)
    root, folder, manifest = retained_goal(external, scope, tmp_path)
    manifest.update(manager_state=state, manager_packet={'stage': 'assign'})
    goal._write(folder/'manifest.json', manifest)
    old = Path(state['native_dir'])
    frozen = {p: p.read_bytes() for p in old.rglob('*') if p.is_file()}
    monkeypatch.setattr(transitions, 'resolve_executable', lambda _: Path(sys.executable))
    transferred = transitions.manager(goal, folder, manifest, provider=target,
        migrate=target == 'claude', answer='继续处理')
    new = transferred['manager_state']
    for key in ('context_reuse', 'quota_recovery', 'design_convergence', 'review_convergence'):
        assert new[key] == 1
    assert new['native_dir'] != str(old) and new['last_call_id'] is None
    assert transferred['manager_packet'] == latest
    retained = json.loads(Path(new['handoff']['history'][-1]['request']['path']).read_bytes().rsplit(b'\n\n', 1)[-1])
    assert retained['packet'] == latest
    assert new['backend'] == target
    assert transitions.manager(goal, folder, transferred, provider=target,
        migrate=target == 'claude', answer='继续处理') == transferred
    count = len(requests)
    result, updated, _, packet = manager.turn(new, tree, transferred['manager_packet'])
    assert len(requests) == count + 1 and '--resume' not in requests[-1][1] and 'resume' not in requests[-1][1]
    assert packet['manager_handoff']['operator_instructions'] == '继续处理'
    assert packet['manager_handoff']['progress_note']
    assert all('result' not in row for row in packet['manager_handoff']['history'])
    assert result['instruction'] == latest['operator_answer']
    assert all(p.read_bytes() == raw for p, raw in frozen.items())


def test_invalid_checkpoint_does_not_accept_transfer(conversation, external, tmp_path, monkeypatch):
    root, folder, manifest = retained_goal(external, external[1], tmp_path)
    before = (folder/'manifest.json').read_bytes()
    (folder/'coding-checkpoint.json').write_text('{}')
    with pytest.raises(goal.GoalBlocked, match='checkpoint changed'):
        transitions.manager(goal, folder, manifest, provider='codex', migrate=False, answer='继续')
    assert (folder/'manifest.json').read_bytes() == before


def test_panel_prepares_shared_launch_once_for_parallel_members(tmp_path, monkeypatch):
    from foundry_runtime import _foundry_review_group as group, _launch_context
    calls, launched = [], []
    launch = {'index': {'mcp_server': {'command': 'shared-client'}}}
    monkeypatch.setattr(_launch_context, 'capture', lambda tree, **kw: calls.append(tree) or launch)
    class Member:
        def __init__(self, executable, folder, **options):
            launched.append(options['launch_context'])
        def __call__(self, request):
            return request['call_id']
    requests = {f'review-{i}': {'call_id': str(i)} for i in range(2)}
    panel = group.Panel([{'executable': sys.executable}]*2, requests, Member, tmp_path/'reviews', tmp_path)
    result = group.prewarm(list(requests), requests, panel)
    assert all(error is None for value, error in result.values())
    assert calls == [tmp_path] and launched == [launch, launch]


def test_coding_runtime_migration_dispatches_fresh_with_recorded_compute(external, tmp_path, monkeypatch):
    from foundry_runtime import _foundry_dispatch as dispatch
    tree, scope, order, launches = external
    original = dispatch.coding_argv
    observed = []
    def coding(*args, **kwargs):
        observed.append((args, kwargs))
        argv = original(*args, **kwargs)
        argv[-1] = argv[-1].replace("print('fixture child completed')", "print('OpenAI Codex vfixture\\n--------\\nsession id: 00000000-0000-0000-0000-000000000001\\n--------')")
        argv[-1] += "; Path('candidate.txt').write_text('handoff')"
        return argv
    monkeypatch.setattr(dispatch, 'coding_argv', coding)
    folder = tmp_path/'original'
    result = goal.run_goal(order, folder, Path(sys.executable), coding_launcher='exec',
        coding_model='recorded-model', coding_effort='high', project_record=scope.record())
    assert result['status'] == 'project_handoff'
    frozen = {p: p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    monkeypatch.setattr(dispatch, 'resume_order', lambda *a, **k: pytest.fail('migration resumed old worker'))
    migrated = goal.recover_goal(order, folder, answer='继续处理中文指令', migrate_runtime=True, handoff_role='coding')
    assert migrated['status'] == 'project_handoff' and len(launches) == 2
    target = Path(migrated['continuation_evidence'])
    saved = json.loads((target/'manifest.json').read_bytes())
    assert saved['requested_compute']['coding'] == {'backend': 'codex', 'model': 'recorded-model', 'effort': 'high'}
    assert saved['continuation']['runtime_transfer']['python'] == sys.executable
    assert goal.recover_goal(order, folder, answer='继续处理中文指令', migrate_runtime=True, handoff_role='coding') == migrated
    assert len(launches) == 2 and all(p.read_bytes() == raw for p, raw in frozen.items())


def test_reviewer_transfer_is_dynamic_and_repeatable(monkeypatch):
    monkeypatch.setattr(transitions, 'resolve_executable', lambda _: Path(sys.executable))
    monkeypatch.setattr(transitions, 'model', lambda name: 'current-'+name)
    checkpoint = {'reviewers': [{'backend': 'claude', 'model': 'old', 'effort': 'high', 'executable': sys.executable}]*2}
    changed = transitions.reviewers(checkpoint, {}, role='review-1', provider='codex', migrate=False)
    assert set(changed) == {'review-1'} and changed['review-1'][-1]['model'] == 'current-astra'
    assert transitions.reviewers(checkpoint, changed, role='review-1', provider='codex', migrate=False) == changed


@pytest.mark.parametrize('flags,expected', [
    (['--handoff-role','review-1','--handoff-provider','codex'], {'handoff_role':'review-1','target_provider':'codex'}),
    (['--handoff-role','manager','--migrate-runtime'], {'handoff_role':'manager','migrate_runtime':True}),
    (['--migrate-runtime'], {'migrate_runtime':True})])
def test_cli_routes_explicit_transitions(external, tmp_path, monkeypatch, flags, expected):
    from foundry_runtime import cli
    root, folder, manifest = retained_goal(external, external[1], tmp_path)
    observed = []
    def recover(*args, **kwargs):
        observed.append(kwargs)
        return {'status':'project_handoff'}
    monkeypatch.setattr(goal, 'recover_goal', recover)
    assert cli.main(['--project-config',str(external[1].path),'--order',str(external[2]),
        '--foundry-goal','--recover-goal',str(root),*flags]) == 0
    assert len(observed) == 1 and all(observed[0][key] == value for key,value in expected.items())


@pytest.mark.parametrize('bad_schema', [False, True])
@pytest.mark.parametrize('target', ['codex', 'claude'])
def test_failed_review_slot_moves_provider_and_completed_slot_is_reused(tmp_path, monkeypatch, bad_schema, target):
    from foundry_runtime import _foundry_review_group as group, _code_index
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    monkeypatch.setattr(_code_index, 'prepare', lambda _, **kw: None)
    calls, spawn, moving = [], native.OwnedProcess.spawn, False
    result = {'candidate_commit': 'a'*40, 'packet_sha256': 'b'*64, 'verdict': 'PASS', 'findings': []}
    def child(argv, **kwargs):
        stream = kwargs['stdin']
        request = json.loads(stream.read().rsplit(b'\n\n', 1)[-1]); stream.seek(0)
        codex = '--output-schema' in argv
        calls.append((request, codex))
        failed = request['packet']['slot'] == 1 and not moving
        value = {'invalid': True} if failed and bad_schema else result
        sid = '00000000-0000-0000-0000-000000000002' if codex else 'review-'+str(request['packet']['slot'])
        if codex:
            rows = [{'type': 'thread.started', 'thread_id': sid}, {'type': 'turn.started'},
                {'type': 'item.completed', 'item': {'id': 'final', 'type': 'agent_message', 'text': json.dumps(value)}},
                {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'cached_input_tokens': 0, 'output_tokens': 5}}]
        else:
            rows = [{'type': 'system', 'subtype': 'init', 'tools': ['StructuredOutput'],
                'mcp_servers': [], 'skills': [], 'plugins': [], 'model': 'fixture', 'session_id': sid}]
            if not failed or bad_schema:
                rows += [{'type': 'assistant', 'session_id': sid, 'message': {'role': 'assistant', 'content': [
                    {'type': 'tool_use', 'name': 'StructuredOutput', 'id': 'f1', 'input': value}]}},
                    {'type': 'user', 'session_id': sid, 'message': {'role': 'user', 'content': [
                    {'type': 'tool_result', 'tool_use_id': 'f1', 'content': 'Structured output provided successfully'}]}},
                    {'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 2,
                    'result': json.dumps(value), 'structured_output': value,
                    'usage': {'input_tokens': 10, 'output_tokens': 5, 'iterations': [{'type': 'message'}]}, 'total_cost_usd': 0.0}]
        script = 'import sys;sys.stdin.buffer.read();sys.stdout.write('+repr('\n'.join(map(json.dumps, rows))+'\n')+')'
        return spawn([sys.executable, '-B', '-c', script], **kwargs)
    monkeypatch.setattr(native.OwnedProcess, 'spawn', child)
    specs = [{'backend': 'claude', 'model': None, 'effort': None, 'executable': sys.executable}]*2
    requests = {f'review-{i}': {'kind': 'review', 'call_id': str(i+1)*64, 'packet': {'slot': i}} for i in range(2)}
    folder = tmp_path/'reviews'
    first = group.Panel(specs, requests, native.NativeAnalyst, folder, tmp_path)
    outcomes = group.prewarm(list(requests), requests, first)
    assert outcomes['review-0'][1] is None and outcomes['review-1'][1] is not None
    frozen = {p: p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    moving = True
    changes = {'review-1': [{'backend': target, 'executable': sys.executable,
                            **({'runtime_transition': 'a'*64} if target == 'claude' else {})}]}
    second = group.Panel(specs, requests, native.NativeAnalyst, folder, tmp_path, resume=True, transfers=changes)
    count = len(calls)
    outcomes = group.prewarm(list(requests), requests, second)
    assert all(error is None and value == result for value, error in outcomes.values())
    assert len(calls) == count+1 and calls[-1][1] == (target == 'codex') and 'continue_from' not in calls[-1][0]
    assert all(p.read_bytes() == raw for p, raw in frozen.items())
    monkeypatch.setattr(_code_index, 'prepare', lambda _, **kw: pytest.fail('completed review replay prepared index'))
    third = group.Panel(specs, requests, native.NativeAnalyst, folder, tmp_path, resume=True, transfers=changes)
    assert all(value == result and error is None for value, error in group.prewarm(list(requests), requests, third).values())
    assert len(calls) == count+1
