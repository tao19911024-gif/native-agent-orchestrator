"""Antigravity headless coding transport under the dispatcher's existing lease."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

from . import _task_deadline
from ._launch_context import worker_environment
from .foundry_native_support import resolve_executable


def user_frame(body):
    return (json.dumps({"event": "user", "message": {"content": body}},
                       ensure_ascii=False) + "\n").encode("utf-8")


def argv(model, session):
    args = [str(resolve_executable("agy")), "--input-format", "stream-json",
            "--output-format", "stream-json", "--mode", "accept-edits"]
    if model:
        args.extend(["--model", model])
    if session:
        args.extend(["--conversation", session])
    return args


def read_result(path, session):
    digest = hashlib.sha256()
    init = result = reported_model = None
    with Path(path).open("rb") as stream:
        for line in stream:
            digest.update(line)
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("event") == "init":
                if init is not None:
                    raise ValueError("duplicate AGY initialization")
                init = row["conversation_id"]
                reported_model = (row.get("init") or {}).get("model")
            elif row.get("event") == "result":
                if result is not None or not init:
                    raise ValueError("unexpected AGY result")
                result = row["result"]
    if not init or init != session or not result or result.get("conversation_id") != init:
        raise ValueError("AGY terminal lacks matching conversation evidence")
    return result, reported_model, {"path": str(Path(path).resolve()), "sha256": digest.hexdigest()}


def coding_report(result):
    response = result.get("response") or ""
    denied = result.get("denied_actions") or []
    if denied:
        details = json.dumps(denied, ensure_ascii=False)
        return (response + "\n\n" if response else "") + "AGY denied actions: " + details
    return response


def verify_terminal(proof, *, session, returncode, attempt_id):
    if (proof.get("schema_version") != "foundry-agy-terminal-1" or
            (proof.get("session_id"), proof.get("attempt_id"), proof.get("returncode"))
            != (session, attempt_id, returncode)):
        raise ValueError("AGY terminal differs from recorded attempt")
    result, reported_model, reference = read_result(proof["stdout"]["path"], session)
    native_rc = proof["native_returncode"]
    if (reference != proof["stdout"] or reported_model != proof.get("reported_model") or type(native_rc) is not int or
            returncode != (native_rc or int(result["status"] != "SUCCESS"))):
        raise ValueError("AGY outcome differs from native stream")
    return result


def run(owner, body, worktree, log, out, tag, *, lease, on_started,
        model=None, resume_session=None, launch_context=None):
    stream_path = log.with_suffix(".agy.jsonl")
    terminal = log.with_suffix(".terminal.json")
    cleanup_path = log.with_suffix(".cleanup.json")
    lease.require_reserved(worktree, tag, {"agy"})
    proc = session = None
    owns_log = False
    try:
        if any(path.exists() for path in (out, stream_path, terminal, cleanup_path)):
            raise FileExistsError("AGY dispatch evidence already exists")
        args = argv(model, resume_session)
        commands = (launch_context or {}).get("settings", {}).get("allowed_commands", [])
        if commands:
            # Anchor each literal command; project entries are not provider rule expressions.
            rules = ["command(regex:^" + re.escape(command).replace(r"\ ", " ") + "$)" for command in commands]
            args.extend(["--settings", json.dumps({"permissions": {"allow": rules}})])
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("x", encoding="utf-8") as human, stream_path.open("xb") as stream:
            owns_log = True
            with tempfile.TemporaryFile() as prompt, stream_path.open("rb") as reader:
                prompt.write(user_frame(body))
                prompt.seek(0)
                _task_deadline.check()
                try:
                    proc = owner.OwnedProcess.spawn(args, cwd=worktree, stdin=prompt,
                        stdout=stream, stderr=human, attempt_id=lease.attempt_id,
                        cleanup_path=cleanup_path, env=worker_environment(os.environ, launch_context))
                except owner.SpawnFailure as exc:
                    proc = exc.process
                    raise
                lease.bind_process(proc.pid, proc.creation_time)
                while True:
                    native_rc = proc.poll()
                    if session is None:
                        while True:
                            start = reader.tell()
                            line = reader.readline()
                            if not line.endswith(b"\n") and native_rc is None:
                                reader.seek(start)
                                break
                            if not line:
                                break
                            if not line.strip():
                                continue
                            row = json.loads(line)
                            if row.get("event") == "init":
                                session = row["conversation_id"]
                                if not isinstance(session, str) or not session or (resume_session and session != resume_session):
                                    raise ValueError("AGY conversation identity differs")
                                on_started(session)
                                break
                    if native_rc is not None:
                        break
                    _task_deadline.check()
                    time.sleep(0.2)
        result, reported_model, reference = read_result(stream_path, session)
        rc = native_rc or int(result["status"] != "SUCCESS")
        proof = {"schema_version": "foundry-agy-terminal-1", "session_id": session,
                 "attempt_id": lease.attempt_id, "executable": args[0],
                 "native_returncode": native_rc, "returncode": rc, "stdout": reference,
                 "reported_model": reported_model}
        terminal.write_text(json.dumps(proof, indent=2), encoding="utf-8")
        out.write_text(coding_report(result), encoding="utf-8")
        return rc
    finally:
        cleanup = proc.stop_and_verify(drain_seconds=0, kill_seconds=5) if proc else owner._not_started(lease)
        if owns_log:
            owner._record_cleanup(lease, cleanup, cleanup_path)
        elif not lease.release(cleanup):
            raise RuntimeError("executor cleanup is unknown; reservation retained")
