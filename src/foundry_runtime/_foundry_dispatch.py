#!/usr/bin/env python3
"""Shared dispatch/resume lifecycle under an explicitly selected project policy.

The project entry owns admission and route choices. This owner retains task/session
identity, journals, native process lifecycle and evidence; it does not accept or
integrate the project's delivery. Steer reuses the active app-server connection.
The recorded worktree lease makes occupancy observable even when a resumed native
command carries no working-directory argument. Preserve those external records and
original sessions across entry and package changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from . import app_server_transport
from ._dispatch_capacity import recover_capacity
from . import _task_deadline as deadline

# Shared native process custody; no alternative coding entrypoint.
from ._executor_lifecycle import CleanupReceipt, ExecutorLease, OwnedProcess, SpawnFailure
from ._compute_selection import compute_kwargs
from ._codex_command import coding_argv, codex_env, resolve_codex
from ._execution_profile import coding_policy, claude_policy
from ._compute_selection import team_kwargs, team_prompt
from ._execution_intent import ExecutionIntent
from ._recovery_authorization import RecoveryAuthorization
from . import result_receipt
from ._app_server_policy import normalize_policy
from ._task_identity import normalized_line

LOCK_NAME = ".pdcl-executor.lock"

def _resumed_index(path) -> int:
    """The N in ...resumedN.<ext>, or 0 when the name carries none."""
    m = re.search(r"resumed(\d+)\.", path.name)
    return int(m.group(1)) if m else 0

def _codex() -> str:
    try:
        return resolve_codex()
    except (OSError, ValueError) as exc:
        die("REFUSE dispatch: " + str(exc))


WORKTREE_RE = re.compile(r"^WORKTREE\s+(\S+)\s*$", re.M)
SECTION_RE = re.compile(
    r"^##\s+WORKTREE\s*$\s*(?:.*?)([A-Za-z]:[\\/][^\s`'\"]+|/[a-zA-Z]/[^\s`'\"]+)", re.M | re.S)



def _utf8_env() -> dict:
    """Child gates print UTF-8, and this parent decodes UTF-8.

    2026-09-08: four subprocess.run calls here used the Windows locale codec (cp1252) for the
    child's pipe. A resume whose amendment was written in Chinese therefore died with
    UnicodeDecodeError inside subprocess, and dispatch reported it as `REFUSE resume: a check is
    red` -- a correct amendment refused by a decode bug wearing a gate's clothes.
    """
    import os as _os

    return {**{key: value for key, value in _os.environ.items() if not key.upper().startswith("GIT_")},
            "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}

def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(msg)
    refusal = SystemExit(1)
    refusal.dispatch_reason = msg
    raise refusal


def _not_started(lease: ExecutorLease) -> CleanupReceipt:
    return CleanupReceipt(lease.attempt_id, None, None, True, 0, True,
                          "no native child was created")


class DispatchCleanupError(RuntimeError):
    """Unknown cleanup retains reachable native custody for reconciliation."""
    def __init__(self, message, process, cleanup):
        super().__init__(message)
        self.process, self.cleanup = process, cleanup


def _record_cleanup(lease: ExecutorLease, cleanup: CleanupReceipt, path: Path) -> None:
    # Preserve an old receipt and retain occupancy if the new result cannot be
    # durably recorded. A failed evidence write must never look like success.
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(asdict(cleanup), stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        lease.retain_unknown("cleanup measured but receipt persistence failed")
        raise
    if not lease.release(cleanup):
        lease.retain_unknown("cleanup or reservation identity is unverified")
        raise RuntimeError("executor cleanup is unknown; reservation retained: " + str(path))


def run_codex(argv: list[str], stdin_text: str | None, worktree: Path,
              log: Path, tag: str, *, timeout_seconds: float = 1800,
              lease: ExecutorLease | None = None, env: dict | None = None,
              on_started=None) -> int:
    import math
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 1800):
        raise ValueError("executor timeout must be positive and at most 1800 seconds")
    lease = lease or ExecutorLease.acquire(worktree, tag, "exec")
    lease.require_reserved(worktree, tag, {"exec", "exec-resume"})
    proc = None
    receipt_path = log.with_suffix(".cleanup.json")
    owns_log = False
    try:
        if log.exists() or receipt_path.exists():
            raise FileExistsError("dispatch evidence already exists: " + str(log))
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("x", encoding="utf-8") as output:
            owns_log = True
            # File-backed stdin makes the execution deadline independent of
            # whether the child ever reads the prompt. The file is task-owned.
            with tempfile.TemporaryFile(mode="w+b") as prompt:
                if stdin_text is not None:
                    prompt.write(stdin_text.encode("utf-8"))
                prompt.seek(0)
                try:
                    deadline.check()
                    proc = OwnedProcess.spawn(argv, cwd=worktree, stdin=prompt,
                                              stdout=output, stderr=subprocess.STDOUT,
                                              cleanup_path=receipt_path,
                                              attempt_id=lease.attempt_id, env=env if env is not None else codex_env(argv[0]))
                except SpawnFailure as exc:
                    proc = exc.process
                    raise
                lease.bind_process(proc.pid, proc.creation_time)
                if on_started is not None:
                    on_started()
                left = deadline.remaining()
                return proc.wait(timeout=min(timeout_seconds, left) if left is not None else timeout_seconds)
    finally:
        cleanup = (proc.stop_and_verify(drain_seconds=0, kill_seconds=5)
                   if proc is not None else _not_started(lease))
        if owns_log:
            try:
                _record_cleanup(lease, cleanup, receipt_path)
            except Exception as exc:
                raise DispatchCleanupError("executor cleanup: " + str(exc), proc, cleanup) from exc
        elif not lease.release(cleanup):
            raise RuntimeError("executor cleanup is unknown; reservation retained")


def run_app_server(codex: str, stdin_text: str, worktree: Path,
                   log: Path, out: Path, tag: str, *, timeout_seconds: float | None = None,
                   app_server_policy=None,
                   lease: ExecutorLease | None = None, on_started=None, on_turn=None,
                   resume_thread_id=None, model=None, effort=None, native_team=None, execution_policy=None, launch_context=None) -> int:
    """Keep the public seam while the extracted owner handles long-session custody."""
    from . import _dispatch_resume
    return _dispatch_resume.run_app_server(
        sys.modules[__name__], codex, stdin_text, worktree, log, out, tag,
        timeout_seconds=timeout_seconds, app_server_policy=app_server_policy,
        lease=lease, on_started=on_started, on_turn=on_turn,
        launch_context=launch_context,
        resume_thread_id=resume_thread_id, model=model, effort=effort, **team_kwargs(native_team),
        **({"execution_policy": execution_policy} if execution_policy is not None else {}))


def report(log: Path, *, project=None, tag=None, launcher="exec") -> None:
    try:
        txt = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    m = re.search(r"(?:session id:|^Claude session:)\s*([0-9a-f-]{8,})", txt, re.M)
    print()
    if m:
        print("SESSION ID: %s" % m.group(1))
        print("  Resume it, never re-dispatch:")
        if project is not None:
            from ._command_display import command
            from ._project_status import resume_argv
            print("    " + command(resume_argv(project.path, tag, m.group(1), {"launcher": launcher}, sys.executable)))
        else:
            print("    " + _project_adapter(None).resume_hint(m.group(1)))
    else:
        print("NO SESSION ID FOUND in %s — the run may not have started." % log)


_UPSTREAM_ERROR_LINE = re.compile(r"(?m)^ERROR: .*$")
_EXIT_TAIL_BYTES = 4096

# codex writes each assistant turn as a bare line `codex` and each tool call as a bare
# line `exec`. Nothing else in a log sits alone on a line with those exact bytes.
_ACTIVITY_MARKERS = ("codex", "exec")


def executor_activity(log: Path) -> tuple[int, int]:
    """(assistant turns, tool calls) over the WHOLE log -- never a tail.

    2026-09-10: TELL1 hit an upstream quota limit 19 minutes IN. It left no report, so the
    two conditions below (an ERROR line, no report) both held and the diagnostic announced
    the run had never reached the executor. It had: three turns, ten tool calls, and a
    correction to the order's own seam map that would have been discarded by a resume "with
    the SAME prompt". A tail cannot see this; the markers are at the top of the file.
    """
    turns = calls = 0
    try:
        with log.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped == "codex":
                    turns += 1
                elif stripped == "exec":
                    calls += 1
    except OSError:
        return (0, 0)
    return (turns, calls)


def explain_nonzero_exit(rc: int, log: Path, out: Path) -> None:
    """Say WHO ended the run, because rc=1 on its own has three unrelated causes.

    2026-09-05: an upstream capacity outage and an executor's own CONTRADICTION stop arrive
    here identically as rc=1. The outage cost this commander a 2.4 MB log tail to identify,
    twice in one morning, and the reflex on an unexplained rc=1 is to rewrite the order --
    which cannot possibly help when the order never reached a model at all. REVOKE1 resume 5
    was cut mid base leg and resume 6 was refused before it started; both looked exactly like
    a bad amendment from the terminal.

    This invents no taxonomy. It reports the upstream's OWN final ERROR lines verbatim.

    2026-09-10: those two conditions -- an ERROR line and no report -- were NOT enough, and
    this function said something false because of it. TELL1 ran for 19 minutes, took three
    turns, made ten tool calls and corrected the order's seam map, then hit a quota wall.
    No report, an ERROR line: it announced the run had never reached the executor and told
    the commander to resume with the same prompt. A run that never started and a run cut
    mid-work are opposite situations with opposite repairs, and only the log can tell them
    apart -- so the third condition is now MEASURED (executor_activity), not assumed.
    """
    if rc == 0:
        return
    try:
        with log.open("rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - _EXIT_TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return
    errors = list(dict.fromkeys(line.strip() for line in _UPSTREAM_ERROR_LINE.findall(tail)))
    delivered = out.is_file() and out.stat().st_size > 0
    turns, calls = executor_activity(log)
    print()
    if errors and not delivered and turns == 0:
        print("!! THE RUN NEVER REACHED YOUR EXECUTOR -- not a refusal, not a stop.")
        print("   No report was written, NO executor turn is in the log, and its last %d bytes"
              % _EXIT_TAIL_BYTES)
        print("   carry the upstream's own error:")
        for line in errors:
            print("     %s" % line)
        print("   YOUR ORDER IS NOT AT FAULT. Resume the SAME session with the SAME prompt.")
    elif errors and not delivered:
        print("!! THE RUN WAS CUT MID-WORK BY THE UPSTREAM -- not a refusal, not a stop,")
        print("   and NOT a run that never started. The log carries %d executor turn(s) and"
              % turns)
        print("   %d tool call(s) before the upstream's own error:" % calls)
        for line in errors:
            print("     %s" % line)
        print("   YOUR ORDER IS NOT AT FAULT, AND NEITHER IS IT UNREAD.")
        print("   DO NOT resume with the same prompt until you have read what it already did:")
        print("     grep -n -x -E 'codex|exec' %s" % log)
        print("   Work it established -- measurements, corrections to your seam map, dead ends --")
        print("   is in that log and nowhere else. Resuming blind re-pays for it and may discard it.")
    elif delivered:
        print("!! rc=%d WITH a report on disk: the executor ran and ended on its own judgement." % rc)
        print("   Read %s before you change one word of the order." % out)
    else:
        print("!! rc=%d, no report, and no upstream ERROR line in the last %d bytes."
              % (rc, _EXIT_TAIL_BYTES))
        print("   Read the tail of %s yourself. Do not assume the order is at fault." % log)


class SteerRefused(RuntimeError):
    def __init__(self, condition: str, message: str):
        super().__init__(message)
        self.condition = condition


def _executor_locks(worktree_parent: Path):
    for lock in sorted(worktree_parent.glob("wt_*/" + LOCK_NAME)):
        try:
            payload = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            yield payload


def resolve_steer_target(tag: str, evidence_root: Path | None = None,
                         worktree_parent: Path | None = None) -> dict:
    """Resolve a tag to its live app-server handle without guessing a thread."""
    if evidence_root is None or worktree_parent is None:
        project_evidence, project_parent = _project_adapter(None).steer_roots()
        evidence_root = evidence_root or project_evidence
        worktree_parent = worktree_parent or project_parent
    handle_path = evidence_root / tag / "active_turn.json"
    if handle_path.is_file():
        try:
            handle = json.loads(handle_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            handle = {}
        if isinstance(handle, dict) and handle.get("launcher") == "app-server":
            try:
                held = json.loads((Path(handle["worktree"]) / LOCK_NAME).read_text(encoding="utf-8"))
                fields = ("attempt_id", "root_pid", "root_creation_time", "owner_pid", "owner_creation_time")
                matching = (handle.get("schema_version") == "pdcl-active-turn-2"
                            and held.get("schema_version") == "pdcl-executor-lock-2"
                            and held.get("state") == "running"
                            and held.get("tag") == handle.get("tag") == tag
                            and held.get("worktree") == str(Path(handle["worktree"]).resolve())
                            and all(handle.get(key) is not None and held.get(key) == handle[key] for key in fields))
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                matching = False
            if matching:
                return handle
            raise SteerRefused("stale attempt identity", "REFUSE steer: active handle does not match the current executor reservation.")
    locks = [row for row in _executor_locks(worktree_parent) if row.get("tag") == tag]
    if any(row.get("launcher") != "app-server" for row in locks):
        raise SteerRefused(
            "wrong launcher",
            "REFUSE steer: wrong launcher for %s; the running executor uses codex exec." % tag,
        )
    raise SteerRefused(
        "no active turn", "REFUSE steer: no active turn for %s." % tag
    )


def deliver_steer(tag: str, prompt: str, *, handle: dict | None = None,
                  gate=None, timeout: float = 10, answer_request=None) -> dict:
    """Gate one correction, then deliver it to exactly the visible active turn."""
    active = handle or resolve_steer_target(tag)
    worktree = str(active.get("worktree") or "")
    if gate is None:
        _project_adapter(None).steer_admission(prompt, tag, worktree)
    else:
        gate(prompt, tag, worktree)
    try:
        response = app_server_transport.send_correction(active, prompt, timeout,
            **({'answer_request': answer_request} if answer_request is not None else {}))
    except (OSError, TimeoutError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise SteerRefused(
            "delivery unconfirmed",
            "UNCONFIRMED steer: delivery unconfirmed for %s; %s. The input may have been admitted; "
            "inspect native input/result evidence before resending. This does not establish that the active turn is absent."
            % (tag, exc),
        ) from exc
    if not response.get("ok"):
        condition = str(response.get("condition") or "stale turn id")
        message = str(response.get("message") or "active turn changed before delivery")
        raise SteerRefused(
            condition,
            "%s steer: %s for %s; %s" % (
                "UNCONFIRMED" if condition == "delivery unconfirmed" else "REFUSE", condition, tag, message),
        )
    return response


def _repository_options(project):
    return {"repository_reader": project.repository_reader} if project is not None else {}


def _worktree_head(worktree: Path, *, repository_reader=None) -> str:
    from ._repository_observation import head
    reader = repository_reader if repository_reader is not None else _project_adapter(None).repository_reader
    return head(worktree, repository_reader=reader, env=_utf8_env())


def _worktree_clean(worktree: Path, *, repository_reader=None) -> bool:
    from ._repository_observation import clean
    reader = repository_reader if repository_reader is not None else _project_adapter(None).repository_reader
    return clean(worktree, repository_reader=reader, env=_utf8_env())


def _dispatch_inputs(order: Path, *, project=None):
    """Freeze the task bytes and target checkout that the executor receives."""
    def task_bytes(path):
        raw = path.read_bytes()
        raw.decode("utf-8-sig")
        return raw
    source = task_bytes(order)
    wt = (project.workspace if project is not None else
          Path(_project_adapter(None).goal_authority().resolve_task(normalized_line(order)).worktree)).resolve(strict=True)
    return wt, source, _worktree_head(wt, **_repository_options(project))


def _intent_bindings(authority, wt, source, head, *, operation, launcher,
                     session=None, model=None, effort=None, app_server_policy=None, native_team=None,
                     execution_decision=None, continuation_sha256=None):
    sha = lambda value: hashlib.sha256(value).hexdigest()
    return {"operation": operation, "repository_id": str(authority.common_dir.resolve()),
            **({"project_context": {"descriptor": str(authority.path), "project_id": authority.project["project_id"],
                                     "evidence_root": authority.project["evidence_root"]}}
               if getattr(authority, "project", None) is not None else {}),
            **({"project_preflight_input": authority.preflight_input}
               if getattr(authority, "preflight_input", None) else {}),
            "authority_sha256": authority.sha256, "order_sha256": sha(source),
            "task_sha256": sha(source),
            "base_commit": head, "worktree": str(wt.resolve(strict=True)),
            "authorization_sha256": sha(source + b"\0" + authority.sha256.encode()),
            "session_id": session, "launcher": launcher, "execution_policy": (
                authority.execution_profile("claude" if launcher == "claude" else "codex")
                if hasattr(authority, "execution_profile") else claude_policy() if launcher == "claude" else coding_policy()),
            **({"execution_decision": execution_decision} if execution_decision is not None else {}),
            **({"continuation_sha256": continuation_sha256} if continuation_sha256 is not None else {}),
            **({'app_server_policy': app_server_policy} if app_server_policy is not None else {}),
            **({'task_deadline_at': deadline.current()} if deadline.current() is not None else {}),
            **({"requested_compute": {**compute_kwargs(model, effort, backend="claude" if launcher == "claude" else "codex"), **team_kwargs(native_team)}}
               if model is not None or effort is not None or native_team is not None else {})}


# Compatibility names keep existing imports and test overrides on the public dispatcher.
from ._dispatch_package import (record_package as _record_package, PackageLaunch,
                               admitted_binding, observed_start)  # noqa: F401 - extracted owner seam


class ExecutionResult(tuple):
    """Keep rc/log/out unpacking while carrying the already-verified interpretation."""

    def __new__(cls, values, *, source, outcome, replayed=False, intent_id=None):
        result = super().__new__(cls, values)
        result.returncode_source, result.outcome = source, outcome
        result.replayed, result.intent_id = replayed, intent_id
        return result


def _intent_outcome(intent, *, replayed=False):
    row = intent.record
    log = Path(row["evidence"]["log"]["path"])
    result = row["evidence"]["result"]
    out = Path(result["path"]) if result else log.with_name(
        log.name.replace("dispatch", "last_message").replace(".log", ".md"))
    return ExecutionResult((row["returncode"], log, out),
        source=row.get('returncode_source', 'native_exit'), outcome=row['outcome'],
        replayed=replayed, intent_id=row['intent_id'])


def _finish_intent(intent, log, out, rc):
    receipt_path = log.with_suffix(".cleanup.json")
    cleanup = CleanupReceipt(**json.loads(receipt_path.read_bytes()))
    def reference(path):
        return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    # The habitual last_message.md is deliberately superseded by resume. Bind
    # replay to an immutable copy, so that normal resume never invalidates it.
    result = None
    if out.is_file():
        retained = log.with_suffix(".result.md")
        with retained.open("xb") as stream:
            stream.write(out.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        result = reference(retained)
    evidence = {"log": reference(log), "result": result, "cleanup": reference(receipt_path)}
    if intent.record.get('launcher') in {'app-server', 'claude'}:
        evidence['terminal'] = reference(log.with_suffix('.terminal.json'))
    intent.finish(cleanup, returncode=rc, evidence=evidence)
    # WorkOrder v2, Phase 3. One call, and the module holds every rule: it declares only
    # what this journal proves and records the task verdict as `not_evaluated`, because a
    # CLAUSE 0 stop exits 0 and `process_completed` has never meant the work was done.
    # It cannot raise; a receipt must not be able to fail the dispatch it records.
    result_receipt.write(intent.record, log, report_ref=str(out) if out.is_file() else None)
    return _intent_outcome(intent)


def _failed_intent(intent, log):
    # A root may have started even if the handoff or its callback raised.
    # Only a measured matching no-child receipt allows retry.
    if intent.state not in {"starting", "active"}:
        return
    cleanup = None
    try:
        cleanup = CleanupReceipt(**json.loads(log.with_suffix(".cleanup.json").read_bytes()))
    except (OSError, ValueError, TypeError):
        pass
    if intent.state == "starting" and cleanup is not None and cleanup.root_pid is None:
        intent.not_started(cleanup)
    else:
        intent.unknown("launcher did not durably complete; reconcile before another execution", cleanup)


def _project_adapter(project):
    """Use explicit project or invocation policy supplied by the project entry."""
    if project is not None:
        return project
    from ._project_context import invocation_adapter
    selected = invocation_adapter()
    if selected is not None:
        return selected
    raise ValueError("select a project descriptor or bind the project adapter before execution")


@recover_capacity
def dispatch_order(order: Path, *, tag: str = "", launcher: str = "exec",
                   execution_intent_id=None, model=None, effort=None,
                   app_server_policy=None, predispatch_receipt=None, native_team=None, execution_decision=None,
                   continuation_prompt=None, project=None, launch_context=None):
    """Serialize canonical LINE, gate frozen authority, then own one native root."""
    selection = {**compute_kwargs(model, effort, backend="claude" if launcher == "claude" else "codex"), **team_kwargs(native_team)}
    if launcher not in {"exec", "app-server", "claude"}:
        raise ValueError("invalid dispatch launcher")
    if app_server_policy is not None and launcher != 'app-server':
        raise ValueError('app-server policy applies only to app-server order dispatch')
    policy = normalize_policy(app_server_policy, capacity_defaults=True) if launcher == 'app-server' else None
    line = normalized_line(order)
    if tag and normalized_line(tag) != line:
        raise ValueError("tag cannot rename the order LINE")
    tag = line
    project_args = {"project": project} if project is not None else {}
    frozen = _dispatch_inputs(order, **project_args)
    wt, source, head = frozen
    continuation_sha256 = (hashlib.sha256(continuation_prompt.encode("utf-8")).hexdigest()
                           if continuation_prompt is not None else None)
    authority = project if project is not None else _project_adapter(None).goal_authority()
    adapter = _project_adapter(project)
    authority.require_task(tag, wt, mode="resume" if continuation_prompt is not None else "dispatch")
    binding = _intent_bindings(authority, wt, source, head,
                               operation="dispatch", launcher=launcher,
                               execution_decision=execution_decision,
                               continuation_sha256=continuation_sha256,
                               app_server_policy=policy,
                               **selection)
    if launch_context is not None:
        binding['launch_context'] = launch_context
    binding = admitted_binding(authority.common_dir, tag, binding)
    with ExecutionIntent.open(authority.common_dir, tag, binding,
                              intent_id=execution_intent_id,
                              source_verifier=adapter.recovery_source_verifier) as intent, observed_start(intent), deadline.scope(intent.frozen.get('task_deadline_at')):
        if intent.state == "completed":
            return _intent_outcome(intent, replayed=True)
        deadline.check()
        adapter.preflight(tag, source.decode("utf-8-sig"), operation="dispatch", env=_utf8_env(),
                          execution_intent_id=intent.intent_id, order=order, worktree=wt,
                          launcher=launcher, predispatch_receipt=predispatch_receipt)
        authority.verify_unchanged()
        if _dispatch_inputs(order, **project_args) != frozen:
            raise ValueError("dispatch inputs changed during gates; re-admission required")
        lease = intent.acquire_lease(wt, tag, launcher)
        handed_off = False
        stem = ("dispatch_continuation_" + continuation_sha256[:16]
                if continuation_sha256 is not None else "dispatch")
        folder = adapter.evidence(tag)
        log = folder / (stem + ".log")
        try:
            authority.verify_unchanged()
            if _dispatch_inputs(order, **project_args) != frozen:
                raise ValueError("dispatch inputs changed before launch; re-admission required")
            body = adapter.execution_plan_prompt(source.decode("utf-8-sig"), execution_decision)
            body = team_prompt(adapter.prompt(body), native_team)
            if continuation_prompt is not None:
                body += "\n\n" + continuation_prompt
            out = log.parent / (log.stem.replace("dispatch", "last_message", 1) + ".md")
            prior = intent.record.get("cleanup")
            if prior is not None and intent.state == "not_started":
                # Only the durable same-intent no-child transition grants this
                # retry path. A failed/unknown root still requires reconciliation.
                if (prior.get("root_pid") is not None or prior.get("cleanup_verified") is not True
                        or prior.get("attempt_id") != intent.record.get("attempt_id")):
                    raise RuntimeError("prior no-child receipt is inconsistent")
                attempts = [int(m[1]) for p in log.parent.glob(stem + "_retry*.log")
                            if (m := re.fullmatch(re.escape(stem) + r"_retry(\d+)\.log", p.name))]
                number = max([0, *attempts]) + 1
                log = log.with_name(f"{stem}_retry{number}.log")
                out = out.with_name(f"{stem.replace('dispatch', 'last_message', 1)}_retry{number}.md")
            if any(path.exists() for path in (out, log, log.with_suffix(".cleanup.json"))):
                raise FileExistsError("prior dispatch evidence exists; use its original session")
            from ._launch_context import capture, bind_coding_model
            launch_context = intent.frozen.get('launch_context')
            if launch_context is None:
                launch_context = capture(wt, root_role=(selection.get('native_team') or {}).get('lead_role', 'executor'),
                                         backend='claude' if launcher == 'claude' else 'codex', coding_model=selection.get('model'))
            bind_coding_model(launch_context, selection.get('model'))
            if launcher != "claude":
                codex = _codex()
            if launcher == "exec":
                argv = coding_argv(codex, "exec", "-C", str(wt), "-o", str(out), "-", **selection, execution_policy=intent.frozen["execution_policy"], launch_context=launch_context)
            package_launch = PackageLaunch(intent, log, lambda: _record_package(
                log, tag, "dispatch", intent, body, launch_context))
            package_launch.start(lease)
            print("=== dispatching %s into %s; intent %s ===" % (tag, wt, intent.intent_id))
            print("    order  : %d bytes -- %s"
                  % (len(body.encode("utf-8")), log.with_suffix(".package.json").name))
            handed_off = True
            if launcher == "claude":
                from . import _dispatch_claude
                rc = _dispatch_claude.run(sys.modules[__name__], body, wt, log, out, tag, lease=lease,
                    on_started=lambda session: package_launch.active(lease, session), **selection,
                    launch_context=launch_context,
                    execution_policy=intent.frozen["execution_policy"],
                    **({"execution_environment": project.execution_profile("claude_environment")} if project else {}))
            elif launcher == "app-server":
                rc = run_app_server(codex, body, wt, log, out, tag, lease=lease, execution_policy=intent.frozen["execution_policy"],
                                    launch_context=launch_context,
                                    on_started=lambda: package_launch.active(lease), on_turn=intent.bind_turn,
                                    app_server_policy=intent.frozen['app_server_policy'], **selection)
            else:
                rc = run_codex(argv, body, wt, log, tag, lease=lease,
                               on_started=lambda: package_launch.active(lease))
            package_launch.finish()
            return _finish_intent(intent, log, out, rc)
        except BaseException as exc:
            if "package_launch" in locals():
                package_launch.finish()
            _failed_intent(intent, log)
            if log.with_suffix('.package.json').is_file():
                exc.dispatch_package = str(log.with_suffix('.package.json'))
            raise
        finally:
            if not handed_off and not lease.release(_not_started(lease)):
                raise RuntimeError("dispatch reservation changed; retained for reconciliation")


@recover_capacity
def resume_order(session: str, wt: Path, tag: str, text: str, *, execution_intent_id=None,
                 recovery_file=None, launcher: str = "exec",
                 app_server_policy=None, model=None, effort=None, native_team=None,
                 answer_from=None, answer_file=None, predispatch_receipt=None, project=None, launch_context=None):
    """Public resume seam; orchestration is extracted to keep this module shrinking."""
    from . import _dispatch_resume
    return _dispatch_resume.resume_order(
        sys.modules[__name__], session, wt, tag, text,
        execution_intent_id=execution_intent_id, recovery_file=recovery_file,
        launcher=launcher, app_server_policy=app_server_policy,
        model=model, effort=effort, **team_kwargs(native_team),
        answer_from=answer_from, answer_file=answer_file, predispatch_receipt=predispatch_receipt,
        **({'launch_context': launch_context} if launch_context is not None else {}),
        **({"project": project} if project is not None else {}))
