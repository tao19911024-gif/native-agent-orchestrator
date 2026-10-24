"""Run one repository-owned coding turn and two independent repository reviews.

The task is the order text. Foundry records execution and review evidence, but
does not choose sources, a plan, or test commands for any agent.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess, sys, uuid
from contextlib import contextmanager
from contextvars import ContextVar

_owned_run_locks = ContextVar('foundry_owned_run_locks', default=())

from ._compute_selection import compute_kwargs, resolve_goal_compute, resolve_goal_profile, freeze_roles, compute_for_kind
from ._compute_selection import resolve_native_team, is_managed, team_kwargs, review_assignments
from .foundry_native_support import resolve_executable
from ._foundry_response_contract import _BLOCKING_SEVERITIES
from .foundry_collaboration import response_contract, validate_response_shape
from .foundry_native import NativeAnalyst
from . import _foundry_review_group as review_group
from . import _foundry_goal_usage as goal_usage
from . import _foundry_goal_auto as goal_auto
from . import _foundry_goal_execution as goal_execution
from . import _foundry_goal_continuation as goal_continuation
from . import _task_identity
from . import _foundry_order_revision as order_revision
from ._foundry_goal_delivery import STOP_OUTCOMES, artifacts, candidate, incomplete, retained_stop_cause, stop_cause


class GoalBlocked(ValueError):
    def __init__(self, message, status="execution_unknown", *, workspace=None):
        super().__init__(message)
        self.status = status
        self.workspace = workspace


class InvalidReview(GoalBlocked):
    """The response needs replacement; the candidate itself remains usable."""


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _write(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with temporary.open("xb") as stream:
        stream.write(_json_bytes(value)); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def _fact(path):
    path = Path(path).resolve(strict=True)
    with path.open("rb") as stream:
        return {"path": str(path), "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}


def _publish_outcome(folder, outcome):
    """Publish completion before disposing explicitly registered task storage."""
    _write(folder / 'outcome.json', outcome)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    if 'disposable_scratch' not in manifest:
        return
    from ._goal_scratch import reclaim
    usage_path = folder / 'usage.json'
    usage = json.loads(usage_path.read_bytes()) if usage_path.exists() else {}
    usage['scratch'] = reclaim(folder, manifest, outcome, usage.get('scratch'))
    _write(usage_path, usage)
    outcome['task_usage'] = _fact(usage_path)
    outcome['delivery_report'] = goal_usage.delivery_report(outcome, usage, manifest.get('delivery_context'))
    _write(folder / 'outcome.json', outcome)


@contextmanager
def _run_lock(folder, *, reuse_owned=False):
    """One controller per evidence directory; released by process exit too."""
    folder = Path(folder).resolve()
    if reuse_owned and any(lease['active'] and lease['pid'] == os.getpid()
                           and lease['folder'] == folder for lease in _owned_run_locks.get()):
        # LiveEvents deliberately copies controller context to its serialized
        # callback and joins it before releasing ownership. Never infer ownership
        # from a PID or a handoff marker alone, or reuse an expired copied scope.
        yield
        return
    folder.parent.mkdir(parents=True, exist_ok=True)
    with folder.with_name("." + folder.name + ".owner.lock").open("a+b") as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b"\0"); handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise GoalBlocked("this Foundry round still has a running controller") from exc
        from ._followup import goal_scope
        scope = goal_scope.set(str(folder))
        lease = {'folder': folder, 'pid': os.getpid(), 'active': True}
        ownership = _owned_run_locks.set((*_owned_run_locks.get(), lease))
        try:
            try:
                os.utime(handle.name, None)  # Identity of this lock ownership interval.
            except OSError:
                pass  # Observability must not change execution admission.
            yield
        finally:
            lease['active'] = False
            _owned_run_locks.reset(ownership)
            goal_scope.reset(scope)
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


_GIT_STALL_SECONDS = 300


def _git_without_deadline(command, tree):
    """Drain both Git pipes; only observed output renews the inactivity interval."""
    import queue
    import threading
    import time

    events = queue.Queue()
    output = [[], []]
    def read(stream, index):
        try:
            while chunk := stream.read1(65536):
                events.put((index, chunk))
        finally:
            stream.close()
            events.put((index, None))

    with subprocess.Popen(command, cwd=tree, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        readers = [threading.Thread(target=read, args=(stream, index))
                   for index, stream in enumerate((process.stdout, process.stderr))]
        for reader in readers:
            reader.start()
        expires = time.monotonic() + _GIT_STALL_SECONDS
        try:
            open_streams = 2
            while open_streams:
                index, chunk = events.get(timeout=max(0, expires - time.monotonic()))
                if chunk is None:
                    open_streams -= 1
                else:
                    output[index].append(chunk)
                    expires = time.monotonic() + _GIT_STALL_SECONDS
            process.wait(timeout=max(0, expires - time.monotonic()))
        except (queue.Empty, subprocess.TimeoutExpired) as exc:
            process.kill()
            process.wait()
            raise subprocess.TimeoutExpired(command, _GIT_STALL_SECONDS) from exc
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            for reader in readers:
                reader.join()
    return subprocess.CompletedProcess(command, process.returncode, *(b''.join(parts) for parts in output))


def _git(tree, *args):
    from . import _task_deadline as deadline
    timeout = deadline.remaining()
    try:
        deadline.check()
        command = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args]
        result = (_git_without_deadline(command, tree) if timeout is None else
                  subprocess.run(command, cwd=tree, capture_output=True, timeout=deadline.remaining()))
    except (subprocess.TimeoutExpired, TimeoutError) as exc:
        reason = (f"git observation stalled for {_GIT_STALL_SECONDS:g}s without output: "
                  if timeout is None else "git observation reached task deadline: ")
        raise GoalBlocked(reason + " ".join(args)) from exc
    if result.returncode:
        raise GoalBlocked("git observation failed: " + result.stderr.decode("utf-8", "replace")[:1000])
    return result.stdout


def _candidate(tree, base=None):
    return candidate(sys.modules[__name__], tree, base)


def _blocking(findings):
    return any(item["severity"] in _BLOCKING_SEVERITIES for item in findings)


def _referenced_fields(context):
    return {field: '/context/' + field for field in
            ('original_order', 'changed_files', 'project_record', 'execution_decision')
            if field in context and len(json.dumps(context[field], ensure_ascii=False).encode('utf-8')) > 4096}


def _validate_review(value, context, tree, *, retained_only=False):
    validate_response_shape(value, response_contract("review", correction_routing=context.get("correction_routing") is True, review_field_reads=context.get("review_field_reads") is True))
    if context.get('review_field_reads') is True:
        reads = value.get('read_fields', [])
        for field, pointer in _referenced_fields(context).items():
            expected = {'pointer': pointer, 'sha256': _sha(_json_bytes(context[field]))}
            if expected not in reads:
                raise InvalidReview('review required field unread or digest differs: ' + pointer, 'review_blocked')
    if (value["candidate_commit"] != context["candidate_commit"]
            or value["packet_sha256"] != _sha(_json_bytes(context))):
        raise InvalidReview("review candidate/context identity differs", "review_blocked")
    if value["verdict"] == "PASS" and _blocking(value["findings"]):
        raise InvalidReview("PASS review carries a blocking finding", "review_blocked")
    if value["verdict"] == "BLOCK" and not _blocking(value["findings"]):
        raise InvalidReview("BLOCK review lacks a blocking finding", "review_blocked")
    if not retained_only and _candidate(tree) != context["candidate_commit"]:
        raise GoalBlocked("candidate changed during independent review", "review_blocked")


def _role_request(run_id, name, packet, *, context_reference=None, read_policy=None):
    request = {"kind": "review", "packet": packet,
            **({"correction_routing": True} if packet.get("correction_routing") is True else {}),
            "response_contract": response_contract("review", correction_routing=packet.get("correction_routing") is True, review_field_reads=packet.get("review_field_reads") is True),
            **({"review_field_reads": True} if packet.get("review_field_reads") is True else {}),
            "call_id": _sha((run_id + ":" + name).encode()),
            "packet_sha256": _sha(_json_bytes(packet))}
    if read_policy is not None:
        request['review_read_policy'] = read_policy
    if context_reference is not None:
        # Select presentation at the request boundary so native admission and
        # recovery compare the same envelope. The complete context hash stays.
        fields = _referenced_fields(packet)
        if fields:
            request.update(packet={key: value for key, value in packet.items() if key not in fields},
                           context_reference=context_reference, context_fields=fields)
    return request


def _runtime_identity():
    """Small compatibility surface used by existing evidence readers."""
    return {"files": {Path(__file__).name: _sha(Path(__file__).read_bytes())}}


def _review_range(tree, base, head, *, retained_only=False):
    base, head = (_git(tree, "rev-parse", "--verify", "--end-of-options", value + "^{commit}")
                  .decode().strip() for value in (base, head))
    if base == head:
        raise GoalBlocked("review range is empty: base equals head")
    try:
        _git(tree, "merge-base", "--is-ancestor", base, head)
    except GoalBlocked as exc:
        raise GoalBlocked("review base is not an ancestor of named head: " + str(exc)) from exc
    if not _git(tree, "diff", "--name-only", "-z", base, head):
        raise GoalBlocked("review range has an empty diff")
    if not retained_only and _candidate(tree) != head:
        raise GoalBlocked("candidate HEAD differs from named head")
    return base, head


def _amendment(order, tree, outcome_path, session, *, operator_additions=None):
    """Read an explicitly selected completed review for this unchanged candidate."""
    path = Path(outcome_path).resolve(strict=True)
    prior = json.loads(path.read_bytes())
    manifest_path = path.with_name("manifest.json")
    manifest = json.loads(manifest_path.read_bytes())
    status = prior.get("status")
    if status not in {"review_blocked", "candidate_verified"}:
        raise GoalBlocked("amendment requires a completed review_blocked or candidate_verified outcome")
    if (not order_revision.matches(sys.modules[__name__], order, manifest)
            or Path(manifest["worktree"]).resolve() != tree):
        raise GoalBlocked("amendment order or worktree differs from prior review")
    base, head = _review_range(tree, manifest["base_commit"], prior["candidate_commit"])
    reviews = prior.get("reviews", [])
    if len(reviews) != 2:
        raise GoalBlocked("amendment requires the completed pair of reviews")
    for review in reviews:
        validate_response_shape(review, response_contract("review", correction_routing="correction_guidance" in review, review_field_reads="read_fields" in review))
        if review["candidate_commit"] != head:
            raise GoalBlocked("amendment finding belongs to another candidate")
        if (review["verdict"] == "BLOCK") != _blocking(review["findings"]):
            raise GoalBlocked("amendment verdict and blocking findings disagree")
    if len({r["packet_sha256"] for r in reviews}) != 1:
        raise GoalBlocked("amendment reviews refer to different packets")
    has_block = any(r["verdict"] == "BLOCK" for r in reviews) or bool(prior.get("host_findings"))
    if has_block != (status == "review_blocked"):
        raise GoalBlocked("amendment outcome disagrees with review verdicts")
    if operator_additions is not None and (not isinstance(operator_additions, str) or not operator_additions.strip()):
        raise ValueError('operator additions must be nonempty text')
    if not any(r["findings"] for r in reviews) and not prior.get("host_findings") and not operator_additions:
        raise GoalBlocked("amendment has no recorded findings; add --prompt-file for operator-directed work on this candidate")
    if not isinstance(session, str) or not session.strip():
        raise ValueError("amendment requires the existing coding session")
    coding, rounds = prior.get("coding"), manifest.get("rounds")
    return {"prior_outcome": _fact(path), "prior_manifest": _fact(manifest_path),
            "candidate_commit": head, "base_commit": base, "coding_session": session,
            "reviews": reviews, "host_findings": prior.get("host_findings", []), "prior_status": status,
            **({'operator_additions': operator_additions} if operator_additions is not None else {}),
            "retained_work": {
                "source_candidate": head,
                "recorded_coding_session": prior.get("coding_session"),
                "coding_evidence": coding.get("evidence", []) if isinstance(coding, dict) else [],
                "usage": prior.get("task_usage"),
                "review_checkpoint": manifest.get("review_checkpoint"),
                "last_round": rounds[-1] if isinstance(rounds, list) and rounds else None}}


def _review_focus(amendment):
    """Render each finding once, retaining its reviewers and immutable sources."""
    findings = {}
    for index, review in [*enumerate(amendment["reviews"]), ("host", {"findings": amendment.get("host_findings", [])})]:
        for finding in review["findings"]:
            key = _json_bytes(finding)
            item = findings.setdefault(key, {"finding": finding, "reviewers": []})
            item["reviewers"].append(index)
    return {key: amendment[key] for key in (
        "prior_outcome", "prior_manifest", "candidate_commit", "base_commit")} | {
        "findings": list(findings.values()),
        "retained_work": amendment.get("retained_work", {}),
        "scope": "Prior findings are leads, not a substitute for independent judgment of the new candidate."}


def _amendment_text(order, amendment, *, project=None):
    delivery = "Follow project rules for handoff and commit ownership " if project else "Commit the resulting changes "
    return ("Continue the existing task on candidate " + amendment["candidate_commit"] + ". "
            "Address the concrete review findings below, preserving completed work. "
            "Use focused checks for changed or uncertain facts. " + delivery +
            "and report the disposition of each finding. The operator explicitly requested "
            "this continuation, including recorded findings in PASS reviews; the prior verdict "
            "is unchanged. If a finding is informational, report the remaining limitation; "
            "do not expand the task to eliminate it. If a finding is mistaken, "
            "give repository evidence rather than making an unnecessary change.\n\n"
            "Original task reference (all original requirements still apply; read if context is missing):\n" +
            json.dumps(_fact(order), ensure_ascii=False, separators=(",", ":")) +
            "\n\nPrior findings and retained work references (not a new task or live member roster):\n" +
            json.dumps(_review_focus(amendment), ensure_ascii=False, separators=(",", ":")) +
            "\nUse the existing session's decisions and completed work first. If context is missing, "
            "read the needed parts of the prior coding report and referenced records instead of "
            "repeating investigation. A review checkpoint retains coding_usage.child_agents; "
            "usage retains coding.observations. Their session_id, parent_binding, model_observation "
            "and source fields can locate previously observed members and work. For automatic "
            "results, follow the last_round references when needed. These observations do not "
            "establish who is currently running or whether a member can be continued. Check "
            "native member availability before using supported follow-up on the original member; "
            "a queued message alone does not restart an idle member. If unavailable, give a "
            "replacement the retained results, decisions, evidence references and only the "
            "unfinished scope. Do not reconstruct completed work to refill context. Reuse "
            "findings while their relevant source versions, dependencies and assumptions apply; "
            "inspect changed inputs, including uncommitted edits, before relying on old results. "
            "An unrelated commit alone does not invalidate them. Missing optional references "
            "are context gaps to resolve from the original session and reports, not reasons "
            "to restart the task or claim the work completed. Preserve decisions and remaining "
            "questions in the existing task notes or final coding report for the next continuation.\n")


def amendment_prompt(order, amendment, execution_decision, predecessor=None, *, project=None):
    """Single prompt authority for ordinary and explicitly recovered amendments."""
    from . import _foundry_dispatch as dispatch
    text = _amendment_text(order, amendment, project=project)
    if amendment.get('operator_additions'):
        text += '\n\nOperator additions for this continuation:\n' + amendment['operator_additions']
    if predecessor is not None:
        text = ("Continue after interrupted execution " + predecessor +
                ". Its effects remain unknown; inspect retained work before continuing.\n\n" + text)
    return dispatch._project_adapter(project).execution_plan_prompt(text, execution_decision)


def run_review(order: Path, evidence_dir: Path, executable: Path, *,
               target_worktree: Path, base_commit: str, candidate_commit: str,
               backend="codex", analyst_model=None, analyst_effort=None, reviewers=None, project_record=None,
               review_author_models=None):
    """Judge a named committed candidate without admitting or launching coding."""
    if review_author_models:
        from ._compute_selection import independent_reviewers
        from ._model_catalogue import model
        explicit = reviewers is not None or analyst_model is not None
        current = reviewers or [{'backend': backend, 'model': analyst_model or model('opus' if backend == 'claude' else 'astra'),
                                'effort': analyst_effort or 'high'}] * 2
        reviewers = independent_reviewers(current, review_author_models, explicit=explicit, available_backends={backend})
    return _run(order, evidence_dir, executable, target_worktree=target_worktree,
                review_range=(base_commit, candidate_commit), backend=backend,
                review_author_models=review_author_models,
                **({"project_record": project_record} if project_record is not None else {}),
                analyst_model=analyst_model, analyst_effort=analyst_effort,
                **({"reviewers": reviewers} if reviewers is not None else {}))


def amendment_manifest(outcome_path, coding_session=None):
    """Read effective settings from the selected review's recorded final round."""
    path = Path(outcome_path)
    manifest = json.loads(path.with_name("manifest.json").read_bytes())
    if (coding_session and manifest.get('delivery_context', {}).get('mode') == 'review-only'
            and not manifest.get('manager_state')):
        # A standalone review has no worker ownership. Resolve the completed
        # candidate within this task's evidence directory, not a host-wide scan.
        candidate = json.loads(path.read_bytes()).get('candidate_commit')
        roots = path.resolve().parent.parent
        matches = []
        for record in [*roots.glob('*/outcome.json'), *roots.glob('*/round-*/outcome.json')]:
            if record.resolve() == path.resolve():
                continue
            try:
                outcome = json.loads(record.read_bytes())
                observed_candidate = outcome.get('candidate_commit') or (outcome.get('candidate_closure') or {}).get('candidate_commit')
                if outcome.get('coding_session') != coding_session or observed_candidate != candidate:
                    continue
                source = amendment_manifest(record)
                if (source.get('order') != manifest.get('order') or
                        source.get('worktree') != manifest.get('worktree') or not source.get('manager_state')):
                    continue
                matches.append((record.stat().st_mtime_ns, record, source))
            except (OSError, ValueError, KeyError):
                continue  # An unrelated/incomplete record does not own this session.
        if matches:
            # Same session/candidate may be handed back more than once. Prefer
            # the latest completed outcome so its most recent retained manager
            # state (including measured quota fallback) owns the amendment.
            # mtime is only a tiebreak after session, candidate, order and tree match.
            _, record, source = max(matches, key=lambda row: row[0])
            for key in ('manager_state', 'native_team', 'task_staffing', 'compute_assignment',
                        'execution_decision', 'coding_launcher', 'task_deadline_at', 'predispatch'):
                if key in source:
                    manifest[key] = source[key]
            manifest['requested_compute'] = {**manifest.get('requested_compute', {}),
                'coding': source.get('requested_compute', {}).get('coding')}
            manifest['execution_decision'] = {**manifest.get('execution_decision', {}),
                                              'staffing_goal': _fact(record.with_name('manifest.json'))}
    if manifest.get("mode") != "auto-amend":
        return manifest
    rounds = manifest.get("rounds") or []
    if not rounds:
        raise ValueError("automatic goal has no recorded completed round to amend")
    ref = rounds[-1]["outcome"]
    final = json.loads(goal_usage._reference(ref))
    root = json.loads(path.read_bytes())
    if any(root.get(key) != final.get(key) for key in
           ("status", "candidate_commit", "coding_session", "reviews")):
        raise ValueError("automatic goal differs from its recorded final review")
    child = json.loads(Path(ref["path"]).with_name("manifest.json").read_bytes())
    if (order_revision.records(sys.modules[__name__], child)[0] != order_revision.records(sys.modules[__name__], manifest)[0]
            or any(child.get(key) != manifest.get(key) for key in ("worktree", "base_commit"))):
        raise ValueError("final round settings belong to a different goal")
    return child


