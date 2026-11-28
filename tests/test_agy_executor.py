"""Offline AGY dispatch through the registered-project entry and durable journal."""
import json
from pathlib import Path
import sys
import subprocess

import pytest

from foundry_runtime import _dispatch_agy as agy, _foundry_dispatch as dispatch, cli
from test_installed_runtime import external, offline


@pytest.mark.parametrize("change", [{"num_turns": 1}, {"conversation_id": "existing"},
                                    {"usage": {"total_tokens": 4}}, {"error": "connection lost"}])
def test_ambiguous_startup_is_not_retryable(tmp_path, change):
    row = {"status": "ERROR", "error": "failed to start conversation: unavailable", "response": "",
           "conversation_id": "", "num_turns": 0, "usage": {"total_tokens": 0}, **change}
    stream = tmp_path / "dispatch.agy.jsonl"
    stream.write_text(json.dumps({"event": "result", "result": row}))
    with pytest.raises(ValueError) as error:
        agy.read_result(stream, None)
    assert not isinstance(error.value, agy.StartRefused)


def test_structured_roles_retain_provider_reason():
    from foundry_runtime._agy_review import ReviewProtocol
    from foundry_runtime.foundry_native import NativeProviderError
    protocol = ReviewProtocol({})
    row = {"status": "ERROR", "error": "failed to start conversation: unavailable", "response": "",
           "conversation_id": "", "num_turns": 0, "usage": {"total_tokens": 0}}
    with pytest.raises(NativeProviderError, match="failed to start conversation"):
        protocol.accept({"event": "result", "result": row})
    protocol = ReviewProtocol({})
    protocol.accept({"event": "init", "conversation_id": "sid", "init": {}})
    with pytest.raises(NativeProviderError, match="fixture failure"):
        protocol.accept({"event": "result", "result": {**row, "conversation_id": "sid", "error": "fixture failure"}})


