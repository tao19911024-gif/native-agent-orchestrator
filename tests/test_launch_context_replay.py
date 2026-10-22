"""R1 proofs: real custody/receipt replay and native no-model config reading."""
import io
import json
from pathlib import Path
import subprocess
import sys
import time
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor

import pytest
from foundry_runtime import _code_index, _launch_context, foundry_native as native


def server(tmp_path):
    return {"command": sys.executable, "args": ["-V"], "env": {
        "TEMP": str(tmp_path), "CODEGRAPH_MCP_TOOLS": "codegraph_explore,codegraph_node"}}


@pytest.mark.parametrize("available", [False, True])
def test_settlement_and_recovery_never_prepare_again(tmp_path, monkeypatch, available):
    choice = server(tmp_path) if available else None
    calls = []
    def prepare(workspace, **kw):
        calls.append(workspace)
        return choice
    monkeypatch.setattr(_code_index, "prepare", prepare)
    result = {"decision": {"status": "blocked", "tasks": []}, "reason": "fixture", "messages": []}
    rows = [
        {"type":"system", "subtype":"init", "tools":["StructuredOutput"], "mcp_servers":[],
         "skills":[], "plugins":[], "model":"fixture", "session_id":"session-1"},
        {"type":"assistant", "session_id":"session-1", "message":{"role":"assistant", "content":[
         {"type":"tool_use", "name":"StructuredOutput", "id":"f1", "input":result}]}},
        {"type":"user", "session_id":"session-1", "message":{"role":"user", "content":[
         {"type":"tool_result", "tool_use_id":"f1", "content":"Structured output provided successfully"}]}},
        {"type":"result", "subtype":"success", "is_error":False, "num_turns":2,
         "result":json.dumps(result), "structured_output":result,
         "usage":{"input_tokens":10, "output_tokens":20, "iterations":[{"type":"message"}]}, "total_cost_usd":0.125},
    ]
    output = "\n".join(map(json.dumps, rows)) + "\n"
    spawn = native.OwnedProcess.spawn
    def fixture_spawn(argv, **kwargs):
        nonlocal choice
        # Availability changes only after the real argv and invocation are frozen.
        choice = None if available else server(tmp_path)
        return spawn([sys.executable, "-B", "-c",
                      "import sys;sys.stdin.buffer.read();sys.stdout.write(" + repr(output) + ")"], **kwargs)
    monkeypatch.setattr(native.OwnedProcess, "spawn", fixture_spawn)
    request = {"kind":"plan", "call_id":"a"*64, "run_id":"test", "attempt_id":"test",
        "order_sha256":"b"*64, "task_id":None, "round":0, "order":"fixture", "task":None,
        "peers":[], "messages":[], "results":{}}
    analyst = native.NativeAnalyst(Path(sys.executable), tmp_path/"evidence", backend="claude", workspace=tmp_path)
    assert analyst(request) == result
    assert len(calls) == 1
    invocation = json.loads((analyst.last_evidence/"invocation.json").read_bytes())
    assert (invocation["launch_context"]["server"] is not None) == available
    assert json.loads((analyst.last_evidence/"receipt.json").read_bytes())["outcome"] == "success"
    monkeypatch.delenv("FOUNDRY_CODEGRAPH_ARGV", raising=False)
    monkeypatch.delenv("FOUNDRY_CODEGRAPH_STATE_DIR", raising=False)
    monkeypatch.setattr(_code_index, "prepare", lambda _, **kw: pytest.fail("read-only recovery prepared index"))
    monkeypatch.setattr(native.OwnedProcess, "spawn", lambda *a, **kw: pytest.fail("recovery respawned"))
    restored = native.NativeAnalyst(Path(sys.executable), tmp_path/"evidence", backend="claude", workspace=tmp_path, resume=True)
    assert restored(request) == result
    assert not restored.evidence_snapshot()["unresolved_call_ids"]
    if sys.platform == 'win32':
        executable = Path(sys.executable)
        with monkeypatch.context() as patch:
            patch.setattr(sys, 'executable', str(executable).upper().replace('\\', '/'))
            spelled = native.NativeAnalyst(executable, tmp_path/"evidence", backend="claude", workspace=tmp_path, resume=True)
            assert spelled(request) == result