def amendment_admission(manifest):
    """Follow recorded amendment manifests to the original admitted receipt."""
    seen = set()
    while manifest:
        if manifest.get("predispatch"):
            return manifest["predispatch"]
        prior = (manifest.get("amendment") or {}).get("prior_manifest")
        if not prior:
            return None
        if prior["path"] in seen:
            raise ValueError("amendment manifest lineage repeats")
        seen.add(prior["path"])
        manifest = json.loads(goal_usage._reference(prior))
    return None



def amendment_launcher(manifest):
    """Use the recorded owner, including checkpoints written before this field existed."""
    if manifest.get("coding_launcher"):
        return manifest["coding_launcher"]
    reference = manifest.get("coding_checkpoint")
    if reference:
        checkpoint = json.loads(goal_usage._reference(reference))
        return checkpoint.get("options", {}).get("coding_launcher")
    return None  # Legacy absence is not evidence of a particular transport.


def prepare_goal(order: Path, evidence_dir: Path, executable: Path, *,
             auto_amend_model=None, auto_amend_effort=None, compute_profile=None,
             team_profile=None, compute_source=None, task_kind=None, staffing_sources=None,
             review_source_detail=None, team_source_detail=None, admission_staffing=None,
             launcher_explicit=None, **kwargs):
    """Run normally, or explicitly continue recorded BLOCK/PASS review findings."""
    explicit_compute = bool(compute_profile or auto_amend_model or auto_amend_effort or "backend" in kwargs or any(kwargs.get(k) for k in
        ("coding_model", "coding_effort", "analyst_model", "analyst_effort", "role_compute", "reviewers")))
    explicit_launcher = ("coding_launcher" in kwargs if launcher_explicit is None else launcher_explicit)
    overrides = {"coding": bool(compute_profile or kwargs.get("coding_model") or kwargs.get("coding_effort") or explicit_launcher),
        "correction": bool(compute_profile or auto_amend_model or auto_amend_effort),
        "reviewers": bool(compute_profile or kwargs.get("reviewers") or kwargs.get("analyst_model") or
            kwargs.get("analyst_effort") or "backend" in kwargs),
        "team": team_profile is not None}
    plan, prior = None, {}
    if kwargs.get("amend_from"):
        prior = amendment_manifest(kwargs["amend_from"], kwargs.get('coding_session'))
        recorded_deadline = prior.get('task_deadline_at')
        if recorded_deadline is not None:
            kwargs['task_deadline_at'] = recorded_deadline
        if prior.get("project_record") is not None:
            from ._project_execution import from_record
            supplied = from_record(kwargs.get("project_record", prior["project_record"]))
            if from_record(prior["project_record"], selected=supplied).record() != prior["project_record"]:
                raise ValueError("amendment project input differs from original run")
            kwargs["project_record"] = prior["project_record"]
        if prior.get("task_staffing"):
            kwargs["task_staffing"] = dict(prior["task_staffing"])
            if prior.get("compute_assignment"):
                kwargs["compute_assignment"] = dict(prior["compute_assignment"])
            admission = amendment_admission(prior)
            if admission:
                kwargs["predispatch_receipt"] = Path(admission["path"])
        recorded_launcher = amendment_launcher(prior)
        if recorded_launcher and ((kwargs.get("coding_launcher", recorded_launcher) == "claude")
                                  != (recorded_launcher == "claude")):
            raise ValueError("amendment must retain the recorded coding provider")
        if recorded_launcher:
            kwargs.setdefault("coding_launcher", recorded_launcher)
        if prior.get('manager_state') is not None:
            kwargs.setdefault('manager_state', prior['manager_state'])
        prior_reviewers = prior.get("reviewers")
        changed_review_provider = (kwargs.get("backend") is not None and prior_reviewers is not None
            and any(row["backend"] != kwargs["backend"] for row in prior_reviewers))
        if (compute_profile is None and not changed_review_provider and
                not any(kwargs.get(k) for k in ("analyst_model", "analyst_effort", "role_compute"))):
            if prior_reviewers is not None:
                kwargs.setdefault("reviewers", prior_reviewers)
                if len({row["backend"] for row in prior_reviewers}) == 1:
                    kwargs.setdefault("backend", prior_reviewers[0]["backend"])
        if compute_profile is None:
            recorded = prior.get("requested_compute", {})
            for role, prefix in (("coding", "coding"), ("review", "analyst")):
                chosen = recorded.get(role) or {}
                provider_changed = role == "review" and kwargs.get("backend", chosen.get("backend")) != chosen.get("backend")
                if not provider_changed and not any(kwargs.get(prefix + "_" + k) for k in ("model", "effort")):
                    for key in ("model", "effort"):
                        if chosen.get(key):
                            kwargs[prefix + "_" + key] = chosen[key]
                if role == "review" and chosen.get("backend"):
                    kwargs.setdefault("backend", chosen["backend"])
    elif kwargs.get("predispatch_receipt"):
        from . import _foundry_dispatch as dispatch
        plan = dispatch._project_adapter(None).goal_execution_plan(
            order, kwargs.get("target_worktree"), kwargs["predispatch_receipt"])
    coding_backend = "claude" if kwargs.get("coding_launcher") == "claude" else "codex"
    decision = dict(prior.get("execution_decision", {}))
    if team_profile is None and not kwargs.get("amend_from") and "native_team" not in kwargs and plan:
        team_profile = plan["team"]["profile"]
        decision["team"] = {**plan["team"], "source": "admission_plan"}
    elif team_profile is not None:
        team_source = staffing_sources["team"] if staffing_sources else "explicit"
        decision["team"] = {"profile": team_profile, "source": team_source,
                            "reason": {"kind": "Task kind selects the team.",
                            "project": "Project default selects the team backend; task kind supplies its profile.",
                            "quota": "Codex runs ahead of its quota "
                            "window; the ui-live row selects the team."}.get(team_source) or ("Explicit coding provider "
                            "selects the team backend; task kind supplies its profile." if team_source_detail == {
                            "profile": "kind", "backend": "explicit"} else "Explicit team choice takes precedence over the admission recommendation.")}
    elif "native_team" in kwargs:
        decision["team"] = {"profile": (kwargs["native_team"] or {}).get("profile", "not_recorded"),
                            "source": "explicit", "reason":
                            "Explicit native team configuration takes precedence over planned or recorded settings."}
    if team_profile is not None:
        kwargs["native_team"] = resolve_native_team(team_profile, backend=coding_backend,
            managed=is_managed(
                kwargs.get('manager_profile'), kwargs.get('manager_state')))
        if team_profile == "adaptive" and not kwargs.get("amend_from"):
            from ._foundry_team_calibration import calibration_history
            tree = kwargs.get("target_worktree")
            if tree is None:
                if kwargs.get("project_record"):
                    from ._project_execution import from_record
                    tree = from_record(kwargs["project_record"]).workspace
                else:
                    from . import _foundry_dispatch as dispatch
                    tree = dispatch._project_adapter(None).goal_workspace(order)
            kwargs["native_team"]["calibration"] = calibration_history(order, tree,
                **({"project_record": kwargs["project_record"]} if kwargs.get("project_record") else {}))
    elif kwargs.get("amend_from") and "native_team" not in kwargs:
        kwargs.update(team_kwargs(prior.get("native_team")))
    if compute_profile is not None:
        expanded, assignment = resolve_goal_profile(compute_profile, coding_backend=coding_backend,
            team_profile=(kwargs.get("native_team") or {}).get("profile", team_profile), task_kind=task_kind,
            backend=kwargs.get("backend", "codex"), role_compute=kwargs.get("role_compute"),
            coding_model=kwargs.get("coding_model"), coding_effort=kwargs.get("coding_effort"),
            analyst_model=kwargs.get("analyst_model"), analyst_effort=kwargs.get("analyst_effort"),
            auto_amend_model=auto_amend_model, auto_amend_effort=auto_amend_effort)
        auto_amend_model = expanded.pop("auto_amend_model")
        auto_amend_effort = expanded.pop("auto_amend_effort", None)
        assignment["selection"] = {"source": compute_source or "explicit", "reason":
            f"Default profile '{compute_profile}' supplies roles without explicit overrides."
            if compute_source == "default" else "Explicit profile; explicit per-role model pairs take precedence."}
        if task_kind:
            assignment["selection"] = {"source": compute_source if compute_source in ("kind", "quota", "project") else "explicit",
                "reason": ("Project default selects the coding provider; task kind supplies other omitted choices."
                           if compute_source == 'project' else
                           "Task kind supplies omitted role choices; explicit model pairs take precedence.")}
            for name, role in assignment["roles"].items():
                if role["source"] == "profile":
                    role["source"] = ((staffing_sources or {}).get('reviewers' if name == 'review' else name, 'project')
                        if compute_source == 'project' else
                        compute_source if compute_source in ("kind", "quota") else "explicit")
        kwargs = {**kwargs, **expanded, "compute_assignment": assignment}
    elif prior.get("task_staffing") and kwargs.get("compute_assignment"):
        assignment = kwargs["compute_assignment"]
        roles = dict(assignment["roles"])
        for name, prefix in (("coding", "coding"), ("correction", "auto_amend")):
            if overrides[name]:
                model = kwargs.get(prefix + "_model") if name == "coding" else auto_amend_model
                effort = kwargs.get(prefix + "_effort") if name == "coding" else auto_amend_effort
                pair = compute_kwargs(model, effort, backend=coding_backend)
                if pair:
                    roles[name] = {"backend": coding_backend, **pair, "source": "explicit"}
        review_pair = compute_kwargs(kwargs.get("analyst_model"), kwargs.get("analyst_effort"),
            backend=kwargs.get("backend", "codex"))
        if overrides["reviewers"] and kwargs.get("reviewers") is None:
            roles["review"] = {"backend": kwargs.get("backend", "codex"), **review_pair,
                "source": "explicit"}
            assignment = {key: value for key, value in assignment.items() if key != "reviewers"}
        kwargs["compute_assignment"] = {**assignment, "roles": roles,
            **({"selection": {"source": "explicit", "reason":
                "Explicit amendment choices replace only their selected roles."}}
                if any(overrides[part] for part in ("coding", "correction", "reviewers")) else {}),
            **({"correction_policy": "explicit"} if auto_amend_model or auto_amend_effort else {})}
    if kwargs.get("reviewers") is not None:
        kwargs["reviewers"] = review_assignments(kwargs["reviewers"])
        if kwargs.get("compute_assignment") is not None and (not prior or overrides["reviewers"]):
            assignment = kwargs["compute_assignment"]
            reviewer_source = staffing_sources["reviewers"] if staffing_sources else "explicit"
            kwargs["compute_assignment"] = {**assignment,
                "roles": {key: row for key, row in assignment["roles"].items() if key != "review"},
                "reviewers": kwargs["reviewers"],
                "selection": {"source": assignment['selection']['source'] if compute_source == 'project' else reviewer_source,
                    "reason": assignment["selection"]["reason"] +
                    (" Task kind supplies independent reviewer assignments." if reviewer_source == "kind" else
                     " Explicit independent reviewer assignments replace the profile's review role.")}}
        decision["reviewers"] = kwargs["reviewers"]
    else:
        decision.pop("reviewers", None)
    decision.setdefault("team", {"profile": (kwargs.get("native_team") or {}).get("profile", "not_recorded"),
        "source": "recorded" if prior else "legacy", "reason":
        "Retain recorded team; no new planning on amendment." if prior else
        "No planning in the supplied legacy receipt/API call; retain the existing team choice without another model call."})
    if plan:
        decision["tests"] = plan["tests"]
        decision["planning_receipt"] = _fact(kwargs["predispatch_receipt"])
    decision.setdefault("tests", {"status": "not_recorded", "reason":
        "Legacy task has no recorded test plan; follow its existing repository contract."})
    if not prior or explicit_compute:
        decision["compute"] = kwargs.get("compute_assignment", {}).get("selection") or {
            "source": compute_source or ("explicit" if explicit_compute else "native"), "reason":
            "Explicit model pairs are retained; omitted roles use their existing native defaults."}
    decision.setdefault("compute", {"source": "recorded", "reason": "Retain the original model settings on amendment."})
    kwargs["execution_decision"] = decision
    if task_kind:
        kwargs["task_staffing"] = {"kind": task_kind, "sources": staffing_sources,
            "review_source_detail": review_source_detail,
            "team_source_detail": team_source_detail,
            "admission": admission_staffing,
            "manager": {"profile": kwargs.get("manager_profile"), "source": staffing_sources["manager"]},
            "team": {"profile": (kwargs.get("native_team") or {}).get("profile"), "source": staffing_sources["team"]},
            "compute": kwargs.get("compute_assignment"), "reviewers": kwargs.get("reviewers")}
    elif prior.get("task_staffing"):
        frozen = kwargs["task_staffing"]
        sources = {**frozen["sources"], **{part: "explicit" for part, value in overrides.items() if value}}
        review_detail = dict(frozen.get("review_source_detail") or {})
        if overrides["reviewers"]:
            if compute_profile or kwargs.get("reviewers") or "backend" in kwargs:
                review_detail["backend"] = "explicit"
            if compute_profile or kwargs.get("reviewers") or kwargs.get("analyst_model"):
                review_detail["model"] = "explicit"
        team_detail = dict(frozen.get("team_source_detail") or {})
        if team_profile is not None:
            team_detail["profile"] = "explicit"
        if explicit_launcher:
            team_detail["backend"] = "explicit"
        kwargs["task_staffing"] = {**frozen, "sources": sources,
            "compute": kwargs.get("compute_assignment"), "reviewers": kwargs.get("reviewers"),
            "review_source_detail": review_detail or None,
            "team_source_detail": team_detail or None,
            "team": {"profile": (kwargs.get("native_team") or {}).get("profile"), "source": sources["team"]}}
        if kwargs.get('manager_profile') and not prior.get('manager_state'):
            kwargs['task_staffing']['manager'] = {'profile': kwargs['manager_profile'], 'source': 'kind'}
            kwargs['task_staffing']['sources'] = {**sources, 'manager': 'kind'}
    print("FOUNDRY DECISION: " + json.dumps(decision, ensure_ascii=True, sort_keys=True), file=sys.stderr)
    correction = compute_kwargs(auto_amend_model, auto_amend_effort, backend=coding_backend)
    if not correction:
        return {'executable': str(executable), 'correction': None, 'options': kwargs}
    _, _, roles = resolve_goal_compute(kwargs.get("role_compute"), coding_backend=coding_backend,
        backend=kwargs.get("backend", "codex"), analyst_model=kwargs.get("analyst_model"),
        analyst_effort=kwargs.get("analyst_effort"), coding_model=kwargs.get("coding_model"),
        coding_effort=kwargs.get("coding_effort"))
    if roles is not None:
        kwargs = {**kwargs, "role_compute": roles}
    return {'executable': str(executable), 'correction': correction, 'options': kwargs}


