"""Goal usage views of existing native/dispatch receipts; never decide verdicts.

Current goals read their dispatch receipt and drained review snapshot. Reading
usage never launches a model, scans native sessions or rewrites historical data.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from ._execution_usage import _bytes, _counters, _reference as _execution_reference
from .foundry_native import task_usage_from_snapshot
from . import _foundry_goal_telemetry as telemetry


def _reference(ref, *, retain=True):
    """Declared reader for an immutable pre-upgrade root-manifest snapshot."""
    try:
        return _execution_reference(ref, retain=retain)
    except ValueError as exc:
        path = Path(ref['path'])
        if path.name != 'manifest.json':
            raise ValueError('retained evidence changed: ' + str(path)) from exc
        archived = path.with_name('manifest-before-' + ref['sha256'] + '.json')
        if not archived.is_file():
            raise ValueError('retained evidence changed: ' + str(path)) from exc
        return _execution_reference({**ref, 'path': str(archived)}, retain=retain)


def _unknown(reason):
    return {"coverage": "uncovered", "usage": None, "reason": str(reason)[:240]}


def capture(log, order_id):
    """Freeze a dispatch observation only when its log/cleanup/attempt match."""
    try:
        log = Path(log).resolve()
        path = log.with_suffix(".receipt.json")
        raw = _bytes(path)
        receipt = json.loads(raw)
        if receipt.get("schema_version") != "pdcl-resultreceipt-1" or receipt.get("order_id") != order_id:
            raise ValueError("coding receipt order or format differs")
        refs = {r["kind"]: r for r in receipt["evidence_refs"]}
        if len(refs) != len(receipt["evidence_refs"]):
            raise ValueError("duplicate coding receipt references")
        for kind, expected in (("log", log), ("cleanup", log.with_suffix(".cleanup.json"))):
            if Path(refs[kind]["path"]).resolve() != expected:
                raise ValueError("coding receipt belongs to another dispatch")
            _reference(refs[kind], retain=False)
        cleanup = json.loads(_reference(refs["cleanup"]))
        if (not receipt.get("execution_intent_id") or not receipt.get("attempt_id")
                or receipt["attempt_id"] != cleanup.get("attempt_id")):
            raise ValueError("coding receipt attempt differs")
        observation = receipt.get("coding_usage") or _unknown("receipt predates coding usage")
        return {**observation, "execution_intent_id": receipt["execution_intent_id"],
                "attempt_id": receipt["attempt_id"], "execution": receipt.get("execution", {}),
                "receipt": {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}}
    except Exception as exc:
        return _unknown(exc)


def _observations(parent):
    if 'managed_turns' in parent:
        rows, gaps = [], []
        for turn in parent['managed_turns']:
            observed, missing = _observations(turn)
            rows.extend(observed)  # Native session/turn IDs distinguish turns; phase retains the role.
            gaps.extend(missing)
        return rows, gaps
    rows = [("coding", parent)]
    if parent.get("backend") == "claude":
        return rows, []
    children = parent.get("child_agents")
    gaps = []
    if not isinstance(children, dict):
        gaps.append({"phase": "children", "reason": "receipt has no child-agent observation"})
    else:
        rows.extend((f"child-{i}", row) for i, row in enumerate(children.get("observations", [])))
        gaps.extend({"phase": "children", **row} for row in children.get("missing_observations", []))
        if children.get("coverage") != "observed_linked_descendants" and not gaps:
            gaps.append({"phase": "children", "reason": "child-agent coverage is incomplete"})
    return rows, gaps


def coding_usage(parent):
    """One row per native turn. Conflicting snapshots stay unknown, never sum."""
    try:
        return _coding_observations(*_observations(parent))
    except Exception as exc:
        return _unknown(exc)


def _coding_observations(observations, child_gaps):
    """Normalize one or several receipt views through the same native identities."""
    try:
        claude = [row for _, row in observations if row.get("coverage") == "claude_session_snapshot"]
        observations = [(phase, row) for phase, row in observations if row.get("coverage") != "claude_session_snapshot"]
        observed, missing, duplicates, conflicts = {}, list(child_gaps), [], set()
        for phase, observation in observations:
            try:
                if observation.get("coverage") not in {"observed_native_turn", "partial_native_turn"}:
                    raise ValueError(observation.get("reason", "coding usage not observed"))
                key = (observation["session_id"], observation["turn_id"])
                if not all(isinstance(v, str) and v for v in key):
                    raise ValueError("native turn identity missing")
                counters = _counters({k: v for k, v in observation["usage"].items() if v is not None})
                responses = observation["response_ids"]
                if (not responses or any(not isinstance(r, str) or not r for r in responses)
                        or len(set(responses)) != observation["response_count"] or len(responses) != len(set(responses))):
                    raise ValueError("native response identities differ from count")
                signature = (counters, sorted(responses), observation["coverage"])
                if not isinstance(observation.get("execution"), dict):
                    raise ValueError("coding execution observation missing")
                if key in observed:
                    if observed[key][0] != signature:
                        conflicts.add(key)
                        raise ValueError("conflicting observations of one native turn")
                    # Identical counters do not establish identical model identity.
                    # Combine native declarations before projecting the one retained
                    # turn into a model group; conflicting pairs remain unknown.
                    _, retained_phase, retained = observed[key]
                    model = retained.get("model_observation", {})
                    declarations = list(model.get("declarations", []))
                    for declaration in observation.get("model_observation", {}).get("declarations", []):
                        if declaration not in declarations:
                            declarations.append(declaration)
                    combined = {**model, "declarations": declarations,
                                "coverage": "observed" if declarations else "unavailable",
                                "source": "native turn_context across duplicate turn observations"}
                    observed[key] = (signature, retained_phase, {**retained, "model_observation": combined})
                    duplicates.append(phase)
                    continue
                observed[key] = (signature, phase, observation)
            except Exception as exc:
                missing.append({"phase": phase, "reason": str(exc)[:240]})
        # Response identity is also global to the observed native session. Do not
        # count it twice if inconsistent retained data assigns it to two turns.
        owners = {}
        for key, (signature, _, _) in observed.items():
            for response in signature[1]:
                identity = (key[0], response)
                if identity in owners and owners[identity] != key:
                    conflicts.update((key, owners[identity]))
                    missing.append({"phase": observed[key][1], "reason": "response identity shared by different turns"})
                owners[identity] = key
        calls = []
        for key, (_, phase, observation) in sorted(observed.items()):
            if key in conflicts:
                continue
            calls.append({"call_id": "/".join(key), "phase": phase, "backend": "codex",
                          "provider_usage_observed": True, "usage": observation["usage"],
                          "outcome": observation["execution"].get("process_outcome"),
                          "observation": observation})
        metrics = task_usage_from_snapshot({"calls": calls})["provider_usage_metrics"]
        if claude:
            from .foundry_claude import aggregate_sessions
            claude_view = aggregate_sessions(claude)
            calls.extend({"call_id": row["session_id"] + "/" + row["attempt_id"], "phase": "coding",
                "backend": "claude", "usage": None, "observation": row} for row in claude)
        complete = bool(calls) and not missing and all(
            r["observation"]["coverage"] == "observed_native_turn" for r in calls)
        return {"coverage": "native-receipts" if complete else "partial" if calls else "uncovered",
                "observations": calls, "observed_turn_count": sum(call["backend"] == "codex" for call in calls), "missing_observations": missing,
                **({"claude_session_usage": claude_view} if claude else {}),
                "duplicate_observations": duplicates, "provider_usage_metrics": metrics,
                "usage_scope": "coding parent and explicitly linked descendants; no coordinator or account-charge coverage; do not add successive snapshots",
                "actual_account_charge": "unknown"}
    except Exception as exc:
        return _unknown(exc)


def _model_usage(coding):
    """Project already deduplicated turns; positions are not claimed work roles."""
    from ._model_catalogue import tier_for_model
    groups = {}
    for call in coding.get("observations", []):
        if call.get("backend") == "claude":
            continue  # Cumulative Claude sessions are separately reported, never treated as Codex turns.
        declarations = call["observation"].get("model_observation", {}).get("declarations", [])
        pair = declarations[0] if len(declarations) == 1 else {}
        position = "leader" if call["phase"] == "coding" else "member"
        key = (position, pair.get("model"), pair.get("effort"))
        groups.setdefault(key, []).append(call)
    rows = []
    for (position, model, effort), calls in groups.items():
        metrics = task_usage_from_snapshot({"calls": calls})["provider_usage_metrics"]
        input_metric = metrics["input_tokens"]
        cached_metric = metrics["cached_input_tokens"]
        complete = all(metric["observed_calls"] == len(calls) and not metric["missing_call_ids"]
                       for metric in (input_metric, cached_metric))
        uncached = (input_metric["observed_sum"] - cached_metric["observed_sum"]
                    if complete else None)
        rows.append({"position": position, "model": model, "tier": tier_for_model(model), "effort": effort,
                     "observed_turn_count": len(calls),
                     "observed_uncached_input_tokens": uncached,
                     "provider_usage_metrics": metrics,
                     "observed_cache_hit_ratio": (cached_metric["observed_sum"] / input_metric["observed_sum"]
                                                   if complete and input_metric["observed_sum"] else None)})
    # Denominators include unknown-model groups. These are shares of observed
    # coding counters, not complete-task shares or estimated account charges.
    for field in ("input_tokens", "output_tokens", "uncached_input_tokens"):
        values = [(r["observed_uncached_input_tokens"] if field == "uncached_input_tokens"
                   else r["provider_usage_metrics"][field]["observed_sum"]) for r in rows]
        total = sum(values) if all(v is not None for v in values) else None
        for row, value in zip(rows, values):
            row["observed_" + field.removesuffix("_tokens") + "_share"] = value / total if total else None
    return rows


def team_execution(coding, team):
    rows = _model_usage(coding)
    if team.get("backend") == "claude":
        from ._model_catalogue import tier_for_model
        unique, conflicts = {}, set()
        for call in coding.get('observations', []):
            if call.get('backend') != 'claude':
                continue
            observation = call['observation']
            for row in observation.get('context_signals', {}).get('response_usage', []):
                key = (observation.get('session_id'), row.get('response_id'), row.get('member_id'))
                if not all(key):
                    continue
                if key in unique and unique[key] != row:
                    conflicts.add(key)
                unique[key] = row
        grouped = {}
        for key, row in unique.items():
            if key in conflicts:
                continue
            group = (key[0], row['member_id'], row.get('model'), row.get('initial_assignment_activity'))
            grouped.setdefault(group, []).append(row)
        members = []
        for (session, member, chosen, activity), responses in grouped.items():
            metrics = {field: sum(r['usage'][field] for r in responses) if all(r['usage'].get(field) is not None for r in responses) else None
                       for field in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens')}
            members.append({'session_id': session, 'member_id': member, 'position': 'leader' if member == 'leader' else 'member',
                'model': chosen, 'tier': tier_for_model(chosen), 'initial_assignment_activity': activity,
                'response_count': len(responses), 'usage': metrics})
        return {"requested": team, "coverage": "partial", "model_usage": members,
                "claude_session_usage": coding.get("claude_session_usage"), "member_turns_observed": None,
                'conflicting_response_ids': [list(k) for k in sorted(conflicts)],
                "scope": "model_usage is deduplicated native per-response member detail, not additive to cumulative claude_session_usage; "
                         "activity is declared initial intent; absent IDs/models/roles remain unknown; member turns and concurrent size are not inferred"}
    # Same deduplicated observations as the aggregate; this is a detail view,
    # never another bill to add to model_usage. Follow-up work is not assumed
    # to share the original spawn's activity.
    turns = []
    from ._model_catalogue import tier_for_model
    for call in coding.get('observations', []):
        if call.get('backend') == 'claude':
            continue
        observation = call['observation']
        models = observation.get('model_observation', {}).get('declarations', [])
        selected = models[0].get('model') if len(models) == 1 else None
        turns.append({'session_id': observation.get('session_id'), 'turn_id': observation.get('turn_id'),
            'position': 'leader' if call['phase'] == 'coding' else 'member',
            'model': selected, 'tier': tier_for_model(selected),
            'initial_assignment_activity': observation.get('parent_binding', {}).get('declared_activity'),
            'usage': observation.get('usage'), 'reading_signals': observation.get('reading_signals'),
            'coverage': observation.get('coverage')})
    return {"requested": {"profile": team["profile"], "version": team["version"],
                          "lead_role": team.get("lead_role"), "member_roles": team.get("member_roles"),
                          "default_member_model": team["config"].get("agents.default_subagent_model")},
            "coverage": coding["coverage"], "model_usage": rows, 'turn_usage': turns,
            "member_turns_observed": sum(r["observed_turn_count"] for r in rows if r["position"] == "member"),
            "scope": "coding leader and linked members only; turn_usage is the detail of model_usage, not additive; "
                     "initial_assignment_activity is declared spawn intent, not observed work or follow-up activity; "
                     "positions do not prove who implemented or verified; "
                     "model/effort are client turn settings, unknown when missing or ambiguous; reviews are separate; "
                     "cached input is included in input, reasoning output in output; ratios use observed coding counters including unknown-model groups; "
                     "partial coverage remains partial; ratios are not quality scores, savings or account charges"}


def delivery_context(mode, *, order=None, run_id=None, amendment=None, requested_compute=None):
    """Freeze observation context once; recovery retains the original start."""
    clock = telemetry.measure(telemetry.started())
    return {"mode": mode, "order": order, "run_id": run_id,
            "amends": amendment.get("prior_outcome") if amendment else None,
            "requested_compute": requested_compute,
            "started_utc": clock["started_utc"] if clock else None}


def delivery(outcome, context=None):
    """Project the owner's result, never infer acceptance from usage or counts."""
    context = context or {}
    result = outcome or {}
    reviews = []
    for review in result.get("reviews", []):
        severities = {}
        for finding in review.get("findings", []):
            severity = finding.get("severity", "unknown")
            severities[severity] = severities.get(severity, 0) + 1
        reviews.append({"verdict": review.get("verdict"),
                        "candidate_commit": review.get("candidate_commit"),
                        "packet_sha256": review.get("packet_sha256"),
                        "finding_count": len(review.get("findings", [])),
                        "findings_by_severity": severities})
    elapsed = {"started_utc": context.get("started_utc"), "completed_utc": None,
               "duration_s": None,
               "scope": "wall time to this observation, including waits and recovery gaps; not model execution time"}
    try:
        clock = telemetry.measure(telemetry.started())
        end = clock["completed_utc"]
        seconds = (datetime.fromisoformat(end) - datetime.fromisoformat(elapsed["started_utc"])).total_seconds()
        if seconds >= 0:
            elapsed.update(completed_utc=end, duration_s=seconds)
    except (TypeError, ValueError, KeyError):
        pass
    return {"coverage": "recorded_outcome" if outcome is not None else "no_outcome",
            "status": result.get("status"), "candidate_commit": result.get("candidate_commit"),
            "coding_session": result.get("coding_session"),
            **({"artifacts": result["artifacts"], "acceptance_pending": True} if result.get("status") == "artifacts_delivered" else {}),
            "context": context, "reviews": reviews, "host_findings": result.get("host_findings", []),
            "elapsed": elapsed, "scope": "recorded verdicts, not a new quality score or proof of defect resolution; no integration claim"}