def test_reviewers_reuse_prepared_launch_without_preparation(tmp_path, monkeypatch):
    from foundry_runtime import _foundry_native_transport as transport
    monkeypatch.setattr(_code_index, "prepare", lambda _, **kw: pytest.fail("review prepared the shared launch again"))
    monkeypatch.setattr(transport, "compute_for_kind", lambda *a: {})
    frozen = {"settings": _launch_context.settings(), "server": server(tmp_path), "index_policy": "prepare"}
    analyst = native.NativeAnalyst(Path(sys.executable), tmp_path/"review", backend="claude", workspace=tmp_path, launch_context=frozen)
    context = {"folder":tmp_path, "allocation":{"attempt_id":"review"}}
    request = {"kind":"review"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(lambda _: transport.invocation(analyst, request, b"", context, {}, "now"), range(2)))
    for record in records:
        assert record["launch_context"] == frozen
        assert record["launch_context"]["server"] == frozen['server']
        assert "--mcp-config" in record["argv"]


@pytest.mark.parametrize("global_entry", [False, True])
@pytest.mark.parametrize("available", [False, True])
def test_native_config_read_enabled_and_independent_of_global(tmp_path, monkeypatch, global_entry, available):
    from foundry_runtime import _codex_command as codex
    from foundry_runtime.app_server_transport import AppServer
    executable = codex.resolve_codex()
    home = tmp_path/"home"
    home.mkdir()
    # Isolated, credential-free home; never load or edit the operator's config.
    for key in ("CODEX_HOME", "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "TMP", "TEMP"):
        monkeypatch.setenv(key, str(home))
    (home/"config.toml").write_text('[mcp_servers.codegraph]\ncommand="operator-codegraph"\nenabled=false\n' if global_entry else "")
    launch = {"settings": _launch_context.settings(), "server": server(tmp_path) if available else None,
              "tool_discipline": {'version': 1, 'index_available': available}}
    argv = codex.coding_argv(executable, "app-server", "--stdio", launch_context=launch)
    process = native.OwnedProcess.spawn(argv, cwd=tmp_path, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, attempt_id="tooleff1-config", env=codex.codex_env(executable))
    rpc = AppServer(process, io.StringIO(), deadline=time.monotonic()+20)
    try:
        rpc.start_readers()
        rpc.request("initialize", {"clientInfo":{"name":"tooleff1-config", "version":"1"}})
        rpc.notify("initialized")
        config = rpc.request("config/read", {"includeLayers":True})["config"]
        hooks = rpc.request('hooks/list', {'cwds': [str(tmp_path)]})['data'][0]
        own = [h for h in hooks['hooks'] if '_tool_discipline' in h.get('command', '')]
        assert len(own) == 1 and own[0]['enabled'] and own[0]['trustStatus'] == 'trusted'
        assert not hooks['errors']
        servers = config.get("mcp_servers") or {}
        if available:
            assert servers["foundry_codegraph"]["enabled"] is True
            assert servers["foundry_codegraph"]["default_tools_approval_mode"] == "approve"
            assert servers["foundry_codegraph"]["command"] == sys.executable
            assert servers["foundry_codegraph"]["enabled_tools"] == ['codegraph_explore', 'codegraph_node']
        else:
            assert "foundry_codegraph" not in servers
        if global_entry:
            assert servers["codegraph"]["enabled"] is False
            assert servers["codegraph"]["command"] == "operator-codegraph"
        else:
            assert "codegraph" not in servers
        (tmp_path/"config-proof.json").write_text(json.dumps({"servers":servers, "available":available, "global_entry":global_entry}))
    finally:
        process.stdin.close()
        cleanup = process.stop_and_verify(drain_seconds=1, kill_seconds=5)
        rpc._stdout_thread.join(timeout=3)
        rpc._stderr_thread.join(timeout=3)
        (tmp_path/"cleanup.json").write_text(json.dumps(asdict(cleanup)))
        assert cleanup.cleanup_verified and cleanup.job_active_processes == 0


def test_claude_environment_uses_the_same_frozen_settings(monkeypatch):
    from foundry_runtime.foundry_claude import environment
    launch=_launch_context.capture(enable_index=False, backend="claude")
    before = environment(launch_context=launch)['CLAUDE_CODE_AUTO_COMPACT_WINDOW']
    assert before == '500000'
    monkeypatch.setenv("FOUNDRY_CONTEXT_TOKENS","900000")
    monkeypatch.setenv('FOUNDRY_COMPACT_TOKENS', '500000')
    assert environment(launch_context=launch)["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == before