def run_prepared(order, evidence_dir, prepared):
    executable = Path(prepared['executable'])
    if prepared['correction'] is None:
        return _run(order, evidence_dir, executable, **prepared['options'])
    return goal_auto.run(sys.modules[__name__], order, evidence_dir, executable,
                         prepared['correction'], prepared['options'])


def run_goal(order: Path, evidence_dir: Path, executable: Path, **kwargs):
    return run_prepared(order, evidence_dir, prepare_goal(order, evidence_dir, executable, **kwargs))


def _session_from_log(log):
    """Reuse retained native identity; token accounting is not needed to resume."""
    try:
        terminal = Path(log).with_suffix(".terminal.json")
        if terminal.exists():
            proof = json.loads(terminal.read_bytes())
            if not isinstance(proof, dict):
                return None
            session = proof["session_id"] if proof.get("schema_version") == "pdcl-claude-terminal-1" else proof["binding"]["thread_id"]
        else:
            from ._native_session import read_retained_session_header
            session = read_retained_session_header(log)
        return session if isinstance(session, str) and session.strip() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _run(order, evidence_dir, executable, **options):
    with _run_lock(evidence_dir):
        if options.get('recovery_file'):
            saved = json.loads(Path(options['recovery_file']).read_bytes())
            ref = saved.get('successor', {}).get('foundry_manifest')
            if ref:
                original = json.loads(goal_usage._reference(ref))
                options.update(manager_state=original['manager_state'],
                               execution_decision=original['execution_decision'])
        from . import _foundry_manager
        state = _foundry_manager.configuration_for(options, evidence_dir)
        options.pop('manager_profile', None)
        if state is not None:
            # This is conversation ownership, not the worker's writing lease.
            with _run_lock(state['native_dir']):
                return goal_execution.run_owned(order, evidence_dir, executable, **{**options, 'manager_state': state})
        return goal_execution.run_owned(order, evidence_dir, executable, **options)




