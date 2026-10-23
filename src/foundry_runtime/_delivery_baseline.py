"""Delivery associations projected from the existing task-report traversal."""
from collections import Counter
from pathlib import Path

from . import _foundry_goal_telemetry as telemetry


def build(report, read):
    """Join retained outcomes and checkpoints using the report's reference reader."""
    invocations, containers, dispatches = [], [], []
    for record in report["records"]:
        ref, kind = record["reference"], record["kind"]
        links = [link for link in report["links"] if ref in (link["from"], link["to"])]
        if kind == "selected_dispatch":
            dispatches.append({"reference": ref, "order_id": record.get("order_id"),
                "execution": record.get("execution_detail"),
                "verification": record.get("verification"), "links": links})
            continue
        if kind not in {"invocation", "unfinished_invocation"}:
            containers.append({"reference": ref, "kind": kind, "links": links})
            continue

        source = read(ref)
        delivery = record["delivery"]
        context = delivery.get("context") or {}
        observation = record.get("configuration_observation", {})
        manifest_ref = observation.get("reference")
        gaps, checks = [], []
        manifest = {}
        if manifest_ref:
            try:
                manifest = read(manifest_ref)
                if not isinstance(manifest, dict):
                    raise ValueError("manifest is not an object")
                if any(context.get(key) is not None and context[key] != manifest.get(key)
                       for key in ("run_id", "order")):
                    raise ValueError("adjacent manifest belongs to another invocation")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                manifest = {}
                gaps.append({"kind": "manifest", "reference": manifest_ref, "reason": str(exc)})
        task = {key: context.get(key, manifest.get(key)) for key in ("order", "run_id")}
        candidate = {"base_commit": manifest.get("base_commit"),
                     "candidate_commit": source.get("candidate_commit")}
        closure = source.get("candidate_closure")
        if closure is not None:
            checks.append({"kind": "candidate_closure", "source": ref, "result": closure})
        checkpoint_ref = manifest.get("review_checkpoint")
        if checkpoint_ref:
            try:
                checkpoint = read(checkpoint_ref)
                reviewed = checkpoint["context"]
                if not isinstance(reviewed, dict):
                    raise ValueError("review context is not an object")
                if any(task[key] is not None and task[key] != checkpoint.get(key)
                       for key in ("run_id", "order")):
                    raise ValueError("review checkpoint belongs to another invocation")
                if (candidate["candidate_commit"] is not None and
                        candidate["candidate_commit"] != reviewed.get("candidate_commit")):
                    raise ValueError("review checkpoint belongs to another candidate")
                candidate = {key: reviewed.get(key, value) for key, value in candidate.items()}
                if "project_review" in reviewed:
                    checks.append({"kind": "project_review", "source": checkpoint_ref,
                                   "result": reviewed["project_review"]})
            except (OSError, ValueError, KeyError, TypeError) as exc:
                gaps.append({"kind": "review_checkpoint", "reference": checkpoint_ref, "reason": str(exc)})
        elapsed = delivery.get("elapsed") or {}
        sessions = source.get("review_sessions") or []
        coding = source.get("coding") or {}
        invocations.append({
            "reference": ref, "kind": kind, "status": delivery.get("status"), "task": task,
            "manifest_observation": observation, "candidate": candidate,
            "runtime_provenance": manifest.get("runtime_provenance"),
            "coding": {"session_id": source.get("coding_session"), "returncode": coding.get("returncode"),
                       "evidence": coding.get("evidence", []),
                       "executions": record.get("coding_executions", []),
                       "usage_reference": record.get("usage_reference")},
            "checks": checks,
            "reviews": [{**review, "reviewer_index": index, "source": ref,
                         "session_id": sessions[index] if index < len(sessions) else None}
                        for index, review in enumerate(delivery.get("reviews", []))],
            "project_acceptance": source.get("project_acceptance"),
            "artifacts": source.get("artifacts", []),
            "elapsed": elapsed if elapsed.get("duration_s") is not None else None,
            "phase_timing": {"source": "adjacent telemetry read now",
                             "path": str(Path(ref["path"]).with_name(telemetry.FILENAME)),
                             "summary": telemetry.summarize(Path(ref["path"]).parent)},
            "links": links, "gaps": gaps})
    return {"unit": "goal_invocation", "invocations": invocations, "containers": containers,
            "selected_dispatches": dispatches, "summary": {
                "invocation_count": len(invocations), "container_count": len(containers),
                "selected_dispatch_count": len(dispatches),
                "status_counts": dict(Counter(row["status"] for row in invocations)),
                "review_verdict_counts": dict(Counter(review["verdict"] for row in invocations
                                                      for review in row["reviews"])),
                "project_acceptance_counts": dict(Counter(
                    (row["project_acceptance"] or {}).get("status", "not_recorded") for row in invocations)),
                "elapsed_observation_count": sum(row["elapsed"] is not None for row in invocations)}}
