import json
from pathlib import Path
import subprocess
import sys
import tomllib

from foundry_runtime import _launch_context as config, _code_index as index
from foundry_runtime import _context_signals as signals


def test_native_arguments_cover_transports(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(index, "prepare", lambda workspace, **kw: seen.append(workspace))
    from foundry_runtime._codex_command import coding_argv
    from foundry_runtime.foundry_codex import codex_argv
    from foundry_runtime._dispatch_claude import argv
    from foundry_runtime.foundry_claude import _claude_argv, environment
    a = coding_argv("codex", "exec", "-C", str(tmp_path), "resume", "session")
    b = coding_argv("codex", "app-server", workspace=tmp_path)
    c = codex_argv(Path("codex"), tmp_path/"schema", workspace=tmp_path)
    for args in (a, b, c):
        assert not any(arg.startswith("model_auto_compact_token_limit=") for arg in args)
        assert "tool_output_token_limit=3000" in args
        assert not any("mcp_servers." in arg for arg in args)
    for args in (argv("claude", "session", workspace=tmp_path),
                 _claude_argv(Path("claude"), "session", {}, workspace=tmp_path)):
        value = json.loads(args[args.index("--settings")+1])
        assert value["bashOutputMaxChars"] == 12000
        assert value["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "500000"
        assert "permissions" not in value
    assert environment()["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "90"
    assert len(seen) == 5 and all(str(p) == str(tmp_path) for p in seen)


def test_launch_overrides_and_mcp_preserve_unrelated_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("FOUNDRY_CONTEXT_TOKENS", "300000")
    server = {"command": "node", "args": ["D:/tool.js", "serve", "--mcp", "--path", str(tmp_path)],
              "env": {"CODEGRAPH_MCP_TOOLS": "explore"}}
    monkeypatch.setattr(index, "prepare", lambda _, **kw: server)
    settings = config.codex_settings(tmp_path)
    parsed = tomllib.loads("\n".join(settings))
    assert "model_auto_compact_token_limit" not in parsed
    assert parsed["mcp_servers"]["foundry_codegraph"]["args"] == server["args"]
    args = config.claude_args(tmp_path)
    assert "--strict-mcp-config" not in args
    assert json.loads(args[args.index("--mcp-config")+1])["mcpServers"]["foundry_codegraph"] == server


def test_signals_deduplicate_and_do_not_invent_first_gap():
    a = {"member": "worker", "id": "a", "timestamp": "2026-10-04T00:00:00Z", "context": 210000, "cache_write": 110000}
    b = {**a, "id": "b", "timestamp": "2026-10-04T00:06:00Z"}
    c = {**a, "member": "other", "id": "c", "timestamp": "2026-10-04T00:07:00Z"}
    result = signals.summarize([a, a, b, c], bound=200000)
    assert result["members"]["worker"]["responses"] == 2
    assert len(result["members"]["worker"]["context_over_bound"]) == 2
    assert result["members"]["worker"]["large_cache_write_after_idle"][0]["idle_seconds"] == 360
    assert not result["members"]["other"]["large_cache_write_after_idle"]


def test_claude_signals_are_optional_and_keep_parent_identity(tmp_path):
    path = tmp_path/"stream.jsonl"
    row = {"type":"assistant", "parent_tool_use_id":"member-call", "timestamp":"2026-10-04T00:00:00Z",
           "message":{"id":"m1", "usage":{"input_tokens":1, "cache_creation_input_tokens":100001, "cache_read_input_tokens":110000}}}
    path.write_text(json.dumps({"type":"system", "message":"status"})+"\n"+json.dumps(row)+"\n"+json.dumps(row)+"\n")
    result = signals.observe_claude(path)
    assert result["members"]["member-call"]["max_context_tokens"] == 210002
    assert result["members"]["member-call"]["responses"] == 1
    path.write_text("invalid")
    assert signals.observe_claude(path)["coverage"] == "uncovered"


def test_codex_context_uses_input_without_double_counting_cache():
    rows = [{"type":"token_usage_record", "timestamp":"2026-10-04T00:00:00Z",
             "payload":{"turn_id":"t", "thread_id":"member", "response_id":"r",
                        "usage":{"input_tokens":190000,"cached_input_tokens":180000}}}]
    result = signals.codex_rows(rows, "t")
    assert result["members"]["member"]["max_context_tokens"] == 190000
    assert not result["members"]["member"]["context_over_bound"]
    assert result["gaps"]


def test_team_brief_is_versioned(monkeypatch):
    monkeypatch.setenv("PDCL_MODEL_CATALOGUE", "snapshot")
    from foundry_runtime._compute_selection import resolve_native_team
    for provider, version in (("claude",15),("codex",20)):
        team = resolve_native_team("adaptive", backend=provider)
        assert team["version"] == version
        assert "Read discipline" in team["instructions"]
        assert "one member per deliverable" in team["instructions"]
        assert "symbolsOnly=true" in team["instructions"]
        assert "Reuse returned unchanged source ranges" in team["instructions"]
        assert "omitted, truncated or changed ranges" in team["instructions"]


def test_index_drift_detects_committed_content(tmp_path):
    import sqlite3, hashlib
    subprocess.run(["git","init","-q",str(tmp_path)],check=True)
    source=tmp_path/"file.py"; source.write_bytes(b"def alpha(): pass\n")
    folder=tmp_path/".codegraph";folder.mkdir()
    with sqlite3.connect(folder/"codegraph.db") as db:
        db.execute("create table files(path text, content_hash text)")
        db.execute("insert into files values (?,?)",("file.py", hashlib.sha256(source.read_bytes()).hexdigest()))
    import pytest
    with pytest.raises(FileNotFoundError):
        index.verify(tmp_path)  # Historical DB needs one successful sync baseline.
    assert index.verify(tmp_path, synced=True)["indexed_files"] == 1
    assert index.verify(tmp_path)["indexed_files"] == 1
    source.write_bytes(b"def bravo(): pass\n")
    with pytest.raises(ValueError, match="differs"):
        index.verify(tmp_path)


def test_unavailable_index_keeps_launch_possible(monkeypatch, tmp_path):
    monkeypatch.delenv("FOUNDRY_CODEGRAPH_ARGV", raising=False)
    assert index.prepare(tmp_path) is None


def test_historical_bound_ignores_current_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("FOUNDRY_CONTEXT_TOKENS", "900000")
    log = tmp_path/"dispatch.log"
    log.with_suffix(".package.json").write_text(json.dumps({"attempt_id":"a", "runtime_provenance":{"launch_context":{"context_tokens":300000,"compact_percent":90}}}))
    receipt={"attempt_id":"a", "evidence_refs":[{"kind":"log","path":str(log)}]}
    bound=signals.recorded_bound(receipt, "claude")
    assert bound["tokens"] == 270000
    e={"member":"m", "id":"r", "timestamp":None, "context":280000, "cache_write":0}
    result=signals.summarize([e],bound=bound["tokens"])
    assert result["members"]["m"]["context_over_bound"]
    assert result["gaps"]
    receipt["attempt_id"]="other"
    assert signals.recorded_bound(receipt, "claude")["tokens"] is None
    assert signals.recorded_bound(receipt, "codex")["tokens"] is None


def test_idle_crosses_native_turns_without_counting_old_response():
    rows=[{"type":"token_usage_record","timestamp":stamp,"payload":{"thread_id":"m","turn_id":turn,
        "response_id":turn,"usage":{"input_tokens":220000,"cache_write_input_tokens":110000}}}
        for turn,stamp in (("a","2026-10-04T00:00:00Z"),("b","2026-10-04T00:06:00Z"))]
    previous=signals.previous_for(rows,"b",{})
    result=signals.codex_rows(rows,"b",previous)
    assert result["members"]["m"]["responses"] == 1
    assert result["members"]["m"]["large_cache_write_after_idle"][0]["idle_seconds"] == 360


def test_recorded_member_guidance_is_never_replaced():
    from foundry_runtime._dispatch_claude import argv
    from foundry_runtime._compute_selection import team_prompt
    codex = {"profile": "adaptive", "version": 11, "instructions": "saved Codex outline guidance"}
    assert team_prompt("continued task", codex) == "saved Codex outline guidance\nTask:\ncontinued task"
    assert codex["version"] == 11
    recorded = {"profile":"adaptive", "version":6, "member_roles":{}, "member_instructions":"saved old instructions"}
    for resume in (False, True):
        args=argv("claude","session",resume=resume,native_team=recorded)
        assert args[args.index("--append-subagent-system-prompt")+1] == "saved old instructions"
        assert recorded["version"] == 6


def test_launch_policy_identity_carries_context_controls(monkeypatch):
    from foundry_runtime._foundry_native_transport import bind_profile
    monkeypatch.setenv("FOUNDRY_CONTEXT_TOKENS","250000")
    assert bind_profile({"metadata":{}},None)["launch_context"]["context_tokens"] == 250000


def test_codex_model_default_does_not_invent_a_context_alarm():
    event={"member":"m", "id":"r", "timestamp":"2026-10-04T00:00:00Z", "context":500000, "cache_write":0}
    result=signals.summarize([event], bound=signals.recorded_bound({}, "codex")["tokens"])
    assert result["context_bound"] is None
    assert result["members"]["m"]["max_context_tokens"] == 500000
    assert not result["members"]["m"]["context_over_bound"]
    assert not signals.summarize([{**event, "context":270000}],bound=270000)["members"]["m"]["context_over_bound"]
    assert signals.summarize([{**event, "context":270001}],bound=270000)["members"]["m"]["context_over_bound"]


def test_packaged_usage_cli_preserves_unicode_history(tmp_path):
    import os
    path=tmp_path/"finish.json"
    path.write_text(json.dumps({"kind":"finish", "created_utc":"2026-10-04T00:00:00Z",
        "task_key":"coding-\u2192-\u6d4b\u8bd5", "data":{}}), encoding="utf-8")
    before=path.read_bytes()
    result=subprocess.run([sys.executable,"-m","foundry_runtime.result_receipt","--usage-event",str(path)],
        env=dict(os.environ,PYTHONIOENCODING="cp1252"),capture_output=True,timeout=10)
    assert result.returncode == 0, result.stderr
    json.loads(result.stdout)
    assert path.read_bytes() == before
