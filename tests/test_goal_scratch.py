import json
from pathlib import Path
import subprocess
import sys

import pytest

from foundry_runtime import _goal_scratch as scratch
from foundry_runtime import _foundry_dispatch as dispatch
from foundry_runtime import foundry_goal as goal
from test_installed_runtime import external, Analyst


def test_cli_registers_only_new_goal_option(external, monkeypatch):
    from foundry_runtime import cli, foundry_entry
    _, scope, order, _ = external
    monkeypatch.setattr(foundry_entry, '_analyst_executable', lambda _: Path(sys.executable))
    prepared = []
    monkeypatch.setattr(goal, 'run_prepared', lambda order, folder, value:
                        prepared.append(value) or {'status': 'project_handoff'})
    argv = ['--project-config', str(scope.path), '--order', str(order), '--foundry-goal',
            '--disposable-scratch']
    assert cli.main(argv) == 0
    assert prepared[0]['options']['disposable_scratch'] is True
    with pytest.raises(SystemExit) as error:
        cli.main([*argv, '--recover-goal', str(order.parent / 'old-goal')])
    assert error.value.code == 2
    assert len(prepared) == 1


@pytest.mark.parametrize('interrupted', [False, True])
def test_goal_reclaims_one_gib_after_review_and_preserves_report(external, tmp_path, monkeypatch, interrupted):
    _, scope, order, launches = external
    folder = tmp_path / 'goal'
    original = dispatch.coding_argv
    outputs = []
    def coding(*args, **kwargs):
        outputs.append(Path(args[args.index('-o') + 1]))
        argv = original(*args, **kwargs)
        argv[-1] += (
            f"; scratch=Path({str(folder / 'disposable')!r}); "
            "f=(scratch/'fixture.bin').open('wb'); block=b'x'*(1024*1024); "
            "[f.write(block) for _ in range(1024)]; f.close(); "
            f"Path({str(folder / 'report.txt')!r}).write_text('retained evidence'); "
            "import subprocess; Path('candidate.txt').write_text('done'); "
            "subprocess.run(['git','add','candidate.txt'],check=True); "
            "subprocess.run(['git','commit','-qm','candidate'],check=True); "
            "print('session id: fixture-session')")
        return argv
    class Review(Analyst):
        def __call__(self, request):
            assert (folder / 'disposable' / 'fixture.bin').stat().st_size == 1024**3
            assert (folder / 'report.txt').read_text() == 'retained evidence'
            return super().__call__(request)
    monkeypatch.setattr(dispatch, 'coding_argv', coding)
    monkeypatch.setattr(goal, 'NativeAnalyst', Review)
    if interrupted:
        class InterruptedReview(Review):
            def __call__(self, request):
                super().__call__(request)
                raise RuntimeError('fixture review interrupted')
        monkeypatch.setattr(goal, 'NativeAnalyst', InterruptedReview)
        with pytest.raises(RuntimeError, match='fixture review interrupted'):
            goal.run_goal(order, folder, Path(sys.executable), coding_launcher='exec',
                          project_record=scope.record(), disposable_scratch=True)
        assert (folder / 'disposable' / 'fixture.bin').stat().st_size == 1024**3
        assert json.loads((folder / 'usage.json').read_bytes())['scratch']['status'] == 'retained'
        monkeypatch.setattr(goal, 'NativeAnalyst', Review)
        outcome = goal.recover_goal(order, folder)
    else:
        outcome = goal.run_goal(order, folder, Path(sys.executable), coding_launcher='exec',
                               project_record=scope.record(), disposable_scratch=True)
    assert outcome['status'] == 'candidate_verified'
    assert not (folder / 'disposable').exists()
    assert (folder / 'report.txt').read_text() == 'retained evidence'
    usage = json.loads(goal.goal_usage._reference(outcome['task_usage']))
    assert usage['scratch']['bytes_reclaimed'] == 1024**3
    assert usage['scratch']['status'] == 'reclaimed'
    assert outcome['delivery_report']['usage_reference'] == outcome['task_usage']
    # The actual dispatcher prompt reaches the synthetic native process.
    assert len(outputs) == 1
    assert 'DISPOSABLE TASK STORAGE' in outputs[0].read_text(encoding='utf-8')
    before = (folder / 'usage.json').read_bytes()
    assert goal.recover_goal(order, folder)['status'] == 'candidate_verified'
    assert len(launches) == 1
    assert (folder / 'usage.json').read_bytes() == before


