"""Carbon manager selection and continuation through the native transport."""
import json
from pathlib import Path
import sys

import pytest

from foundry_runtime import cli, foundry_goal, foundry_native
from foundry_runtime import _foundry_manager as manager, _foundry_goal_transitions as transitions
from test_installed_runtime import external, offline


@pytest.mark.parametrize("launcher,project_default", [("app-server", False), ("claude", True), ("agy", False)])
def test_carbon_manager_preview(external, capsys, launcher, project_default):
    _, project, order, _ = external
    args = ["--project-config", str(project.path), "--order", str(order), "--foundry-goal",
            "--launcher", launcher, "--print-staffing"]
    if project_default:
        descriptor = json.loads(project.path.read_bytes())
        descriptor["default_staffing"] = {"manager_profile": "carbon"}
        project.path.write_text(json.dumps(descriptor), encoding="utf-8")
    else:
        args.extend(["--manager-profile", "carbon"])
    assert cli.main(args) == 0
    line = next(line for line in capsys.readouterr().err.splitlines() if line.startswith("FOUNDRY COMPUTE: "))
    staffing = json.loads(line.removeprefix("FOUNDRY COMPUTE: "))
    assert staffing["manager"] == {"model": "gemini-4-carbon-high", "effort": "high", "backend": "agy"}
    assert staffing["staffing_sources"]["manager"] == ("project" if project_default else "explicit")
    assert "coordination_policy" not in staffing
    assert staffing["roles"]["coding"]["backend"] == ("codex" if launcher == "app-server" else launcher)


def test_carbon_manager_resumes_and_replays(tmp_path, monkeypatch):
    state = manager.configuration("carbon", tmp_path / "manager")
    state["executable"] = sys.executable
    assert state["expert"]["model"] == state["model"]
    original = foundry_native.OwnedProcess.spawn
    calls = []
    def spawn(argv, **kwargs):
        source = kwargs["stdin"]
        body = json.loads(source.read())["message"]["content"]
        source.seek(0)
        envelope = json.loads(body.rsplit("\n\n", 1)[-1])
        assert "Delegate shell commands" in body
        assert argv[argv.index("--mode") + 1] == "plan"
        assert argv[argv.index("--model") + 1] == state["model"]
        assert "--dangerously-skip-permissions" not in argv
        result = {"action": "delegate", "instruction": "Run the selected test.", "artifacts": [],
                  "remaining_work": ["Run the selected test."], "expert_request": None}
        sid = "carbon-fixture"
        if calls:
            assert argv[argv.index("--conversation") + 1] == sid
        calls.append(envelope)
        rows = [{"event": "init", "conversation_id": sid, "init": {"model": state["model"]}},
                {"event": "result", "result": {"status": "SUCCESS", "conversation_id": sid,
                                              "structured_output": result}}]
        code = "import sys;sys.stdin.buffer.read();print(" + repr("\n".join(map(json.dumps, rows))) + ")"
        return original([sys.executable, "-B", "-c", code], **kwargs)
    monkeypatch.setattr(foundry_native.OwnedProcess, "spawn", spawn)
    first, saved, _, _ = manager.turn(state, tmp_path, {"stage": "assignment"})
    packet = {"stage": "worker_returned", "operator_answer": "Check the corrected case."}
    last, final, _, _ = manager.turn(saved, tmp_path, packet)
    assert first["session_id"] == last["session_id"] == "carbon-fixture"
    assert manager.turn(saved, tmp_path, packet)[0] == last
    _, latest, history = transitions.manager_history(foundry_goal, final, tmp_path, read_only=True)
    assert latest == last["call_id"] and len(history) == len(calls) == 2

