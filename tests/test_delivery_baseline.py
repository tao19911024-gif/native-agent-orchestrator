"""Delivery associations are projections of retained task evidence."""
import hashlib
import json

import pytest

from foundry_runtime._foundry_goal_usage import task_report


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value).encode()
    path.write_bytes(raw)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def completed(folder, run="run-1", head="candidate-1", previous=None):
    task = {"order": {"path": "task.md", "sha256": "order-hash"}, "run_id": run}
    context = {**task, "mode": "goal"}
    if previous:
        context.update(mode="continuation", continues=previous)
    checkpoint = save(folder / "review-checkpoint.json", {**task, "context": {
        "base_commit": "base-1", "candidate_commit": head,
        "project_review": {"status": "ready", "checks": [{"name": "unit", "status": "passed"}],
                           "observation": {"path": "project-check.json", "sha256": "check-hash"}}}})
    save(folder / "manifest.json", {"schema_version": 1, **task, "tag": "TASK1",
         "worktree": "workspace", "base_commit": "base-1", "review_checkpoint": checkpoint,
         "runtime_provenance": {"version": "fixture-version", "distribution_record_sha256": "runtime-hash"}})
    usage = save(folder / "usage.json", {"coding": {"coverage": "uncovered", "observations": []}})
    return save(folder / "outcome.json", {
        "status": "candidate_verified", "candidate_commit": head, "coding_session": "coding-session",
        "coding": {"returncode": 0, "evidence": [{"path": "coding.log", "sha256": "log-hash"}]},
        "candidate_closure": {"status": "passed", "candidate_commit": head,
                              "receipt": {"path": "closure.json", "sha256": "closure-hash"}},
        "reviews": [{"verdict": "PASS", "candidate_commit": head, "findings": [], "packet_sha256": "packet"}],
        "review_sessions": ["review-session"],
        "project_acceptance": {"status": "accepted", "evidence": [{"path": "acceptance.json"}]},
        "task_usage": usage, "delivery_report": {"delivery": {
            "status": "candidate_verified", "candidate_commit": head, "context": context,
            "elapsed": {"started_utc": "2026-10-01T00:00:00+00:00",
                        "completed_utc": "2026-10-01T00:02:00+00:00", "duration_s": 120}}}})


def test_complete_associations_and_duplicate_selection_leave_sources_unchanged(tmp_path):
    outcome = completed(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    report = task_report([tmp_path, tmp_path / "manifest.json", tmp_path / "outcome.json"])
    baseline = report["delivery_baseline"]
    assert baseline["summary"]["invocation_count"] == 1
    row = baseline["invocations"][0]
    assert row["reference"] == outcome
    assert row["task"]["run_id"] == "run-1"
    assert row["task"]["order"]["sha256"] == "order-hash"
    assert row["candidate"] == {"base_commit": "base-1", "candidate_commit": "candidate-1"}
    assert row["runtime_provenance"]["distribution_record_sha256"] == "runtime-hash"
    assert row["coding"]["session_id"] == "coding-session"
    assert row["coding"]["returncode"] == 0
    assert [c["kind"] for c in row["checks"]] == ["candidate_closure", "project_review"]
    assert row["checks"][1]["result"]["checks"][0]["status"] == "passed"
    assert row["reviews"][0]["session_id"] == "review-session"
    assert row["reviews"][0]["source"] == outcome
    assert row["project_acceptance"]["status"] == "accepted"
    assert row["elapsed"]["duration_s"] == 120
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_round_containers_and_continuations_do_not_duplicate_invocations(tmp_path):
    prior = completed(tmp_path / "prior")
    current = completed(tmp_path / "current", "run-2", "candidate-2", prior)
    root = save(tmp_path / "root" / "outcome.json", {"status": "candidate_verified", "reviews": [],
        "rounds": [{"outcome": prior}, {"outcome": current}]})
    baseline = task_report([root["path"], current["path"], prior["path"]])["delivery_baseline"]
    assert baseline["summary"]["invocation_count"] == 2
    assert baseline["summary"]["container_count"] == 1
    assert baseline["summary"]["review_verdict_counts"] == {"PASS": 2}
    assert any(link["relation"] == "continues" for row in baseline["invocations"] for link in row["links"])
    assert "duration_s" not in baseline["summary"]


@pytest.mark.parametrize("problem", ["changed_hash", "different_candidate", "different_run"])
def test_unavailable_or_unrelated_checkpoint_preserves_recorded_outcome(tmp_path, problem):
    completed(tmp_path)
    path = tmp_path / "review-checkpoint.json"
    checkpoint = json.loads(path.read_bytes())
    if problem == "different_candidate":
        checkpoint["context"]["candidate_commit"] = "another-candidate"
    else:
        checkpoint["run_id"] = "another-run"
    ref = save(path, checkpoint)
    if problem != "changed_hash":
        manifest = json.loads((tmp_path / "manifest.json").read_bytes())
        manifest["review_checkpoint"] = ref
        save(tmp_path / "manifest.json", manifest)
    row = task_report([tmp_path])["delivery_baseline"]["invocations"][0]
    assert row["status"] == "candidate_verified"
    assert row["reviews"][0]["verdict"] == "PASS"
    assert [c["kind"] for c in row["checks"]] == ["candidate_closure"]
    assert any(g["kind"] == "review_checkpoint" for g in row["gaps"])


def test_pending_and_legacy_review_preserve_missing_results(tmp_path):
    pending = tmp_path / "pending"
    save(pending / "manifest.json", {"schema_version": 1, "run_id": "pending-run",
         "order": {"path": "task.md"}, "state": "awaiting_input"})
    review = save(tmp_path / "review" / "outcome.json", {"status": "review_recorded",
        "candidate_commit": "candidate-1", "reviews": [{"verdict": "PASS", "findings": []}]})
    baseline = task_report([pending, review["path"]])["delivery_baseline"]
    assert baseline["summary"]["project_acceptance_counts"] == {"not_recorded": 2}
    assert baseline["summary"]["elapsed_observation_count"] == 0
    row = next(r for r in baseline["invocations"] if r["kind"] == "unfinished_invocation")
    assert row["task"]["run_id"] == "pending-run"
    assert row["status"] == "awaiting_input"
    assert row["checks"] == row["reviews"] == []
    assert row["elapsed"] is None


def test_phase_timing_reuses_parallel_group_and_retained_elapsed(tmp_path):
    from foundry_runtime import _foundry_goal_telemetry as telemetry

    completed(tmp_path)
    telemetry.record(tmp_path, {"group": "reviews", "duration_s": 32,
                               "members": {"review-0": 30, "review-1": 25}})
    row = task_report([tmp_path])["delivery_baseline"]["invocations"][0]
    timing = row["phase_timing"]["summary"]
    assert timing["review_group_wall_s"] == 32
    assert timing["review_serial_s"] == 55
    assert row["elapsed"]["duration_s"] == 120


def test_manifest_from_another_invocation_does_not_supply_task_or_checks(tmp_path):
    completed(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_bytes())
    manifest["run_id"] = "another-run"
    manifest["base_commit"] = "unrelated-base"
    save(tmp_path / "manifest.json", manifest)
    row = task_report([tmp_path])["delivery_baseline"]["invocations"][0]
    assert row["task"]["run_id"] == "run-1"
    assert row["candidate"]["base_commit"] is None
    assert [c["kind"] for c in row["checks"]] == ["candidate_closure"]
    assert row["gaps"][0]["kind"] == "manifest"
