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
from ._execution_profile import agy_policy
from ._launch_context import worker_environment
from .foundry_native_support import resolve_executable


def user_frame(body):
    return (json.dumps({"event": "user", "message": {"content": body}},
                       ensure_ascii=False) + "\n").encode("utf-8")


def command_guidance(settings):
    if settings == agy_policy():
        return "Trusted AGY coding session. Run the shell commands required by the work order, including dynamic arguments."
    commands = settings.get("allowed_commands", [])
    if commands:
        return ("Project-approved shell commands (launch.FOUNDRY_AGY_COMMANDS). Run each exact line "
                "as a separate command. Use native file tools for other reads and edits.\n" +
                "\n".join(commands))
    return ("Foundry has no project-approved shell commands for this worker. Use native file tools "
            "for reads and edits. For unattended shell checks, configure launch.FOUNDRY_AGY_COMMANDS "
            "in the project descriptor before starting a new assignment.")


def argv(model, session):
    args = [str(resolve_executable("agy")), "--input-format", "stream-json",
            "--output-format", "stream-json", "--mode", "accept-edits"]
    if model:
        args.extend(["--model", model])
    if session:
        args.extend(["--conversation", session])
    return args


class StartRefused(ValueError):
    """A sole native terminal explicitly reports that no worker turn began."""

    def __init__(self, message, reference):
        super().__init__(message)
        self.reference = reference


def start_refusal(result):
    error = result.get("error")
    usage = result.get("usage")
    if (result.get("status") == "ERROR" and result.get("conversation_id") == ""
            and type(result.get("num_turns")) is int and result["num_turns"] == 0 and result.get("response") == ""
            and isinstance(usage, dict) and usage.get("total_tokens") == 0
            and all(type(value) is int and value == 0 for value in usage.values())
            and isinstance(error, str) and error.startswith("failed to start conversation:")):
        return error


def read_result(path, session):
    digest = hashlib.sha256()
    init = result = reported_model = None
    events = 0
    with Path(path).open("rb") as stream:
        for line in stream:
            digest.update(line)
            if not line.strip():
                continue
            row = json.loads(line)
            events += 1
            if result is not None:
                raise ValueError("unexpected AGY event after terminal result")
            if row.get("event") == "init":
                if init is not None:
                    raise ValueError("duplicate AGY initialization")
                init = row["conversation_id"]
                reported_model = (row.get("init") or {}).get("model")
            elif row.get("event") == "result":
                result = row["result"]
    reference = {"path": str(Path(path).resolve()), "sha256": digest.hexdigest()}
    if (events == 1 and session is None and not row.get("conversation_id") and isinstance(result, dict)
            and (error := start_refusal(result))):
        raise StartRefused(error, reference)
    if not init or init != session or not result or result.get("conversation_id") != init:
        raise ValueError("AGY terminal lacks matching conversation evidence")
    return result, reported_model, reference


def verify_start_refusal(reference):
    try:
        read_result(reference["path"], None)
    except StartRefused as refusal:
        if refusal.reference == reference:
            return
    raise ValueError("AGY startup refusal differs from the recorded stream")


def coding_report(result):
    response = result.get("response") or ""
    if result.get("status") != "SUCCESS" and result.get("error"):
        response = "\n\n".join(filter(None, (response, str(result["error"]))))
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
        settings = (launch_context or {}).get("settings", {})
        if settings == agy_policy():
            args.append("--dangerously-skip-permissions")
        elif commands := settings.get("allowed_commands"):
            # Anchor each literal command; project entries are not provider rule expressions.
            rules = ["command(regex:^" + re.escape(command).replace(r"\ ", " ") + "$)" for command in commands]
            args.extend(["--settings", json.dumps({"permissions": {"allow": rules}})])
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("x", encoding="utf-8") as human, stream_path.open("xb") as stream:
            owns_log = True
            with tempfile.TemporaryFile() as prompt, stream_path.open("rb") as reader:
                prompt.write(user_frame(command_guidance(settings) + "\n\n" + body))
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
        try:
            result, reported_model, reference = read_result(stream_path, session)
        except StartRefused as exc:
            exc.process_identity = (lease.attempt_id, proc.pid, proc.creation_time)
            raise
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
