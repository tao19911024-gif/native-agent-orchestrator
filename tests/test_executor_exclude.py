"""Real Git/lease checks: runtime occupancy never becomes project content."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess

import pytest

from foundry_runtime._executor_lifecycle import ExecutorLease


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / ".gitignore").write_bytes(b"project-only\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    return root


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("content", [None, b"# preserve\r\nother\r\n", b"other", b".pdcl-executor.lock\r\nother\r\n"])
def test_lease_excluded_from_status_and_add(tmp_path, linked, content):
    root = repository(tmp_path)
    exclude = root / ".git" / "info" / "exclude"
    if content is None:
        exclude.unlink()
    else:
        exclude.write_bytes(content)
    tree = tmp_path / "linked" if linked else root
    if linked:
        git(root, "worktree", "add", "-q", "-b", "linked", str(tree))
    head = git(tree, "rev-parse", "HEAD")
    ignore = (tree / ".gitignore").read_bytes()
    before = content or b""
    expected = before if b".pdcl-executor.lock" in before.splitlines() else (
        before + (b"\n" if before and not before.endswith(b"\n") else b"") + b".pdcl-executor.lock\n")
    for _ in range(2):
        lease = ExecutorLease.acquire(tree, "FIXTURE", "exec")
        try:
            assert lease.path.exists()
            assert exclude.read_bytes() == expected
            assert git(tree, "status", "--porcelain", "--untracked-files=all") == ""
            git(tree, "add", "--all")
            assert git(tree, "diff", "--cached", "--name-only") == ""
            assert git(tree, "rev-parse", "HEAD") == head
            assert (tree / ".gitignore").read_bytes() == ignore
        finally:
            lease.path.unlink()  # No executor was started in this fixture.


def test_concurrent_linked_worktrees_append_once(tmp_path):
    root = repository(tmp_path)
    trees = [tmp_path / f"tree-{i}" for i in range(2)]
    for i, tree in enumerate(trees):
        git(root, "worktree", "add", "-q", "-b", f"branch-{i}", str(tree))
    exclude = root / ".git" / "info" / "exclude"
    exclude.write_bytes(b"# retained\n")
    with ThreadPoolExecutor(max_workers=2) as pool:
        leases = list(pool.map(lambda tree: ExecutorLease.acquire(tree, "FIXTURE", "exec"), trees))
    try:
        assert exclude.read_bytes() == b"# retained\n.pdcl-executor.lock\n"
        assert all(git(tree, "status", "--porcelain") == "" for tree in trees)
    finally:
        for lease in leases:
            lease.path.unlink()
