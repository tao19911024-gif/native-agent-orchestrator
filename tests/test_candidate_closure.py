from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from foundry_runtime import foundry_goal as owner
from foundry_runtime._candidate_closure import verify


def git(tree, *args):
    return subprocess.check_output(['git', '-C', str(tree), *args]).decode().strip()


@pytest.mark.parametrize('pin', [False, True])
def test_fresh_checkout_detects_raw_byte_eol_drift_and_cleans_only_fixture(tmp_path, pin):
    tree = tmp_path/'source'; tree.mkdir()
    git(tree, 'init', '-q')
    git(tree, 'config', 'user.email', 'fixture@example.invalid')
    git(tree, 'config', 'user.name', 'Fixture')
    git(tree, 'config', 'core.autocrlf', 'true')
    (tree/'artifact.json').write_bytes(b'{\n}\n')
    if pin:
        (tree/'.gitattributes').write_bytes(b'artifact.json text eol=lf\n')
    git(tree, 'add', '.')
    git(tree, 'commit', '-qm', 'candidate')
    head = git(tree, 'rev-parse', 'HEAD')
    before = (tree/'artifact.json').read_bytes()
    adapter = SimpleNamespace(candidate_closure_command=lambda checkout, source: [sys.executable,
        '-c', "import os; from pathlib import Path; assert Path(os.environ['TMP']).parent == Path.cwd().parent; assert Path('artifact.json').read_bytes() == b'{\\n}\\n'"])
    result = verify(owner, adapter, tree, head, tmp_path/'evidence', None)
    assert result['status'] == ('passed' if pin else 'failed')
    assert result['checkout_cleanup'] == 'removed'
    assert not Path(result['checkout']).exists()
    assert (tree/'artifact.json').read_bytes() == before
    assert git(tree, 'rev-parse', 'HEAD') == head
    assert Path(result['receipt']['path']).exists()


def test_adapter_failure_is_retained_not_evaluated(tmp_path):
    def broken(*args):
        raise RuntimeError('adapter unavailable')
    result=verify(owner, SimpleNamespace(candidate_closure_command=broken), tmp_path, 'candidate',tmp_path/'evidence',None)
    assert result['status']=='not_evaluated'
    assert 'adapter unavailable' in result['error']
    assert Path(result['receipt']['path']).is_file()


@pytest.fixture
def checkout_case(tmp_path):
    tree = tmp_path / 'source'; tree.mkdir()
    git(tree, 'init', '-q')
    git(tree, 'config', 'user.email', 'fixture@example.invalid')
    git(tree, 'config', 'user.name', 'Fixture')
    (tree / 'candidate.txt').write_text('candidate')
    git(tree, 'add', '.')
    git(tree, 'commit', '-qm', 'candidate')
    adapter = SimpleNamespace(candidate_closure_command=lambda checkout, source:
        [sys.executable, '-c', "from pathlib import Path; assert Path('candidate.txt').read_text() == 'candidate'"])
    return tree, git(tree, 'rev-parse', 'HEAD'), adapter


def test_checkout_taking_sixty_seconds_keeps_same_round(checkout_case, tmp_path, monkeypatch):
    from foundry_runtime import _candidate_closure as closure
    tree, head, adapter = checkout_case
    original = closure.OwnedProcess.spawn
    invocations = []
    def slow_checkout(argv, **kwargs):
        invocations.append(argv)
        if Path(argv[0]).stem == 'git':
            argv = [sys.executable, '-c',
                'import subprocess,sys,time; time.sleep(60); sys.exit(subprocess.call(sys.argv[1:]))', *argv]
        return original(argv, **kwargs)
    monkeypatch.setattr(closure.OwnedProcess, 'spawn', slow_checkout)
    before = git(tree, 'worktree', 'list', '--porcelain')
    result = verify(owner, adapter, tree, head, tmp_path / 'evidence', None)
    assert result['status'] == 'passed', result
    assert len(invocations) == 2 and Path(invocations[0][0]).stem == 'git'
    assert Path(invocations[0][0]).is_absolute()
    assert result['phases']['checkout']['elapsed_seconds'] >= 60
    assert all(row['cleanup']['cleanup_verified'] for row in result['phases'].values())
    assert result['checkout_cleanup'] == 'removed'
    assert git(tree, 'worktree', 'list', '--porcelain') == before
    assert len(list((tmp_path / 'evidence').glob('candidate-closure-*'))) == 1


def test_missing_commit_reports_git_error_without_running_checks(checkout_case, tmp_path):
    tree, _, adapter = checkout_case
    before = git(tree, 'worktree', 'list', '--porcelain')
    result = verify(owner, adapter, tree, '0' * 40, tmp_path / 'evidence', None)
    assert result['status'] == 'not_evaluated'
    assert result['error'].startswith('candidate checkout failed:')
    assert result['phases']['checkout']['returncode'] != 0
    assert result['phases']['checkout']['cleanup']['cleanup_verified']
    assert 'verification' not in result['phases']
    assert git(tree, 'worktree', 'list', '--porcelain') == before


def test_task_deadline_cleans_checkout_process_before_removal(checkout_case, tmp_path, monkeypatch):
    import time
    from foundry_runtime import _candidate_closure as closure, _task_deadline
    tree, head, adapter = checkout_case
    original = closure.OwnedProcess.spawn
    def sleeping_checkout(argv, **kwargs):
        assert Path(argv[0]).stem == 'git'
        return original([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs)
    monkeypatch.setattr(closure.OwnedProcess, 'spawn', sleeping_checkout)
    with _task_deadline.scope(time.time() + 2):
        result = verify(owner, adapter, tree, head, tmp_path / 'evidence', None)
    assert result['status'] == 'not_evaluated'
    assert 'task deadline' in result['error']
    assert result['phases']['checkout']['started']
    assert result['phases']['checkout']['cleanup']['cleanup_verified']
    assert result['checkout_cleanup'] == 'removed'
    assert 'verification' not in result['phases']
