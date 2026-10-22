import json
import os

import pytest

from foundry_runtime import _launch_context as launch
from foundry_runtime._project_execution import open_project
from test_installed_runtime import external


def configured(external, values):
    tree, selected, order, calls = external
    project = selected.project
    project["launch"] = values
    selected.path.write_text(json.dumps(project))
    return open_project(selected.path)


def test_descriptor_launch_capture_and_restore(external, monkeypatch):
    monkeypatch.setenv("FOUNDRY_TOOL_TOKENS", "111")
    monkeypatch.delenv("FOUNDRY_CODEGRAPH_ARGV", raising=False)
    selected = configured(external, {"FOUNDRY_TOOL_TOKENS": 4321,
        "FOUNDRY_CONTEXT_TOKENS": 250000, "FOUNDRY_CODEGRAPH_ARGV": []})
    with selected.launch_scope():
        frozen = launch.capture(enable_index=False)
        assert frozen["settings"]["tool_tokens"] == 4321
        assert frozen["settings"]["context_tokens"] == 250000
        assert os.environ["FOUNDRY_CODEGRAPH_ARGV"] == "[]"
        captured_settings = launch.codex_settings(launch=frozen)
        assert 'tool_output_token_limit=4321' in captured_settings
    assert os.environ["FOUNDRY_TOOL_TOKENS"] == "111"
    assert "FOUNDRY_CODEGRAPH_ARGV" not in os.environ
    assert launch.codex_settings(launch=frozen) == captured_settings


def test_cli_uses_descriptor_scope(external, monkeypatch):
    from foundry_runtime import cli
    selected = configured(external, {"FOUNDRY_TOOL_TOKENS": 4321})
    def run(*args):
        assert launch.settings()["tool_tokens"] == 4321
        raise RuntimeError("fixture stops before provider launch")
    monkeypatch.setenv("FOUNDRY_TOOL_TOKENS", "111")
    monkeypatch.setattr(cli, "_selected_main", run)
    with pytest.raises(RuntimeError, match="fixture stops"):
        cli.main(["--project-config", str(selected.path), "--order", str(external[2])])
    assert os.environ["FOUNDRY_TOOL_TOKENS"] == "111"


def test_old_descriptor_keeps_environment(external, monkeypatch):
    monkeypatch.setenv("FOUNDRY_TOOL_TOKENS", "222")
    with external[1].launch_scope():
        assert launch.settings()["tool_tokens"] == 222


@pytest.mark.parametrize("values", [[], {"FOUNDRY_TOOL_TOKENS": "3000"},
    {"FOUNDRY_CODEGRAPH_ARGV": "node"}, {"FOUNDRY_CODEGRAPH_STATE_DIR": []},
    {"HOME": "elsewhere"}])
def test_descriptor_launch_json_boundary(external, values):
    with pytest.raises(ValueError, match="launch"):
        configured(external, values)
