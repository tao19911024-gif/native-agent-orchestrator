"""Prepare the selected codegraph installation without editing global MCP config."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

TOOLS = ('codegraph_explore', 'codegraph_node')
import time


def _run(argv, workspace, env):
    result = subprocess.run(argv, cwd=workspace, env=env, capture_output=True, text=True, encoding="utf-8",
                            timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError(result.stderr[-1200:] or result.stdout[-1200:])
    return result.stdout


def verify(workspace, *, synced=False):
    """Check indexed bytes, including committed changes invisible to old status.

    Git status is recorded, not required clean. The graph represents working files,
    not necessarily HEAD. Coverage omissions remain explicit, never 'symbol absent'.
    """
    deadline = time.monotonic() + 30
    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise subprocess.TimeoutExpired('codegraph content/Git verification', 30)
        return seconds
    workspace = Path(workspace).resolve()
    with sqlite3.connect((workspace / ".codegraph/codegraph.db").as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute("select path, content_hash from files").fetchall()
    for name, expected in rows:
        remaining()
        path = workspace / name
        size = path.stat().st_size
        raw = (f"codegraph:oversize:{size}" if size > 1048576 else path.read_bytes().decode("utf-8", errors="replace")).encode()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("index differs from worktree: " + name)
    git = subprocess.run(["git", "-C", str(workspace), "status", "--porcelain=v1", "-z"],
                         capture_output=True, timeout=remaining(),
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if git.returncode:
        raise ValueError("cannot observe worktree Git state")
    tracked = subprocess.run(["git", "-C", str(workspace), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             capture_output=True, timeout=remaining(),
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if tracked.returncode:
        raise ValueError("cannot observe Git file inventory")
    indexed = {name for name, _ in rows}
    missing = sorted(set(tracked.stdout.decode("utf-8", errors="replace").rstrip("\0").split("\0")) - indexed - {""})
    # Keep the indexer's successful exclusion inventory in this verification
    # record. Unknown/new paths require sync; no duplicate extension registry.
    # Markdown is not indexed by the pinned 1.6.2 extractor.
    paths = tracked.stdout.decode('utf-8', errors='replace').rstrip('\0').split('\0')
    rules = {name: hashlib.sha256((workspace / name).read_bytes()).hexdigest()
             for name in paths if Path(name).name in ('.gitignore', '.codegraphignore', 'codegraph.json')}
    # Git owns both local and user-wide ignore locations (including worktrees).
    for label, args in (('git-info-exclude', ['rev-parse', '--git-path', 'info/exclude']),
                        ('git-global-exclude', ['config', '--path', '--get', 'core.excludesFile'])):
        result = subprocess.run(['git', '-C', str(workspace), *args], capture_output=True,
                                timeout=remaining(), creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode not in ((0, 1) if label == 'git-global-exclude' else (0,)):
            raise ValueError('cannot observe Git ignore configuration')
        name = result.stdout.decode('utf-8').strip()
        if not name and label == 'git-global-exclude':
            name = str(Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'git/ignore')
        path = Path(name)
        if not path.is_absolute():
            path = workspace / path
        rules[label] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None}
    inventory = hashlib.sha256(json.dumps({'missing': [name for name in missing
        if Path(name).suffix.lower() != '.md' and not name.startswith('.codegraph/')], 'rules': rules},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    verification = workspace / '.codegraph/foundry-verification.json'
    if synced:
        from .foundry_goal import _write
        _write(verification, {'version': '1.6.2', 'unindexed_paths_sha256': inventory})
    else:
        prior = json.loads(verification.read_bytes())
        if prior != {'version': '1.6.2', 'unindexed_paths_sha256': inventory}:
            raise ValueError('index file inventory changed; sync required')
    remaining()
    return {"unindexed_git_path_count": len(missing), "unindexed_git_path_sample": missing[:20], "indexed_files": len(rows), "git_status_sha256": hashlib.sha256(git.stdout).hexdigest(),
            "coverage": "indexed file contents verified; excluded, unsupported and oversize files require direct reads"}


def prepare(workspace, *, observation=None):
    observation = {} if observation is None else observation
    for key in ('preparation_error', 'verification', 'preparation'):
        observation.pop(key, None)
    observation.update(status='unavailable', fallback='scoped-direct-reads',
                       reason='workspace is not supplied')
    if workspace is None:
        return None
    try:
        command = json.loads(os.environ.get("FOUNDRY_CODEGRAPH_ARGV", "[]"))
        if not isinstance(command, list) or not command or not all(isinstance(v, str) for v in command):
            raise ValueError("set FOUNDRY_CODEGRAPH_ARGV to the pinned codegraph command array")
        root = Path(workspace).resolve()
        # Index-only child settings. Do not redirect native provider homes or login.
        state = os.environ.get("FOUNDRY_CODEGRAPH_STATE_DIR")
        if not state:
            raise ValueError("set FOUNDRY_CODEGRAPH_STATE_DIR to the approved D:/H: state directory")
        home = Path(state) / "foundry-codegraph" / hashlib.sha256(str(root).encode()).hexdigest()[:16]
        temp = home / "tmp"
        temp.mkdir(parents=True, exist_ok=True)
        overrides = {"USERPROFILE": str(home), "HOME": str(home), "TMP": str(temp), "TEMP": str(temp),
                     "CODEGRAPH_TELEMETRY": "0", "CODEGRAPH_NO_DAEMON": "0", "CODEGRAPH_MCP_TOOLS": ','.join(TOOLS)}
        env = {**os.environ, **overrides}
        version = _run([*command, "--version"], root, env).strip()
        if version != "1.6.2":
            raise ValueError("codegraph installation not qualified: " + version)
        operation = "sync" if (root / ".codegraph/codegraph.db").is_file() else "init"
        record = None
        if operation == 'sync':
            try:
                record = verify(root)
            except (OSError, ValueError, sqlite3.Error):
                pass  # Changed or unreadable indexed bytes require the existing writer.
        preparation = 'verified_reuse' if record is not None else operation
        if record is None:
            try:
                _run([*command, operation, str(root), *(["--yes"] if operation == "init" else [])], root, env)
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                # Another owner may have completed the same update. Recheck the
                # actual bytes once, without retrying sync or touching its lock.
                observation['preparation_error'] = str(exc)
                preparation = 'verified_after_writer_conflict'
                record = verify(root)
            else:
                record = verify(root, synced=True)
        observation.update(status='ready', reason=None, verification=record, preparation=preparation)
        print("Foundry code index: " + json.dumps({"version": version, **record}), file=sys.stderr)
        return {"command": command[0], "args": [*command[1:], "serve", "--mcp", "--path", str(root)], "env": overrides}
    except (OSError, ValueError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        observation['reason'] = str(exc)
        print("Foundry code index unavailable; use scoped direct reads: " + str(exc), file=sys.stderr)
        return None


def outside_index(targets, workspace, cwd=None):
    """Prove explicit targets cannot reach indexed files; unknown stays unknown."""
    if not targets or not workspace:
        return False
    root = Path(workspace).resolve()
    base = Path(cwd or root).resolve()
    try:
        paths = [(base / target).resolve() for target in targets]
        if any(root.is_relative_to(path) for path in paths):
            return False  # Ancestors include the indexed workspace.
        internal = [path for path in paths if path.is_relative_to(root)]
        if not internal:
            return True
        with sqlite3.connect((root / '.codegraph/codegraph.db').as_uri() + '?mode=ro', uri=True) as db:
            for path in internal:
                if not path.exists() or path == root:
                    return False
                name = path.relative_to(root).as_posix().lower()
                prefix = name.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '/%'
                if db.execute("select 1 from files where lower(path)=? or lower(path) like ? escape '!' limit 1",
                              (name, prefix)).fetchone():
                    return False
        return True
    except (OSError, ValueError, sqlite3.Error):
        return False