def _record_usage(folder, coding, native, outcome, *, native_team=None, context=None):
    # Preserve observations without masking the execution/review result.
    usage = {}
    try:
        usage_path = folder / "usage.json"
        from ._expert_consultation import observe as observe_experts
        manifest_path = folder / 'manifest.json'
        manifest = json.loads(manifest_path.read_bytes()) if manifest_path.exists() else {}
        if outcome is not None:
            from ._goal_intervention import retain_events
            outcome['unanswered_live_events'] = retain_events(manifest, [])
            outcome['live_manager_call'] = manifest.get('live_manager_call')
        if outcome is not None and manifest.get('coordination'):
            outcome['coordination'] = manifest['coordination']
        if outcome is not None and manifest.get('candidate_closure'):
            outcome['candidate_closure'] = manifest['candidate_closure']
        usage = goal_usage.task_usage(coding, native, native_team=native_team)
        if manifest.get('disposable_scratch'):
            previous = json.loads(usage_path.read_bytes()) if usage_path.exists() else {}
            usage['scratch'] = previous.get('scratch') or {
                **manifest['disposable_scratch'], 'status': 'retained', 'bytes_reclaimed': 0, 'files_removed': 0}
        usage['expert_consultations'] = observe_experts(manifest.get('manager_state') or manifest.get('expert_state'))
        manager_usage = folder / 'manager-usage.json'
        if manager_usage.exists():
            usage['manager'] = {'scope': 'native manager conversation; includes prior turns, separate from reviews',
                                'snapshot': json.loads(manager_usage.read_bytes())}
        _write(usage_path, usage)
        if outcome is not None:
            outcome["task_usage"] = _fact(usage_path)
    except Exception as exc:
        if outcome is not None:
            outcome["task_usage"] = goal_usage._unknown(exc)
        print("Foundry usage unavailable: " + str(exc), file=sys.stderr)
    if outcome is not None:
        try:
            outcome["delivery_report"] = goal_usage.delivery_report(outcome, usage, context)
        except Exception as exc:
            outcome["delivery_report"] = goal_usage._unknown(exc)