def round_reports(rounds):
    """Join existing round references for reading; never total successive snapshots."""
    rows, seen = [], set()
    for record in rounds:
        identity = (record["outcome"]["path"], record["outcome"]["sha256"])
        if identity in seen:
            continue
        seen.add(identity)
        row = {"references": record}
        try:
            outcome = json.loads(_reference(record["outcome"]))
            row["delivery"] = delivery(outcome)
            retained = outcome.get("delivery_report", {})
            saved = retained.get("delivery")
            if saved is not None:
                if (saved.get("status"), saved.get("candidate_commit")) != (outcome.get("status"), outcome.get("candidate_commit")):
                    raise ValueError("delivery differs from round outcome")
                # Outcome reviews and host findings own the facts; an older saved summary can
                # retain timing/context without hiding newer review fields.
                row["delivery"] = {**saved, "reviews": row["delivery"]["reviews"],
                                   "host_findings": row["delivery"]["host_findings"]}
                for kind in ("coding", "reviews", "team_execution"):
                    if kind in retained:
                        row[kind] = retained[kind]
                row["usage_source"] = "outcome.delivery_report"
            if "usage" not in record:
                raise ValueError("round has no retained usage")
            if outcome.get("task_usage") != record["usage"]:
                raise ValueError("usage reference differs from round outcome")
            usage = json.loads(_reference(record["usage"]))
            row["usage_source"] = "referenced usage.json"
            for kind in ("coding", "reviews"):
                view = usage.get(kind, {})
                row[kind] = {key: view.get(key) for key in ("coverage", "provider_usage_metrics")}
                if "claude_session_usage" in view:
                    row[kind]["claude_session_usage"] = view["claude_session_usage"]
                if "by_backend" in view:
                    row[kind]["by_backend"] = view["by_backend"]
            if "team_execution" in usage:
                row["team_execution"] = usage["team_execution"]
        except Exception as exc:
            row["usage_unavailable"] = _unknown(exc)
        rows.append(row)
    return rows


