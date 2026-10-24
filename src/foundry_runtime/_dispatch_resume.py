"""Resume execution through the dispatcher's existing admission and custody owners.

This is an extracted region of :mod:`dispatch`, not another entry point.  The
public CLI and callers still enter through ``_foundry_dispatch.resume_order``; the
``owner`` argument keeps all mutable authorities and monkeypatchable seams in
that module while this file owns the resume-specific orchestration.
"""

from __future__ import annotations

import re
from pathlib import Path

from ._app_server_policy import normalize_policy
from ._compute_selection import team_kwargs, team_prompt


def _recovery_candidate_matches(owner, recovery, worktree, project):
    options = owner._repository_options(project)
    if recovery.data['schema_version'] == 'pdcl-execution-recovery-5':
        return recovery.candidate_matches(**options)
    return (owner._worktree_head(worktree, **options) == recovery.data['successor']['candidate_head']
            and owner._worktree_clean(worktree, **options))


def run_app_server(
    owner,
    codex: str,
    stdin_text: str,
    worktree: Path,
    log: Path,
    out: Path,
    tag: str,
    *,
    timeout_seconds: float | None = None,
    app_server_policy=None,
    lease=None,
    on_started=None,
    on_turn=None,
    resume_thread_id=None,
    model=None,
    effort=None,
    native_team=None,
    execution_policy=None,
    launch_context=None,
) -> int:
    """Use the one app-server transport with dispatcher-owned evidence custody."""
    policy = normalize_policy(app_server_policy, timeout_seconds=timeout_seconds)
    lease = lease or owner.ExecutorLease.acquire(worktree, tag, "app-server")
    lease.require_reserved(worktree, tag, {"app-server"})
    cleanup = None
    process = None
    entered_transport = False

    def on_cleanup(value):
        nonlocal cleanup
        cleanup = value

    try:
        if any(
            path.exists()
            for path in (
                log,
                out,
                log.with_suffix(".cleanup.json"),
                log.with_suffix(".terminal.json"),
            )
        ):
            raise FileExistsError("dispatch evidence already exists: " + str(log))
        entered_transport = True
        rc, _pid = owner.app_server_transport.run_executor(
            codex,
            stdin_text,
            worktree,
            log,
            out,
            tag,
            log.parent / "active_turn.json",
            lambda pid: on_started() if on_started else None,
            lease=lease,
            on_cleanup=on_cleanup,
            launch_context=launch_context,
            app_server_policy=policy,
            **({"execution_policy": execution_policy} if execution_policy is not None else {}),
            **({"on_turn": on_turn} if on_turn is not None else {}),
            **(
                {"resume_thread_id": resume_thread_id}
                if resume_thread_id is not None
                else {}
            ),
            **owner.compute_kwargs(model, effort),
            **team_kwargs(native_team),
        )
        return rc
    except BaseException as exc:
        process = getattr(exc, "process", None)
        raise
    finally:
        if not entered_transport:
            lease.release(owner._not_started(lease))
        elif cleanup is None:
            lease.retain_unknown("transport returned no measured cleanup receipt")
            raise RuntimeError("app-server cleanup is unknown; reservation retained")
        else:
            try:
                owner._record_cleanup(lease, cleanup, log.with_suffix(".cleanup.json"))
            except Exception as exc:
                raise owner.DispatchCleanupError(
                    "app-server cleanup: " + str(exc), process, cleanup
                ) from exc