def _finish_review(checkpoint, folder, *, resume=False, transfer_role=None, transfer_provider=None, migrate_runtime=False, retained_only=False):
    from ._task_deadline import scope
    with scope(checkpoint.get('task_deadline_at')):
        return _finish_review_owned(checkpoint, folder, resume=resume, transfer_role=transfer_role,
            transfer_provider=transfer_provider, migrate_runtime=migrate_runtime, retained_only=retained_only)


def _finish_review_owned(checkpoint, folder, *, resume=False, transfer_role=None, transfer_provider=None, migrate_runtime=False, retained_only=False):
    """Normal execution and recovery share one review/finalization path."""
    from . import _foundry_dispatch as dispatch
    context, tree = checkpoint["context"], Path(checkpoint["worktree"])
    native, outcome = None, None
    try:
        names = ["review-0", "review-1"]
        manifest = json.loads((folder/'manifest.json').read_bytes())
        reference = (manifest['review_checkpoint']
                     if checkpoint.get('context_delivery') == 'reference-v1' else None)
        requests = {name: _role_request(checkpoint["run_id"], name, context, context_reference=reference,
                                       read_policy=checkpoint.get('review_read_policy'))
                    for name in names}
        reviewers = checkpoint.get('reviewers') or [checkpoint['analyst']] * 2
        transfers = manifest.get('review_transfers', {})
        if context.get("project_record"):
            reviewers = [{**row, "execution_profiles": context["project_record"]["context"]["execution_profiles"]}
                         for row in reviewers]
        analyst = review_group.Panel(reviewers, requests, NativeAnalyst,
                                     folder / 'reviews', tree, resume=resume,
                                     validate=lambda value: _validate_review(value, context, tree, retained_only=retained_only),
                                     rejected_review=InvalidReview, transfers=transfers)
        if transfer_provider or migrate_runtime:
            from ._foundry_goal_transitions import reviewers as transfer_reviewers
            transfers = transfer_reviewers(checkpoint, transfers, role=transfer_role,
                provider=transfer_provider, migrate=migrate_runtime, completed=analyst.results)
            if transfers != manifest.get('review_transfers', {}):
                analyst = review_group.Panel(reviewers, requests, NativeAnalyst,
                    folder/'reviews', tree, resume=resume,
                    validate=lambda value: _validate_review(value, context, tree, retained_only=retained_only),
                    rejected_review=InvalidReview, transfers=transfers)
        if transfers != manifest.get('review_transfers', {}):
            manifest['review_transfers'] = transfers
            _write(folder/'manifest.json', manifest)
        if 'implementation_evidence' in checkpoint:
            from ._compute_selection import independent_reviewers
            selected = [(transfers.get(name) or [row])[-1] for name, row in zip(names, reviewers)]
            selected = [{k: v for k, v in row.items() if k in ('backend', 'model', 'effort', 'executable')} for row in selected]
            try:
                participation = checkpoint['implementation_evidence']
                independent_reviewers(selected, participation.get('review_exclusion_models', participation['implementation_models']), explicit=True)
            except ValueError as exc:
                raise GoalBlocked(str(exc), 'review_blocked') from exc
        try:
            # The panel reuses completed verdicts and gives each unfinished slot
            # a fresh attempt on explicit recovery, retaining every prior receipt.
            if retained_only and any(name not in analyst.results for name in names):
                raise GoalBlocked('retained review is incomplete; no model calls were made', 'review_blocked')
            outcomes = review_group.prewarm(names, requests, analyst)
            reviews, sessions = [], []
            for name in names:
                value, error = outcomes[name]
                if error is not None:
                    raise error
                reviews.append(value)
                sessions.append(analyst.native_session(requests[name]))
            if len(set(sessions)) != 2:
                raise GoalBlocked("independent reviews reused one native session", "review_blocked")
            native = analyst.drain()
        except BaseException as exc:
            analyst.cancel(str(exc))
            native = analyst.drain()
            raise
        if not retained_only and _candidate(tree) != context["candidate_commit"]:
            raise GoalBlocked("candidate changed during independent review", "review_blocked")
        host = [] if retained_only else ([f for f in context.get("project_review", {}).get("findings", []) if f["severity"] in _BLOCKING_SEVERITIES]
                if context.get("project_record") else dispatch._project_adapter(None).host_findings(context))
        accepted = all(review["verdict"] == "PASS" for review in reviews) and not host
        outcome = {"status": ("review_recorded" if retained_only else "candidate_verified") if accepted else "review_blocked",
                   "candidate_commit": context["candidate_commit"], "integration_pending": accepted and not retained_only,
                   "coding": checkpoint["coding"], "reviews": reviews, **({"host_findings": host} if host else {}),
                   "review_sessions": sessions, "native": native}
        if retained_only:
            outcome['retained_recovery'] = {'model_calls': 0, 'acceptance_evaluated': False,
                'current_head': _git(tree, 'rev-parse', 'HEAD').decode().strip()}
        if checkpoint.get('implementation_evidence'):
            outcome['implementation_evidence'] = checkpoint['implementation_evidence']
            outcome['model_independence'] = checkpoint['model_independence']
        if not retained_only and context.get("project_record"):
            from ._project_execution import from_record
            from . import _foundry_dispatch as dispatch
            try:
                project = from_record(context["project_record"])
                outcome["project_acceptance"] = project.accept_result(folder,
                    task={"order": checkpoint["order"], "run_id": checkpoint["run_id"]},
                    subject={"base_commit": context["base_commit"], "candidate_commit": context["candidate_commit"],
                             "review_verdicts": reviews}, env=dispatch._utf8_env())
            except (OSError, ValueError) as exc:
                outcome["project_acceptance"] = {"status": "not_evaluated", "source": "project_check", "error": str(exc)}
        if checkpoint.get("coding_session"):
            outcome["coding_session"] = checkpoint["coding_session"]
    finally:
        # A failed recovery must not replace a prior measured snapshot with an
        # empty one (e.g. the native executable changed before initialization).
        if not (resume and outcome is None and native is None and (folder / "usage.json").exists()):
            _record_usage(folder, checkpoint["coding_usage"], native, outcome,
                          native_team=checkpoint.get("native_team"),
                          context=checkpoint.get("delivery_context"))
    if not retained_only and _candidate(tree) != outcome["candidate_commit"]:
        raise GoalBlocked("candidate changed during independent review", "review_blocked")
    _publish_outcome(folder, outcome)
    return outcome



