"""Shared project descriptors and read-only onboarding; execution lives in dispatch."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

from ._repository_observation import git as native_git

SCHEMA = "foundry-project-1"


def _git(workspace, *args):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    run = native_git(["--no-optional-locks", "-c", "core.fsmonitor=false", *args],
                     workspace, timeout=None, env=env)
    if run.returncode:
        raise ValueError(run.stderr.strip() or "Git could not inspect this workspace")
    return run.stdout.rstrip("\r\n")


def identity(workspace):
    workspace = Path(workspace).resolve(strict=True)
    root = Path(_git(workspace, "rev-parse", "--show-toplevel")).resolve()
    common = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    identifier = hashlib.sha256(os.path.normcase(str(common)).encode("utf-8")).hexdigest()[:24]
    return {"project_id": identifier, "workspace": str(root), "git_common_dir": str(common)}


def descriptor(workspace, config_path, rules=None):
    project = identity(workspace)
    root = Path(project["workspace"])
    entries = list(dict.fromkeys(rules or (["AGENTS.md"] if (root / "AGENTS.md").is_file() else [])))
    return {"schema_version": SCHEMA, **project, "rule_sources": entries,
            "evidence_root": str(Path(config_path).resolve().parent / "runs" / project["project_id"])}


def load(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA:
        raise ValueError("expected a foundry-project-1 descriptor")
    for key in ("project_id", "workspace", "git_common_dir", "evidence_root"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError("descriptor needs a nonempty " + key)
    if not all(Path(value[key]).is_absolute() for key in ("workspace", "git_common_dir", "evidence_root")):
        raise ValueError("workspace, git_common_dir and evidence_root must be absolute")
    if not isinstance(value.get("rule_sources"), list) or any(
            not isinstance(item, str) or not item.strip() for item in value["rule_sources"]):
        raise ValueError("rule_sources must be a list of file references")
    if "history_roots" in value and (not isinstance(value["history_roots"], list) or
            any(not isinstance(item, str) or not item.strip() for item in value["history_roots"])):
        raise ValueError("history_roots must be a list of directory references")
    if "execution" in value:
        from ._project_execution import validate_execution
        validate_execution(value)
    if "launch" in value:
        from ._launch_context import validate_project_launch
        validate_project_launch(value["launch"])
    if 'default_provider' in value and value['default_provider'] not in ('codex', 'claude'):
        raise ValueError('default_provider must be codex or claude')
    return value


def inspect(project):
    current = identity(project["workspace"])
    root = Path(current["workspace"])
    sources = []
    for name in project["rule_sources"]:
        path = Path(name)
        path = path if path.is_absolute() else root / path
        sources.append({"reference": name, "path": str(path.resolve()), "exists": path.is_file()})
    package = root / "package.json"
    package_info = None
    if package.is_file():
        try:
            data = json.loads(package.read_text(encoding="utf-8-sig"))
            package_info = {"package_manager": data.get("packageManager"),
                            "script_names": sorted(data.get("scripts", {}))}
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            package_info = {"read_error": str(exc)}
    mismatch = [key for key in current if os.path.normcase(current[key]) != os.path.normcase(project[key])]
    return {"status": "inspected", "project": project, "observed_identity": current,
            "identity_matches": not mismatch, "identity_mismatches": mismatch,
            "head": _git(root, "rev-parse", "HEAD"),
            "branch": _git(root, "branch", "--show-current"),
            # Status can invoke configured clean/process filters; onboarding must not.
            "working_changes": None, "working_changes_observation": "not_checked",
            "rule_sources": sources, "package": package_info,
            "environment": {"platform": sys.platform, "python": sys.version.split()[0],
                "provider_commands": {name: shutil.which(name) for name in ("codex", "claude")},
                "provider_authentication": "not_checked"},
            "capabilities": {"registration": True, "read_only_inspection": True,
                "external_task_execution": "execution" in project,
                "project_acceptance_adapter": bool(project.get("execution", {}).get("accept_result_argv")),
                "goal_team_orchestration": "execution" in project},
            "next_work": ["Select the project execution owner and retain its existing preflight before launch",
                          "Goal/team/review use the selected project context; provider authentication and live execution remain unverified",
                          "Project-owned acceptance is separate from candidate_verified; configure an observation hook or accept manually"],
            "scope": "No model calls, project commands, task launch, installs, Git writes or deployment"}


def register(workspace, config_path, rules=None):
    path = Path(config_path).resolve()
    proposed = descriptor(workspace, path, rules)
    # Inspect before publication, so a missing Git workspace creates no descriptor.
    report = inspect(proposed)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(proposed, ensure_ascii=False, indent=2) + "\n")
        created = True
    except FileExistsError:
        if load(path) != proposed:
            raise ValueError("descriptor already exists with different settings; inspect/edit that file rather than overwrite it")
        created = False
    return {**report, "status": "registered", "config_path": str(path), "created": created}


def add_arguments(parser):
    parser.add_argument("--foundry-project", choices=("inspect", "register", "check", "status"),
                        help="project onboarding and read-only status; never launches tasks")
    parser.add_argument("--project-config", help="descriptor path for onboarding or project --order/--resume")
    parser.add_argument("--status-evidence-root", help="existing evidence root for --foundry-project status --worktree (adapter journal observation)")
    parser.add_argument('--status-goal', help='exact goal directory for compact read-only completion observation')
    parser.add_argument('--status-dispatch', help='exact dispatch package receipt for read-only completion observation')
    parser.add_argument('--follow-up-plan', action='store_true', help='include optional heartbeat polling plan; requires explicit user request, not an event callback')
    parser.add_argument("--project-rule", action="append", default=[],
                        help="rule file reference for inspect/register; repeat for multiple entry files")
    parser.add_argument("--project-preflight-input", metavar="JSON_FILE",
                        help="project preflight JSON; on goal recovery explicitly replaces resume inputs only")
    parser.add_argument("--project-resume-preflight-input", metavar="JSON_FILE",
                        help="project-owned inputs for goal worker resumes; omission retains initial inputs")


def handle(args, parser):
    if args.follow_up_plan and not (args.status_goal or args.status_dispatch):
        parser.error('--follow-up-plan requires exact --status-goal or --status-dispatch')
    if args.status_goal or args.status_dispatch:
        if args.foundry_project != 'status' or any((args.project_config, args.status_evidence_root,
                args.worktree, args.order, args.resume, args.steer, args.foundry_goal,
                args.foundry_review, args.foundry_analyze, args.foundry_usage, args.recover_goal,
                args.handoff_provider, args.project_rule, args.project_preflight_input,
                args.project_resume_preflight_input)) or (args.status_goal and args.status_dispatch):
            parser.error('follow-up status requires --foundry-project status and one exact goal or dispatch receipt')
        from ._followup import status as goal_status, dispatch_status
        try:
            report = (goal_status(args.status_goal, plan=args.follow_up_plan) if args.status_goal
                      else dispatch_status(args.status_dispatch, plan=args.follow_up_plan))
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            report = {'status': 'inspection_failed', 'error': str(exc), 'read_only': True}
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 2 if report['status'] == 'inspection_failed' else 0
    if (args.project_preflight_input or args.project_resume_preflight_input) and (args.foundry_project or not args.project_config):
        parser.error("--project-preflight-input requires project --order/--resume, not onboarding")
    if not args.foundry_project:
        if args.status_evidence_root:
            parser.error('--status-evidence-root requires --foundry-project status')
        if args.project_rule or (args.project_config and not (args.order or args.resume or args.steer)):
            parser.error("--project-rule requires onboarding; --project-config requires onboarding or --order/--resume/--steer")
        return None
    if any((args.order, args.resume, args.steer, args.foundry_goal, args.foundry_review,
            args.foundry_analyze, args.foundry_usage, args.recover_goal, args.handoff_provider)):
        parser.error("project onboarding is separate from task execution; remove task-operation arguments")
    mode = args.foundry_project
    workspace_status = mode == 'status' and not args.project_config and args.worktree and args.status_evidence_root
    if args.status_evidence_root and not workspace_status:
        parser.error('--status-evidence-root requires status with --worktree and without --project-config')
    if mode in {"register", "check", "status"} and not args.project_config and not workspace_status:
        parser.error("register/check require --project-config")
    if mode in {"register", "inspect"} and not args.worktree:
        parser.error("inspect/register require --worktree")
    if mode in {"check", "status"} and (args.worktree or args.project_rule) and not workspace_status:
        parser.error("check uses the workspace and rules recorded in --project-config")
    try:
        if mode == "status":
            from ._project_status import status
            report = status(args.project_config, args.tag or None,
                            workspace=args.worktree, evidence_root=args.status_evidence_root)
            print(json.dumps(report, ensure_ascii=True, indent=2))
            return 0 if not report['errors'] else 2
        elif mode == "register":
            report = register(args.worktree, args.project_config, args.project_rule)
        elif mode == "check":
            report = inspect(load(args.project_config))
        else:
            report = inspect(descriptor(args.worktree, args.project_config or Path.cwd() / "foundry-project.json", args.project_rule))
    except (OSError, ValueError) as exc:
        report = {"status": "inspection_failed", "error": str(exc), "external_task_execution": False}
        print(json.dumps(report, ensure_ascii=True))
        return 2
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["identity_matches"] and all(r["exists"] for r in report["rule_sources"]) else 2
