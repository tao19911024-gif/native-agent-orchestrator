"""Recovery planning uses real local provider receipts; no business tasks."""
import json
import os
from pathlib import Path
import sys

import pytest

from test_manager_recovery import conversation
from test_installed_runtime import external, offline
from test_goal_preflight_recovery import retained_goal
from foundry_runtime import _foundry_manager as manager
from foundry_runtime import _foundry_recovery_preflight as preflight
from foundry_runtime import foundry_goal as goal
from foundry_runtime._foundry_cli_help import recovery_invocation


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_manager_reuses_recorded_launch_without_changing_ambient(conversation, tmp_path, monkeypatch, backend):
    state, calls, failures = conversation
    state['backend'] = backend
    failures.clear()
    monkeypatch.setenv('FOUNDRY_COMPACT_TOKENS', '240000')
    _, state, _, _ = manager.turn(state, tmp_path, {'stage': 'assign'})
    before = {str(p): p.read_bytes() for p in Path(state['native_dir']).rglob('*') if p.is_file()}
    monkeypatch.setenv('FOUNDRY_COMPACT_TOKENS', '0')
    manager.turn(state, tmp_path, {'stage': 'worker_returned'})
    assert os.environ['FOUNDRY_COMPACT_TOKENS'] == '0'
    assert len(calls) == 2
    for name, raw in before.items():
        assert Path(name).read_bytes() == raw
    for path in Path(state['native_dir']).glob('call-*/invocation.json'):
        assert json.loads(path.read_bytes())['launch_context']['settings']['compact_tokens'] == 240000


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_probe_inspects_every_receipt_without_writes(conversation, external, tmp_path, backend):
    state, calls, failures = conversation
    state['backend'] = backend
    failures.clear()
    tree, scope, order, _ = external
    _, state, _, _ = manager.turn(state, tree, {'stage': 'assign'})
    _, state, _, _ = manager.turn(state, tree, {'stage': 'worker_returned'})
    _, folder, manifest = retained_goal(external, scope, tmp_path)
    manifest['manager_state'] = state
    goal._write(folder / 'manifest.json', manifest)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = preflight._probe({'folder': str(folder), 'order': str(order)})
    assert result['status'] == 'ready', result
    assert next(c for c in result['checks'] if c['check'] == 'native_policy_and_invocations')['detail']['calls'] == 2
    assert len(calls) == 2
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    invocation = next(Path(state['native_dir']).glob('call-*/invocation.json'))
    row = json.loads(invocation.read_bytes())
    row['argv'][0] = 'different-executable.exe'
    invocation.write_text(json.dumps(row), encoding='utf-8')
    result = preflight._probe({'folder': str(folder), 'order': str(order)})
    assert result['status'] == 'attention_required'
    assert next(c for c in result['checks'] if c['check'] == 'native_policy_and_invocations')['status'] == 'unavailable'
    assert len(result['checks']) == 4  # Continue diagnosis after the first failure.
    assert len(calls) == 2


def test_command_uses_native_runtime_and_scoped_frozen_environment(tmp_path):
    native = tmp_path / 'native'; native.mkdir()
    policy = {'runtime_provenance': {'python': 'D:/native/python.exe'},
              'backend_identity': {'launch_context': {'compact_tokens': 240000}}}
    (native / 'native-manifest.json').write_text(json.dumps(policy))
    manifest = {'runtime_provenance': {'python': 'D:/goal/python.exe'},
                'order_revision_runtime': {'python': 'D:/revision/python.exe'},
                'manager_state': {'native_dir': str(native)},
                'project_record': {'context': {'descriptor': "D:/项目/it's.json"}}}
    command = recovery_invocation('order.md', tmp_path, manifest)
    assert command['argv'][0] == 'D:/native/python.exe'
    assert command['environment'] == {'FOUNDRY_COMPACT_TOKENS': '240000'}
    assert 'finally' in command['powershell'] and "it''s.json" in command['powershell']
    transition = recovery_invocation('order.md', tmp_path, manifest, python=sys.executable,
                                     extra_args=['--migrate-runtime'])
    assert transition['argv'][0] == sys.executable
    assert transition['environment'] == {}


def test_recorded_route_preserves_all_user_options(monkeypatch):
    captured = []
    monkeypatch.setattr(preflight.subprocess, 'call', lambda argv, **kw: captured.append((argv, kw)) or 7)
    report = {'runtime_python': 'D:/old/python.exe', 'environment': {'FOUNDRY_COMPACT_TOKENS': '240000'}}
    argv = ['--recover-goal', 'goal', '--prompt-file', '中文.txt', '--runtime-python', 'D:/wrong/python.exe']
    assert preflight.execute_recorded(report, argv, entry='D:/PDCL/bin/dispatch.py') == 7
    assert captured[0][0] == ['D:/old/python.exe', 'D:/PDCL/bin/dispatch.py', '--runtime-python',
                              'D:/old/python.exe', '--recover-goal', 'goal', '--prompt-file', '中文.txt']
    assert captured[0][1]['env']['FOUNDRY_COMPACT_TOKENS'] == '240000'


