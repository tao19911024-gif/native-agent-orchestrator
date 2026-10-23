"""Foundry dispatch adapters: repository access, no host-selected evidence packet."""
from __future__ import annotations

import json
import argparse
from pathlib import Path
import sys
import uuid

from ._compute_selection import compute_kwargs, resolve_goal_profile, GOAL_PROFILES, DEFAULT_GOAL_PROFILE, profile_pair
from ._model_catalogue import BACKEND, model, resolved
from ._compute_selection import (REVIEW_PROFILES, review_assignments, TASK_KINDS, DEFAULT_TASK_KIND,
                                resolve_native_team, is_managed)


def _print_report(value) -> None:
    encoding = getattr(sys.stdout, "encoding", None)
    ensure_ascii = bool(encoding and not encoding.lower().replace("-", "").startswith("utf"))
    print(json.dumps(value, ensure_ascii=ensure_ascii, indent=2, sort_keys=True))
    from ._foundry_cli_help import continuation_advice
    advice = continuation_advice(value)
    if advice:
        print(advice, file=sys.stderr)


def _analyst_executable(backend: str = "codex") -> Path:
    from .foundry_native_support import resolve_executable
    return resolve_executable(backend)


def add_arguments(ap) -> None:
    ap.add_argument("--provider", choices=("codex", "claude"),
                    help="explicit provider for a new task, including admission, coding and review; no quota check required")
    ap.add_argument("--foundry-usage", nargs="+", metavar="RECORD",
                    help="read delivery baseline and usage from goal directories, manifests, outcomes or dispatch receipts; no model calls or writes")
    ap.add_argument("--foundry-goal", action="store_true",
                    help="code, test and independently review one order")
    ap.add_argument("--foundry-review", action="store_true",
                    help="independently review an existing committed range without coding")
    ap.add_argument("--review-base", help="base commit of --foundry-review")
    ap.add_argument("--review-head", help="candidate commit of --foundry-review")
    ap.add_argument("--foundry-analyze", action="store_true",
                    help="inspect the target repository and propose the work")
    ap.add_argument("--amend-from", help="completed BLOCK/PASS review outcome; use --prompt-file for operator additions, including PASS with no findings")
    ap.add_argument("--coding-session", help="existing coding session to continue for --amend-from")
    ap.add_argument("--handoff-provider", choices=("codex", "claude"),
                    help="with --recover-goal, transfer the selected unfinished role to a new provider session")
    ap.add_argument('--handoff-role', choices=('coding', 'manager', 'review-0', 'review-1'),
                    help='role for explicit provider handoff; coding is the default')
    ap.add_argument('--migrate-runtime', action='store_true',
                    help='explicitly transfer a settled goal phase to this runtime using retained evidence and fresh native sessions')
    ap.add_argument("--recover-goal", help="recover recorded review; with --prompt-file, continue unfinished coding in its original session")
    ap.add_argument('--recovery-preflight', action='store_true',
                    help='with --recover-goal, inspect retained runtime, launch settings, checkpoint and all native calls without execution')
    ap.add_argument("--order-revision", help="operator decision binding exact old/new contract bytes for an unfinished manager --recover-goal")
    ap.add_argument('--upgrade-runtime', help='at a settled goal boundary, start the next round on current, an installed version, or an interpreter path')
    ap.add_argument('--upgrade-requested-runtime', help=argparse.SUPPRESS)
    ap.add_argument("--retained-only", action="store_true", help="with --recover-goal, recover completed historical reviews without model calls or project acceptance")
    ap.add_argument("--goal-evidence")
    ap.add_argument("--analysis-evidence")
    ap.add_argument("--analyst-backend", choices=("codex", "claude"), default=None)
    ap.add_argument("--analyst-exe")
    ap.add_argument("--analyst-model")
    ap.add_argument("--analyst-effort")
    ap.add_argument("--review-profile", choices=tuple(REVIEW_PROFILES),
                    help="two separately configured independent reviewers for goal or review")
    ap.add_argument("--auto-amend-model", help="opt in to automatic BLOCK corrections using this coding model")
    ap.add_argument("--auto-amend-effort", help="reasoning effort for --auto-amend-model")
    ap.add_argument("--coding-model")
    ap.add_argument("--coding-effort")
    ap.add_argument("--compute-profile", choices=tuple(GOAL_PROFILES),
                    help="explicit goal coding/review/correction model assignment")
    ap.add_argument("--task-kind", choices=tuple(TASK_KINDS),
                    help="new goal staffing: small, cross-module, or ui-live; defaults to small")
    ap.add_argument("--print-staffing", action="store_true",
                    help="print resolved new-goal staffing without admission, coding or review; asks vendors for current models")
    ap.add_argument("--team-profile", choices=("adaptive", "none"),
                    help="explicit native team or single executor; new goals otherwise use the task kind")
    ap.add_argument("--manager-profile", choices=("opus", "astra"),
                    help="persistent manager; new Codex-executed goals use Opus until a measured Claude quota refusal; retained goals keep their manager")
    ap.add_argument('--review-author-model', action='append', default=[],
                    help='known design author model for standalone review; repeat for multiple authors')


