"""Read task evidence for the native manager's next model/effort decision.

No learned ranking: tasks and mixed teams are not interchangeable model trials.
The goal usage owner supplies deduplication, coverage and observed model identity.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess

from ._execution_usage import _reference
from ._foundry_evidence import evidence_roots
from ._foundry_goal_usage import task_report
from ._task_identity import normalized_line
from ._project_context import invocation_adapter


def _capture(path):
    raw = path.read_bytes()
    return json.loads(raw), {"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest()}


def _repository(tree):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    result = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            cwd=tree, env=env, capture_output=True, timeout=10, check=True)
    return Path(os.fsdecode(result.stdout).strip()).resolve()


def _sample(path, manifest, manifest_ref, order_text):
    report = task_report([path])
    selected = report["selected_outcomes"][0]
    terminal = next(row for row in report["records"] if row["reference"] == selected)
    # Usage may include unfinished predecessor manifests. They supply observed
    # usage and missing-evidence context, not settled rounds or empty reviews.
    round_records = [row for row in report["records"] if row["kind"] == "invocation"]
    rounds = [row["delivery"] for row in round_records]
    models = []
    for row in report["coding_model_usage"]:
        metrics = row["provider_usage_metrics"]
        models.append({key: row.get(key) for key in ("position", "model", "effort",
            "observed_turn_count", "observed_uncached_input_tokens", "observed_input_share",
            "observed_output_share", "observed_cache_hit_ratio")} | {
                "input_tokens": metrics["input_tokens"]["observed_sum"],
                "output_tokens": metrics["output_tokens"]["observed_sum"]})
    sample = {"outcome": selected, "manifest": manifest_ref, "order": manifest["order"],
            "task_excerpt": order_text[:1000], "task_excerpt_truncated": len(order_text) > 1000,
            "team_version": (manifest.get("native_team") or {}).get("version"),
            "status": terminal["delivery"]["status"],
            "candidate_commit": terminal["delivery"].get("candidate_commit"),
            "final_review_verdicts": [r["verdict"] for r in terminal["delivery"]["reviews"]],
            "round_count": len(rounds),
            "unfinished_records": [{"reference": row["reference"], "kind": row["kind"],
                                    "status": row["delivery"]["status"]}
                                   for row in report["records"] if row["kind"].startswith("unfinished_")],
            "amendment_rounds": (sum(r["context"]["mode"] == "amendment" for r in rounds)
                                 if all(r.get("context", {}).get("mode") for r in rounds) else None),
            "blocked_rounds": sum(r["status"] == "review_blocked" for r in rounds),
            "other_round_statuses": sorted({r["status"] for r in rounds
                                           if r["status"] not in {"candidate_verified", "review_blocked"}}),
            "findings_recorded": sum(r["finding_count"] for d in rounds for r in d["reviews"]),
            "host_findings_recorded": sum(len(d.get("host_findings", [])) for d in rounds),
            "coverage": report["coverage"], "gap_count": len(report["gaps"]),
            "coding_models": models,
            "review_backends": sorted(report["reviews_by_backend"])}
    correction = {"recorded_review_count": 0, "unrecorded_review_count": 0, "rounds": []}
    repair_links = [link for link in report["links"] if link["relation"] in {"amends", "continues"}]
    for record in round_records:
        reviews = []
        for review in record.get("review_correction_guidance", []):
            advice = review["guidance"]
            if isinstance(advice, dict):
                correction["recorded_review_count"] += 1
                reviews.append({key: review[key] for key in ("reviewer_index", "verdict", "finding_count")} | {
                    "cause": advice.get("cause"), "recommended_model_action": advice.get("model")})
            else:
                correction["unrecorded_review_count"] += 1
        config = record.get("configuration_observation", {})
        execution = config.get("execution_decision")
        decision = execution.get("correction") if isinstance(execution, dict) else None
        predecessors = [{"outcome": link["to"], "relation": link["relation"]}
                        for link in repair_links if link["from"] == record["reference"]]
        linked = predecessors or any(link["to"] == record["reference"] for link in repair_links)
        if reviews or isinstance(decision, dict) or linked:
            delivery = record["delivery"]
            row = {"outcome": record["reference"], "reviews": reviews,
                   "status": delivery["status"], "candidate_commit": delivery.get("candidate_commit"),
                   "review_verdicts": [r["verdict"] for r in delivery.get("reviews", [])],
                   "predecessors": predecessors,
                   "coding_model_observation": {key: value for key, value in record.get(
                       "coding_model_observation", {"coverage": "uncovered", "reason": "no readable usage artifact"}
                       ).items() if key != "scope"}}
            if predecessors and "legacy_manifest" in record:
                row["predecessor_source"] = record["legacy_manifest"]
            if isinstance(decision, dict):
                row["recorded_decision"] = {key: decision.get(key) for key in ("model_action", "reason")}
                row["decision_manifest"] = config["reference"]
                row["decision_source"] = config["source"]
            correction["rounds"].append(row)
    sample["correction_feedback"] = correction
    claude = report["coding"].get("claude_session_usage")
    if claude is not None:
        # The usage owner already reconciles cumulative resumed sessions. Keep
        # its metrics and gaps; these are not Codex turns or worker-role totals.
        sample["claude_coding_session_usage"] = {
            key: claude[key] for key in ("coverage", "by_model", "gaps", "scope", "actual_account_charge")}
        sample["claude_coding_session_usage"]["session_ids"] = [
            row["session_id"] for row in claude["sessions"]]
    cost = report.get("api_price_equivalent")
    if cost is not None:
        # Project the existing price owner, never recalculate or sum examples.
        # Full gaps/sources remain retrievable through the selected outcome.
        sample["api_price_equivalent"] = {key: cost[key] for key in (
            "price_date", "currency", "coverage", "observed_subtotal_usd_min",
            "observed_subtotal_usd_max", "unpriced_group_count", "scope", "assumptions",
            "actual_account_charge", "reason") if key in cost}
        sample["api_price_equivalent"]["rows"] = cost.get("rows", [])
        sample["api_price_equivalent"]["gap_count"] = len(cost.get("usage_gaps", []))
    return sample


def calibration_history(order, tree, *, sample_size=6, project_record=None):
    """Recent distinct orders in this Git repository, not an all-project census.

    Six is a prompt retrieval window, not a task/agent/call limit. Complete source
    references stay accessible. Discovery visits registered orders' standard and
    explicitly recorded CLI locations only; fixtures and plain CLI logs stay out.
    """
    result = {"schema_version": 4, "evidence_root": None,
              "selection": "latest outcome file timestamp per other order in the same Git repository; not a representative benchmark",
              "sample_size": sample_size, "samples": [], "gaps": [], "available_orders": 0,
              "scope": "observational task examples, not comparable model trials; coding counters and "
                       "price-owner coding/review/manager estimates where observed; "
                       "coding_models contains Codex turn counters; claude_coding_session_usage contains "
                       "Claude cumulative session counters, not per-member roles or per-task costs; "
                       "Claude input/cache-read/cache-creation are separate counters, unlike Codex input including cache; "
                       "examples may share sessions and must not be summed; "
                       "review verdicts are not integration or per-member quality scores. "
                       "Choose models by comparable task scope and acceptance first, then observed cost "
                       "including corrections; inspect findings before blaming model capability. "
                       "correction_feedback preserves reviewer advice, not proven failure causes; "
                       "read reviews[reviewer_index].correction_guidance.reason in its linked outcome "
                       "for the explanation. Recorded decisions come from separately referenced manifests, "
                       "not proof that the chosen model executed or resolved the defect. "
                       "Correction rounds retain explicit predecessor links and recorded verdicts; "
                       "their coding model observations are separate from requested choices. "
                       "Codex models are native turn declarations; position is not a work-role attribution. "
                       "Claude model lists are cumulative session observations, not new calls in that round; "
                       "rounds may share usage and cannot be summed. A later PASS does not prove the model change caused success. "
                       "Missing or partial prices are not cheap successful work. These observations "
                       "do not justify automatic downgrades or changing explicit user model choices."}
    try:
        if project_record is None:
            adapter = invocation_adapter()
            if adapter is None:
                raise ValueError("calibration history requires a selected project adapter")
            evidence_root, sources = adapter.calibration_sources()
        else:
            evidence_root = Path(project_record["context"]["evidence_root"])
            sources = []
            for location in project_record["context"]["history_roots"]:
                for path in sorted(Path(location).glob("*/*/outcome.json")):
                    try:
                        manifest, _ = _capture(path.with_name("manifest.json"))
                        if manifest.get("project_record", {}).get("context", {}).get("project_id") == project_record["context"]["project_id"]:
                            sources.append((Path(manifest["order"]["path"]), path))
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        result["gaps"].append({"path": str(path), "reason": str(exc)[:240]})
        result["evidence_root"] = str(evidence_root)
        repository = _repository(tree)
        current = normalized_line(Path(order))
        identities = {str(Path(tree).resolve()): repository}
        candidates = []
        for prior_order, selected_path in sources:
            try:
                tag = normalized_line(prior_order)
            except ValueError:
                continue  # Templates and non-order documents are not tasks.
            if tag == current:
                continue
            try:
                paths = {selected_path} if selected_path else set()
                for folder in ([] if selected_path else evidence_roots({evidence_root / tag}, tag)):
                    paths.update(folder.glob("outcome.json"))
                    paths.update(folder.glob("*/outcome.json"))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                result["gaps"].append({"tag": tag, "reason": str(exc)[:240]})
                continue
            for path in sorted(paths):
                try:
                    # Automatic rounds live below an invocation, never a sample
                    # independently from the owning root (including a live root).
                    manifest, ref = _capture(path.with_name("manifest.json"))
                    if ((manifest.get("mode") == "auto-amend" and manifest.get("state") != "completed")
                            or Path(manifest["order"]["path"]).resolve() != prior_order.resolve()
                            or "candidate_commit" in manifest  # Existing run_review manifest contract, including legacy.
                            or (manifest.get("delivery_context") or {}).get("mode") == "review-only"):
                        continue
                    parent_manifest = path.parent.parent / "manifest.json"
                    if parent_manifest.is_file():
                        parent = _capture(parent_manifest)[0]
                        if parent.get("mode") == "auto-amend":
                            children = {Path(r["outcome"]["path"]).resolve() for r in parent.get("rounds", [])}
                            if parent.get("active_round"):
                                children.add((Path(parent["active_round"]) / "outcome.json").resolve())
                            if path.resolve() in children:
                                continue
                    prior_tree = manifest.get("worktree")
                    if not prior_tree:
                        continue
                    if prior_tree not in identities:
                        identities[prior_tree] = _repository(prior_tree)
                    if identities[prior_tree] != repository:
                        continue
                    candidates.append((path.stat().st_mtime_ns, str(path), tag, manifest, ref))
                except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
                    result["gaps"].append({"tag": tag, "path": str(path), "reason": str(exc)[:240]})
        latest = {}
        for item in sorted(candidates, key=lambda item: (item[0], item[1]), reverse=True):
            latest.setdefault(item[2], item)
        result["available_orders"] = len(latest)
        for _, name, tag, manifest, ref in list(latest.values())[:sample_size]:
            try:
                _reference(ref)  # A changing source is not a frozen example.
                text = _reference(manifest["order"]).decode("utf-8-sig")
                result["samples"].append({"tag": tag, **_sample(Path(name), manifest, ref, text)})
            except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, StopIteration) as exc:
                result["gaps"].append({"tag": tag, "reason": str(exc)[:240]})
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        result["gaps"].append({"reason": str(exc)[:240]})
    return result