def resume_order(
    owner,
    session: str,
    wt: Path,
    tag: str,
    text: str,
    *,
    execution_intent_id=None,
    recovery_file=None,
    launcher: str = "exec",
    app_server_policy=None,
    model=None,
    effort=None,
    native_team=None,
    answer_from=None,
    answer_file=None,
    predispatch_receipt=None,
    project=None,
    before_attempt=None,
    launch_context=None,
):
    """Admit one correction, preserving exec-resume as the byte-compatible default."""
    from ._native_session import read_session_worktree

    selection = {**owner.compute_kwargs(model, effort, backend="claude" if launcher == "claude" else "codex"), **team_kwargs(native_team)}
    if launcher not in {"exec", "app-server", "claude"}:
        raise ValueError("invalid resume launcher")
    if app_server_policy is not None and launcher != "app-server":
        raise ValueError("app-server policy applies only to app-server resume")
    policy = normalize_policy(app_server_policy, capacity_defaults=True) if launcher == "app-server" else None
    frozen_launcher = launcher if launcher in {"app-server", "claude"} else "exec-resume"
    tag = owner.normalized_line(tag)
    if launcher == "claude":
        from ._dispatch_claude import session_worktree
        read_session_worktree = lambda sid: session_worktree(
            project.common_dir if project is not None else owner._project_adapter(None).goal_authority().common_dir, tag, sid)
    wt = wt.resolve(strict=True)
    if read_session_worktree(session).resolve() != wt:
        raise ValueError(
            "resume session belongs to another worktree; refusing mismatched occupancy"
        )
    authority = project if project is not None else owner._project_adapter(None).goal_authority()
    if project is not None and (answer_from or answer_file or predispatch_receipt):
        raise ValueError("project admission does not consume PDCL predispatch receipts")
    head = owner._worktree_head(wt, **owner._repository_options(project))
    source_verifier = owner._project_adapter(project).recovery_source_verifier
    recovery = None
    read_only = False
    if recovery_file is not None:
        recovery = owner.RecoveryAuthorization.load(
            recovery_file,
            task_key=tag,
            worktree=wt,
            session_id=session,
            candidate_head=None,
            amendment=text.encode("utf-8"),
            source_verifier=source_verifier,
        )
        if launcher == "app-server" and app_server_policy is None:
            # Recovery continues the predecessor's frozen policy, including an
            # older policy without capacity retries. Admission below still
            # verifies the exact predecessor and authorization hashes.
            predecessor = (authority.common_dir / "pdcl-dispatch" / ("task-" + tag)
                           / "intents" / recovery.data["predecessor"]["intent_id"])
            record = owner.ExecutionIntent.read_history(predecessor, task_key=tag)[0]
            policy = normalize_policy(record["frozen"]["app_server_policy"])
        candidate = recovery.data["successor"]["candidate_head"]
        read_only = not _recovery_candidate_matches(owner, recovery, wt, project)
        head = candidate
    authority.require_task(tag, wt, mode="resume", recovery=recovery)
    binding = owner._intent_bindings(
        authority,
        wt,
        text.encode("utf-8"),
        head,
        operation="resume",
        launcher=frozen_launcher,
        session=session,
        app_server_policy=policy,
        **selection,
    )
    if launch_context is not None:
        binding['launch_context'] = launch_context
    binding = owner.admitted_binding(authority.common_dir, tag, binding)
    with (
        owner.ExecutionIntent.open(
            authority.common_dir,
            tag,
            binding,
            intent_id=execution_intent_id,
            recovery=recovery,
            read_only=read_only,
            source_verifier=source_verifier,
        ) as intent,
        owner.observed_start(intent),
        owner.deadline.scope(intent.frozen.get('task_deadline_at')),
    ):
        if intent.state == "completed":
            return owner._intent_outcome(intent, replayed=True)
        if before_attempt is not None:
            before_attempt()
        owner.deadline.check()
        return _resume_attempt(
            owner,
            session,
            wt,
            tag,
            text,
            authority,
            intent,
            head,
            recovery=recovery,
            launcher=launcher,
            project=project,
            admission_options={k: v for k, v in {"answer_from": answer_from,
                "answer_file": answer_file, "predispatch_receipt": predispatch_receipt}.items() if v is not None},
            **selection,
        )