def _manager_checkpoint(folder, manifest, *, allow_failed=False):
    """Resume a completed worker's feedback; never repeat an uncertain dispatch."""
    if manifest.get('manager_phase') not in {'assignment_pending', 'feedback_pending', 'review_pending', 'delegation_ready', 'artifacts_pending', 'expert_pending', 'awaiting_input'}:
        raise GoalBlocked('manager has no completed worker feedback to resume; inspect the original dispatch')
    if (folder / 'outcome.json').exists() and manifest.get('manager_phase') != 'awaiting_input':
        raise GoalBlocked("manager decision is already delivered")
    if manifest.get('coding_checkpoint'):
        path = folder / 'coding-checkpoint.json'
        if manifest['coding_checkpoint'] != _fact(path):
            raise GoalBlocked('manager worker checkpoint changed')
        checkpoint = json.loads(path.read_bytes())
        if not checkpoint.get('coding_session'):
            raise GoalBlocked('worker did not complete; use its existing answer or execution recovery path')
        if checkpoint['returncode']:
            if not allow_failed:
                raise GoalBlocked('worker failed; explicit operator instructions are required before manager recovery')
            from ._recovery_reconciliation import completed_coding
            completed_coding(checkpoint)
    else:
        checkpoint = {**{key: manifest[key] for key in ('order', 'run_id', 'worktree', 'base_commit')},
            **manifest['manager_start'], 'returncode': None, 'evidence': [],
            'coding_usage': goal_usage._unknown('worker has not been dispatched')}
    if any(checkpoint.get(key) != manifest.get(key) for key in ('order', 'run_id', 'worktree', 'base_commit')):
        raise GoalBlocked('manager worker checkpoint belongs to another run')
    for fact in checkpoint['evidence']:
        if fact != _fact(fact['path']):
            raise GoalBlocked('retained coding evidence changed')
    return checkpoint


