import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from foundry_runtime import _release_retention as retention, release_selection


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


@pytest.fixture
def estate(tmp_path, monkeypatch):
    home = tmp_path / 'foundry'
    versions = [home / 'releases' / f'0.1.0a{n}' for n in range(1, 5)]
    for path in versions:
        write(path / 'Scripts/python.exe', {})
    selection = write(home / 'current.json', {'python': str(versions[3] / 'Scripts/python.exe')})
    evidence = home / 'evidence'
    evidence.mkdir()
    monkeypatch.setattr(retention, 'host_snapshot', lambda **kwargs: {'processes': [], 'registrations': []})
    return home, selection, versions, evidence


def plan(estate):
    _, selection, versions, evidence = estate
    return retention.inventory(selection, versions[2] / 'Scripts/python.exe', roots=[evidence])


def authority_adapter(evidence, status='closed', worktree=None):
    row = SimpleNamespace(status=status, worktree=str(worktree or evidence / 'worker'), line_number=7)
    authority = SimpleNamespace(path=evidence.parent / 'PORTFOLIO.md', sha256='observed',
                                resolve_task=lambda tag: row)
    return SimpleNamespace(evidence=lambda tag: evidence / tag, goal_authority=lambda: authority)


def test_retired_owner_releases_history_but_not_hosted_custody(estate):
    _, selection, versions, evidence = estate
    adapter = authority_adapter(evidence)
    path = write(evidence / 'TASK/goal-old/manifest.json', {'tag': 'TASK', 'mode': 'auto-amend',
        'order': {'path': str(evidence.parent / 'orders/TASK_old.md')}})
    write(evidence / 'TASK/host/request.json', {'task_name': 'Foundry-held', 'run_id': 'run',
                                             'python': str(versions[0] / 'Scripts/python.exe')})
    report = retention.inventory(selection, versions[2] / 'Scripts/python.exe', roots=[evidence], project_adapter=adapter)
    assert not report['unknown_provenance']
    assert report['candidates'] == [str(versions[1])]
    assert report['classified_history'][0]['source'] == str(path)
    assert report['classified_history'][0]['reason'] == 'owner-retired historical evidence'


@pytest.mark.parametrize('conflict', ['tag', 'worktree', 'order', 'relative_order', 'local_order', 'accepted', 'blocked', 'unreadable'])
def test_owner_closure_never_overrides_identity_conflict_or_unreadable_record(estate, conflict):
    _, selection, versions, evidence = estate
    record = {'tag': 'TASK', 'order': {'path': str(evidence.parent / 'orders/TASK_old.md')}}
    status = conflict if conflict in ('accepted', 'blocked') else 'closed'
    if conflict == 'tag': record['tag'] = 'OTHER'
    if conflict == 'worktree': record['worktree'] = str(evidence / 'other')
    if conflict == 'order': record['order']['path'] = str(evidence.parent / 'orders/OTHER_old.md')
    if conflict == 'relative_order': record['order']['path'] = 'orders/OTHER_old.md'
    if conflict == 'local_order': record['order']['path'] = str(evidence / 'TASK/OTHER_old.md')
    path = write(evidence / 'TASK/goal-old/manifest.json', record)
    if conflict == 'unreadable': path.write_text('{invalid')
    report = retention.inventory(selection, versions[2] / 'Scripts/python.exe', roots=[evidence],
                                 project_adapter=authority_adapter(evidence, status))
    assert not report['candidates'] and not report['classified_history']


def test_real_test_receipt_classifies_broken_fixtures_without_name_based_exclusion(estate):
    home, selection, versions, evidence = estate
    root = evidence / 'arbitrary-temp'
    write(root / 'goal-one/manifest.json', ['deliberately malformed fixture'])
    receipt = write(home / 'test_run_record.json', {'schema_version': 'pdcl-test-run-record-1',
        'run': {'command': {'argv': ['python', '-m', 'pytest', '--basetemp', str(root)]},
                'exit_code': 1, 'finished_utc': '2026-10-01T00:00:00Z'}})
    report = retention.inventory(selection, versions[2] / 'Scripts/python.exe', roots=[evidence],
                                 test_run_records=[receipt])
    assert not report['gaps'] and len(report['classified_history']) == 1
    assert report['candidates'] == list(map(str, versions[:2]))
    write(evidence / 'pytest-looks-like-fixture/goal-real/manifest.json', {'order': {'path': 'real'}})
    report = retention.inventory(selection, versions[2] / 'Scripts/python.exe', roots=[evidence],
                                 test_run_records=[receipt])
    assert not report['candidates'] and len(report['unknown_provenance']) == 1
    record = json.loads(receipt.read_text()); record['run']['finished_utc'] = None
    write(receipt, record)
    with pytest.raises(ValueError, match='completed pytest'):
        retention.inventory(selection, versions[2] / 'Scripts/python.exe', roots=[evidence], test_run_records=[receipt])


