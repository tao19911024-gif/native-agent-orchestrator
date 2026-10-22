"""Legacy goal migration after removal of an extension-owned executable."""
import json
from pathlib import Path
import sys

import pytest

from foundry_runtime import foundry_goal as goal, _foundry_dispatch as dispatch
from foundry_runtime import _dispatch_claude as claude, foundry_native_support as support
from foundry_runtime._recovery_reconciliation import completed_coding
from foundry_runtime._foundry_goal_delivery import coding_recovery
from test_installed_runtime import external, offline


@pytest.fixture
def legacy_goal(external, tmp_path, monkeypatch):
    tree, scope, order, _ = external
    old = tmp_path / 'extension-removed.exe'; old.write_bytes(b'old CLI')
    current = tmp_path / 'retained.exe'; current.write_bytes(b'new CLI')
    monkeypatch.setattr(claude, 'resolve_executable', lambda _: current)
    monkeypatch.setattr(support, 'resolve_executable', lambda _: current)
    calls, spawn = [], dispatch.OwnedProcess.spawn
    def child(argv, **kwargs):
        calls.append(argv)
        sid = argv[argv.index('--session-id') + 1]
        rows = [{'type': 'system', 'subtype': 'init', 'session_id': sid},
                {'type': 'result', 'session_id': sid, 'subtype': 'error_during_execution',
                 'is_error': True, 'result': "Your account is on hold and can't use Claude Code"}]
        script = 'import sys; sys.stdin.buffer.read(); print(' + repr('\n'.join(map(json.dumps, rows))) + '); sys.exit(1)'
        return spawn([sys.executable, '-B', '-c', script], **kwargs)
    monkeypatch.setattr(dispatch.OwnedProcess, 'spawn', child)
    folder = tmp_path / 'original'
    specs = [{'backend': 'claude', 'model': 'recorded-model', 'effort': 'high', 'executable': str(old)}] * 2
    goal.run_goal(order, folder, old, backend='claude', coding_launcher='claude',
                  reviewers=specs, project_record=scope.record())
    checkpoint = json.loads((folder / 'coding-checkpoint.json').read_bytes())
    package_path = next(Path(f['path']).with_suffix('.package.json') for f in checkpoint['evidence']
                        if f['path'].endswith('.log'))
    package = json.loads(package_path.read_bytes())
    journal = Path(package.pop('intent_journal'))
    package_path.write_text(json.dumps(package))
    old.unlink()
    (tree / 'unfinished.txt').write_text('保留已有工作', encoding='utf-8')
    return order, folder, scope, current, calls, package_path, journal


def test_legacy_completion_reads_journal_not_start_snapshot(legacy_goal):
    order, folder, scope, current, calls, package_path, journal = legacy_goal
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    checkpoint = json.loads((folder / 'coding-checkpoint.json').read_bytes())
    frozen = {p: p.read_bytes() for p in journal.rglob('*') if p.is_file()}
    package = package_path.read_bytes()
    assert json.loads(package)['execution_state'] == 'active'
    proof = completed_coding(checkpoint)
    assert proof['state'] == 'completed' and proof['returncode'] == 1
    assert Path(proof['journal']) == journal
    report = coding_recovery(folder, manifest)
    assert report['status'] == 'explicit_continuation_available'
    assert report['stop_cause'] == 'provider_error'
    assert package_path.read_bytes() == package
    assert all(p.read_bytes() == raw for p, raw in frozen.items())


def test_legacy_dispatch_status_uses_selected_authority(legacy_goal):
    from types import SimpleNamespace
    from foundry_runtime._project_context import adapter_scope
    from foundry_runtime._followup import _dispatch_status
    _, _, scope, _, _, package_path, journal = legacy_goal
    with adapter_scope(SimpleNamespace(goal_authority=lambda: scope)):
        report = _dispatch_status(package_path)
    assert report['status'] == 'result_available'
    assert report['returncode'] == 1 and Path(report['journal']) == journal


def test_legacy_cleanup_alone_does_not_replace_missing_journal(legacy_goal):
    _, folder, _, _, _, _, journal = legacy_goal
    # Simulate missing storage using a moved test journal, never business data.
    journal.rename(journal.with_name('fixture-hidden'))
    from foundry_runtime._execution_intent import IntentConflict
    checkpoint = json.loads((folder / 'coding-checkpoint.json').read_bytes())
    with pytest.raises((IntentConflict, OSError, ValueError)):
        completed_coding(checkpoint)


