"""Project-owned execution inputs for the existing dispatcher, not a launcher.

The explicit order/correction is the task source. The existing intent journal
owns execution state; foreign projects do not need a second PORTFOLIO registry.
"""
from dataclasses import dataclass, field, replace
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess

from ._foundry_project import identity, load
from ._task_identity import normalized_line
from ._execution_profile import profiles


class AdmissionRefused(SystemExit):
    """A completed project judgement, distinct from an interrupted gate."""

    def __init__(self, message, *, receipt, instructions):
        super().__init__(message)
        path = Path(receipt).resolve(strict=True)
        self.feedback = {'kind': 'assignment_refused', 'judgement': message,
                         'receipt': {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()},
                         'instructions_sha256': hashlib.sha256(instructions.encode('utf-8')).hexdigest()}
        try:
            result = json.loads(path.read_bytes()).get('result')
            if isinstance(result, dict):
                self.feedback['refused_judgements'] = {name: row for name, row in result.items()
                    if isinstance(row, dict) and row.get('status') == 'REFUSE'}
        except (ValueError, AttributeError):
            pass  # Legacy project receipts retain exact-instruction comparison.


@dataclass(frozen=True)
class ProjectExecution:
    recovery_source_verifier = None

    path: Path
    sha256: str
    project: dict
    preflight_input_json: str = "{}"
    execution_profiles_json: str = field(default_factory=lambda: json.dumps(profiles(), sort_keys=True))
    resume_preflight_input_json: str | None = None

    @property
    def context(self):
        from ._project_context import ProjectContext
        return ProjectContext.descriptor_context(self)

    @property
    def default_provider(self):
        return self.project.get('default_provider')

    @property
    def default_disposable_scratch(self):
        return self.project.get('default_disposable_scratch', False)

    def execution_profile(self, provider):
        return self.context.record()["execution_profiles"][provider]

    @contextmanager
    def launch_scope(self):
        """Apply descriptor-owned launch inputs through the existing launch capture."""
        environment = {key: json.dumps(value) if isinstance(value, list) else str(value)
                       for key, value in self.project.get("launch", {}).items()}
        previous = {key: os.environ.get(key) for key in environment}
        os.environ.update(environment)
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    @property
    def catalogue_cache(self):
        return self.common_dir / "pdcl-dispatch" / "model-catalogue.json"

    def record(self):
        return {"context": self.context.record(), "preflight_input": self.preflight_input,
                **({"resume_preflight_input": json.loads(self.resume_preflight_input_json)}
                   if self.resume_preflight_input_json is not None else {})}

    @property
    def preflight_input(self):
        # Return a copy; editing the source file or a caller's dictionary cannot
        # change the already selected invocation.
        return json.loads(self.preflight_input_json)

    @property
    def common_dir(self):
        return Path(self.project["git_common_dir"])

    @property
    def workspace(self):
        return Path(self.project["workspace"])

    @property
    def repository_reader(self):
        from ._repository_observation import git
        return git

    def evidence(self, tag):
        return Path(self.project["evidence_root"]) / normalized_line(tag)

    def steer_roots(self):
        return Path(self.project["evidence_root"]), self.workspace.parent

    def steer_admission(self, text, tag, worktree):
        # An in-flight correction retains the admitted task/workspace. Launch
        # preflight (often clean-tree/base-commit checks) is not a live-message gate.
        self.require_task(tag, worktree, mode="steer")

    def require_task(self, tag, worktree, *, mode="dispatch", recovery=None):
        self.verify_unchanged()
        normalized_line(tag)
        if Path(worktree).resolve() != self.workspace.resolve():
            raise ValueError("task workspace differs from project descriptor")
        if recovery is not None:
            from ._project_recovery import require_context
            require_context(self, recovery)

    def verify_unchanged(self):
        if hashlib.sha256(self.path.read_bytes()).hexdigest() != self.sha256:
            raise ValueError("project descriptor changed during dispatch")
        current = identity(self.workspace)
        if any(current[key] != self.project[key] for key in current):
            raise ValueError("project repository identity changed")

    def execution_plan_prompt(self, text, decision):
        from ._execution_plan_prompt import execution_plan_prompt
        return execution_plan_prompt(text, decision)

    def review_context(self, folder, *, task, subject, env):
        from ._project_hooks import observe
        return observe(self, "review_context", folder, task=task, subject=subject, env=env)

    def accept_result(self, folder, *, task, subject, env):
        from ._project_hooks import observe
        return observe(self, "accept_result", folder, task=task, subject=subject, env=env)

    def prompt(self, text):
        references = [str((self.workspace / name).resolve()) for name in self.project["rule_sources"]]
        return (text + "\n\nPROJECT EXECUTION CONTEXT\n" + json.dumps({
            "project_id": self.project["project_id"], "workspace": str(self.workspace),
            "rule_sources": references}, ensure_ascii=False) +
            "\nRead the project rule entries before editing, reusing unchanged entries already "
            "present in context. Follow referenced guidance for the current task: locate relevant "
            "headings or symbols first, then read the required sections and source ranges. Expand "
            "when evidence is missing or the project requires full coverage. Keep the combined "
            "returned text of each tool call within its output budget; separate per-command limits "
            "do not enlarge the enclosing call's budget. Avoid bundling whole documents and source "
            "files into one response. Preserve large results in task evidence and return the needed "
            "excerpts with their paths and ranges. After truncation, retrieve only the missing "
            "ranges needed for the task; reuse portions already received until they change. "
            "The order defines this task; the project's own rules define tests, review, "
            "handoff and integration. Do not substitute the tool repository's policies. "
            "Report evidence and remaining work; process completion is not project acceptance.\n")

    def preflight(self, tag, text, *, operation, env, execution_intent_id=None,
                  order=None, worktree=None, launcher="exec", predispatch_receipt=None,
                  answer_from=None, answer_file=None):
        """Run only the project-selected check, with argv and JSON stdin, no shell.

        A zero exit means its preflight passed, never that the task is accepted.
        Empty argv means the project has no separate preflight command.
        """
        if operation == "dispatch" and predispatch_receipt is not None:
            raise ValueError("project admission does not consume PDCL predispatch receipts")
        argv = self.project["execution"]["preflight_argv"]
        if not argv:
            return
        folder = self.evidence(tag)
        folder.mkdir(parents=True, exist_ok=True)
        inputs = (json.loads(self.resume_preflight_input_json)
                  if operation == "resume" and self.resume_preflight_input_json is not None
                  else self.preflight_input)
        request = {"workspace": str(self.workspace), "tag": tag,
                   "operation": operation, "instructions": text,
                   "preflight_input": inputs,
                   "execution_intent_id": execution_intent_id}
        result = subprocess.run(argv, cwd=self.workspace, input=json.dumps(request),
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        # Each attempt remains observable without overwriting an earlier refusal.
        import uuid
        receipt = folder / ("project-preflight-" + uuid.uuid4().hex + ".json")
        receipt.write_text(json.dumps({"argv": argv, "operation": operation,
            "execution_intent_id": execution_intent_id,
            "preflight_input": inputs,
            "instructions_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "returncode": result.returncode, "stdout": result.stdout,
            "stderr": result.stderr}, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        if result.returncode:
            raise ValueError("project preflight did not pass; see " + str(receipt))


def goal_defaults(project):
    """Read new-goal defaults from descriptors or their project-owned adapters."""
    provider = getattr(project, 'default_provider', None)
    scratch = getattr(project, 'default_disposable_scratch', False)
    if provider not in (None, 'codex', 'claude'):
        raise ValueError('default_provider must be codex or claude')
    if type(scratch) is not bool:
        raise ValueError('default_disposable_scratch must be a boolean')
    return {'provider': provider, 'disposable_scratch': scratch}


def validate_execution(project):
    execution = project.get("execution")
    if not isinstance(execution, dict) or execution.get("owner") != "foundry":
        raise ValueError("project execution owner is not configured; reconcile its existing dispatch entry first")
    argv = execution.get("preflight_argv")
    if not isinstance(argv, list) or any(not isinstance(item, str) or not item for item in argv):
        raise ValueError("execution.preflight_argv must be an argv list (empty for no separate check)")
    for name in ("review_context_argv", "accept_result_argv"):
        if name in execution and (not isinstance(execution[name], list) or
                any(not isinstance(item, str) or not item for item in execution[name])):
            raise ValueError("execution." + name + " must be an argv list")


def open_project(path, *, preflight_input=None, resume_preflight_input=None):
    path = Path(path).resolve(strict=True)
    raw = path.read_bytes()
    project = load(path)
    validate_execution(project)
    if preflight_input is None:
        preflight_input = {}
    if not isinstance(preflight_input, dict):
        raise ValueError("project preflight input must be a JSON object")
    frozen_input = json.dumps(preflight_input, sort_keys=True, ensure_ascii=True, allow_nan=False,
                              separators=(",", ":"))
    selected = ProjectExecution(path, hashlib.sha256(raw).hexdigest(), project, frozen_input)
    if resume_preflight_input is not None:
        if not isinstance(resume_preflight_input, dict):
            raise ValueError("project resume preflight input must be a JSON object")
        selected = replace(selected, resume_preflight_input_json=json.dumps(
            resume_preflight_input, sort_keys=True, ensure_ascii=True, allow_nan=False))
    selected.verify_unchanged()
    return selected


def select_cli(args, parser):
    """Choose project context before PDCL route policy, never start an executor."""
    if not args.project_config:
        return None
    if (sum(bool(mode) for mode in (args.order, args.resume, args.steer)) != 1 or args.foundry_analyze or args.foundry_usage
            or args.answer_from or args.answer_file or args.predispatch_receipt):
        parser.error("project execution supports order/resume/steer and goal/review; project rules own admission and acceptance")
    goal_only = ("amend_from", "coding_session", "review_base", "review_head", "goal_evidence",
                 "analysis_evidence", "analyst_backend", "analyst_exe", "analyst_model", "analyst_effort",
                 "review_profile", "auto_amend_model", "auto_amend_effort", "compute_profile",
                 "task_kind", "print_staffing", "team_profile", "manager_profile",
                 "handoff_provider", "handoff_role", "migrate_runtime")
    if not (args.foundry_goal or args.foundry_review) and any(getattr(args, name, None) is not None and getattr(args, name) is not False for name in goal_only):
        parser.error("project direct execution does not yet support goal/review/team options")
    if args.resume and not args.tag:
        parser.error("project resume requires the original --tag")
    try:
        input_path = getattr(args, "project_preflight_input", None)
        values = json.loads(Path(input_path).read_text(encoding="utf-8-sig")) if input_path else None
        resume_path = getattr(args, "project_resume_preflight_input", None)
        resume_values = json.loads(Path(resume_path).read_text(encoding="utf-8-sig")) if resume_path else None
        if resume_path and not isinstance(resume_values, dict):
            raise ValueError("project resume preflight input must be a JSON object")
        selected = open_project(args.project_config, preflight_input=values, resume_preflight_input=resume_values)
        if input_path and not isinstance(values, dict):
            raise ValueError("project preflight input must be a JSON object")
        if args.worktree and Path(args.worktree).resolve() != selected.workspace.resolve():
            raise ValueError("--worktree differs from project descriptor")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    args.worktree = str(selected.workspace)
    return selected


def from_record(record, *, selected=None):
    if not isinstance(record, dict) or not isinstance(record.get("context"), dict):
        raise ValueError("retained run has no descriptor project context")
    context = record["context"]
    if selected is None:
        selected = open_project(context["descriptor"], preflight_input=record.get("preflight_input"),
                                resume_preflight_input=record.get("resume_preflight_input"))
    selected = replace(selected, execution_profiles_json=json.dumps(context["execution_profiles"], sort_keys=True))
    if selected.context.record() != context:
        raise ValueError("project context differs from retained invocation")
    return selected