def test_receipt_matching_resolves_each_record_once(estate, monkeypatch):
    home, selection, versions, evidence = estate
    manifest = write(evidence / 'goal-real/manifest.json', {'run_id': 'real',
        'runtime_provenance': {'python': str(versions[0] / 'Scripts/python.exe')}})
    receipts = [write(home / f'receipt-{n}.json', {'schema_version': 'pdcl-test-run-record-1',
        'run': {'command': {'argv': ['python', '-m', 'pytest', '--basetemp', str(home / f'fixtures-{n}')]},
                'exit_code': 0, 'finished_utc': '2026-10-01T00:00:00Z'}}) for n in range(9)]
    calls = []
    original = Path.resolve
    def observe(path, *args, **kwargs):
        if path == manifest:
            calls.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', observe)
    report = retention.inventory(selection, versions[2] / 'Scripts/python.exe',
                                 roots=[evidence], test_run_records=receipts)
    assert report['candidates'] == [str(versions[1])]
    assert len(calls) == 1, 'receipt count must not multiply Windows path-handle I/O'


@pytest.mark.parametrize('kind', ['fixture', 'artifact'])
def test_review_is_exact_record_evidence_not_a_directory_exemption(estate, kind):
    import hashlib
    home, selection, versions, evidence = estate
    path = write(evidence / 'goal-fixture/manifest.json',
                 ['fixture'] if kind == 'fixture' else [{'path': 'report.md', 'sha256': 'a' * 64}])
    source = write(home / 'original-test-command.json', ['python', '-m', 'pytest'])
    fact = lambda p: {'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
    review = write(home / 'review.json', {'schema_version': 'foundry-retention-evidence-review-1',
        'observations': [{'kind': kind, 'record': fact(path), 'evidence': [fact(source)],
                          'reason': 'inspected original test command and intentional malformed fixture'}]})
    report = retention.inventory(selection, versions[2] / 'Scripts/python.exe', roots=[evidence], evidence_review=review)
    assert report['candidates'] == list(map(str, versions[:2]))
    write(evidence / 'goal-fixture/new/goal-business/manifest.json', {'order': {'path': 'real'}})
    report = retention.inventory(selection, versions[2] / 'Scripts/python.exe', roots=[evidence], evidence_review=review)
    assert not report['candidates']
    source.write_text('different original evidence')
    with pytest.raises(ValueError, match='evidence changed'):
        retention.inventory(selection, versions[2] / 'Scripts/python.exe', roots=[evidence], evidence_review=review)


def test_review_cannot_follow_manifest_change_during_long_scan(estate, monkeypatch):
    import hashlib
    home, selection, versions, evidence = estate
    path = write(evidence / 'goal-one/manifest.json', {'order': {'path': 'original'}})
    source = write(home / 'source.json', {'observed': 'legacy launch'})
    fact = lambda p: {'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
    review = write(home / 'review.json', {'schema_version': 'foundry-retention-evidence-review-1',
        'observations': [{'kind': 'runtime', 'record': fact(path), 'evidence': [fact(source)],
                          'reason': 'legacy command', 'interpreters': [str(home / 'legacy/python.exe')]}]})
    original = retention._walk
    def changed(*args, **kwargs):
        write(path, {'order': {'path': 'new'}, 'run_id': 'different'})
        yield from original(*args, **kwargs)
    monkeypatch.setattr(retention, '_walk', changed)
    with pytest.raises(ValueError, match='evidence changed'):
        retention.inventory(selection, versions[2] / 'Scripts/python.exe', roots=[evidence], evidence_review=review)


@pytest.mark.parametrize('change', ['task_hold', 'pth'])
def test_rotation_refreshes_task_holds_and_python_dependencies_without_archive_walk(estate, monkeypatch, change):
    _, selection, versions, _ = estate
    holds, removals = [], []
    monkeypatch.setattr(retention, '_walk', lambda *a, **k: pytest.fail('daily rotation must not walk archives'))
    def remove(target, root, current, **kwargs):
        removals.append(target)
        shutil.rmtree(target)
        if change == 'task_hold':
            holds.append(versions[1])
        else:
            pth = versions[3] / 'Lib/site-packages/shared.pth'
            pth.parent.mkdir(parents=True)
            pth.write_text(str(versions[1] / 'Lib/site-packages'))
        return {'exists': False}
    monkeypatch.setattr(retention, '_remove_release', remove)
    result = retention.prune(selection, versions[2] / 'Scripts/python.exe', versions[:2], keep_releases=holds)
    assert removals == [versions[0]]
    assert result['deleted'] == [str(versions[0])] and result['retained'] == [str(versions[1])]
    assert not result['errors']
    assert all(r['scan']['directories'] == 0 for r in result['inventories'])


def test_live_external_python_pth_keeps_shared_release_and_not_prefix_neighbour(estate, monkeypatch):
    home, _, versions, _ = estate
    environment = home / 'scratch/python-env'
    executable = environment / 'Scripts/python.exe'
    write(executable, {})
    pth = environment / 'Lib/site-packages/shared.pth'
    pth.parent.mkdir(parents=True)
    pth.write_text(str(versions[1] / 'Lib/site-packages'))
    monkeypatch.setattr(retention, 'host_snapshot', lambda: {'registrations': [], 'processes': [
        {'ProcessId': 77, 'ExecutablePath': str(executable), 'CommandLine': str(executable)}]})
    assert plan(estate)['candidates'] == [str(versions[0])]
    assert list(retention._python_dependencies(executable, [str(home / 'releases/0.1.0a')])) == []


@pytest.mark.parametrize('runtime_home', [False, True])
def test_venv_creation_history_is_not_a_runtime_dependency(estate, runtime_home):
    home, _, versions, _ = estate
    environment = home / 'external'
    environment.mkdir()
    base = versions[0] if runtime_home else Path('C:/Program Files/Python313')
    (environment / 'pyvenv.cfg').write_text(
        f'home = {base}\nexecutable = {versions[1]}/Scripts/python.exe\n'
        f'command = {versions[1]}/Scripts/python.exe -m venv {environment}\n')
    dependencies = list(retention._python_dependencies(environment / 'Scripts/python.exe', list(map(str, versions))))
    assert [release for release, _ in dependencies] == ([str(versions[0])] if runtime_home else [])


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows venv launcher contract')
def test_real_windows_venv_runs_without_recorded_creator(tmp_path):
    import sys
    import venv
    environment = tmp_path / 'child'
    venv.EnvBuilder(with_pip=False).create(environment)
    config = environment / 'pyvenv.cfg'
    missing = tmp_path / 'nonexistent-creator/Scripts/python.exe'
    lines = config.read_text().splitlines()
    config.write_text('\n'.join(
        f'executable = {missing}' if line.startswith('executable =') else
        f'command = {missing} -m venv {environment}' if line.startswith('command =') else line
        for line in lines) + '\n')
    raw = subprocess.check_output([str(environment / 'Scripts/python.exe'), '-I', '-c',
        'import sys,json,sqlite3; print(json.dumps([sys._base_executable,sys.base_prefix]))'], text=True)
    base, prefix = json.loads(raw)
    assert Path(base).resolve() == Path(sys._base_executable).resolve()
    assert Path(prefix).resolve() == Path(sys.base_prefix).resolve()
    assert not missing.exists()


def test_selection_and_owner_are_refreshed_after_long_walk(estate, monkeypatch):
    _, selection, versions, evidence = estate
    original = retention._walk
    def changed(*args, **kwargs):
        yield from original(*args, **kwargs)
        write(selection, {'python': str(versions[1] / 'Scripts/python.exe')})
    monkeypatch.setattr(retention, '_walk', changed)
    assert plan(estate)['candidates'] == [str(versions[0])]
    write(evidence / 'TASK/goal-one/manifest.json', {'order': {'path': 'TASK_order.md'}})
    closed = authority_adapter(evidence, 'closed').goal_authority()
    reopened = authority_adapter(evidence, 'running').goal_authority()
    calls = iter([closed, reopened])
    adapter = SimpleNamespace(evidence=lambda tag: evidence / tag, goal_authority=lambda: next(calls))
    report = retention.inventory(selection, versions[2] / 'Scripts/python.exe', roots=[evidence], project_adapter=adapter)
    assert not report['candidates'] and any('owner or record changed' in x['reason'] for x in report['gaps'])


def test_deep_waiting_goal_and_completed_blocked_round_keep_recorded_runtime(estate):
    _, _, versions, evidence = estate
    folder = evidence / 'tag' / 'old' / 'continued' / 'deep' / 'goal-one' / 'round-0'
    write(folder / 'manifest.json', {'run_id': 'r', 'order': {'path': 'order.md'}, 'state': 'completed',
        'runtime_provenance': {'python': str(versions[0] / 'Scripts/python.exe')}})
    write(folder / 'outcome.json', {'status': 'coding_incomplete'})
    report = plan(estate)
    assert str(versions[0]) not in report['candidates']
    assert str(folder / 'manifest.json') in report['unfinished_goals']
    assert report['candidates'] == [str(versions[1])]


@pytest.mark.parametrize('outcome', [None, 'awaiting_input', 'provider_limit', 'review_blocked', 'new_vendor_failure'])
def test_unknown_goal_provenance_protects_all_possible_releases(estate, outcome):
    _, _, _, evidence = estate
    path = write(evidence / 'continue-one/manifest.json', {'run_id': 'r', 'order': {'path': 'order'}})
    if outcome:
        write(path.with_name('outcome.json'), {'status': outcome})
    report = plan(estate)
    assert report['candidates'] == [] and len(report['keep']) == 4
    assert len(report['unknown_provenance']) == 1
    assert all(any('no trustworthy' in reason['reason'] for reason in item['reasons']) for item in report['keep'])


def test_successful_goal_does_not_permanently_keep_its_old_runtime(estate):
    _, _, versions, evidence = estate
    path = write(evidence / 'goal-one/manifest.json', {'run_id': 'r', 'order': {'path': 'order'},
        'runtime_provenance': {'python': str(versions[0] / 'Scripts/python.exe')}})
    write(path.with_name('outcome.json'), {'status': 'candidate_verified'})
    assert str(versions[0]) in plan(estate)['candidates']


def test_unreadable_evidence_and_host_failure_are_not_unused_proof(estate, monkeypatch):
    _, _, _, evidence = estate
    (evidence / 'manifest.json').write_text('{broken')
    def failed():
        raise subprocess.TimeoutExpired('host snapshot', 1)
    monkeypatch.setattr(retention, 'host_snapshot', failed)
    report = plan(estate)
    assert not report['candidates'] and len(report['gaps']) == 2


def test_junction_alias_and_cycle_scan_once_without_a_depth_limit(estate):
    _, selection, versions, evidence = estate
    write(evidence / 'a/b/c/d/e/goal-one/manifest.json', {'order': {'path': 'order'}, 'run_id': 'r'})
    alias = evidence.parent / 'alias'
    try:
        alias.symlink_to(evidence, target_is_directory=True)
        (evidence / 'a/back').symlink_to(evidence, target_is_directory=True)
    except OSError:
        pytest.skip('directory symlink privilege unavailable')
    report = retention.inventory(selection, versions[2] / 'Scripts/python.exe', roots=[alias, evidence])
    assert len(report['unfinished_goals']) == 1
    assert len(report['unknown_provenance']) == 1 and not report['gaps']
    assert len(report['evidence_roots']) == 2


@pytest.mark.parametrize('finished,registered,expected_keep', [(False, False, True), (True, True, True), (True, False, False)])
def test_hosted_request_retires_only_with_matching_finish_and_absent_registration(estate, monkeypatch, finished, registered, expected_keep):
    _, _, versions, evidence = estate
    path = write(evidence / 'hosted/run/request.json', {'task_name': 'Foundry-run', 'run_id': 'one',
        'python': str(versions[0] / 'Scripts/python.exe')})
    if finished:
        write(path.with_name('finished.json'), {'run_id': 'one', 'returncode': 1})
    monkeypatch.setattr(retention, 'host_snapshot', lambda: {'processes': [], 'registrations': [
        {'name': 'Foundry-run', 'actions': [{'Execute': str(versions[0] / 'Scripts/pythonw.exe')}]}] if registered else []})
    assert (str(versions[0]) not in plan(estate)['candidates']) is expected_keep


def test_mismatched_finished_receipt_does_not_retire_a_hosted_request(estate):
    _, _, versions, evidence = estate
    path = write(evidence / 'hosted/run/request.json', {'task_name': 'Foundry-run', 'run_id': 'one',
        'python': str(versions[0] / 'Scripts/python.exe')})
    write(path.with_name('finished.json'), {'run_id': 'other', 'returncode': 0})
    assert str(versions[0]) not in plan(estate)['candidates']


def test_registration_arguments_and_checkpoint_executables_are_dependencies(estate, monkeypatch):
    _, _, versions, evidence = estate
    write(evidence / 'goal-one/manifest.json', {'run_id': 'one', 'order': {'path': 'order'},
        'runtime_provenance': {'python': str(versions[3] / 'Scripts/python.exe')}})
    write(evidence / 'goal-one/coding-checkpoint.json', {'analyst': {'executable': str(versions[0] / 'native/claude.exe')}})
    monkeypatch.setattr(retention, 'host_snapshot', lambda: {'processes': [], 'registrations': [
        {'name': 'Foundry-run', 'actions': [{'Execute': 'C:/Windows/cmd.exe',
            'Arguments': str(versions[1] / 'Scripts/python.exe')}]}]})
    assert not plan(estate)['candidates']


def test_prune_rechecks_processes_before_each_target_and_never_replays_saved_plan(estate, monkeypatch):
    _, selection, versions, evidence = estate
    snapshots, removals = [], []
    def snapshot(**kwargs):
        snapshots.append(1)
        return {'registrations': [], 'processes': [] if len(snapshots) <= 1 else [
            {'ProcessId': 42, 'CommandLine': str(versions[1] / 'Scripts/python.exe')}]}
    def remove(path, root, current, **kwargs):
        assert path == versions[0] and root == path.parent
        removals.append(path)
        shutil.rmtree(path)  # Only this pytest fixture; native production remover is not invoked.
        return {'exists': False}
    monkeypatch.setattr(retention, 'host_snapshot', snapshot)
    monkeypatch.setattr(retention, '_remove_release', remove)
    report = retention.prune(selection, versions[2] / 'Scripts/python.exe', versions[:2])
    assert len(snapshots) == 2 and removals == [versions[0]]
    assert report['deleted'] == [str(versions[0])] and report['retained'] == [str(versions[1])]


def test_historical_records_do_not_block_rotation_and_rejection_is_not_retried(estate, monkeypatch):
    _, selection, versions, evidence = estate
    write(evidence / 'old-fixture/manifest.json', {'run_id': 'old', 'order': {'path': 'order'}})
    calls = []
    def refused(*args, **kwargs):
        calls.append(args[0])
        raise OSError('blocked by policy')
    monkeypatch.setattr(retention, '_walk', lambda *a, **k: pytest.fail('archive walk'))
    monkeypatch.setattr(retention, '_remove_release', refused)
    report = retention.prune(selection, versions[2] / 'Scripts/python.exe', versions[:2])
    assert len(report['errors']) == len(report['inventories']) == 1 and not report['deleted']
    assert calls == [versions[0]]


def test_paths_outside_direct_release_children_are_refused(estate):
    home, selection, versions, evidence = estate
    for path in (home, home / 'scratch/runtime', versions[0] / 'Scripts', versions[0] / '../..'):
        with pytest.raises(ValueError, match='exact direct version'):
            retention.prune(selection, versions[2] / 'Scripts/python.exe', [path])


def test_cli_rotation_plan_uses_only_current_task_holds(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(retention, 'installed_inventory', lambda *a, **k: seen.append((a, k)) or {'keep': []})
    assert release_selection.main(['--selection', 'current.json', '--retention-plan',
        '--rollback-python', 'old/python.exe', '--evidence-root', 'external']) == 0
    assert seen == [(('current.json', 'old/python.exe'), {'keep_releases': []})]
    assert json.loads(capsys.readouterr().out) == {'keep': []}


@pytest.mark.parametrize('owner', ['manager_state', 'expert_state'])
def test_unfinished_owner_follows_native_executable_dependencies(estate, owner):
    _, _, versions, evidence = estate
    native = evidence / 'native-owner'
    write(native / 'native-manifest.json', {'executable': str(versions[0] / 'native/claude.exe')})
    write(native.with_name('native-owner-experts') / 'question/native-manifest.json',
          {'executable': str(versions[1] / 'native/claude.exe')})
    write(evidence / 'goal-one/manifest.json', {'run_id': 'r', 'order': {'path': 'order'},
        'runtime_provenance': {'python': str(versions[3] / 'Scripts/python.exe')},
        owner: {'native_dir': str(native), 'expert': {'backend': 'claude'}}})
    report = plan(estate)
    assert not report['candidates'] and not report['gaps']
    assert any(r['reason'] == 'unfinished native session dependency' for r in report['keep'][0]['reasons'])


def test_missing_started_native_manifest_keeps_unknown_dependencies(estate):
    _, _, versions, evidence = estate
    write(evidence / 'goal-one/manifest.json', {'run_id': 'r', 'order': {'path': 'order'},
        'runtime_provenance': {'python': str(versions[3] / 'Scripts/python.exe')},
        'manager_state': {'native_dir': str(evidence / 'missing'), 'last_call_id': 'previous'}})
    report = plan(estate)
    assert not report['candidates'] and len(report['gaps']) == 1


def test_project_history_roots_use_existing_workspace_resolution(estate):
    home, selection, versions, evidence = estate
    workspace = home / 'workspace'
    absolute = home / 'archive'
    relative = workspace / 'old-evidence'
    write(home / 'projects/project.json', {'schema_version': 'foundry-project-1',
        'workspace': str(workspace), 'evidence_root': str(evidence),
        'history_roots': ['old-evidence', str(absolute)]})
    for root, version in zip([relative, absolute], versions):
        write(root / 'a/b/c/d/e/goal-old/manifest.json', {'run_id': 'r', 'order': {'path': 'order'},
            'runtime_provenance': {'python': str(version / 'Scripts/python.exe')}})
    roots, gaps = retention.known_roots(selection)
    assert not gaps and relative in roots and absolute in roots
    report = retention.inventory(selection, versions[2] / 'Scripts/python.exe', roots=[evidence, relative, absolute])
    assert not report['candidates'] and not report['gaps'] and len(report['unfinished_goals']) == 2


def test_deep_ordinary_directories_do_not_repeat_realpath_io(estate, monkeypatch):
    _, _, _, evidence = estate
    write(evidence / '/'.join(['n'] * 40) / 'goal-one/manifest.json', {'run_id': 'r'})
    calls = []
    original = Path.resolve
    def observe(path, *args, **kwargs):
        calls.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', observe)
    gaps = []
    assert len(list(retention._walk([evidence], gaps))) == 1
    assert not gaps
    assert len(calls) <= 2, 'ordinary descendants must reuse their canonical root, not reopen every path'


def test_scan_reports_physical_root_cost_without_double_counting(estate):
    _, selection, versions, evidence = estate
    nested = evidence / 'nested'
    first = write(evidence / 'ordinary/manifest.json', {'report': 'one'})
    second = write(nested / 'deep/request.json', {'report': 'two'})
    gaps, metrics = [], {'directories': 0, 'records': 0}
    assert set(retention._walk([evidence, nested, evidence], gaps, metrics)) == {first, second}
    assert not gaps
    rows = {r['root']: r for r in metrics['roots']}
    assert set(rows) == {str(evidence), str(nested)}
    assert rows[str(evidence)]['directories'] == 2
    assert rows[str(nested)]['directories'] == 2
    assert sum(r['directories'] for r in rows.values()) == metrics['directories'] == 4
    assert sum(r['discovered_records'] for r in rows.values()) == metrics['records'] == 2
    assert all(r['reader_seconds'] >= 0 for r in rows.values())


def test_refresh_attributes_discovery_to_registered_root(estate):
    _, selection, versions, evidence = estate
    directory = evidence / 'existing'
    write(directory / 'manifest.json', {'report': 'initial'})
    discovery = {}
    retention.inventory(selection, versions[2] / 'Scripts/python.exe',
                        roots=[evidence], _discovery=discovery)
    write(directory / 'added/request.json', {'report': 'new'})
    report = retention.inventory(selection, versions[2] / 'Scripts/python.exe',
                                 roots=[evidence], _discovery=discovery)
    assert not report['gaps']
    assert report['scan']['roots'][0]['root'] == str(evidence)
    assert report['scan']['roots'][0]['directories'] == report['scan']['directories'] == 2
    assert report['scan']['roots'][0]['discovered_records'] == 2
    assert report['scan']['records'] == 2


def test_installation_names_do_not_hide_task_evidence(estate):
    _, _, _, evidence = estate
    write(evidence / 'snapshot/venv/pyvenv.cfg', {})
    inside = write(evidence / 'snapshot/venv/lib/python3.14/site-packages/pkg/goal-example/manifest.json', {})
    expected = write(evidence / 'snapshot/venv/work/deep/goal-real/manifest.json', {})
    # A directory merely named venv has no installation ownership marker.
    other = write(evidence / 'other/venv/lib/deep/goal-real/manifest.json', {})
    report = plan(estate)
    assert set(report['unfinished_goals']) == {str(inside), str(expected), str(other)}


def test_payload_named_external_junction_cannot_silently_skip_dependency(estate):
    home, _, _, evidence = estate
    external = home / 'external'
    write(external / 'goal-one/manifest.json', {})
    write(evidence / 'venv/pyvenv.cfg', {})
    try:
        (evidence / 'venv/Lib').symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip('directory symlink privilege unavailable')
    report = plan(estate)
    assert not report['candidates']
    assert any('junction leaves' in row['reason'] for row in report['gaps'])


def test_directory_attribute_io_overlaps_without_losing_records(estate, monkeypatch):
    from contextlib import contextmanager
    from threading import Barrier, current_thread, main_thread
    _, _, _, evidence = estate
    expected = {write(evidence / f'branch-{n}/manifest.json', {}) for n in range(4)}
    original = retention.os.scandir
    ready = Barrier(4)

    class Entry:
        def __init__(self, entry):
            self.path, self.name = entry.path, entry.name

        def is_dir(self, **kwargs):
            assert current_thread() is not main_thread(), 'attribute I/O serialized on caller'
            ready.wait(timeout=10)
            return False

    @contextmanager
    def observed(directory):
        with original(directory) as entries:
            yield entries if Path(directory) == evidence else [Entry(e) for e in entries]

    monkeypatch.setattr(retention.os, 'scandir', observed)
    gaps = []
    assert set(retention._walk([evidence], gaps)) == expected
    assert not gaps


def test_attribute_failure_reports_gap_and_still_reads_later_sibling(estate, monkeypatch):
    from contextlib import contextmanager
    _, _, _, evidence = estate
    expected = write(evidence / 'manifest.json', {})
    original = retention.os.scandir

    class Unreadable:
        path = str(evidence / 'unreadable')
        name = 'unreadable'

        def is_dir(self, **kwargs):
            raise OSError('attribute read failed')

    @contextmanager
    def observed(directory):
        with original(directory) as entries:
            yield [Unreadable(), *entries]

    monkeypatch.setattr(retention.os, 'scandir', observed)
    gaps = []
    assert list(retention._walk([evidence], gaps)) == [expected]
    assert len(gaps) == 1 and gaps[0]['source'] == Unreadable.path
    assert 'attribute read failed' in gaps[0]['reason']


def test_explicit_history_audit_refreshes_new_registered_root(estate, monkeypatch):
    home, selection, versions, evidence = estate
    added = home / 'new-project'
    write(added / 'goal-new/manifest.json', {'runtime_provenance': {'python': str(versions[0] / 'Scripts/python.exe')}})
    calls = []
    def roots(*args):
        calls.append(1)
        return ([evidence] if len(calls) == 1 else [evidence, added]), []
    monkeypatch.setattr(retention, 'known_roots', roots)
    discovery = {}
    retention.inventory(selection, versions[2] / 'Scripts/python.exe', _discovery=discovery)
    report = retention.inventory(selection, versions[2] / 'Scripts/python.exe', _discovery=discovery)
    assert str(versions[0]) not in report['candidates']
    assert report['scan']['added_roots'] == [str(added)]


def test_reordering_or_removing_roots_does_not_drop_observed_dependencies(estate, monkeypatch):
    home, selection, versions, evidence = estate
    other = home / 'other'
    other.mkdir()
    write(evidence / 'goal-held/manifest.json', {
        'runtime_provenance': {'python': str(versions[0] / 'Scripts/python.exe')}})
    discovery = {}
    retention.inventory(selection, versions[2] / 'Scripts/python.exe',
                        roots=[evidence, other], _discovery=discovery)
    def unexpected(*args, **kwargs):
        pytest.fail('unchanged discovery must not restart a tree walk')
    monkeypatch.setattr(retention, '_walk', unexpected)
    for roots in ([other, evidence], [other]):
        report = retention.inventory(selection, versions[2] / 'Scripts/python.exe',
                                     roots=roots, _discovery=discovery)
        assert report['candidates'] == [str(versions[1])]
        assert not report['gaps']
        assert str(evidence) in report['discovery_roots']


def test_new_nested_root_is_scanned_even_if_ancestor_was_scanned(estate):
    _, selection, versions, evidence = estate
    discovery = {}
    retention.inventory(selection, versions[2] / 'Scripts/python.exe',
                        roots=[evidence], _discovery=discovery)
    added = evidence / 'registered-later'
    write(added / 'goal-new/manifest.json', {
        'runtime_provenance': {'python': str(versions[0] / 'Scripts/python.exe')}})
    report = retention.inventory(selection, versions[2] / 'Scripts/python.exe',
                                 roots=[evidence, added], _discovery=discovery)
    assert report['candidates'] == [str(versions[1])]
    assert report['scan']['added_roots'] == [str(added)]


@pytest.mark.parametrize('record_kind', ['goal', 'hosted_request'])
@pytest.mark.parametrize('new_directory', [False, True])
def test_new_records_under_known_root_refresh_runtime_dependencies(estate, record_kind, new_directory):
    _, selection, versions, evidence = estate
    directory = evidence / 'nested' / 'goal-new'
    (directory.parent if new_directory else directory).mkdir(parents=True)
    discovery = {}
    first = retention.inventory(selection, versions[2] / 'Scripts/python.exe',
                                roots=[evidence], _discovery=discovery)
    assert str(versions[1]) in first['candidates']
    interpreter = str(versions[1] / 'Scripts/python.exe')
    if record_kind == 'goal':
        path = write(directory / 'manifest.json', {
            'order': {'path': 'new-order.md'}, 'run_id': 'new-task',
            'runtime_provenance': {'python': interpreter}})
    else:
        path = write(directory / 'request.json', {
            'task_name': 'Foundry-new-task', 'run_id': 'new-task', 'python': interpreter})
    refreshed = retention.inventory(selection, versions[2] / 'Scripts/python.exe',
                                    roots=[evidence], _discovery=discovery)
    assert str(versions[1]) not in refreshed['candidates']
    records = (refreshed['unfinished_goals'] if record_kind == 'goal'
               else refreshed['hosted_registrations'])
    assert str(path) in records


def test_changed_directory_refresh_does_not_enumerate_unchanged_archive(estate, monkeypatch):
    _, selection, versions, evidence = estate
    archive = evidence / 'archive' / 'nested' / 'goal-completed'
    write(archive / 'manifest.json', {'order': {'path': 'old'}, 'run_id': 'old'})
    write(archive / 'outcome.json', {'status': 'candidate_verified'})
    discovery = {}
    retention.inventory(selection, versions[2] / 'Scripts/python.exe',
                        roots=[evidence], _discovery=discovery)
    scanned = []
    original = retention.os.scandir

    def scandir(directory):
        scanned.append(Path(directory))
        return original(directory)

    monkeypatch.setattr(retention.os, 'scandir', scandir)
    unchanged = retention.inventory(selection, versions[2] / 'Scripts/python.exe',
                                    roots=[evidence], _discovery=discovery)
    assert unchanged['scan']['directories'] == 0
    assert unchanged['scan']['discovery'] == 'known-record refresh'
    path = write(evidence / 'goal-new' / 'manifest.json', {
        'runtime_provenance': {'python': str(versions[1] / 'Scripts/python.exe')}})
    result = retention.inventory(selection, versions[2] / 'Scripts/python.exe',
                                 roots=[evidence], _discovery=discovery)
    assert str(versions[1]) not in result['candidates']
    assert path.parent in scanned
    assert not any(directory.is_relative_to(evidence / 'archive') for directory in scanned)


def test_directory_change_during_discovery_is_seen_on_refresh(estate, monkeypatch):
    from contextlib import contextmanager
    _, selection, versions, evidence = estate
    directory = evidence / 'goal-new'
    directory.mkdir()
    path = directory / 'manifest.json'
    original = retention.os.scandir

    @contextmanager
    def scandir(target):
        with original(target) as entries:
            yield entries
        if Path(target) == directory and not path.exists():
            write(path, {'runtime_provenance': {'python': str(versions[1] / 'Scripts/python.exe')}})

    monkeypatch.setattr(retention.os, 'scandir', scandir)
    discovery = {}
    first = retention.inventory(selection, versions[2] / 'Scripts/python.exe',
                                roots=[evidence], _discovery=discovery)
    assert str(versions[1]) in first['candidates']
    second = retention.inventory(selection, versions[2] / 'Scripts/python.exe',
                                 roots=[evidence], _discovery=discovery)
    assert str(versions[1]) not in second['candidates']


def test_directory_stat_failure_on_refresh_preserves_runtime(estate, monkeypatch):
    _, selection, versions, evidence = estate
    discovery = {}
    retention.inventory(selection, versions[2] / 'Scripts/python.exe',
                        roots=[evidence], _discovery=discovery)
    original = Path.stat

    def stat(path, *args, **kwargs):
        if path == evidence:
            raise PermissionError('synthetic directory stat unavailable')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', stat)
    result = retention.inventory(selection, versions[2] / 'Scripts/python.exe',
                                 roots=[evidence], _discovery=discovery)
    assert result['candidates'] == []
    assert any('synthetic directory stat unavailable' in row['reason'] for row in result['gaps'])


@pytest.mark.parametrize('remove_tree', [False, True])
def test_refresh_accepts_removed_evidence_and_rediscovers_replacement(estate, remove_tree):
    _, selection, versions, evidence = estate
    path = write(evidence / 'disposable' / 'manifest.json', {
        'order': {'path': 'fixture.md'}, 'run_id': 'fixture',
        'runtime_provenance': {'python': str(versions[1] / 'Scripts/python.exe')}})
    discovery = {}
    before = retention.inventory(selection, versions[2] / 'Scripts/python.exe',
                                 roots=[evidence], _discovery=discovery)
    assert str(versions[1]) not in before['candidates']
    if remove_tree:
        shutil.rmtree(path.parent)
    else:
        path.unlink()
    removed = retention.inventory(selection, versions[2] / 'Scripts/python.exe',
                                  roots=[evidence], _discovery=discovery)
    assert not removed['gaps']
    assert str(versions[1]) in removed['candidates']
    write(path.parent / 'replacement' / 'manifest.json', {
        'order': {'path': 'replacement.md'}, 'run_id': 'replacement',
        'runtime_provenance': {'python': str(versions[1] / 'Scripts/python.exe')}})
    restored = retention.inventory(selection, versions[2] / 'Scripts/python.exe',
                                   roots=[evidence], _discovery=discovery)
    assert not restored['gaps']
    assert str(versions[1]) not in restored['candidates']


def test_refresh_keeps_missing_dependency_of_present_goal_as_gap(estate):
    _, selection, versions, evidence = estate
    native = write(evidence / 'native' / 'native-manifest.json', {
        'executable': str(versions[1] / 'Scripts/python.exe')})
    path = write(evidence / 'goal-held' / 'manifest.json', {
        'runtime_provenance': {'python': str(versions[1] / 'Scripts/python.exe')},
        'manager_state': {'native_dir': str(native.parent), 'last_call_id': 'retained'}})
    discovery = {}
    retention.inventory(selection, versions[2] / 'Scripts/python.exe',
                        roots=[evidence], _discovery=discovery)
    native.unlink()
    result = retention.inventory(selection, versions[2] / 'Scripts/python.exe',
                                 roots=[evidence], _discovery=discovery)
    assert path.exists() and result['candidates'] == []
    assert any('native-manifest.json' in row['reason'] for row in result['gaps'])


def test_receipt_owned_fixture_needs_no_content_read_but_host_request_does(estate, monkeypatch):
    home, selection, versions, evidence = estate
    root = evidence / 'completed-tests'
    manifest = write(root / 'goal-fixture/manifest.json', ['arbitrary fixture content'])
    request = write(root / 'host/request.json', {'task_name': 'Foundry-pending', 'run_id': 'run',
                                               'python': str(versions[0] / 'Scripts/python.exe')})
    receipt = write(home / 'receipt.json', {'schema_version': 'pdcl-test-run-record-1',
        'run': {'command': {'argv': ['python', '-m', 'pytest', '--basetemp', str(root)]},
                'exit_code': 0, 'finished_utc': '2026-10-01T00:00:00Z'}})
    original = Path.read_bytes
    reads = []
    def read(path):
        reads.append(path)
        if path == manifest:
            raise OSError('fixture body is unnecessary for receipt-based ownership')
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', read)
    report = retention.inventory(selection, versions[2] / 'Scripts/python.exe',
                                 roots=[evidence], test_run_records=[receipt])
    assert not report['gaps']
    assert report['candidates'] == [str(versions[1])]
    assert manifest not in reads and request in reads
    assert report['classified_history'][0]['reason'] == 'pytest fixture'


def test_rotation_stops_at_total_deadline_and_keeps_current_rollback_and_task_hold(estate, monkeypatch):
    _, selection, versions, _ = estate
    clock = [0.0]
    monkeypatch.setattr(retention, 'perf_counter', lambda: clock[0])
    removed = []
    def remove(path, root, current, **kwargs):
        assert 0 < kwargs['timeout'] <= 120
        removed.append(path)
        shutil.rmtree(path)
        clock[0] = 601
        return {'exists': False}
    monkeypatch.setattr(retention, '_remove_release', remove)
    report = retention.prune(selection, versions[2] / 'Scripts/python.exe', keep_releases=[versions[1]])
    assert removed == [versions[0]] and report['errors']
    assert all(p.exists() for p in versions[1:])
    assert report['remaining'] == list(map(str, versions[1:]))


def test_rotation_plan_distinguishes_explicit_hold_from_historical_fixture(estate, monkeypatch):
    _, selection, versions, evidence = estate
    write(evidence/'old/manifest.json', {'runtime_provenance': {'python': str(versions[0]/'Scripts/python.exe')}})
    monkeypatch.setattr(retention, '_walk', lambda *a, **k: pytest.fail('archive walk'))
    report = retention.installed_inventory(selection, versions[2]/'Scripts/python.exe', keep_releases=[versions[1]])
    assert report['candidates'] == [str(versions[0])]
    assert report['scan']['directories'] == 0


def test_idle_task_hold_preserves_its_python_environment_dependency(estate):
    _, selection, versions, _ = estate
    pth = versions[1] / 'Lib/site-packages/shared.pth'
    pth.parent.mkdir(parents=True)
    pth.write_text(str(versions[0] / 'Lib/site-packages'))
    report = retention.installed_inventory(selection, versions[2]/'Scripts/python.exe', keep_releases=[versions[1]])
    assert not report['candidates']


def test_cli_history_audit_preserves_explicit_evidence_arguments(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(retention, 'inventory', lambda *a, **k: seen.append(k) or {})
    assert release_selection.main(['--selection', 'current.json', '--audit-history',
        '--rollback-python', 'old/python.exe', '--evidence-root', 'external']) == 0
    assert seen == [{'extra_roots': ['external']}]


def test_cli_rotates_all_unused_installed_versions(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(retention, 'prune', lambda *a, **k: seen.append((a, k)) or {})
    assert release_selection.main(['--selection', 'current.json', '--prune-unused',
        '--rollback-python', 'old/python.exe', '--keep-release', 'held']) == 0
    assert seen == [(('current.json', 'old/python.exe', None), {'keep_releases': ['held']})]


def test_relative_target_is_not_reported_remaining_after_deletion(estate, monkeypatch):
    home, selection, versions, _ = estate
    monkeypatch.chdir(home)
    def remove(path, *args, **kwargs):
        shutil.rmtree(path)
        return {'exists': False}
    monkeypatch.setattr(retention, '_remove_release', remove)
    report = retention.prune(selection, versions[2]/'Scripts/python.exe', ['releases/0.1.0a1'])
    assert report['deleted'] == [str(versions[0])] and report['remaining'] == []