def handle_mode(a, ap, *, project=None, project_adapter=None) -> int | None:
    if getattr(a, 'upgrade_runtime', None):
        from ._foundry_goal_upgrade import handle
        return handle(a, ap, project=project, project_adapter=project_adapter)
    if getattr(a, "foundry_usage", None):
        if any(getattr(a, name, None) for name in (
                "order", "resume", "steer", "foundry_goal", "foundry_review", "foundry_analyze", "recover_goal")):
            ap.error("--foundry-usage reads existing records; choose it separately from execution")
        from ._foundry_goal_usage import task_report
        report = task_report(a.foundry_usage)
        _print_report(report)
        return 0 if report["records"] else 2
    project_options = {"project_record": project.record()} if project is not None else {}
    def evidence_for(order, kind):
        return project.evidence(order) / (kind + "-" + uuid.uuid4().hex) if project else project_adapter.goal_evidence(order, kind)
    requested_provider = getattr(a, "provider", None)
    provider = requested_provider
    backend = a.analyst_backend or provider or "codex"
    review = getattr(a, "foundry_review", False)
    amend_from = getattr(a, "amend_from", None)
    coding_session = getattr(a, "coding_session", None)
    auto_model, auto_effort = getattr(a, "auto_amend_model", None), getattr(a, "auto_amend_effort", None)
    recovery = getattr(a, "recover_goal", None)
    if getattr(a, 'retained_only', False) and not recovery:
        ap.error('--retained-only requires --recover-goal')
    recovery_file = getattr(a, "recovery_file", None)
    order_revision_file = getattr(a, 'order_revision', None)
    if order_revision_file and not (a.foundry_goal and recovery):
        ap.error('--order-revision requires --foundry-goal --recover-goal')
    if recovery_file and (a.foundry_goal or a.foundry_analyze or review) and not (a.foundry_goal and (amend_from or recovery)):
        ap.error('--recovery-file in Foundry requires --recover-goal with --prompt-file, or --amend-from with --coding-session')
    profile = getattr(a, "compute_profile", None)
    review_profile = getattr(a, "review_profile", None)
    compute_source = "explicit" if profile or any((a.coding_model, a.coding_effort,
        a.analyst_model, a.analyst_effort, a.analyst_backend, a.analyst_exe,
        review_profile, auto_model, auto_effort)) else "native"
    team_profile = getattr(a, "team_profile", None)
    manager_profile = getattr(a, "manager_profile", None)
    task_kind = getattr(a, "task_kind", None)
    launcher_explicit = getattr(a, "launcher_explicit", False)
    kind_chooses_provider = (a.foundry_goal and not amend_from and not provider and not launcher_explicit
            and profile in (None, "claude")
            and not any(name and not name.startswith("claude-") for name in (a.coding_model, auto_model)))
    # Task kind and explicit operator choices own staffing, not quota pacing.
    if kind_chooses_provider and task_kind == "ui-live":
        a.launcher = "claude"
        # Kind selects Claude coding, not the explicit --provider all-roles contract.
        # A reviewer model pair without a backend retains the CLI's Codex default,
        # except for a Claude model pair which keeps the kind's Claude backend.
        backend = a.analyst_backend or (
            "codex" if a.analyst_model and not a.analyst_model.startswith("claude-") else "claude")
    handoff_provider = getattr(a, "handoff_provider", None)
    handoff_role = getattr(a, 'handoff_role', None)
    migrate_runtime = getattr(a, 'migrate_runtime', False)
    if (handoff_provider or handoff_role or migrate_runtime) and (not recovery or not a.foundry_goal):
        ap.error('role transfer requires --foundry-goal --recover-goal')
    if handoff_role and not (handoff_provider or migrate_runtime):
        ap.error('--handoff-role requires --handoff-provider or --migrate-runtime')
    if handoff_provider and handoff_role in (None, 'coding', 'manager') and not getattr(a, 'prompt_file', None):
        ap.error('coding/manager handoff requires --prompt-file; review handoff retains its frozen packet')
    supplied_admission = getattr(a, "predispatch_receipt", None)
    if provider and recovery:
        ap.error("Recovery uses the recorded provider; omit --provider")
    if provider == "claude":
        if a.analyst_backend not in (None, "claude") or review_profile not in (None, "claude"):
            ap.error("--provider claude selects Claude for every role")
        if profile not in (None, "claude"):
            ap.error("--provider claude requires a Claude compute profile")
        if a.foundry_goal and not amend_from:
            profile = profile or "claude"
        if (a.foundry_goal or review) and not amend_from and not any((a.analyst_model, a.analyst_effort, a.analyst_backend, a.analyst_exe)):
            review_profile = review_profile or "claude"
    coding_backend = "claude" if getattr(a, "launcher", None) == "claude" else "codex"
    if review_profile and (not (a.foundry_goal or review) or recovery):
        ap.error("--review-profile selects new goal/review assignments; recovery uses recorded reviewers")
    if review_profile and any((a.analyst_model, a.analyst_effort, a.analyst_backend, a.analyst_exe)):
        ap.error("choose --review-profile or a shared analyst override, not both")
    if supplied_admission and (not a.foundry_goal or any((review, amend_from, recovery))):
        # Direct order mode consumes this at its existing dispatch owner.
        if a.foundry_goal or a.foundry_analyze or review:
            ap.error("--predispatch-receipt applies to a new Foundry goal")
    if team_profile and not a.foundry_goal:
        ap.error("--team-profile requires --foundry-goal")
    if manager_profile and not a.foundry_goal:
        ap.error("--manager-profile requires --foundry-goal")
    if task_kind and (not a.foundry_goal or amend_from or recovery):
        ap.error("--task-kind applies only to a new Foundry goal")
    if getattr(a, "print_staffing", False) and (not a.foundry_goal or amend_from or recovery):
        ap.error("--print-staffing applies only to a new Foundry goal")
    if profile and not a.foundry_goal:
        ap.error("--compute-profile requires --foundry-goal")
    if recovery and (not a.foundry_goal or any((amend_from, coding_session, auto_model, auto_effort,
            a.coding_model, a.coding_effort, a.analyst_model, a.analyst_effort, a.analyst_exe,
            a.goal_evidence, a.worktree if project is None else None, profile, manager_profile)) or backend != "codex"):
        from ._foundry_cli_help import RECOVERY_USAGE
        ap.error("Recovery keeps --order and uses recorded workspace/model settings. "
                 "Remove workspace/model/profile overrides and amendment options. Use: " + RECOVERY_USAGE)
    if (auto_model or auto_effort) and not a.foundry_goal:
        ap.error("automatic amendment requires --foundry-goal")
    if (amend_from or coding_session) and (not a.foundry_goal or not (amend_from and coding_session)):
        ap.error("--amend-from and --coding-session require each other and --foundry-goal")
    if (getattr(a, "review_base", None) or getattr(a, "review_head", None)) and not review:
        ap.error("--review-base and --review-head require --foundry-review")
    if not (a.foundry_goal or a.foundry_analyze or review):
        return None
    if sum((a.foundry_goal, a.foundry_analyze, review)) != 1:
        ap.error("choose one Foundry operation")
    if not a.order or a.resume or a.steer:
        from ._foundry_cli_help import RECOVERY_USAGE
        ap.error("Foundry requires --order without a session operation" +
                 (". Recovery: " + RECOVERY_USAGE if recovery else ""))
    if review and not all((a.worktree, a.review_base, a.review_head)):
        ap.error("--foundry-review requires --worktree, --review-base and --review-head")
    order = Path(a.order).resolve()
    if project is None:
        record_foundry_location = project_adapter.record_foundry_location
        from ._foundry_cli_help import announce_review_recovery
    else:
        def record_foundry_location(order, evidence):
            # External evidence is already owned by this explicit descriptor.
            pass
        def announce_review_recovery(order, evidence):
            if evidence is not None:
                print("Retained project run: " + str(evidence) + "; continue with the same --project-config.", file=sys.stderr)
    if recovery:
        try:
            from .foundry_goal import recover_goal
            prompt_file = getattr(a, "prompt_file", None)
            answer = Path(prompt_file).read_text(encoding="utf-8-sig") if prompt_file else None
            if answer is not None and not answer.strip():
                raise ValueError("--prompt-file is empty")
            if team_profile is not None and answer is None:
                raise ValueError("--team-profile on recovery requires --prompt-file for unfinished coding")
            if getattr(a, "prompt", None):
                raise ValueError("Foundry coding recovery uses --prompt-file for the operator answer")
            if project is not None:
                saved = json.loads((Path(recovery) / "manifest.json").read_bytes())
                from ._project_execution import from_record
                from_record(saved.get("project_record"), selected=project)
            recovery_input = {}
            if answer is not None:
                recovery_input['restore_manager'] = True
            if project is not None and (a.project_preflight_input or a.project_resume_preflight_input):
                if a.project_preflight_input and a.project_resume_preflight_input:
                    raise ValueError("goal recovery takes one project preflight input file")
                recovery_input['project_preflight_input'] = project.record().get(
                    "resume_preflight_input", project.preflight_input)
            if not getattr(a, "retained_only", False):
                record_foundry_location(order, Path(recovery))
            result = recover_goal(order, Path(recovery), **({"answer": answer} if answer is not None else {}),
                                  **({"team_profile": team_profile} if team_profile is not None else {}),
                                  **({"target_provider": handoff_provider} if handoff_provider else {}),
                                  **({'handoff_role': handoff_role} if handoff_role else {}),
                                  **({'migrate_runtime': True} if migrate_runtime else {}),
                                  **({'recovery_file': recovery_file} if recovery_file else {}),
                                  **({'order_revision_file': order_revision_file} if order_revision_file else {}),
                                  **({'retained_only': True} if getattr(a, 'retained_only', False) else {}), **recovery_input)
            if result.get("continuation_evidence"):
                record_foundry_location(order, Path(result["continuation_evidence"]))
        except (OSError, RuntimeError, ValueError, ImportError, KeyError) as exc:
            print("REFUSE Foundry recovery: %s" % exc, file=sys.stderr)
            from ._foundry_cli_help import announce_recovery_failure
            announce_recovery_failure(order, recovery)
            announce_review_recovery(order, recovery)
            return 2
        _print_report(result)
        return 0 if result.get("status") in {"candidate_verified", "project_handoff"} else 2
    evidence = None
    try:
        if amend_from:
            prompt_file = getattr(a, 'prompt_file', None)
            amend_prompt = Path(prompt_file).read_text(encoding='utf-8-sig') if prompt_file else None
            if amend_prompt is not None and not amend_prompt.strip():
                raise ValueError('--prompt-file is empty')
            from .foundry_goal import amendment_manifest, amendment_launcher
            prior = amendment_manifest(amend_from, getattr(a, 'coding_session', None))
            prior_review_backend = (prior.get("requested_compute", {}).get("review") or {}).get("backend")
            if not profile and a.analyst_backend is None:
                backend = prior_review_backend or "codex"
            prior_launcher = amendment_launcher(prior)
            if provider and prior_launcher and ("claude" if prior_launcher == "claude" else "codex") != provider:
                raise ValueError("An amendment retains its provider; cross-provider transfer is not a native resume")
            if prior_launcher and not getattr(a, "launcher_explicit", False):
                a.launcher = prior_launcher
            coding_backend = "claude" if a.launcher == "claude" else "codex"
            if coding_backend == "claude" and not a.analyst_backend and not prior_review_backend:
                backend = "claude"
        # Apply new-invocation defaults only. Recovery above owns frozen settings;
        # explicit assignments and amendments retain their existing semantics.
        staffing_sources = None
        review_source_detail = None
        team_source_detail = None
        admission_staffing = None
        if a.foundry_goal and (not amend_from or (
                prior.get('delivery_context', {}).get('mode') == 'review-only' and not prior.get('manager_state'))):
            task_kind = task_kind or DEFAULT_TASK_KIND
            kind_row = TASK_KINDS[task_kind]
            admission_provider = requested_provider or "claude"
            admission_source = "explicit" if requested_provider else "kind"
            if supplied_admission:
                admission_provider = json.loads(Path(supplied_admission).read_bytes()).get("provider", "codex")
                admission_source = "receipt"
            admission_staffing = {"provider": admission_provider,
                "model": model("sonnet") if admission_provider == "claude" else "native-default",
                "effort": "high" if admission_provider == "claude" else "native-default",
                "source": admission_source}
            kind_backend = "claude" if task_kind == "ui-live" else "codex"
            team_backend_source = ("explicit" if launcher_explicit or requested_provider or
                                   coding_backend != kind_backend else "kind")
            team_source_detail = {"profile": "explicit" if team_profile else "kind",
                                  "backend": team_backend_source}
            # The kind supplies omitted parts. Provider and individual flags own
            # only the parts they explicitly select.
            staffing_sources = {part: "explicit" if value else "kind" for part, value in (
                ("manager", manager_profile), ("team", team_profile or team_backend_source == "explicit"),
                ("coding", a.coding_model or a.compute_profile or requested_provider or launcher_explicit),
                ("correction", auto_model or a.compute_profile or requested_provider or launcher_explicit),
                ("reviewers", a.review_profile or a.compute_profile or a.analyst_model or a.analyst_backend or
                 requested_provider))}
            review_source_detail = {
                "backend": "explicit" if a.analyst_backend or a.review_profile or requested_provider or
                    (task_kind == "ui-live" and a.analyst_model and backend == "codex") else "kind",
                "model": "explicit" if review_profile or a.compute_profile or a.analyst_model else "kind"}
            profile = profile or ("claude" if coding_backend == "claude" else
                                  "sol-claude" if kind_row["compute_profile"] == "claude" else
                                  kind_row["compute_profile"])
            compute_source = ("explicit" if a.compute_profile or requested_provider or launcher_explicit else "kind")
            # New Codex execution starts with Opus coordination. A provider
            # selection is not evidence that Claude quota is unavailable;
            # measured refusal transfers the retained manager in the goal owner.
            manager_profile = 'opus' if coding_backend == 'codex' else manager_profile or kind_row["manager_profile"]
            if coding_backend == 'codex' and getattr(a, 'manager_profile', None) == 'astra':
                staffing_sources['manager'] = 'operator-rule'
            team_profile = team_profile or kind_row["team_profile"]
            if (not review_profile and not a.compute_profile
                    and not any((a.analyst_model, a.analyst_effort, a.analyst_exe))
                    and a.analyst_backend in (None, "claude")):
                if not (backend == "codex" and (a.analyst_backend == "codex" or requested_provider == "codex")):
                    review_profile = kind_row["review_profile"]
            if (backend == "codex" and not review_profile and not any((a.analyst_model, a.analyst_effort))
                    and (a.analyst_backend == "codex" or requested_provider == "codex")
                    and BACKEND[GOAL_PROFILES[profile]["review"][0]] == "claude"):
                a.analyst_model, a.analyst_effort = profile_pair(DEFAULT_GOAL_PROFILE, "review")
                review_source_detail["model"] = "provider-default"
        if a.foundry_goal and amend_from and not manager_profile:
            from ._compute_selection import missing_manager_profile
            manager_profile = missing_manager_profile(prior)
        if review and backend == "codex" and not (a.analyst_model or a.analyst_effort):
            a.analyst_model, a.analyst_effort = profile_pair(DEFAULT_GOAL_PROFILE, "review")
        if provider == "claude" and a.foundry_analyze and not (a.analyst_model or a.analyst_effort):
            a.analyst_model, a.analyst_effort = model("sonnet"), "high"
        # Diagnose incomplete assignments before spending a predispatch model call.
        compute_kwargs(a.analyst_model, a.analyst_effort, backend=backend)
        selected = {"review" if review or a.foundry_goal else "analysis": {
            "backend": backend, **compute_kwargs(a.analyst_model, a.analyst_effort,
                                                          backend=backend)}}
        if a.foundry_goal:
            compute_kwargs(a.coding_model, a.coding_effort, backend=coding_backend)
            compute_kwargs(auto_model, auto_effort, backend=coding_backend)
            if profile:
                _, assignment = resolve_goal_profile(profile, backend=backend, coding_backend=coding_backend,
                    team_profile=team_profile, task_kind=task_kind,
                    coding_model=a.coding_model, coding_effort=a.coding_effort,
                    analyst_model=a.analyst_model, analyst_effort=a.analyst_effort,
                    auto_amend_model=auto_model, auto_amend_effort=auto_effort)
                selected = assignment["roles"]
                if staffing_sources:
                    selected = {name: {**row, "source": (review_source_detail["model"]
                                if name == "review" else staffing_sources.get(name, row["source"]))}
                                for name, row in selected.items()}
            else:
                selected.update(coding={"backend": coding_backend, **compute_kwargs(a.coding_model, a.coding_effort, backend=coding_backend)},
                                correction=compute_kwargs(auto_model, auto_effort, backend=coding_backend) or None)
        if review_profile:
            selected.pop("review", None)
        printed_team = resolve_native_team(team_profile, backend=coding_backend,
                                          managed=is_managed(manager_profile)) if team_profile else None
        recorded_manager = prior.get('manager_state') if amend_from else None
        if amend_from and printed_team is None:
            printed_team = prior.get('native_team')
        printed_profile = team_profile or (printed_team or {}).get('profile')
        print("FOUNDRY COMPUTE: " + json.dumps({"task_kind": task_kind, "profile": profile, "roles": selected,
               **({'coordination_policy': 'opus_until_measured_claude_quota_unavailable',
                   'requested_manager_profile': getattr(a, 'manager_profile', None)}
                  if a.foundry_goal and not amend_from and coding_backend == 'codex' else {}),
               **({"reviewers": review_assignments(review_profile)} if review_profile else {}),
               "team_profile": team_profile, "manager_profile": manager_profile,
               "manager": {"model": model(manager_profile),
                           "effort": "high"} if manager_profile else (
                           {k: recorded_manager[k] for k in ('model', 'effort', 'backend', 'profile') if k in recorded_manager}
                           if recorded_manager else None),
               "team": {"backend": coding_backend, "profile": printed_profile,
                        "lead_model": selected.get("coding", {}).get("model"),
                        "member_roles": (printed_team or {}).get("member_roles", {})} if printed_profile else None,
               **({"staffing_sources": staffing_sources} if staffing_sources else {}),
               **({"review_source_detail": review_source_detail} if review_source_detail else {}),
               **({"team_source_detail": team_source_detail} if team_source_detail else {}),
               **({"admission": admission_staffing} if admission_staffing else {}),
               "model_catalogue": resolved(),
               "scope": "new goal staffing; amendments and recovery retain recorded choices"},
               ensure_ascii=True, sort_keys=True), file=sys.stderr)
        if getattr(a, "print_staffing", False):
            return 0
        if (profile == "astra" and team_profile == "none" and not amend_from and not review_profile
                and all(role.get("model") == model("astra") for role in selected.values())):
            print("Astra profile selected for coding, review and correction; adaptive delegation is off. "
                  "For routine development use --task-kind small; for delegated work add "
                  "--team-profile adaptive. Explicit selection is preserved.", file=sys.stderr)
        workspace = project.workspace if project is not None else Path(a.worktree).resolve(strict=True) if review else project_adapter.goal_workspace(order)
        executable = Path(a.analyst_exe) if a.analyst_exe else _analyst_executable(backend)
        if review:
            from . import foundry_goal
            evidence = Path(a.goal_evidence).resolve() if a.goal_evidence else evidence_for(order, "review")
            if a.goal_evidence:
                record_foundry_location(order, evidence)
            result = foundry_goal.run_review(
                order, evidence, executable, target_worktree=workspace, **project_options,
                base_commit=a.review_base, candidate_commit=a.review_head,
                backend=backend, analyst_model=a.analyst_model,
                analyst_effort=a.analyst_effort,
                review_author_models=a.review_author_model,
                **({"reviewers": review_assignments(review_profile)} if review_profile else {}))
        elif a.foundry_analyze:
            from ._foundry_analysis import analyze_order
            evidence = Path(a.analysis_evidence).resolve() if a.analysis_evidence else evidence_for(order, "analysis")
            admission = evidence.with_name(evidence.name + "-predispatch")
            if not project_adapter.goal_admission(order, workspace, admission, **({"provider": provider} if provider else {})):
                return 3
            result = analyze_order(order, evidence, workspace, executable,
                                   backend=backend, model=a.analyst_model,
                                   effort=a.analyst_effort)
        else:
            evidence = Path(a.goal_evidence).resolve() if a.goal_evidence else evidence_for(order, "goal")
            admission = evidence.with_name(evidence.name + "-predispatch")
            if project is None and not amend_from and not supplied_admission and not getattr(a, '_boundary_continuation', None) and not project_adapter.goal_admission(
                    order, workspace, admission, True, admission_staffing["provider"]):
                return 3
            from . import foundry_goal
            if a.goal_evidence and not getattr(a, '_prepare_goal', False):
                record_foundry_location(order, evidence)
            runner = foundry_goal.prepare_goal if getattr(a, '_prepare_goal', False) else foundry_goal.run_goal
            result = runner(
                order, evidence, executable, target_worktree=workspace, **project_options,
                predispatch_receipt=None if amend_from or project is not None or getattr(a, '_boundary_continuation', None) else (supplied_admission or admission / "admission.json"),
                amend_from=amend_from, coding_session=coding_session or getattr(a, '_boundary_session', None),
                **({'amend_prompt': amend_prompt} if amend_from and amend_prompt is not None else {}),
                auto_amend_model=auto_model, auto_amend_effort=auto_effort,
                compute_profile=profile,
                compute_source=compute_source,
                task_kind=task_kind, staffing_sources=staffing_sources,
                review_source_detail=review_source_detail,
                team_source_detail=team_source_detail,
                admission_staffing=admission_staffing,
                launcher_explicit=launcher_explicit,
                **({"recovery_file": recovery_file} if recovery_file else {}),
                **({"reviewers": review_assignments(review_profile)} if review_profile else {}),
                **({"team_profile": team_profile} if team_profile else {}),
                **({"manager_profile": manager_profile} if manager_profile else {}),
                **({"backend": backend} if a.analyst_backend is not None or a.analyst_exe or not amend_from else {}),
                coding_launcher=a.launcher,
                **({'continuation': a._boundary_continuation} if getattr(a, '_boundary_continuation', None) else {}),
                **({'task_deadline_at': a.task_deadline} if getattr(a, 'task_deadline', None) is not None else {}),
                **({'app_server_policy': a.app_server_policy} if getattr(a, 'app_server_policy', None) is not None else {}),
                **{"analyst_" + key: value for key, value in compute_kwargs(
                    a.analyst_model, a.analyst_effort, backend=backend).items()},
                **{"coding_" + key: value for key, value in compute_kwargs(
                    a.coding_model, a.coding_effort, backend=coding_backend).items()},
            )
    except (OSError, RuntimeError, ValueError, ImportError) as exc:
        print("REFUSE Foundry: %s" % exc, file=sys.stderr)
        if evidence is not None:
            from ._foundry_cli_help import announce_recovery_failure
            announce_recovery_failure(order, evidence)
        announce_review_recovery(order, evidence)
        return 2
    if getattr(a, '_prepare_goal', False):
        a._prepared_goal = result
        return 0
    _print_report(result)
    return 0 if result.get("status") in {"ready", "candidate_verified", "project_handoff"} else 2
