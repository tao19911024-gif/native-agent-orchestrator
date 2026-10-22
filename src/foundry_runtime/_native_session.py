"""Native session identity and workspace binding; no project routing policy."""
from __future__ import annotations
import json
import os
import re
from pathlib import Path

def _session_root() -> Path:
    if "CODEX_HOME" in os.environ:
        value = os.environ["CODEX_HOME"]
        if not value.strip():
            raise ValueError("CODEX_HOME is empty; session store not found")
        return Path(value).expanduser() / "sessions"
    return Path.home() / ".codex" / "sessions"


SESSION_METADATA_BYTES = 1024 * 1024
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
def read_session_worktree(session_id: str, session_root=None) -> Path:
    """Bind an exact UUID to its first session_meta record; never infer from text.

Only YYYY/MM/DD rollout files in the selected account's sessions store are
considered. One bounded line is read; later transcript/instructions never own
the session's working directory. Missing or duplicate records fail closed.
"""
    if not isinstance(session_id, str) or not _UUID.fullmatch(session_id):
        raise ValueError("session id must be a canonical UUID")
    identity = session_id.lower()
    root = Path(session_root) if session_root is not None else _session_root()
    pattern = "[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/rollout-*-" + identity + ".jsonl"
    paths = list(root.glob(pattern)) if root.is_dir() else []
    if not paths:
        raise ValueError("exact session not found in selected session store")
    if len(paths) != 1:
        raise ValueError("ambiguous session: multiple exact UUID records")
    path = paths[0]
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        with path.open("rb") as stream:
            line = stream.readline(SESSION_METADATA_BYTES + 1)
        if not line or len(line) > SESSION_METADATA_BYTES:
            raise ValueError("session metadata is absent or exceeds 1 MiB")
        record = json.loads(line.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("session metadata cannot be read within selected store: " + str(exc)) from exc
    if (not isinstance(record, dict) or record.get("type") != "session_meta"
            or not isinstance(record.get("payload"), dict)):
        raise ValueError("first session metadata record is missing")
    payload = record["payload"]
    if payload.get("id") != identity:
        raise ValueError("session metadata identity does not match requested UUID")
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip() or "\0" in cwd or not Path(cwd).is_absolute():
        raise ValueError("session metadata cwd must be a nonempty absolute path")
    return Path(cwd).resolve()


def read_retained_session_header(log: str | Path) -> str:
    """Read the one identity-bearing native startup header without live metadata.

    Ordinary ``codex exec`` writes a text startup header rather than a protocol
    event.  Only the first complete header owns the session identity; later body
    text can quote arbitrary headers and UUIDs.
    """
    banner, separators, identities = False, 0, []
    with Path(log).open(encoding="utf-8", errors="replace") as stream:
        for raw in stream:
            line = raw.rstrip("\r\n")
            if re.fullmatch(r"OpenAI Codex v\S+", line):
                if banner:
                    break
                banner = True
            elif not banner:
                if line and not re.match(
                        r"\d{4}-\d{2}-\d{2}T\S+\s+(?:WARN|ERROR|INFO|DEBUG|TRACE)\s", line):
                    break
            elif line == "--------":
                separators += 1
                if separators == 2:
                    break
            elif separators != 1 or not re.fullmatch(r"[^:\s][^:]*:.*", line):
                break
            elif line.startswith("session id:"):
                identities.append(line.removeprefix("session id:").strip())
    if separators != 2 or len(identities) != 1:
        raise ValueError("exact dispatch startup header must name one session")
    return identities[0].lower()


def recorded_session(record, log=None):
    """Resolve the recorded provider identity for status and retained launch reuse."""
    session = (record.get('turn_binding') or {}).get('thread_id') or record.get('session_id')
    log = record.get('evidence', {}).get('log', {}).get('path') or log
    if not session and log and record.get('frozen', {}).get('launcher') in ('exec', 'exec-resume'):
        try:
            session = read_retained_session_header(log)
        except (OSError, ValueError):
            pass
    return session


def read_session_header(log: str | Path, worktree: str | Path, session_root=None) -> str:
    """Bind a retained startup identity to its live session metadata."""
    session = read_retained_session_header(log)
    bound = (read_session_worktree(session) if session_root is None
             else read_session_worktree(session, session_root))
    if bound.resolve() != Path(worktree).resolve():
        raise ValueError("dispatch session worktree differs")
    return session