def delivery_report(outcome, usage, context=None):
    """Publish with outcome.json atomically, never ahead of the owner's verdict."""
    report = {"delivery": delivery(outcome, context),
              "usage_reference": outcome.get("task_usage"),
              "actual_account_charge": "unknown"}
    for kind in ("coding", "reviews"):
        view = usage.get(kind, {})
        report[kind] = {key: view.get(key) for key in ("coverage", "provider_usage_metrics")}
        if "claude_session_usage" in view:
            report[kind]["claude_session_usage"] = view["claude_session_usage"]
        if "by_backend" in view:
            report[kind]["by_backend"] = view["by_backend"]
    if "team_execution" in usage:
        report["team_execution"] = usage["team_execution"]
    return report


def task_usage(parent, snapshot, *, native_team=None):
    """Keep provider/role counters separate; missing observations are not zero."""
    result = {"schema_version": 1, "coding": coding_usage(parent),
            "reviews": task_usage_from_snapshot(snapshot),
            "uncovered_scopes": ["predispatch model judgement", "coordinator work",
                                 "reviewer child agents", "nested PDCL model calls"],
            "actual_account_charge": "unknown",
            "usage_scope": "one goal's retained coding receipt and final review snapshot; not a full-task or cross-provider total"}
    if native_team is not None:
        result["team_execution"] = team_execution(result["coding"], native_team)
    return result


