import json
import tomllib

import pytest

from foundry_runtime import _code_index, _launch_context, _tool_discipline


def test_new_launch_exposes_precise_reads_in_both_transports(monkeypatch, tmp_path):
    monkeypatch.setenv('FOUNDRY_CODEGRAPH_ARGV', '["node","codegraph.js"]')
    monkeypatch.setenv('FOUNDRY_CODEGRAPH_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setattr(_code_index, '_run', lambda *a: '1.6.2')
    monkeypatch.setattr(_code_index, 'verify', lambda *a, **kw: {'indexed_files': 1})
    launch = _launch_context.capture(tmp_path)
    expected = ['codegraph_explore', 'codegraph_node']
    assert launch['server']['env']['CODEGRAPH_MCP_TOOLS'].split(',') == expected
    codex = tomllib.loads('\n'.join(_launch_context.codex_settings(launch=launch)))
    assert codex['mcp_servers']['foundry_codegraph']['enabled_tools'] == expected
    claude = _launch_context.claude_args(launch=launch)
    server = json.loads(claude[claude.index('--mcp-config') + 1])['mcpServers']['foundry_codegraph']
    assert server == launch['server']


@pytest.mark.parametrize('names', [None, 'codegraph_explore', 'explore'])
def test_retained_explore_only_launch_stays_explore_only(names):
    env = {} if names is None else {'CODEGRAPH_MCP_TOOLS': names}
    launch = {'settings': _launch_context.settings(),
              'server': {'command': 'node', 'args': [], 'env': env}}
    before = json.dumps(launch)
    codex = tomllib.loads('\n'.join(_launch_context.codex_settings(launch=launch)))
    assert codex['mcp_servers']['foundry_codegraph']['enabled_tools'] == ['codegraph_explore']
    assert json.dumps(launch) == before


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_precise_query_allows_followup_direct_verification(provider, tmp_path):
    call = ({'message': {'content': [{'type': 'tool_use', 'name': 'mcp__foundry_codegraph__codegraph_node'}]}}
            if provider == 'claude' else
            {'payload': {'type': 'function_call', 'name': 'mcp__foundry_codegraph__codegraph_node'}})
    transcript = tmp_path / 'member.jsonl'
    transcript.write_text(json.dumps(call), encoding='utf-8')
    event = {'tool_name': 'PowerShell', 'tool_input': {'command': 'rg cleanup src'},
             'transcript_path': str(transcript)}
    assert _tool_discipline.check(event, {'index_available': True}) == {}
    transcript.write_text(json.dumps({'message': {'content': [{'type': 'text', 'text': 'codegraph_node'}]}}))
    assert _tool_discipline.check(event, {'index_available': True})