def _recover_manager_feedback(order, folder, manifest, *, answer=None):
    if (folder / 'outcome.json').exists():
        return json.loads((folder / 'outcome.json').read_bytes())
    checkpoint = _manager_checkpoint(folder, manifest, allow_failed=bool(answer))
    if manifest.get('worker_recovery_migration'):
        from ._foundry_goal_transitions import manager as transfer_manager
        manifest = transfer_manager(sys.modules[__name__], folder, manifest, provider=None, migrate=True, answer=answer,
                                    worker_feedback=manifest['manager_packet'])
    options = dict(checkpoint['options'])
    if manifest.get('project_record') is not None:
        options['project_record'] = manifest['project_record']
    options.update(manager_state=manifest.get('manager_state'), execution_decision=manifest.get('execution_decision'))
    if (manifest.get('manager_state') or {}).get('quota_fallback'):
        options['reviewers'] = manifest['reviewers']
    with _run_lock((manifest.get('manager_state') or manifest['expert_state'])['native_dir']):
        return goal_execution.run_owned(order, folder, Path(checkpoint['executable']),
            target_worktree=Path(checkpoint['worktree']), coding_session=checkpoint['coding_session'],
            continuation=manifest.get('continuation'),
            retained={**checkpoint, 'manifest': manifest}, manager_answer=answer, **options)