@pytest.mark.parametrize('status', ['review_blocked', 'coding_incomplete', 'awaiting_input', 'project_handoff'])
def test_unfinished_scratch_is_retained(tmp_path, status):
    manifest = {'run_id': 'new-goal'}
    scratch.register(tmp_path, manifest)
    file = tmp_path / 'disposable' / 'input.txt'; file.write_text('recovery input')
    result = scratch.reclaim(tmp_path, manifest, {'status': status})
    assert result['status'] == 'retained'
    assert file.read_text() == 'recovery input'


def test_historical_goal_does_not_adopt_existing_directory(tmp_path):
    root = tmp_path / 'disposable'; root.mkdir()
    (root / 'input.txt').write_text('history')
    assert scratch.reclaim(tmp_path, {'run_id': 'old'}, {'status': 'candidate_verified'}) is None
    with pytest.raises(FileExistsError):
        scratch.register(tmp_path, {'run_id': 'new'})
    assert (root / 'input.txt').read_text() == 'history'


def test_delivered_artifact_in_scratch_keeps_directory(tmp_path):
    manifest = {'run_id': 'new'}
    scratch.register(tmp_path, manifest)
    report = tmp_path / 'disposable' / 'report.txt'; report.write_text('report')
    result = scratch.reclaim(tmp_path, manifest,
        {'status': 'artifacts_delivered', 'artifacts': [{'path': str(report)}]})
    assert result['status'] == 'retained'
    assert report.read_text() == 'report'


def test_cleanup_failure_preserves_evidence_and_counts_only_removed_bytes(tmp_path, monkeypatch):
    manifest = {'run_id': 'new'}
    scratch.register(tmp_path, manifest)
    root = tmp_path / 'disposable'
    (root / 'locked').write_bytes(b'locked'); (root / 'free').write_bytes(b'free')
    original = Path.unlink
    def unlink(path, *args, **kwargs):
        if path.name == 'locked':
            raise PermissionError('fixture locked file')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', unlink)
    result = scratch.reclaim(tmp_path, manifest, {'status': 'candidate_verified'})
    assert result['status'] == 'partial'
    assert result['bytes_reclaimed'] == 4
    assert result['errors'] and (root / 'locked').read_bytes() == b'locked'
    monkeypatch.setattr(Path, 'unlink', original)
    final = scratch.reclaim(tmp_path, manifest, {'status': 'candidate_verified'}, result)
    assert final['status'] == 'reclaimed' and final['bytes_reclaimed'] == 10
    assert scratch.reclaim(tmp_path, manifest, {'status': 'candidate_verified'}, final) == final


def test_link_target_outside_goal_is_preserved(tmp_path):
    manifest = {'run_id': 'new'}
    folder = tmp_path / 'goal'; folder.mkdir()
    scratch.register(folder, manifest)
    outside = tmp_path / 'outside'; outside.mkdir(); (outside / 'keep').write_bytes(b'keep')
    link = folder / 'disposable' / 'linked'
    if sys.platform == 'win32':
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], check=True,
                       capture_output=True)
    else:
        link.symlink_to(outside, target_is_directory=True)
    result = scratch.reclaim(folder, manifest, {'status': 'candidate_verified'})
    assert result['status'] == 'reclaimed' and result['bytes_reclaimed'] == 0
    assert (outside / 'keep').read_bytes() == b'keep'
