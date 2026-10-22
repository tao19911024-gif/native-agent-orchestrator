import json
import subprocess
import sys
from pathlib import Path

import pytest

from foundry_runtime import _tool_discipline as rules, _code_index, _launch_context


@pytest.mark.parametrize('name,field', [('Agent','prompt'), ('spawn_agent','message')])
def test_role_cost_is_shared_and_versions_are_not_fixed(name, field):
    for model in ('opus', 'claude-opus-99', 'gpt-99-astra'):
        event = {'tool_name': name, 'tool_input': {'model': model, field: 'Read source files'}}
        assert rules.check(event, {})['hookSpecificOutput']['permissionDecision'] == 'deny'
        event['tool_input'][field] = '[decision: resolve conflicting transaction ownership] Read relevant source.'
        assert rules.check(event, {}) == {}
    for model in ('sonnet', 'haiku', 'gpt-99-sol', 'gpt-99-terra', 'gpt-99-luna'):
        assert rules.check({'tool_name': name, 'tool_input': {'model': model, field: '查找中文符号'}}, {}) == {}
    assert rules.check({'tool_name': name, 'tool_input': {field: 'Read source'}}, {})


@pytest.mark.parametrize('name', ['Bash', 'PowerShell', 'exec_command'])
def test_index_failure_and_explicit_scoped_fallback_do_not_block(name, tmp_path):
    event = {'tool_name': name, 'tool_input': {'command': 'rg 示例应用 src'}}
    assert rules.check(event, {'index_available': False}) == {}
    assert rules.check(event, {'index_available': True})
    event['tool_input']['command'] += '\n#idx: 服务占用，直接核对 src 中的符号'
    assert rules.check(event, {'index_available': True}) == {}
    event['tool_input']['command'] = 'rg 示例应用 src'
    transcript = tmp_path / 'member.jsonl'
    transcript.write_text(json.dumps({'type': 'assistant', 'message': {'content': [
        {'type': 'tool_use', 'name': 'mcp__foundry_codegraph__codegraph_explore'}]}})+'\n', encoding='utf-8')
    event['transcript_path'] = str(transcript)
    assert rules.check(event, {'index_available': True}) == {}
    # Invalid/truncated records and merely mentioning the tool do not count.
    transcript.write_text('{"partial":\n'+json.dumps({'message': {'content': [{'type': 'text', 'text': 'codegraph_explore'}]}}))
    assert rules.check(event, {'index_available': True})


def test_index_preparation_timeout_is_recorded_once(monkeypatch, tmp_path):
    monkeypatch.setenv('FOUNDRY_CODEGRAPH_ARGV', '["node","codegraph"]')
    monkeypatch.setenv('FOUNDRY_CODEGRAPH_STATE_DIR', str(tmp_path / 'state'))
    calls = []
    def timeout(*a, **kw):
        calls.append((a, kw))
        raise subprocess.TimeoutExpired(a[0], kw['timeout'])
    monkeypatch.setattr(subprocess, 'run', timeout)
    launch = _launch_context.capture(tmp_path)
    assert len(calls) == 1 and calls[0][1]['timeout'] == 30
    assert launch['server'] is None and not launch['tool_discipline']['index_available']
    assert launch['index_observation']['status'] == 'unavailable'
    assert 'timed out' in launch['index_observation']['reason']
    _launch_context.codex_settings(launch=launch)
    _launch_context.claude_args(launch=launch)
    assert len(calls) == 1


def test_new_and_retained_launch_contracts_do_not_change_old_hooks():
    legacy = {'settings': _launch_context.settings(), 'server': None}
    assert 'hooks=' not in ' '.join(_launch_context.codex_settings(launch=legacy))
    assert '_claude_tool_input' in _launch_context.claude_args(launch=legacy)[1]
    launch = {**legacy, 'tool_discipline': {'version': 1, 'index_available': False}}
    assert '_tool_discipline' in _launch_context.claude_args(launch=launch)[1]
    assert 'trusted_hash' in ' '.join(_launch_context.codex_settings(launch=launch))


@pytest.mark.parametrize('event', [
    {'tool_name': 'Write', 'tool_input': {'file_path': '/tmp/test.py'}},
    {'tool_name': 'apply_patch', 'tool_input': {'command': '*** Add File: /tmp/test.py\n+test'}},
    {'tool_name': 'Bash', 'tool_input': {'command': 'echo test > /tmp/result'}},
    {'tool_name': 'PowerShell', 'tool_input': {'command': 'Set-Content C:/Users/X/AppData/Local/Temp/result test'}},
])
def test_forbidden_temp_writes_are_shared(event, monkeypatch):
    # /tmp resolves through the native TEMP authority; the host may place it
    # on D:. Explicitly model C: for this prohibition, without writing there.
    monkeypatch.setenv('TEMP', 'C:/Users/Fixture/AppData/Local/Temp')
    assert rules.check(event, {})
    assert rules.check({'tool_name': 'Bash', 'tool_input': {'command': 'cat /tmp/retained-log'}}, {}) == {}
    assert rules.check({'tool_name': 'Write', 'tool_input': {'file_path': 'D:/scratch/test.py'}}, {}) == {}


@pytest.mark.parametrize('event', [
    {'tool_name': 'Write', 'tool_input': {'file_path': '/tmp/test.py'}},
    {'tool_name': 'apply_patch', 'tool_input': {'command': '*** Add File: /tmp/test.py\n+test'}},
    {'tool_name': 'Bash', 'tool_input': {'command': 'echo test > /tmp/result'}},
])
def test_relocated_temp_writes_follow_observed_volume(event, monkeypatch):
    monkeypatch.setenv('TEMP', 'D:/Foundry/tmp')
    assert rules.check(event, {}) == {}


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_installed_hook_receives_utf8_without_rewriting_chinese(provider):
    event = {'tool_name': 'Bash', 'tool_input': {'command': 'rg 示例应用 src\n#idx: 索引已失效，读取当前源码'}}
    result = subprocess.run([sys.executable, '-I', '-m', 'foundry_runtime._tool_discipline',
        provider, rules.encoded({'version': 1, 'index_available': True})],
        input=json.dumps(event, ensure_ascii=False).encode(), capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    if provider == 'claude':
        assert value['hookSpecificOutput']['updatedInput']['command'] == 'exec </dev/null\n' + event['tool_input']['command']
    else:
        assert value == {}