def recover_goal(order: Path, evidence_dir: Path, *, answer=None, team_profile=None, target_provider=None,
                 project_preflight_input=None, handoff_role=None, migrate_runtime=False, recovery_file=None, retained_only=False,
                 restore_manager=False, order_revision_file=None):
    """Recover review, or explicitly answer unfinished coding in its old session."""
    if retained_only and any(v is not None and v is not False for v in
            (answer, team_profile, target_provider, project_preflight_input, handoff_role, migrate_runtime, recovery_file, restore_manager, order_revision_file)):
        raise ValueError('retained-only recovery cannot continue or transfer execution')
    if handoff_role not in (None, 'coding', 'manager', 'review-0', 'review-1'):
        raise ValueError('unknown handoff role')
    if handoff_role and not (target_provider or migrate_runtime):
        raise ValueError('handoff role requires a provider or explicit runtime migration')
    if target_provider is not None and (target_provider not in {"codex", "claude"} or
            (handoff_role in (None, 'coding', 'manager') and answer is None)):
        raise ValueError("provider handoff needs codex/claude and a coding continuation answer")
    if team_profile is not None and answer is None:
        raise ValueError("an explicit team choice on recovery requires a coding answer")
    if restore_manager and answer is None:
        raise ValueError('manager staffing on recovery requires a coding answer')
    folder = Path(evidence_dir).resolve(strict=True)
    manifest = json.loads((folder / "manifest.json").read_bytes())
    if not order_revision_file and not order_revision.matches(sys.modules[__name__], order, manifest):
        raise GoalBlocked("recovery order differs from recorded task; an authorized contract revision requires --order-revision")
    if (manifest.get('mode') != 'auto-amend' and answer is None and not retained_only
            and not recovery_file and not order_revision_file):
        active, current = goal_auto.active_round(folder, manifest, consumed=True)
        recorded, _ = goal_auto.active_round(folder, manifest)
        if active != recorded:
            result = recover_goal(order, active, team_profile=team_profile, target_provider=target_provider,
                project_preflight_input=project_preflight_input, handoff_role=handoff_role,
                migrate_runtime=migrate_runtime, restore_manager=restore_manager)
            return {**result, 'continuation_evidence': str(active),
                    'result_outcome': _fact(active / 'outcome.json')}
    if manifest.get("mode") == "auto-amend":
        with _run_lock(folder):
            manifest = json.loads((folder / "manifest.json").read_bytes())
            if not order_revision_file and not order_revision.matches(sys.modules[__name__], order, manifest):
                raise GoalBlocked("recovery order differs from recorded task")
            active = Path(manifest["active_round"]).resolve()
            if active.parent != folder:
                raise GoalBlocked("active round is not in the recorded goal")
            prior_manifest = dict(manifest)
            prior_state = manifest.get('state')
            if not retained_only and prior_state != 'completed':
                manifest['state'] = 'running'
                manifest.pop('error', None)
                _write(folder / 'manifest.json', manifest)
            try:
                if not (active/'manifest.json').exists() and manifest.get('round_start') and not retained_only:
                    start = manifest['round_start']
                    result = _run(order, active, Path(start['executable']), initializing=True, **start['options'])
                else:
                    result = recover_goal(order, active, answer=answer, team_profile=team_profile, restore_manager=restore_manager,
                                      target_provider=target_provider, project_preflight_input=project_preflight_input,
                                      handoff_role=handoff_role, migrate_runtime=migrate_runtime, recovery_file=recovery_file, retained_only=retained_only,
                                      order_revision_file=order_revision_file)
                if retained_only:
                    return result
                if result.get('continuation_evidence'):
                    if prior_state != 'completed':
                        _write(folder / 'manifest.json', prior_manifest)
                    return result
                selected_outcome = goal_auto.outcome_path(folder, manifest)
                if manifest.get("state") == "completed" and selected_outcome.exists():
                    saved_outcome = json.loads(selected_outcome.read_bytes())
                    if saved_outcome.get('candidate_closure') == result.get('candidate_closure'):
                        return saved_outcome
                rounds = list(manifest.get("rounds", []))
                record = goal_auto.round_record(sys.modules[__name__], active)
                if rounds and Path(rounds[-1]["outcome"]["path"]).resolve() == active / "outcome.json":
                    rounds[-1] = record
                elif (active.name == "round-" + str(len(rounds)) or
                      any(Path(row['round']).resolve() == active for row in manifest.get('runtime_upgrades', []))):
                    rounds.append(record)
                else:
                    raise GoalBlocked("active round does not follow the recorded completed rounds")
                child = json.loads((active / "manifest.json").read_bytes())
                manifest.update({k: child[k] for k in ("worktree", "base_commit", "tag") if k in child})
                manifest["rounds"] = rounds
                if result["status"] == "review_blocked":
                    result = {**result, "auto_amend_stop":
                              "review recovered; continue corrections explicitly with --amend-from "
                              + str(active / "outcome.json")}
                if manifest.get('runtime_upgrades'):
                    from ._foundry_goal_upgrade import publish
                    publish(sys.modules[__name__], folder, manifest, active, result)
                else:
                    result = goal_auto.finish(sys.modules[__name__], folder, manifest, result)
                _write(folder / "manifest.json", manifest)
                return result
            except BaseException as exc:
                if not retained_only and prior_state != 'completed':
                    manifest.update(state='interrupted', error=str(exc))
                    _write(folder / 'manifest.json', manifest)
                raise
    with _run_lock(folder):
        manifest = json.loads((folder / "manifest.json").read_bytes())
        if order_revision_file:
            manifest = order_revision.admit(sys.modules[__name__], folder, manifest, order, order_revision_file)
        if not order_revision.matches(sys.modules[__name__], order, manifest):
            raise GoalBlocked("recovery order differs from recorded task")
        if project_preflight_input is not None:
            from ._project_execution import from_record
            prior = from_record(manifest.get('project_record'))
            updated = from_record({**prior.record(), 'resume_preflight_input': project_preflight_input})
            if manifest.get('review_checkpoint'):
                raise GoalBlocked('preflight replacement requires unfinished goal coding')
            manifest['project_record'] = updated.record()
        closure = manifest.get('candidate_closure') or {}
        if (not retained_only and answer is None and not target_provider and not migrate_runtime
                and closure.get('status') in ('failed', 'not_evaluated')):
            # The worker already handed back a candidate. Retry only the owning
            # review lifecycle, never replay coding/manager calls or a cached RED.
            tree = Path(manifest['worktree'])
            _review_range(tree, manifest['base_commit'], closure['candidate_commit'])
            checkpoint = (json.loads(goal_usage._reference(manifest['coding_checkpoint']))
                          if manifest.get('coding_checkpoint') else {})
            for fact in checkpoint.get('evidence', []):
                goal_usage._reference(fact)
            options = dict(checkpoint.get('options', {}))
            for key in ('amend_from', 'amend_prompt', 'coding_session', 'review_range'):
                options.pop(key, None)
            for key in ('manager_state', 'native_team', 'project_record', 'reviewers',
                        'execution_decision', 'task_staffing', 'compute_assignment', 'task_deadline_at'):
                if key in manifest:
                    options[key] = manifest[key]
            review = manifest.get('requested_compute', {}).get('review') or {}
            options.setdefault('backend', review.get('backend', 'codex'))
            for key in ('model', 'effort'):
                if review.get(key):
                    options.setdefault('analyst_' + key, review[key])
            executable = checkpoint.get('executable') or review.get('executable') or str(resolve_executable(options['backend']))
            return goal_execution.run_owned(order, folder, Path(executable),
                target_worktree=tree, review_range=(manifest['base_commit'], closure['candidate_commit']),
                coding_session=checkpoint.get('coding_session'),
                retained={**checkpoint, 'manifest': manifest, 'run_id': manifest['run_id']}, **options)
        if recovery_file is not None:
            if target_provider is not None or team_profile is not None or handoff_role is not None:
                raise ValueError('explicit worker recovery retains its roles/team; use --migrate-runtime for manager transfer')
            from ._foundry_goal_continuation import recover_worker
            return recover_worker(sys.modules[__name__], order, folder, manifest, answer,
                                  recovery_file, migrate_runtime=migrate_runtime)
        selected_role = handoff_role or ('review' if manifest.get('review_checkpoint') else
                                        'manager' if migrate_runtime and manifest.get('manager_state') else 'coding')
        if (manifest.get('manager_state') is not None and manifest.get('manager_phase') == 'dispatch_pending'
                and not retained_only):
            manifest = goal_continuation.restore_completed_feedback(sys.modules[__name__], folder, manifest)
        if selected_role.startswith('review') and not manifest.get('review_checkpoint'):
            raise GoalBlocked('review transfer requires a retained review checkpoint')
        if migrate_runtime and selected_role == 'coding' and answer is None:
            raise ValueError('coding runtime migration requires a continuation answer')
        if (target_provider or migrate_runtime) and selected_role == 'manager':
            from ._foundry_goal_transitions import manager as transfer_manager
            if (migrate_runtime and target_provider is None and manifest.get('manager_phase') == 'awaiting_input'
                    and (folder / 'outcome.json').exists()):
                from ._foundry_goal_transitions import manager_history
                manager_history(sys.modules[__name__], manifest['manager_state'], manifest['worktree'])
                if answer is None and order_revision.pending(manifest):
                    _, revisions = order_revision.records(sys.modules[__name__], manifest)
                    answer = revisions[-1][1]['authorization_text']
                return goal_continuation.continue_coding(sys.modules[__name__], order, folder, manifest, answer,
                    team_profile=team_profile, manager_migration=True)
            manifest = transfer_manager(sys.modules[__name__], folder, manifest,
                provider=target_provider, migrate=migrate_runtime, answer=answer,
                worker_feedback=manifest.get('manager_packet') if manifest.get('manager_phase') == 'feedback_pending' else None)
            return _recover_manager_feedback(order, folder, manifest, answer=answer)
        if selected_role.startswith('review') and (target_provider or migrate_runtime) and answer is not None:
            raise ValueError('review transfer retains its frozen review packet; omit --prompt-file')
        if order_revision.pending(manifest):
            if manifest.get('manager_phase') == 'awaiting_input' and (folder / 'outcome.json').exists():
                # Preserve the delivered question and use its existing answer-successor
                # lifecycle. A revision's retained operator ruling is itself an answer.
                if answer is None:
                    _, revisions = order_revision.records(sys.modules[__name__], manifest)
                    answer = revisions[-1][1]['authorization_text']
                return goal_continuation.continue_coding(sys.modules[__name__], order, folder, manifest, answer,
                    team_profile=team_profile, target_provider=target_provider, migrate_runtime=migrate_runtime)
            return _recover_manager_feedback(order, folder, manifest, answer=answer)
        if answer is not None:
            if (manifest.get('expert_state') and manifest.get('manager_phase') in {'expert_pending', 'delegation_ready'}
                    and not manifest.get('coding_checkpoint') and team_profile is None and target_provider is None and not migrate_runtime):
                return _recover_manager_feedback(order, folder, manifest, answer=answer)
            if (manifest.get('manager_state') is not None
                    and manifest.get('manager_phase') in {'assignment_pending', 'feedback_pending', 'delegation_ready'}
                    and not (folder / 'outcome.json').exists()
                    and ((not manifest.get('coding_checkpoint') and manifest.get('manager_phase') == 'assignment_pending')
                         or (manifest.get('coding_checkpoint') and
                             (manifest.get('manager_phase') == 'feedback_pending' or
                              json.loads((folder / 'coding-checkpoint.json').read_bytes()).get('returncode') == 0)))
                    and team_profile is None and target_provider is None and not migrate_runtime):
                return _recover_manager_feedback(order, folder, manifest, answer=answer)
            from ._compute_selection import missing_manager_profile
            manager_profile = missing_manager_profile(manifest) if restore_manager else None
            return goal_continuation.continue_coding(sys.modules[__name__], order, folder, manifest, answer,
                                                       team_profile=team_profile, target_provider=target_provider,
                                                       migrate_runtime=migrate_runtime, manager_profile=manager_profile)
        ref = manifest.get("review_checkpoint")
        if not ref:
            if retained_only:
                raise GoalBlocked('retained-only recovery requires a completed review checkpoint')
            outcome_path = folder / 'outcome.json'
            if outcome_path.exists():
                outcome = json.loads(outcome_path.read_bytes())
                if outcome.get('status') == 'project_handoff':
                    return outcome
            if manifest.get('manager_state') is not None or manifest.get('expert_state'):
                return _recover_manager_feedback(order, folder, manifest)
            raise GoalBlocked("no completed coding/review checkpoint; inspect the original dispatch records")
        path = folder / "review-checkpoint.json"
        if ref != _fact(path):
            raise GoalBlocked("review checkpoint differs from recorded stage")
        checkpoint = json.loads(path.read_bytes())
        if checkpoint.get("schema_version") != 1:
            raise GoalBlocked("unsupported review checkpoint version")
        context = checkpoint["context"]
        if (checkpoint["order"] != manifest["order"] or checkpoint["run_id"] != manifest["run_id"]
                or checkpoint["worktree"] != manifest["worktree"]
                or context["base_commit"] != manifest["base_commit"]):
            raise GoalBlocked("review checkpoint does not describe this run")
        tree = Path(checkpoint["worktree"])
        _review_range(tree, context["base_commit"], context["candidate_commit"], retained_only=retained_only)
        for fact in checkpoint["coding"]["evidence"]:
            if fact != _fact(fact["path"]):
                raise GoalBlocked("retained coding evidence changed")
        if (folder / "outcome.json").exists():
            return json.loads((folder / "outcome.json").read_bytes())
        resumable = (folder / 'reviews').exists()
        from ._followup import announce
        if not retained_only:
            announce(folder, manifest)
        return _finish_review(checkpoint, folder, resume=resumable,
            transfer_role=handoff_role if selected_role.startswith('review') else None,
            transfer_provider=target_provider, migrate_runtime=migrate_runtime, retained_only=retained_only)