@pytest.mark.parametrize('refused', [False, True])
def test_migration_selects_fresh_cli_and_freezes_retry(legacy_goal, monkeypatch, refused):
    order, folder, scope, current, calls, package_path, journal = legacy_goal
    frozen = {p: p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    working = (scope.workspace / 'unfinished.txt').read_bytes()
    options = dict(answer='保留原有工作继续', target_provider='claude', migrate_runtime=True)
    handoff = goal.goal_continuation.dispatch_handoff
    if refused:
        monkeypatch.setattr(goal.goal_continuation, 'dispatch_handoff',
                            lambda *a, **k: (_ for _ in ()).throw(goal.GoalBlocked('fixture admission refused')))
        with pytest.raises(goal.GoalBlocked, match='fixture admission refused'):
            goal.recover_goal(order, folder, **options)
        # A later extension update cannot change the already frozen review CLI.
        monkeypatch.setattr(support, 'resolve_executable', lambda _: pytest.fail('reselected frozen CLI'))
        monkeypatch.setattr(goal.goal_continuation, 'dispatch_handoff', handoff)
    result = goal.recover_goal(order, folder, **options)
    assert len(calls) == 2 and all('--resume' not in argv for argv in calls)
    target = Path(result['continuation_evidence'])
    saved = json.loads((target / 'coding-checkpoint.json').read_bytes())
    assert saved['executable'] == str(current)
    assert all(row['executable'] == str(current) for row in saved['options']['reviewers'])
    assert (scope.workspace / 'unfinished.txt').read_bytes() == working
    assert all(p.read_bytes() == raw for p, raw in frozen.items())
    assert goal.recover_goal(order, folder, **options) == result
    assert len(calls) == 2


def test_codex_worker_resumes_same_session_with_missing_claude_review_cli(external, tmp_path, monkeypatch):
    from foundry_runtime import app_server_transport as transport
    from test_owned_goal_recovery import SERVER, SID
    tree, scope, order, _ = external
    home = tmp_path / 'codex'; sessions = home / 'sessions/2026/10/10'; sessions.mkdir(parents=True)
    (sessions / f'rollout-fixture-{SID}.jsonl').write_text(json.dumps(
        {'type': 'session_meta', 'payload': {'id': SID, 'cwd': str(tree)}}) + '\n')
    monkeypatch.setenv('CODEX_HOME', str(home))
    original, calls = transport.run_executor, []
    def run(*args, **kwargs):
        calls.append(kwargs.get('resume_thread_id'))
        def spawn(argv, **opts):
            return transport.OwnedProcess.spawn([sys.executable, '-B', '-c', 'HANG=False\n' + SERVER], **opts)
        return original(*args, process_factory=spawn, **kwargs)
    monkeypatch.setattr(transport, 'run_executor', run)
    old = tmp_path / 'removed-claude.exe'; old.write_bytes(b'old')
    current = tmp_path / 'retained-claude.exe'; current.write_bytes(b'new')
    selections = []
    monkeypatch.setattr(support, 'resolve_executable', lambda backend: selections.append(backend) or current)
    reviewers = [{'backend': 'claude', 'model': 'recorded-model', 'effort': 'high', 'executable': str(old)}] * 2
    folder = tmp_path / 'codex-goal'
    goal.run_goal(order, folder, old, backend='claude', coding_launcher='app-server',
                  reviewers=reviewers, project_record=scope.record())
    frozen = {p: p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    old.unlink()
    result = goal.recover_goal(order, folder, answer='按检查点裁决继续原会话')
    assert result['status'] == 'project_handoff' and calls == [None, SID]
    assert selections == ['claude']  # One selection shared by both fresh reviewers.
    checkpoint = json.loads((Path(result['continuation_evidence']) / 'coding-checkpoint.json').read_bytes())
    assert checkpoint['coding_session'] == SID
    assert all(r['executable'] == str(current) for r in checkpoint['options']['reviewers'])
    assert all(p.read_bytes() == raw for p, raw in frozen.items())
    assert goal.recover_goal(order, folder, answer='按检查点裁决继续原会话') == result
    assert calls == [None, SID] and selections == ['claude']