def _requested_model_usage(reviews, role="review"):
    """Break down observed native role counters by requested settings, per backend.

    Requests are not proof of the served model. Missing assignments keep their
    counters in an unknown group; failures are work too, not discarded samples.
    """
    rows = []
    for backend, calls in sorted(reviews.items()):
        total = task_usage_from_snapshot({"input_bytes": None, "calls": calls})
        groups = {}
        for call in calls:
            requested = call.get("requested_compute") or {}
            key = (requested.get("model"), requested.get("effort"))
            groups.setdefault(key, []).append(call)
        for (model, effort), selected in groups.items():
            usage = task_usage_from_snapshot({"input_bytes": None, "calls": selected})
            row = {"backend": backend, "requested_model": model, "requested_effort": effort,
                   "served_model": None, "model_basis": "requested settings; served model unobserved",
                   "receipt_call_count": len(selected),
                   "non_success_call_count": len(usage["non_success_call_ids"]),
                   "assignment_conflict_call_count": sum(c["requested_compute_coverage"] == "conflicting" for c in selected),
                   "provider_usage_metrics": usage["provider_usage_metrics"]}
            for field in ("input_tokens", "output_tokens"):
                value = usage["provider_usage_metrics"][field]["observed_sum"]
                denominator = total["provider_usage_metrics"][field]["observed_sum"]
                row["observed_" + field + "_share"] = value / denominator if value is not None and denominator else None
            row["scope"] = f"deduplicated {role} attempts only; shares use observed counters within this backend, including unknown assignments; not coding or account costs"
            rows.append(row)
    return rows


def _native_attempts(calls, role, gaps):
    """Reconcile repeated native snapshots once for each actual role execution."""
    unique, conflicts, duplicates = {}, set(), 0
    requested_values = {}
    for call in calls:
        # call_id derives from request content: two real attempts may share it.
        # A cleanup attempt id names the actual execution; old evidence paths
        # scope older calls that lack that id. Snapshot position never does.
        try:
            attempt = (call.get("cleanup") or {}).get("attempt_id")
            identity = ("attempt", attempt) if attempt else (
                ("evidence", str(Path(call["evidence"]).resolve())) if call.get("evidence") else None)
            if (not identity or not isinstance(identity[1], str)
                    or not isinstance(call.get("backend"), str) or not call["backend"]
                    or not isinstance(call.get("call_id"), str) or not call["call_id"]):
                raise ValueError(f"{role} execution identity unavailable")
        except (AttributeError, TypeError, ValueError) as exc:
            gaps.append({"kind": f"{role}_identity", "reason": str(exc)})
            continue
        key = identity
        values = requested_values.setdefault(key, {"model": set(), "effort": set()})
        requested = call.get("requested_compute")
        for field in values:
            value = requested.get(field) if isinstance(requested, dict) else None
            if isinstance(value, str) and value:
                values[field].add(value)
        signature = (call["backend"], call["call_id"], call.get("usage"), call.get("provider_usage_observed"), call.get("outcome"))
        if key in unique:
            duplicates += 1
            if unique[key][0] != signature:
                conflicts.add(key)
        else:
            unique[key] = (signature, call)
    reviews = {}
    for key, (_, call) in unique.items():
        if key in conflicts:
            gaps.append({"kind": f"{role}_conflict", "call_id": call["call_id"], "reason": f"conflicting snapshots of one {role} execution"})
            continue
        values = requested_values[key]
        ambiguous = any(len(v) > 1 for v in values.values())
        requested = {field: next(iter(v)) if len(v) == 1 else None for field, v in values.items()}
        if ambiguous:
            gaps.append({"kind": f"{role}_assignment_conflict", "call_id": call["call_id"],
                         "reason": "conflicting requested settings; counters retained, ambiguous fields unknown"})
        reviews.setdefault(call["backend"], []).append({k: call.get(k) for k in (
            "call_id", "backend", "provider_usage_observed", "usage", "outcome", "evidence",
            "cumulative_session", "ended_utc")} | {
                "attempt_id": (call.get("cleanup") or {}).get("attempt_id"),
                "requested_compute": requested,
                "requested_compute_coverage": "conflicting" if ambiguous else (
                    "recorded" if any(requested.values()) else "unavailable")})
    return reviews, duplicates