@pytest.mark.parametrize("route", ["reconcile", "authorize", "legacy_close"])
def test_recover_interrupted_agy_dispatch(external, tmp_path, monkeypatch, route):
    from foundry_runtime import recovery
    from foundry_runtime._execution_intent import ExecutionIntent
    from foundry_runtime._recovery_reconciliation import _execution_events
    tree, scope, order, _ = external
    sid = "00000000-0000-0000-0000-000000000001"
    rows = [{"event": "init", "conversation_id": sid, "init": {}}, {"event": "result", "result":
        {"conversation_id": sid, "status": "ERROR", "response": "", "error": "fixture service error"}}]
    args = [sys.executable, "-B", "-c", "import sys;sys.stdin.buffer.read();print(" +
            repr("\n".join(map(json.dumps, rows))) + ")"]
    if route == "legacy_close":
        # An older controller exited after spawning the CLI but before receiving init.
        args = [sys.executable, "-B", "-c", "pass"]
    controller = """
import sys
from pathlib import Path
from foundry_runtime import _dispatch_agy as agy, _foundry_dispatch as dispatch
from foundry_runtime._project_execution import open_project
import json
agy.argv = lambda *a: json.loads(sys.argv[3])
def interrupted(*args): raise RuntimeError('fixture interrupted publication')
dispatch._finish_intent = interrupted
dispatch.dispatch_order(Path(sys.argv[2]), project=open_project(sys.argv[1]), launcher='agy', model='gemini-4-neon-high')
"""
    proc = subprocess.run([sys.executable, "-B", "-c", controller, str(scope.path), str(order), json.dumps(args)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode
    log = scope.evidence("TASK1") / "dispatch.log"
    if route == "legacy_close":
        assert "lacks matching conversation" in proc.stderr
        journal, = (scope.common_dir / "pdcl-dispatch/task-TASK1/intents").iterdir()
        record = ExecutionIntent.read_history(journal, task_key="TASK1")[0]
        assert record["root_pid"] is None and record["cleanup"]["root_pid"]
        closed = ExecutionIntent.settle_abandoned(scope.common_dir, "TASK1", record["frozen"],
            intent_id=record["intent_id"], output=tmp_path / "legacy.cleanup.json", close_reason="Retire the abandoned task")
        assert closed["state"] == "closed" and closed["cleanup"]["root_pid"] == record["cleanup"]["root_pid"]
        return
    assert "fixture interrupted publication" in proc.stderr
    package = log.with_suffix(".package.json")
    journal = Path(json.loads(package.read_bytes())["intent_journal"])
    record = ExecutionIntent.read_history(journal, task_key="TASK1")[0]
    assert record["state"] == "unknown"
    plan = recovery.command_plan(package, journal, record)
    assert plan["status"] == "explicit_recovery_commands"
    if route == "reconcile":
        saved = ExecutionIntent.settle_completed_turn(scope.common_dir, "TASK1", record["frozen"],
                                                     intent_id=record["intent_id"], log=log)
        assert saved["outcome"] == "process_failed" and saved["returncode"] == 1
        assert Path(saved["evidence"]["result"]["path"]).read_text() == "fixture service error"
        _execution_events(journal)
        return
    grant = tmp_path / "ruling.md"
    grant.write_text("Continue the original session.\n\n## owner-recovery\n- **Authority:** operator\n"
                     "- **Granted:** 2026-10-08\n- **Scope:** TASK1; recover this candidate.\n")
    commands = {key: [str(grant) if arg == "<prompt-file>" else "owner-recovery" if arg == "<grant-anchor>" else arg
                      for arg in argv] for key, argv in plan["argv"].items()}
    assert recovery.main(commands["reconcile"][3:]) == 0
    assert recovery.main([*commands["authorize"][3:], "--operator-grant-document", str(grant)]) == 0
    calls = []
    def continued(model, session):
        calls.append((model, session))
        return args
    monkeypatch.setattr(agy, "argv", continued)
    resume = dict(project=scope, launcher="agy", model="gemini-4-neon-high", recovery_file=commands["authorize"][-1])
    attempt = dispatch.resume_order(sid, tree, "TASK1", grant.read_text(), **resume)
    assert attempt[0] == 1
    assert dispatch.resume_order(sid, tree, "TASK1", grant.read_text(), **resume).replayed
    assert calls == [("gemini-4-neon-high", sid)]


@pytest.mark.parametrize("status,expected,commands", [("SUCCESS", 0, None), ("SUCCESS", 0, ["git status -sb", "pnpm test"]), ("ERROR", 1, [])])
def test_order_and_resume(external, monkeypatch, capsys, status, expected, commands):
    tree, scope, order, codex_launches = external
    original_argv = agy.argv
    monkeypatch.setattr(agy, "resolve_executable", lambda _: sys.executable)
    launches = []
    script = """
import json, sys
from pathlib import Path
args = sys.argv[1:]
body = json.loads(sys.stdin.buffer.read())['message']['content']
session = args[args.index('--conversation')+1] if '--conversation' in args else 'fixture-session'
print(json.dumps({'event':'init','conversation_id':session,'init':{'model':'gemini-4-neon-high'}}))
print(json.dumps({'event':'result','result':{'conversation_id':session,'response':json.dumps({'body':body,'argv':args}),
      'status':STATUS, 'usage':{'input_tokens':19}}}))
""".replace("STATUS", repr(status))
    def fixture_argv(model, session):
        args = original_argv(model, session)
        launches.append(args)
        return [sys.executable, "-I", "-c", script, *args[1:]]
    monkeypatch.setattr(agy, "argv", fixture_argv)
    selection = {"project": scope, "launcher": "agy", "model": "gemini-4-neon-high"}
    from foundry_runtime._execution_profile import agy_policy
    if commands is None:
        monkeypatch.delenv("FOUNDRY_AGY_COMMANDS", raising=False)
        settings = agy_policy()
    else:
        monkeypatch.setenv("FOUNDRY_AGY_COMMANDS", json.dumps(commands))
        settings = {"allowed_commands": commands}
    first = dispatch.dispatch_order(order, **selection)
    assert first[0] == expected
    dispatch.report(first[1], project=scope, tag="TASK1", launcher="agy")
    printed = capsys.readouterr().out
    assert "SESSION ID: fixture-session" in printed and "--launcher agy" in printed
    assert any("--resume fixture-session " in line and "--coding-model gemini-4-neon-high" in line
               for line in printed.splitlines())
    package = json.loads(first[1].with_suffix(".package.json").read_bytes())
    receipt = json.loads(first[1].with_suffix(".receipt.json").read_bytes())
    message = json.loads(first[1].with_suffix(".result.md").read_bytes())
    assert agy.command_guidance(settings) in message["body"]
    assert ("--dangerously-skip-permissions" in message["argv"]) == (commands is None)
    assert ("--settings" in message["argv"]) == bool(commands)
    assert package["launch_context"]["settings"] == settings
    assert receipt["execution"]["returncode_source"] == "agy_result"
    assert receipt["coding_usage"]["backend"] == "agy"
    assert receipt["coding_usage"]["reported_model"] == "gemini-4-neon-high"
    assert receipt["coding_usage"]["native_usage"] == {"input_tokens": 19}
    assert package["executor"] == {"launcher": "agy", "session_id": "fixture-session",
                                   "requested_model": "gemini-4-neon-high"}
    from foundry_runtime import _foundry_goal_usage as usage
    report = usage.coding_usage(usage.capture(first[1], "TASK1"))
    assert [call["backend"] for call in report["observations"]] == ["agy"]
    assert dispatch.dispatch_order(order, **selection).replayed
    if expected == 0:
        answer = "Apply the requested correction."
        if commands is None:
            monkeypatch.setenv("FOUNDRY_AGY_COMMANDS", "[]")
        else:
            monkeypatch.delenv("FOUNDRY_AGY_COMMANDS")
        resumed = dispatch.resume_order("fixture-session", tree, "TASK1", answer, **selection)
        assert resumed[0] == 0
        reply = json.loads(resumed[1].with_suffix(".result.md").read_bytes())
        assert agy.command_guidance(settings) in reply["body"]
        assert ("--dangerously-skip-permissions" in reply["argv"]) == (commands is None)
        assert "--conversation" in launches[-1] and "fixture-session" in launches[-1]
        assert dispatch.resume_order("fixture-session", tree, "TASK1", answer, **selection).replayed
    assert len(launches) == (2 if expected == 0 else 1)
    assert not codex_launches


@pytest.mark.parametrize("commands", [[], ["git status -sb", "pnpm test"]])
def test_goal_staffing(external, capsys, monkeypatch, commands):
    _, scope, order, _ = external
    monkeypatch.delenv("FOUNDRY_AGY_COMMANDS", raising=False)
    descriptor = json.loads(scope.path.read_bytes())
    descriptor["launch"] = {"FOUNDRY_AGY_COMMANDS": commands}
    scope.path.write_text(json.dumps(descriptor), encoding="utf-8")
    assert cli.main(["--project-config", str(scope.path), "--order", str(order), "--foundry-goal",
                     "--launcher", "agy", "--print-staffing"]) == 0
    line = next(line for line in capsys.readouterr().err.splitlines() if line.startswith("FOUNDRY COMPUTE: "))
    row = json.loads(line.removeprefix("FOUNDRY COMPUTE: "))
    assert row["roles"]["coding"]["backend"] == "agy"
    assert row["roles"]["coding"]["model"] == "gemini-4-neon-high"
    assert row["team"]["profile"] == "none"
    assert row["agy_command_guidance"] == agy.command_guidance({"allowed_commands": commands})
    assert row["manager_profile"] == "opus" and row["reviewers"]


@pytest.mark.parametrize("preview", [True, False])
def test_conflicting_explicit_assignment_stops_before_admission(external, monkeypatch, capsys, preview):
    from foundry_runtime import foundry_entry
    _, scope, order, _ = external
    monkeypatch.setattr(foundry_entry, "_analyst_executable", lambda *_: pytest.fail("admission started"))
    args = ["--project-config", str(scope.path), "--order", str(order), "--foundry-goal", "--launcher", "agy",
            "--coding-model", "gemini-4-neon-high", "--review-model", "opus", "gemini-4-neon-low"]
    assert cli.main(args + (["--print-staffing"] if preview else [])) != 0
    assert "--review-model opus sonnet" in capsys.readouterr().err


def test_registered_goal_prepares_agy_correction(external, monkeypatch):
    from foundry_runtime import foundry_goal, foundry_entry
    _, scope, order, _ = external
    monkeypatch.setattr(foundry_entry, "_analyst_executable", lambda _: Path(sys.executable))
    prepared = []
    monkeypatch.setattr(foundry_goal, "run_prepared", lambda order, folder, value:
                        prepared.append(value) or {"status": "project_handoff"})
    assert cli.main(["--project-config", str(scope.path), "--order", str(order), "--foundry-goal",
                     "--launcher", "agy"]) == 0
    assert prepared[0]["options"]["coding_model"] == "gemini-4-neon-high"
    assert prepared[0]["correction"]["model"] == "gemini-4-neon-high"


def test_empty_native_response_records_denied_command():
    result = {"status": "SUCCESS", "response": "", "denied_actions": [
        {"action": "command", "display_name": "Bash"}]}
    report = agy.coding_report(result)
    assert "denied" in report and "Bash" in report
    result["response"] = "Edited greeting.txt."
    assert agy.coding_report(result).startswith("Edited greeting.txt.")


def test_explicit_review_pair_uses_agy_native_protocol(external, tmp_path, monkeypatch):
    from foundry_runtime import foundry_goal as goal, foundry_native as native
    from foundry_runtime import _code_index
    from foundry_runtime._compute_selection import named_reviewer, review_assignments
    from test_installed_runtime import git
    tree, scope, order, _ = external
    base = git(tree, "rev-parse", "HEAD")
    (tree / "candidate.txt").write_text("candidate")
    git(tree, "add", ".")
    git(tree, "commit", "-qm", "candidate")
    head = git(tree, "rev-parse", "HEAD")
    pair = [dict(named_reviewer(name), executable=sys.executable) for name in
            ("gemini-4-carbon-high", "sol")]
    with pytest.raises(ValueError, match="distinct"):
        review_assignments([pair[0], named_reviewer("gemini-4-carbon-low")])
    monkeypatch.setattr(_code_index, "prepare", lambda *a, **k: None)
    spawn = native.OwnedProcess.spawn
    calls = []
    def child(argv, **kwargs):
        stream = kwargs["stdin"]
        raw = stream.read()
        stream.seek(0)
        is_agy = "--input-format" in argv
        if is_agy:
            raw = json.loads(raw)["message"]["content"].encode()
            assert "--dangerously-skip-permissions" not in argv
            assert argv[argv.index("--mode") + 1] == "plan"
            assert "write_file(*)" in json.loads(argv[argv.index("--settings") + 1])["permissions"]["deny"]
        request = json.loads(raw.rsplit(b"\n\n", 1)[-1])
        value = {"candidate_commit": head, "packet_sha256": request["packet_sha256"],
                 "verdict": "PASS", "findings": []}
        sid = "00000000-0000-0000-0000-00000000000" + ("1" if is_agy else "2")
        rows = ([{"event": "init", "conversation_id": sid, "init": {"model": pair[0]["model"]}},
                 {"event": "result", "result": {"status": "SUCCESS", "conversation_id": sid,
                                                 "structured_output": value}}] if is_agy else [
            {"type": "thread.started", "thread_id": sid}, {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": json.dumps(value)}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2}}])
        calls.append(is_agy)
        script = "import sys;sys.stdin.buffer.read();print(" + repr("\n".join(map(json.dumps, rows))) + ")"
        return spawn([sys.executable, "-B", "-c", script], **kwargs)
    monkeypatch.setattr(native.OwnedProcess, "spawn", child)
    result = goal.run_review(order, tmp_path / "review", Path(sys.executable), target_worktree=tree,
            base_commit=base, candidate_commit=head, project_record=scope.record(), reviewers=pair)
    assert result["status"] == "candidate_verified"
    assert sorted(calls) == [False, True]
