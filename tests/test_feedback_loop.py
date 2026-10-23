"""Retained review feedback uses the existing amendment and session owners."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from foundry_runtime import cli, foundry_entry, foundry_goal as goal, _followup
from foundry_runtime import _foundry_dispatch as dispatch, _native_session
from test_installed_runtime import Analyst, external, offline
from test_manager_input_wait import manager_io


FINDING = {"severity": "P2", "file": "conflict.py", "line": "2",
           "summary": "The conflict response must retain commandKey and latest after retries."}
WORKER = "00000000-0000-4000-8000-000000000001"


def retained(tmp_path, *, session="original-worker", findings=True):
    order = tmp_path / "TASK1_order.md"
    order.write_text("Preserve command identity and latest state in conflict responses.")
    folder = tmp_path / "prior"
    folder.mkdir()
    goal._write(folder / "manifest.json", {
        "run_id": "prior-run", "order": goal._fact(order), "worktree": str(tmp_path),
        "base_commit": "base", "runtime_provenance": {"python": "D:/retained/python.exe"},
        "project_record": {"context": {"descriptor": "D:/Project O'Brien/project.json"}}})
    review = {"candidate_commit": "candidate", "packet_sha256": "f" * 64,
              "verdict": "PASS", "findings": [FINDING] if findings else []}
    goal._write(folder / "outcome.json", {"status": "candidate_verified",
        "candidate_commit": "candidate", "coding_session": session, "reviews": [review, review],
        "coding": {"evidence": [{"path": "coding.log", "sha256": "log"}]}})
    return folder


def test_status_binds_feedback_and_command_without_mutating_records(tmp_path):
    folder = retained(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    view = _followup.status(folder)["feedback"]
    assert view["task"]["run_id"] == "prior-run"
    assert view["candidate_commit"] == "candidate"
    assert view["findings"] == [{"finding": FINDING, "reviewers": [0, 1]}]
    command = view["continuation"]["invocation"]
    assert command["argv"][0] == "D:/retained/python.exe"
    assert command["argv"][-4:] == ["--amend-from", str(folder / "outcome.json"),
                                     "--coding-session", "original-worker"]
    assert "O''Brien" in command["powershell"]
    assert view["artifacts"]["outcome"] == goal._fact(folder / "outcome.json")
    assert view["artifacts"]["coding"][0]["path"] == "coding.log"
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("session,findings,expected", [
    (None, True, "coding_session_required"), ("worker", False, "no_recorded_findings")])
def test_status_keeps_feedback_when_no_continuation_is_selected(tmp_path, session, findings, expected):
    view = _followup.status(retained(tmp_path, session=session, findings=findings))["feedback"]
    assert view["continuation"]["status"] == expected
    assert "invocation" not in view["continuation"]
    assert bool(view["findings"]) == findings


def test_automatic_root_uses_final_round_runtime_and_preserves_selected_outcome(tmp_path):
    child = retained(tmp_path)
    manifest = json.loads((child / "manifest.json").read_bytes())
    outcome = json.loads((child / "outcome.json").read_bytes())
    goal._write(tmp_path / "manifest.json", {**manifest, "run_id": "root", "mode": "auto-amend",
        "active_round": str(child), "rounds": [{"outcome": goal._fact(child / "outcome.json")}],
        "runtime_provenance": {"python": "D:/older/python.exe"}})
    goal._write(tmp_path / "outcome.json", outcome)
    view = _followup.status(tmp_path)["feedback"]
    invocation = view["continuation"]["invocation"]
    assert invocation["runtime_python"] == "D:/retained/python.exe"
    assert invocation["argv"][-3] == str(tmp_path / "outcome.json")
    assert view["artifacts"]["rounds"][0]["outcome"] == goal._fact(child / "outcome.json")


def test_amendment_command_retains_native_interpreter_and_environment(tmp_path):
    folder = retained(tmp_path)
    native = tmp_path / "native"
    native.mkdir()
    goal._write(native / "native-manifest.json", {
        "runtime_provenance": {"python": "D:/native/python.exe"},
        "backend_identity": {"launch_context": {"compact_tokens": 240000}}})
    manifest = json.loads((folder / "manifest.json").read_bytes())
    manifest["manager_state"] = {"native_dir": str(native)}
    goal._write(folder / "manifest.json", manifest)
    command = _followup.status(folder)["feedback"]["continuation"]["invocation"]
    assert command["runtime_python"] == "D:/native/python.exe"
    assert command["environment"] == {"FOUNDRY_COMPACT_TOKENS": "240000"}
    assert "finally" in command["powershell"]


def test_missing_runtime_leaves_candidate_feedback_visible(tmp_path):
    folder = retained(tmp_path)
    manifest = json.loads((folder / "manifest.json").read_bytes())
    manifest.pop("runtime_provenance")
    goal._write(folder / "manifest.json", manifest)
    view = _followup.status(folder)["feedback"]
    assert view["candidate_commit"] == "candidate" and view["findings"]
    assert view["continuation"]["status"] == "unavailable"
    assert "invocation" not in view["continuation"]


def test_partial_review_keeps_its_existing_recovery_route(tmp_path):
    folder = retained(tmp_path)
    result = json.loads((folder / "outcome.json").read_bytes())
    result.update(status="review_blocked", reviews=result["reviews"][:1])
    goal._write(folder / "outcome.json", result)
    view = _followup.status(folder)["feedback"]
    assert view["continuation"]["status"] == "review_completion_required"
    assert len(view["findings"]) == 1


@pytest.mark.parametrize("severity", ["P2", "P1"])
def test_generated_command_closes_review_feedback_through_original_session(
        external, manager_io, tmp_path, monkeypatch, severity):
    tree, scope, order, launches = external
    requests, decisions = manager_io
    decisions.extend([{'action': 'delegate', 'instruction': 'Apply the retained review findings',
                       'expert_request': None, 'artifacts': []},
                      {'action': 'review', 'instruction': 'Review the corrected candidate',
                       'expert_request': None, 'artifacts': []}])
    monkeypatch.setattr(goal, '_session_from_log', lambda _: WORKER)
    original_argv = dispatch.coding_argv
    received, check_results = [], []
    def coding(*args, **kwargs):
        argv = original_argv(*args, **kwargs)
        header = "OpenAI Codex vfixture\n--------\nsession id: " + WORKER + "\n--------"
        argv[-1] = "print(" + repr(header) + ", flush=True); " + argv[-1]
        correction = len(launches) > 1
        body = ("def response(key, latest):\n    return " +
                ("{'status': 409, 'commandKey': key, 'latest': latest}\n" if correction else
                 "{'status': 409}\n"))
        check = ("from conflict import response\n"
                 "assert response('retry-1', {'revision': 2}) == "
                 "{'status': 409, 'commandKey': 'retry-1', 'latest': {'revision': 2}}\n")
        argv[-1] += ("; import subprocess; Path('conflict.py').write_text(" + repr(body) +
                     "); Path('check.py').write_text(" + repr(check) +
                     "); subprocess.run(['git','add','conflict.py','check.py'],check=True); "
                     "subprocess.run(['git','commit','-qm','candidate'],check=True)")
        return argv
    class CheckingAnalyst(Analyst):
        def __call__(self, request):
            result = super().__call__(request)
            check = subprocess.run([sys.executable, "-B", "check.py"], cwd=tree, capture_output=True)
            check_results.append(check.returncode)
            if check.returncode:
                result.update(verdict="BLOCK" if severity == "P1" else "PASS",
                              findings=[{**FINDING, "severity": severity}])
            if request.get('correction_routing'):
                result['correction_guidance'] = {'cause': 'implementation' if check.returncode else 'none',
                    'model': 'retain', 'reason': 'The candidate check identifies the response contract.'}
            return result
    original_resume = dispatch.resume_order
    def resume(session, worktree, tag, text, **kwargs):
        received.append((session, text))
        return original_resume(session, worktree, tag, text, **kwargs)
    monkeypatch.setattr(dispatch, "coding_argv", coding)
    monkeypatch.setattr(dispatch, "resume_order", resume)
    monkeypatch.setattr(goal, "NativeAnalyst", CheckingAnalyst)
    monkeypatch.setattr(_native_session, "read_session_worktree", lambda _: tree)
    monkeypatch.setattr(foundry_entry, "_analyst_executable", lambda _: Path(sys.executable))
    prior = tmp_path / "prior"
    first = goal.run_goal(order, prior, Path(sys.executable), coding_launcher="exec", project_record=scope.record())
    assert first["status"] == ("review_blocked" if severity == "P1" else "candidate_verified")
    before = {p: p.read_bytes() for p in prior.rglob("*") if p.is_file()}
    feedback = _followup.status(prior)["feedback"]
    assert feedback["continuation"]["status"] == "available"
    command = feedback["continuation"]["invocation"]["argv"]
    successor = tmp_path / "corrected"
    assert cli.main(command[3:] + ["--goal-evidence", str(successor)]) == 0
    final = json.loads((successor / "outcome.json").read_bytes())
    assert len(launches) == 2 and len(received) == 1
    assert len(requests) == 2 and not decisions
    assert received[0][0] == WORKER
    assert FINDING["summary"] in received[0][1] and first["candidate_commit"] in received[0][1]
    assert check_results == [1, 1, 0, 0]
    assert final["status"] == "candidate_verified" and all(not r["findings"] for r in final["reviews"])
    assert final["candidate_commit"] != first["candidate_commit"]
    view = _followup.status(successor)["feedback"]
    assert view["predecessor"] == goal._fact(prior / "outcome.json")
    assert view["continuation"]["status"] == "no_recorded_findings"
    assert before == {p: p.read_bytes() for p in prior.rglob("*") if p.is_file()}
    from foundry_runtime._foundry_goal_usage import task_report
    baseline = task_report([successor])["delivery_baseline"]
    assert baseline["summary"]["invocation_count"] == 2
    assert {row["candidate"]["candidate_commit"] for row in baseline["invocations"]} == {
        first["candidate_commit"], final["candidate_commit"]}