def _codex_sessions(calls, gaps, role='manager'):
    """Select one latest verified cumulative exec total per retained session."""
    from .foundry_codex import usage_observation
    sessions = {}
    selected = []
    for call in calls:
        try:
            observation = usage_observation(call)
            stamp = datetime.fromisoformat(observation['ended_utc'].replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                raise ValueError('Codex usage timestamp has no timezone')
            sessions.setdefault(observation['session_id'], []).append((stamp, observation))
        except Exception as exc:
            gaps.append({'kind': role + '_usage', 'call_id': call['call_id'], 'reason': str(exc)[:240]})
    for session, observations in sessions.items():
        try:
            ordered = sorted(observations, key=lambda row: row[0])
            for (before_time, before), (after_time, after) in zip(ordered, ordered[1:]):
                if before_time == after_time and before['usage'] != after['usage']:
                    raise ValueError('conflicting Codex session snapshots at the same time')
                if any(type(value) is int and type(after['usage'].get(key)) is int
                       and after['usage'][key] < value for key, value in before['usage'].items()):
                    raise ValueError('Codex cumulative session counters regressed')
            latest = dict(ordered[-1][1])
            # A cumulative total cannot be assigned to just the final model if
            # a conversation's requested settings differ or were unrecorded.
            choices = [row.get('requested_compute') or {} for _, row in ordered]
            if any(choice != choices[0] for choice in choices):
                latest['requested_compute'] = {}
                latest['requested_compute_coverage'] = 'conflicting'
            selected.append(latest)
        except (ValueError, TypeError, KeyError) as exc:
            gaps.append({'kind': role + '_usage', 'session_id': session, 'reason': str(exc)[:240]})
    return selected


def task_report(paths):
    """Read selected goal outcomes and their links without running or changing work.

    Automatic roots are containers, not another execution. Explicitly supplied
    unlinked reviews stay separate selections; tag/name similarity is not a link.
    """
    records, links, gaps, pending, seen, cache = [], [], [], [], set(), {}
    coding, coding_gaps, review_calls, manager_calls, expert_calls, consultations = [], [], [], [], [], []
    dispatches, sessions = [], []

    def read(ref):
        key = (str(Path(ref["path"]).resolve()), ref["sha256"])
        if key not in cache:
            cache[key] = json.loads(_reference(ref))
        return cache[key]

    def enqueue(ref, origin=None, relation="selected"):
        if origin is not None:
            links.append({"from": origin, "to": ref, "relation": relation})
        pending.append(ref)

    def snapshot(path):
        path = Path(path).resolve()
        selected_manifest = path/'manifest.json' if path.is_dir() else path if path.name == 'manifest.json' else None
        if selected_manifest and selected_manifest.is_file():
            manifest = json.loads(_bytes(selected_manifest))
            if manifest.get('runtime_upgrades'):
                # The root now owns a round pointer. Keep the root reference so
                # its complete rounds/history are traversed, not only the last.
                path = selected_manifest
        if path.is_dir():
            path = path / ("outcome.json" if (path / "outcome.json").is_file() else "manifest.json")
        # A completed invocation selected by its manifest is still counted once.
        if (path.name == "manifest.json" and path.with_name("outcome.json").is_file()
                and not json.loads(_bytes(path)).get('runtime_upgrades')):
            path = path.with_name("outcome.json")
        raw = _bytes(path)
        ref = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
        cache[(str(path), ref["sha256"])] = json.loads(raw)
        return ref

    def execution_view(observation):
        view = {key: observation.get(key) for key in (
            "session_id", "turn_id", "execution", "receipt", "window")}
        if not observation.get("receipt"):
            return view
        try:
            receipt = read(observation["receipt"])
            terminal_ref = next((r for r in receipt.get("evidence_refs", []) if r.get("kind") == "terminal"), None)
            if terminal_ref:
                terminal = read(terminal_ref)
                binding = ({"attempt_id": terminal.get("attempt_id"), "thread_id": terminal.get("session_id"), "turn_id": None}
                           if terminal.get("schema_version") == "pdcl-claude-terminal-1" else terminal.get("binding", {}))
                if (binding.get("attempt_id"), binding.get("thread_id"), binding.get("turn_id")) != (
                        observation.get("attempt_id"), observation.get("session_id"), observation.get("turn_id")):
                    raise ValueError("terminal identity differs from coding observation")
                view["terminal_reference"] = terminal_ref
                view["expiration_cause"] = terminal.get("expiration_cause")
                view["interrupt_result"] = terminal.get("interrupt_result")
        except Exception as exc:
            gaps.append({"reference": observation["receipt"], "kind": "execution_detail", "reason": str(exc)})
        return view

    def configuration_view(ref, *, manifest=None):
        # Configuration is a separate observation, not proof that a prompt was
        # delivered or that the provider served the requested model.
        view = {"status": "unavailable", "source": "adjacent manifest read now; not bound by outcome",
                "scope": "recorded configuration only; not prompt delivery, served model or optimization benefit"}
        try:
            if manifest is None:
                path = Path(ref["path"]).with_name("manifest.json")
                raw = _bytes(path)
                config_ref = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
                manifest = json.loads(raw)
            else:
                config_ref = ref
                view["source"] = "selected manifest observation"
            view["reference"] = config_ref
            if not isinstance(manifest, dict):
                raise ValueError("manifest is not an object")
            team = manifest.get("native_team")
            team_view = {"status": "not_recorded"}
            if team is not None:
                if not isinstance(team, dict):
                    raise ValueError("native_team is not an object")
                team_view = {"status": "recorded", **{key: team.get(key) for key in (
                    "profile", "version", "worker_capacity", "calibration_prompt_format")}}
                config = team.get('config') or {}
                native_wait = config.get('features.multi_agent_v2') or {}
                if not isinstance(native_wait, dict):
                    native_wait = {}
                team_view['native_wait'] = {
                    'process_completion_wait': team.get('process_completion_wait'),
                    'member_wait': {k:native_wait[k] for k in ('min_wait_timeout_ms', 'default_wait_timeout_ms',
                        'max_wait_timeout_ms', 'non_code_mode_only') if k in native_wait},
                    'process_max_wait_ms': config.get('background_terminal_max_timeout'),
                    'code_mode_default_yield_ms': config.get('features.code_mode.default_exec_yield_time_ms'),
                    'scope': 'Frozen native configuration; not a measured wait or execution timeout'}
                feedback = team.get("calibration")
                team_view["calibration"] = {"status": "not_recorded"}
                if feedback is not None:
                    if (not isinstance(feedback, dict) or not isinstance(feedback.get("samples"), list)
                            or not isinstance(feedback.get("gaps"), list)):
                        raise ValueError("calibration samples/gaps are not recorded lists")
                    team_view["calibration"] = {"status": "recorded", "sample_count": len(feedback["samples"]),
                        "gap_count": len(feedback["gaps"]), "selection": feedback.get("selection"),
                        "sample_outcomes": [sample.get("outcome") for sample in feedback["samples"]]}
            view.update(status="observed", requested_compute=manifest.get("requested_compute"),
                        compute_assignment=manifest.get("compute_assignment"), native_team=team_view,
                        execution_decision=manifest.get("execution_decision"))
        except Exception as exc:
            view["reason"] = str(exc)
        return view

    def collect_experts(observations):
        for consultation in observations:
            consultations.append({k: v for k, v in consultation.items() if k != 'calls'})
            expert_calls.extend(consultation.get('calls', []))
            gaps.extend({'kind': 'expert_usage', 'reason': e} for e in consultation.get('errors', []))

    def collect_usage(usage_ref, row, *, projected=None, current_experts=False):
        usage = read(usage_ref) if projected is None else projected
        if not current_experts:
            collect_experts(usage.get('expert_consultations', []))
        row["usage_reference"] = usage_ref
        view = usage.get("coding", {})
        local = _coding_observations(
            [(call["phase"], call["observation"]) for call in view.get("observations", [])],
            view.get("missing_observations", []))
        row["coding_model_observation"] = {
            "coverage": local["coverage"], "source": usage_ref,
            "codex_turn_models": [{key: model[key] for key in (
                "position", "model", "effort", "observed_turn_count")}
                for model in _model_usage(local)],
            "gaps": local.get("missing_observations", []),
            "scope": "models observed in this usage artifact; Codex native turn declarations, not requested models; "
                     "Claude cumulative session snapshots may include earlier rounds; no per-round cost or causal attribution"}
        if "reason" in local:
            row["coding_model_observation"]["reason"] = local["reason"]
        if "claude_session_usage" in local:
            claude = local["claude_session_usage"]
            row["coding_model_observation"]["claude_cumulative_models"] = {
                "coverage": claude["coverage"], "models": [r["model"] for r in claude["by_model"]],
                "session_ids": [r["session_id"] for r in claude["sessions"]],
                "gaps": claude["gaps"]}
        for call in view.get("observations", []):
            phase, observation = call["phase"], call["observation"]
            coding.append((phase, observation))
            if phase == "coding" and observation.get("session_id"):
                sessions.append((row["reference"], observation["session_id"], observation.get("turn_id")))
                row.setdefault("coding_executions", []).append(execution_view(observation))
        coding_gaps.extend(view.get("missing_observations", []))
        if view.get("coverage") not in {"native-receipts", "partial"}:
            coding_gaps.append({"reference": row["reference"], "reason": view.get("reason", "coding not observed")})
        review_calls.extend(usage.get("reviews", {}).get("calls", []))
        manager = usage.get('manager')
        if manager is not None:
            row['manager_usage'] = {'scope': manager['scope'], 'source': usage_ref}
            for call in manager.get('snapshot', {}).get('calls', []):
                manager_calls.append({**call, 'attempt_id': (call.get('cleanup') or {}).get('attempt_id')})

    for path in paths:
        try:
            enqueue(snapshot(path))
        except Exception as exc:
            gaps.append({"path": str(path), "reason": str(exc)})
    selected = list(pending)
    while pending:
        ref = pending.pop()
        try:
            key = (str(Path(ref["path"]).resolve()), ref["sha256"])
            if key in seen:
                continue
            seen.add(key)
            outcome = read(ref)
            if isinstance(outcome, dict) and outcome.get("schema_version") == "pdcl-resultreceipt-1":
                receipt_path = Path(ref["path"])
                if not receipt_path.name.endswith(".receipt.json"):
                    raise ValueError("dispatch selection needs its original .receipt.json path")
                log = receipt_path.with_name(receipt_path.name.removesuffix(".receipt.json") + ".log")
                observation = capture(log, outcome.get("order_id"))
                if observation.get("receipt") != ref:
                    raise ValueError(observation.get("reason", "dispatch receipt changed during read"))
                row = {"reference": ref, "kind": "selected_dispatch",
                       "order_id": outcome.get("order_id"), "execution": outcome.get("execution"),
                       "verification": outcome.get("verification"),
                       "session_id": observation.get("session_id"), "turn_id": observation.get("turn_id"),
                       "window": observation.get("window"), "usage_scope": "separate selected dispatch; not added to Foundry coding"}
                row["execution_detail"] = execution_view(observation)
                records.append(row)
                dispatches.append((row, observation))
                continue
            if (Path(ref["path"]).name == "manifest.json" and isinstance(outcome, dict)
                    and outcome.get("schema_version") == 1 and isinstance(outcome.get("order"), dict)):
                row = {"reference": ref, "kind": "unfinished_invocation",
                       "manager_phase": outcome.get('manager_phase'),
                       "delivery": {"status": outcome.get("state", "no_outcome"), "error": outcome.get("error"),
                                    "context": outcome.get("delivery_context", {}),
                                    "scope": "manifest observation; no terminal outcome or integration claim"},
                       "source": "current manifest and adjacent usage read now; not bound by an outcome"}
                records.append(row)
                row["configuration_observation"] = configuration_view(ref, manifest=outcome)
                continued = (outcome.get("delivery_context") or {}).get("continues")
                if continued:
                    enqueue(continued, ref, "continues")
                if not (outcome.get('runtime_upgrades') and outcome.get('state') == 'completed'):
                    gaps.append({"reference": ref, "kind": "missing_outcome", "reason": "no terminal outcome retained"})
                previous = (outcome.get("amendment") or {}).get("prior_outcome")
                if previous:
                    enqueue(previous, ref, "amends")
                if outcome.get("mode") == "auto-amend":
                    row["kind"] = ('completed_upgraded_root' if outcome.get('runtime_upgrades') and
                                   outcome.get('state') == 'completed' else 'unfinished_automatic_root')
                    for round_record in outcome.get("rounds", []):
                        try:
                            enqueue(round_record["outcome"], ref, "round")
                        except (TypeError, KeyError) as exc:
                            gaps.append({"reference": ref, "kind": "round", "reason": str(exc)})
                    if outcome.get("active_round"):
                        active = snapshot(outcome["active_round"])
                        enqueue(active, ref, "active_round_current_read")
                    continue
                from ._expert_consultation import observe as observe_experts
                # An interrupted usage snapshot may predate the current expert
                # attempt. The selected unfinished manifest owns its live view.
                collect_experts(observe_experts(outcome.get('manager_state') or outcome.get('expert_state')))
                try:
                    try:
                        usage_ref = snapshot(Path(ref["path"]).with_name("usage.json"))
                    except FileNotFoundError:
                        # The goal already freezes returned managed turns here.
                        # Reuse that owner, never scan growing logs or native sessions.
                        checkpoint_ref = outcome['coding_checkpoint']
                        checkpoint = read(checkpoint_ref)
                        if (checkpoint.get('schema_version') != 1 or
                                any(checkpoint.get(k) != outcome.get(k)
                                    for k in ('run_id', 'order', 'worktree'))):
                            raise ValueError('coding checkpoint belongs to another invocation')
                        projected = {'coding': coding_usage(checkpoint['coding_usage'])}
                        row['checkpoint_usage_observation'] = {
                            'reference': checkpoint_ref, 'manager_phase': outcome.get('manager_phase'),
                            'scope': 'retained returned coding turns only; current or later execution, '
                                     'manager/reviewer usage and final acceptance are not covered'}
                        collect_usage(checkpoint_ref, row, projected=projected)
                    else:
                        collect_usage(usage_ref, row, current_experts=True)
                except Exception as exc:
                    gap = {"reference": ref, "kind": "usage", "reason": str(exc)}
                    gaps.append(gap)
                    coding_gaps.append(gap)
                continue
            if (not isinstance(outcome, dict) or not isinstance(outcome.get("reviews"), list)
                    or not isinstance(outcome.get("status"), str)):
                raise ValueError("not a retained Foundry goal/review outcome")
            report = outcome.get("delivery_report", {})
            row = {"reference": ref, "delivery": delivery(outcome), "kind": "invocation"}
            row["review_correction_guidance"] = [
                {"reviewer_index": index, "verdict": review.get("verdict"),
                 "finding_count": len(review.get("findings", [])),
                 "guidance": review.get("correction_guidance")}
                for index, review in enumerate(outcome["reviews"])]
            row["configuration_observation"] = configuration_view(ref)
            records.append(row)
            saved = report.get("delivery")
            if isinstance(saved, dict) and (saved.get("status"), saved.get("candidate_commit")) == (
                    outcome.get("status"), outcome.get("candidate_commit")):
                row["delivery"] = {**saved, "reviews": row["delivery"]["reviews"],
                                   "host_findings": row["delivery"]["host_findings"]}
                # Useful compact evidence survives a missing full usage file, but
                # without call identities it cannot enter a deduplicated total.
                row["retained_metrics"] = {k: report[k] for k in ("coding", "reviews", "team_execution") if k in report}
            context = row["delivery"].get("context", {})
            if context.get("continues"):
                enqueue(context["continues"], ref, "continues")
            previous = context.get("amends")
            if not context:
                # Legacy manifests are a separately labelled current read. Never
                # pretend the adjacent file is hash-bound by the outcome itself.
                manifest_path = Path(ref["path"]).with_name("manifest.json")
                try:
                    raw = _bytes(manifest_path)
                    manifest = json.loads(raw)
                    row["legacy_manifest"] = {"path": str(manifest_path), "sha256": hashlib.sha256(raw).hexdigest(),
                                              "source": "adjacent manifest read now"}
                    previous = (manifest.get("amendment") or {}).get("prior_outcome")
                except Exception as exc:
                    gaps.append({"reference": ref, "kind": "legacy_context", "reason": str(exc)})
            if previous:
                enqueue(previous, ref, "amends")
            rounds = outcome.get("rounds")
            if isinstance(rounds, list):
                row["kind"] = "automatic_root"
                for record in rounds:
                    try:
                        enqueue(record["outcome"], ref, "round")
                    except (TypeError, KeyError) as exc:
                        gaps.append({"reference": ref, "kind": "round", "reason": str(exc)})
                continue
            try:
                collect_usage(outcome["task_usage"], row)
            except Exception as exc:
                gap = {"reference": ref, "kind": "usage", "reason": str(exc)}
                gaps.append(gap)
                coding_gaps.append(gap)
        except Exception as exc:
            gaps.append({"reference": ref, "reason": str(exc)})
    normalized = _coding_observations(coding, coding_gaps)
    for row, observation in dispatches:
        for source, session, turn in sessions:
            if session == row["session_id"] and row["turn_id"]:
                links.append({"from": source, "to": row["reference"],
                              "relation": "same_native_turn" if turn == row["turn_id"] else "same_native_session",
                              "scope": "native identity only; no candidate authorship, continuation order or acceptance inference"})
        dispatch_usage = coding_usage(observation)
        row["coding_model_usage"] = _model_usage(dispatch_usage)
        row["coding"] = {k: v for k, v in dispatch_usage.items() if k != "observations"}
    reviews, duplicates = _native_attempts(review_calls, "review", gaps)
    managers, manager_duplicates = _native_attempts(
        [c for c in manager_calls if c.get('backend') == 'codex'], "manager", gaps)
    for call in manager_calls:
        if call.get('backend') not in {'codex', 'claude'}:
            gaps.append({'kind': 'manager_identity', 'call_id': call.get('call_id'),
                         'reason': 'manager backend is missing or unsupported'})
    manager_sessions = _codex_sessions(managers.get('codex', []), gaps)
    experts, expert_duplicates = _native_attempts(expert_calls, 'expert', gaps)
    expert_sessions = _codex_sessions(experts.get('codex', []), gaps, role='expert')
    review_usage = dict(reviews)
    persistent = [call for call in reviews.get('codex', []) if call.get('cumulative_session')]
    if persistent:
        review_usage['codex'] = ([call for call in reviews['codex'] if not call.get('cumulative_session')]
                                 + _codex_sessions(persistent, gaps, role='review'))
    manager_view = task_usage_from_snapshot({'input_bytes': None, 'calls': manager_sessions})
    manager_view.update(
        coverage='partial' if any(g['kind'].startswith('manager_') for g in gaps if 'kind' in g)
                 else 'observed_sessions' if manager_sessions else 'unavailable',
        usage_scope='latest verified cumulative Codex snapshot per manager session; may include earlier work; '
                    'never sum resumed attempts; separate from coding, reviews and account charge')
    from .foundry_claude import session_usage
    claude_sessions = session_usage(reviews.get('claude', []))
    # The full source observations can contain nested member snapshots and
    # collaboration metadata. Keep them in the already linked usage artifacts.
    compact_coding = {k: v for k, v in normalized.items() if k != "observations"}
    compact_coding["turns"] = [{"call_id": call["call_id"], "phase": call["phase"],
                                "usage": call["usage"],
                                "model_observation": call["observation"].get("model_observation"),
                                "source": call["observation"].get("source")}
                               for call in normalized.get("observations", [])]
    report = {"schema_version": "pdcl-foundry-task-report-1", "selected_outcomes": [ref for ref in selected if isinstance(read(ref), dict)
                and isinstance(read(ref).get("reviews"), list) and isinstance(read(ref).get("status"), str)],
            "selected_records": selected,
            "records": records, "links": links, "gaps": gaps,
            "coding": compact_coding, "coding_model_usage": _model_usage(normalized),
            "reviews_by_backend": {backend: task_usage_from_snapshot({"input_bytes": None, "calls": calls})
                                   for backend, calls in reviews.items()},
            "review_requested_model_usage": _requested_model_usage(review_usage),
            "codex_review_session_usage": task_usage_from_snapshot({'input_bytes': None, 'calls': review_usage.get('codex', [])}),
            "claude_review_session_usage": claude_sessions,
            "claude_manager_session_usage": session_usage([c for c in manager_calls if c.get('backend') == 'claude']),
            "codex_manager_usage": manager_view,
            "manager_requested_model_usage": _requested_model_usage({'codex': manager_sessions}, role="manager session"),
            "duplicate_manager_observations": manager_duplicates,
            "expert_consultations": consultations,
            "claude_expert_session_usage": session_usage(experts.get('claude', [])),
            "codex_expert_usage": task_usage_from_snapshot({'input_bytes': None, 'calls': expert_sessions}),
            "expert_requested_model_usage": _requested_model_usage({'codex': expert_sessions}, role='expert session'),
            "duplicate_expert_observations": expert_duplicates,
            "duplicate_review_observations": duplicates,
            "coverage": "partial" if gaps or normalized.get("coverage") != "native-receipts" else "linked_records",
            "actual_account_charge": "unknown",
            "scope": "selected Foundry outcomes or unfinished manifests and explicit predecessor/round links; unlinked reviews need explicit selection; "
                     "explicit dispatch receipts are displayed separately and never added to Foundry counters; "
                     "Raw review snapshots retain every attempt; Codex review pricing/session usage counts cumulative formatting continuations once. "
                     "Manager and expert usage are separate: Codex reconciles attempts then selects the latest cumulative total per session; Claude uses cumulative sessions; unrecorded coordinator work, reviewer children and account charges are not included; "
                     "coding/review/provider counters stay separate; model shares include unknown-model coding groups and describe observed counters only; "
                     "no summed elapsed time, savings claim or task-wide acceptance verdict"}
    from ._foundry_cost import report_cost
    try:
        report["api_price_equivalent"] = report_cost(report)
    except Exception as exc:
        report["api_price_equivalent"] = _unknown(exc)
    try:
        report['execution_efficiency'] = execution_efficiency(report, normalized)
    except Exception as exc:
        report['execution_efficiency'] = _unknown(exc)
    return report


def execution_efficiency(report, coding):
    """Project existing reconciled evidence; never introduce a second cost ledger."""
    members, waits, gaps = [], [], list(coding.get('missing_observations', []))
    wait_identities = {}
    claude = team_execution(coding, {'backend': 'claude'})
    for row in claude['model_usage']:
        members.append({'backend': 'claude', **row,
            'coverage': 'observed_responses' if all(v is not None for v in row['usage'].values()) else 'partial'})
    for identity in claude['conflicting_response_ids']:
        gaps.append({'kind': 'member_usage_conflict', 'identity': identity})
    for call in coding.get('observations', []):
        observation = call['observation']
        backend = call.get('backend', 'codex')
        if backend == 'codex':
            models = observation.get('model_observation', {}).get('declarations', [])
            members.append({'backend': backend, 'session_id': observation.get('session_id'),
                'turn_id': observation.get('turn_id'),
                'position': 'leader' if call['phase'] == 'coding' else 'member',
                'model': models[0].get('model') if len(models) == 1 else None,
                'initial_assignment_activity': observation.get('parent_binding', {}).get('declared_activity'),
                'usage': observation.get('usage'), 'coverage': observation.get('coverage')})
        signal = (observation.get('context_signals') or {}).get('waiting_signals') if backend == 'claude' else observation.get('waiting_signals')
        if signal is None:
            gaps.append({'kind': 'waiting_unobserved', 'call_id': call['call_id']})
        else:
            selected = []
            for wait in signal.get('calls', []):
                key = (backend, observation.get('session_id'), observation.get('turn_id'), wait['call_id'])
                if key not in wait_identities:
                    observed_wait = dict(wait)
                    wait_identities[key] = observed_wait
                    selected.append(observed_wait)
                else:
                    wait_identities[key]['returned'] |= wait.get('returned', False)
                    for field in ('result', 'observed_seconds'):
                        if field in wait:
                            wait_identities[key][field] = wait[field]
            for interval in signal.get('intervals', []):
                key = (backend, observation.get('session_id'), observation.get('turn_id'), interval['call_id'])
                if key in wait_identities:
                    wait_identities[key]['interval'] = interval
            waits.append({'backend': backend, 'call_id': call['call_id'],
                **{k:v for k,v in signal.items() if k != 'intervals'}, 'calls': selected})
        if backend == 'claude' and not (observation.get('context_signals') or {}).get('response_usage'):
            gaps.append({'kind': 'member_usage_unobserved', 'call_id': call['call_id']})
    # Cumulative Claude sessions spanning roles remain a shared-stage row.
    # These are references to the existing cost rows, never additive totals.
    stages = [{'stage': row['role'], 'cost_row_index': index,
               'model': row.get('model'), 'backend': row.get('backend'),
               'token_breakdown': row.get('token_breakdown'),
               'api_price_equivalent_usd': {'min': row.get('usd_min'), 'max': row.get('usd_max')},
               'attribution': 'shared_session' if len(row.get('roles', [])) > 1 else 'recorded_role'}
              for index, row in enumerate(report.get('api_price_equivalent', {}).get('rows', []))]
    states = [{'reference': row.get('reference'), 'recorded_status': row.get('delivery', {}).get('status'),
               'manager_phase': row.get('manager_phase', row.get('checkpoint_usage_observation', {}).get('manager_phase')),
               'waiting_reason': 'decision_required' if row.get('delivery', {}).get('status') == 'awaiting_input'
                   or row.get('manager_phase') == 'awaiting_input' else 'not_established'}
              for row in report.get('records', [])]
    wait_summary = {}
    for call in wait_identities.values():
        group = wait_summary.setdefault(call.get('reason', 'unknown'), {
            'calls': 0, 'returned': 0, 'timeouts': 0, 'timed_calls': 0, 'max_observed_seconds': None})
        group['calls'] += 1
        group['returned'] += bool(call.get('returned'))
        group['timeouts'] += call.get('result') == 'timeout'
        if call.get('observed_seconds') is not None:
            group['timed_calls'] += 1
            group['max_observed_seconds'] = max(group['max_observed_seconds'] or 0, call['observed_seconds'])
    return {'stage_costs': stages, 'member_usage': members, 'waiting_observations': waits,
            'waiting_by_reason': wait_summary,
            'recorded_states': states, 'gaps': gaps, 'coverage': 'partial',
            'scope': 'Detail of existing api_price_equivalent/coding observations, not additive. '
                     'Stage means recorded coding/review/manager/expert role; reading/rework token allocation unknown. '
                     'Historical states are not current liveness; per-call durations require paired native timestamps, not wasted tokens. '
                     'Member coverage is observed only; absent descendants and reviewer members remain unknown. '
                     'No elapsed-time sum, billing, acceptance or savings claim.'}