def _resume_attempt(
    owner,
    session,
    wt,
    tag,
    text,
    authority,
    intent,
    head,
    *,
    recovery=None,
    launcher="exec",
    model=None,
    effort=None,
    native_team=None,
    admission_options=None,
    project=None,
):
    """Reserve before gate inputs, then retain numbered evidence and terminal truth."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", tag):
        raise ValueError("invalid resume tag")
    lease_launcher = launcher if launcher in {"app-server", "claude"} else "exec-resume"
    lease = intent.acquire_lease(wt, tag, lease_launcher)
    handed_off = False
    log = None
    try:
        from ._native_session import read_session_worktree
        if launcher == "claude":
            from ._dispatch_claude import session_worktree
            read_session_worktree = lambda sid: session_worktree(authority.common_dir, tag, sid)

        if read_session_worktree(session).resolve() != wt.resolve(strict=True):
            raise ValueError(
                "resume session belongs to another worktree; refusing mismatched occupancy"
            )
        # Corrections use the same admission path as before extraction.  This is
        # deliberately before any evidence rewrite or native launch: the old
        # resume door once skipped every order gate while nearly half of worker
        # instructions used it.
        adapter = owner._project_adapter(project)
        adapter.preflight(tag, text, operation="resume", env=owner._utf8_env(),
                          execution_intent_id=intent.intent_id, worktree=wt,
                          launcher=launcher, **(admission_options or {}))
        authority.verify_unchanged()
        if owner._worktree_head(wt, **owner._repository_options(project)) != head:
            raise ValueError("resume inputs changed during admission")
        if recovery is not None:
            recovery.revalidate()
            if not _recovery_candidate_matches(owner, recovery, wt, project):
                raise ValueError("recovery candidate worktree changed during admission")
        ev = adapter.evidence(tag)
        # Count logs as well as reports.  A killed run can leave only its log;
        # choosing the number from reports alone once truncated 4.2 MB of the
        # only failure evidence on the following correction.
        n = 1 + max(
            [0]
            + [
                owner._resumed_index(q)
                # Any file of a numbered run claims its number: a launch refused before
                # the transport started leaves only dispatch_resumedN.cleanup.json.
                for pattern in ("last_message_resumed*.md", "dispatch_resumed*.*")
                for q in (ev.glob(pattern) if ev.is_dir() else ())
            ]
        )
        out = ev / ("last_message_resumed%d.md" % n)
        log = ev / ("dispatch_resumed%d.log" % n)
        # The habitual report name must stop answering after a resume. Preserve
        # its bytes and replace it with an unmistakable pointer to the new run.
        stale = ev / "last_message.md"
        if stale.is_file():
            prior_report_text = stale.read_text(encoding="utf-8", errors="replace")
            if not prior_report_text.startswith("SUPERSEDED"):
                keep = ev / ("last_message_pre_resume%d.md" % n)
                with keep.open("x", encoding="utf-8") as saved:
                    saved.write(prior_report_text)
                stale.write_text(
                    "\n".join(
                        [
                            "SUPERSEDED - this run was RESUMED and this file is NOT the result.",
                            "",
                            "  report for the current run:   %s" % out.name,
                            "  what this file used to hold:  %s" % keep.name,
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )
                print(
                    "  superseded last_message.md (it named an older run) -> %s"
                    % keep.name
                )
        if read_session_worktree(session).resolve() != wt.resolve(strict=True):
            raise ValueError("resume session worktree changed during admission")
        # Keep the correction on stdin.  Passing it as one argv element lost
        # everything after the first newline in two measured resume attempts.
        body = team_prompt(adapter.prompt(text), native_team)
        if launcher == "app-server":
            print("=== resuming %s in %s via app-server ===" % (session, wt))
        else:
            print("=== resuming %s in %s ===" % (session, wt))
        print(
            "    prompt: %d bytes, %d line(s) -- sent on stdin"
            % (len(text.encode("utf-8")), text.count("\n") + 1)
        )
        authority.verify_unchanged()
        if owner._worktree_head(wt, **owner._repository_options(project)) != head:
            raise ValueError("resume inputs changed before launch")
        if recovery is not None:
            recovery.revalidate()
            if not _recovery_candidate_matches(owner, recovery, wt, project):
                raise ValueError("recovery candidate worktree changed before launch")
            if launcher == "app-server":
                # Consumption is one-way; every launch precondition is checked first.
                owner.app_server_transport.require_fresh_launch(out, log.parent / "active_turn.json", log)
            intent.consume_recovery(lease)
        from ._launch_context import capture, bind_coding_model
        from ._dispatch_package import retained_launch
        # Explicit boundary upgrades freeze their new launch policy in this
        # intent. Ordinary resumes keep using the session's retained package.
        launch_context = intent.frozen.get('launch_context')
        if launch_context is None:
            launch_context = retained_launch(ev, authority.common_dir, tag, session)
        if launch_context is None:
            launch_context = capture(wt, root_role=(native_team or {}).get('lead_role', 'executor'),
                                     backend='claude' if launcher == 'claude' else 'codex')
            launch_context.pop('tool_discipline', None)
            launch_context['policy_observation'] = 'legacy launch policy unavailable; no new hook enforcement added on resume'
        bind_coding_model(launch_context, model)
        package_launch = owner.PackageLaunch(
            intent,
            log,
            lambda: owner._record_package(log, tag, "resume", intent, body, launch_context),
        )
        package_launch.start(lease)
        handed_off = True
        if launcher == "claude":
            from . import _dispatch_claude
            rc = _dispatch_claude.run(owner, body, wt, log, out, tag, lease=lease,
                on_started=lambda sid: package_launch.active(lease, sid), resume_session=session,
                launch_context=launch_context,
                model=model, effort=effort, **team_kwargs(native_team), execution_policy=intent.frozen["execution_policy"],
                **({"execution_environment": project.execution_profile("claude_environment")} if project else {}))
        elif launcher == "app-server":
            rc = owner.run_app_server(
                owner._codex(),
                body,
                wt,
                log,
                out,
                tag,
                lease=lease,
                on_started=lambda: package_launch.active(lease, session),
                on_turn=intent.bind_turn,
                launch_context=launch_context,
                resume_thread_id=session,
                app_server_policy=intent.frozen["app_server_policy"],
                execution_policy=intent.frozen["execution_policy"],
                model=model,
                effort=effort,
                **team_kwargs(native_team),
            )
        else:
            argv = owner.coding_argv(
                owner._codex(),
                "exec",
                "-C",
                str(wt),
                "resume",
                session,
                "-o",
                str(out),
                "-",
                **owner.compute_kwargs(model, effort),
                **team_kwargs(native_team), execution_policy=intent.frozen["execution_policy"],
                launch_context=launch_context,
            )
            from ._launch_context import worker_environment
            rc = owner.run_codex(
                argv,
                body,
                wt,
                log,
                tag,
                lease=lease,
                env=worker_environment(owner.codex_env(argv[0]), launch_context),
                on_started=lambda: package_launch.active(lease, session),
            )
        package_launch.finish()
        return owner._finish_intent(intent, log, out, rc)
    except BaseException as exc:
        if "package_launch" in locals():
            package_launch.finish()
        if log is not None:
            owner._failed_intent(intent, log)
            if log.with_suffix('.package.json').is_file():
                exc.dispatch_package = str(log.with_suffix('.package.json'))
        raise
    finally:
        if not handed_off and not lease.release(owner._not_started(lease)):
            raise RuntimeError(
                "resume reservation changed; retained for reconciliation"
            )
