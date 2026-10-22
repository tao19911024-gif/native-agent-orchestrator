"""Project-neutral Git observations; project composition selects the reader."""
from dataclasses import dataclass
import re
import subprocess


@dataclass(frozen=True)
class GitObservation:
    returncode: int
    stdout: str
    stderr: str
    via: str


def git(args, worktree, timeout=60, *, fallback_timeout=None, env=None):
    """Native reader. No project-specific alternate host is selected here."""
    try:
        result = subprocess.run(["git", "-C", str(worktree), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return GitObservation(127, "", "native git could not run: %r" % (exc,), "native")
    return GitObservation(result.returncode, result.stdout or "", result.stderr or "", "native")


def head(worktree, *, repository_reader=git, env=None):
    result = repository_reader(["--no-optional-locks", "rev-parse", "HEAD"], str(worktree),
                               timeout=5, fallback_timeout=5, env=env)
    value = (result.stdout or "").strip()
    if result.returncode or not re.fullmatch(r"[0-9a-f]{40,64}", value):
        raise ValueError("cannot bind dispatch to a verified worktree HEAD")
    return value


def clean(worktree, *, repository_reader=git, env=None):
    # Only the executor-owned lease is excluded; all other changes remain visible.
    result = repository_reader(["--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all",
                                "--", ".", ":(exclude).pdcl-executor.lock"],
                               str(worktree), timeout=10, fallback_timeout=10, env=env)
    if result.returncode:
        raise ValueError("cannot establish clean recovery candidate worktree")
    return not (result.stdout or "").strip()


def snapshot(worktree, *, repository_reader=git, env=None):
    """Freeze unfinished Git content without staging it or claiming review."""
    import hashlib
    from pathlib import Path
    def read(*args):
        result = repository_reader(['--no-optional-locks', *args], str(worktree),
                                   timeout=60, fallback_timeout=60, env=env)
        if result.returncode:
            raise ValueError('cannot freeze recovery candidate: ' + result.stderr)
        return result.stdout
    paths = ('.', ':(exclude).pdcl-executor.lock')
    files = {}
    for name in read('ls-files', '--others', '--exclude-standard', '-z', '--', *paths).split('\0'):
        if name:
            with (Path(worktree) / name).open('rb') as stream:
                files[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    diff = ('diff', '--no-ext-diff', '--no-textconv', '--binary', '--full-index')
    changed = set(read('ls-files', '--modified', '--deleted', '-z', '--', *paths).split('\0'))
    changed.update(read(*diff, '--cached', '--name-only', '-z', '--', *paths).split('\0'))
    working_files = {}
    for name in sorted(changed - {''}):
        path = Path(worktree) / name
        if path.is_file():
            with path.open('rb') as stream:
                working_files[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
        else:
            working_files[name] = None
    digest = lambda text: hashlib.sha256(text.encode('utf-8')).hexdigest()
    return {'head': head(worktree, repository_reader=repository_reader, env=env),
            'index_sha256': digest(read(*diff, '--cached', '--', *paths)),
            'worktree_sha256': digest(read(*diff, '--', *paths)),
            'working_files': working_files, 'untracked': files}
