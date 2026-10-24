"""Project-owned review/acceptance observations using the existing process owner.

Hooks read already produced project evidence; they do not run tests, commit or
publish. Requests and responses live in the existing run, never a new registry.
"""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import time
import uuid
from ._executor_lifecycle import OwnedProcess
from . import _task_deadline


def command_references(argv, workspace):
    """Retain readable executable/script identities, without guessing flag values."""
    references = []
    for value in argv:
        try:
            path = (Path(workspace) / value).resolve()
            if path.is_file():
                references.append({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        except (OSError, ValueError):
            continue  # Literal arguments are already retained in argv.
    return references


def observe(selected, operation, folder, *, task, subject, env):
    argv = selected.project["execution"].get(operation + "_argv")
    if not argv:
        return {"status": "ready", "criteria": list(selected.context.rule_sources), "findings": [], "checks": []} if operation == "review_context" else {
            "status": "not_evaluated", "reasons": ["Project-owned acceptance has not been observed."], "evidence": []}
    invocation = uuid.uuid4().hex
    folder = Path(folder) / (operation + "-" + invocation)
    folder.mkdir(parents=True)
    request = {"schema_version": "foundry-project-hook-1", "operation": operation,
               "project": selected.context.record(), "task": task,
               "workspace": str(selected.workspace), "subject": subject,
               "command": {"argv": argv, "references": command_references(argv, selected.workspace)}}
    request_path, response_path = folder / "request.json", folder / "response.json"
    request_path.write_text(json.dumps(request, ensure_ascii=True) + "\n", encoding="utf-8")
    process = None
    try:
        if not isinstance(argv, list) or any(not isinstance(x, str) or not x for x in argv):
            raise ValueError("project hook argv must be a nonempty argv list")
        with request_path.open("rb") as stdin, response_path.open("xb") as stdout, (folder / "stderr.txt").open("xb") as stderr:
            try:
                _task_deadline.check()
                process = OwnedProcess.spawn(argv, cwd=str(selected.workspace), stdin=stdin, stdout=stdout,
                    stderr=stderr, attempt_id=invocation, env=env)
                while (rc := process.poll()) is None:
                    _task_deadline.check()
                    time.sleep(.05)
            finally:
                if process is not None:
                    cleanup = process.stop_and_verify(drain_seconds=0, kill_seconds=2)
                    (folder / "cleanup.json").write_text(json.dumps(asdict(cleanup)), encoding="utf-8")
        if rc or not cleanup.cleanup_verified:
            raise ValueError("project observation did not complete cleanly")
        response = json.loads(response_path.read_bytes())
        if response.get("schema_version") != request["schema_version"] or response.get("subject") != subject:
            raise ValueError("project observation does not bind the requested subject")
        allowed = {"ready", "awaiting_project_action"} if operation == "review_context" else {"accepted", "not_met", "not_evaluated"}
        if response.get("status") not in allowed:
            raise ValueError("unknown project observation status")
        fields = ("criteria", "findings", "checks") if operation == "review_context" else ("reasons", "evidence")
        if any(not isinstance(response.get(key), list) for key in fields):
            raise ValueError("project observation is missing its evidence lists")
        for finding in response.get("findings", []):
            if (not isinstance(finding, dict) or not {"severity", "summary", "file", "line"} <= finding.keys()
                    or finding["severity"] not in {"P0", "P1", "P2", "P3"}):
                raise ValueError("invalid project review finding")
        result = response
    except Exception as exc:
        result = {"status": "not_evaluated", "error": str(exc), "source": "project_check"}
    path = folder / "observation.json"
    path.write_text(json.dumps(result, ensure_ascii=True, indent=2)+"\n", encoding="utf-8")
    return {**result, "observation": {"path": str(path.resolve()),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}