def test_project_adapter_override_is_seen_before_old_runtime_execution(tmp_path, monkeypatch):
    from types import SimpleNamespace
    native = tmp_path / 'native'; native.mkdir()
    python = tmp_path / 'old.exe'; python.write_bytes(b'fixture')
    (native / 'native-manifest.json').write_text(json.dumps({
        'runtime_provenance': {'python': str(python)},
        'backend_identity': {'launch_context': {'compact_tokens': 240000}}}))
    monkeypatch.setenv('FOUNDRY_COMPACT_TOKENS', '0')
    monkeypatch.setattr(preflight.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=json.dumps({'status': 'ready', 'checks': []})))
    adapter = SimpleNamespace(launch_scope=lambda: preflight.launch_scope({'FOUNDRY_COMPACT_TOKENS': '0'}))
    result = preflight.inspect_recovery(tmp_path, {'manager_state': {'native_dir': str(native)}},
                                       'order.md', project_adapter=adapter)
    assert result['status'] == 'attention_required'
    assert result['launch_conflicts']['FOUNDRY_COMPACT_TOKENS']['project_adapter'] == '0'
    assert os.environ['FOUNDRY_COMPACT_TOKENS'] == '0'


def test_waiting_migration_command_requires_the_actual_operator_answer(tmp_path, monkeypatch):
    from types import SimpleNamespace
    manifest = {'manager_phase': 'awaiting_input', 'runtime_provenance': {'python': sys.executable},
                'project_record': {'context': {'descriptor': 'project.json'}}}
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    (tmp_path / 'outcome.json').write_text(json.dumps({'status': 'awaiting_input'}))
    monkeypatch.setattr(preflight, 'inspect_recovery', lambda *a, **kw: {
        'status': 'attention_required', 'runtime_python': sys.executable,
        'checks': [{'check': name, 'status': 'passed'} for name in
                   ('goal_contract', 'checkpoint', 'archived_history')]})
    args = SimpleNamespace(recover_goal=str(tmp_path), recovery_preflight=True, migrate_runtime=False,
        handoff_provider=None, upgrade_runtime=None, retained_only=False, recovery_file=None,
        order_revision=None, order='order.md', prompt_file=None)
    report = preflight.prepare(args)
    command = report['explicit_migration']
    assert command['operator_answer_required'] is True
    assert command['argv'][-3:] == ['--migrate-runtime', '--handoff-role', 'manager']
    assert command['argv'][command['argv'].index('--prompt-file') + 1] == '<prompt-file>'
    assert '$PromptFile' in command['powershell']


@pytest.mark.skipif(os.name != 'nt', reason='PowerShell recovery command contract')
def test_rendered_powershell_preserves_chinese_arguments_and_restores_environment(tmp_path):
    import subprocess
    entry = tmp_path / 'dispatch.py'
    entry.write_text("import json, os, sys\nprint(json.dumps({'argv':sys.argv[1:],'compact':os.environ.get('FOUNDRY_COMPACT_TOKENS')}))\nsys.exit(7)\n", encoding='utf-8')
    native = tmp_path / 'native'; native.mkdir()
    (native / 'native-manifest.json').write_text(json.dumps({
        'runtime_provenance': {'python': sys.executable},
        'backend_identity': {'launch_context': {'compact_tokens': 240000}}}))
    manifest = {'runtime_provenance': {'python': sys.executable,
                    'adapter': {'module': '_pdcl_project', 'path': str(tmp_path / '_pdcl_project.py')}},
                'manager_state': {'native_dir': str(native)}}
    order = tmp_path / "中文任务'修订.md"
    plan = recovery_invocation(order, tmp_path, manifest)
    script = tmp_path / 'rendered.ps1'
    script.write_text("$env:FOUNDRY_COMPACT_TOKENS='0'; " + plan['powershell'] +
        "; Write-Output ('AFTER=' + $env:FOUNDRY_COMPACT_TOKENS); exit $LASTEXITCODE", encoding='utf-8-sig')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-File', str(script)],
        capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
    assert result.returncode == 7, result.stderr
    lines = result.stdout.splitlines()
    observed = json.loads(lines[0])
    assert observed['argv'] == plan['argv'][2:]
    assert observed['compact'] == '240000'
    assert lines[-1] == 'AFTER=0'
