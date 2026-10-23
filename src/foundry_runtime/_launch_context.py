"""Native per-launch context controls; no global provider configuration writes."""
import json
import os
import shlex
import sys
from pathlib import Path
from contextlib import contextmanager
from contextvars import ContextVar

_retained_settings = ContextVar('foundry_retained_launch_settings', default=None)


@contextmanager
def retained_settings(values):
    """A continuing native conversation owns its recorded launch settings."""
    token = _retained_settings.set(values)
    try:
        yield
    finally:
        _retained_settings.reset(token)

READING_DEFAULTS = {
    'claude': {'unit': 'lines', 'single': 150, 'turn': 400, 'index_charge': 250},
    'codex': {'unit': 'chars', 'single': 12000, 'turn': 48000, 'index_charge': 12000},
}


def reading_budget(backend, limits):
    defaults = dict(READING_DEFAULTS[backend])
    if backend == 'codex':
        defaults.update(single=limits['bash_chars'], turn=limits['bash_chars'] * 4,
                        index_charge=limits['bash_chars'])
    for field in ('single', 'turn', 'index_charge'):
        key = f'FOUNDRY_{backend.upper()}_READ_{field.upper()}'
        raw = os.environ.get(key)
        # Invalid optional calibration retains the documented default; it must
        # not strand an otherwise runnable task during environment changes.
        if raw is not None and raw.isdecimal() and int(raw) > 0:
            defaults[field] = int(raw)
    return defaults


def validate_project_launch(values):
    """Validate JSON types at the descriptor boundary; native capture owns use."""
    integers = {"FOUNDRY_CONTEXT_TOKENS", "FOUNDRY_COMPACT_PERCENT", "FOUNDRY_COMPACT_TOKENS",
                "FOUNDRY_BASH_CHARS", "FOUNDRY_TOOL_TOKENS", "FOUNDRY_WAIT_INTERVAL_SECONDS"}
    integers.update(f'FOUNDRY_{backend.upper()}_READ_{field.upper()}'
                    for backend in READING_DEFAULTS for field in ('single', 'turn', 'index_charge'))
    if not isinstance(values, dict):
        raise ValueError("launch must be an object")
    for key, value in values.items():
        if key in integers:
            valid = type(value) is int
        elif key == "FOUNDRY_CODEGRAPH_ARGV":
            valid = isinstance(value, list) and all(isinstance(v, str) for v in value)
        elif key == "FOUNDRY_CODEGRAPH_STATE_DIR":
            valid = isinstance(value, str)
        else:
            raise ValueError("unknown launch setting: " + key)
        if not valid:
            raise ValueError("invalid launch setting type: " + key)


def settings(backend="codex"):
    retained = _retained_settings.get()
    if retained is not None:
        return dict(retained)
    # Operator-tunable launch inputs, not execution/call/time budgets.
    return {"context_tokens": int(os.environ.get("FOUNDRY_CONTEXT_TOKENS", "500000" if backend == "claude" else "300000")),
            "compact_tokens": int(os.environ.get("FOUNDRY_COMPACT_TOKENS", "0")),
            "compact_percent": int(os.environ.get("FOUNDRY_COMPACT_PERCENT", "90")),
            "bash_chars": int(os.environ.get("FOUNDRY_BASH_CHARS", "12000")),
            "tool_tokens": int(os.environ.get("FOUNDRY_TOOL_TOKENS", "0" if backend == "codex" else "3000"))}


def claude_settings(launch=None):
    s = launch["settings"] if launch is not None else settings("claude")
    window = s['context_tokens']
    if s.get('compact_tokens', 0) > 0 and s['compact_percent'] > 0:
        window = min(window, (s['compact_tokens'] * 100 + s['compact_percent'] - 1) // s['compact_percent'])
    return {"bashOutputMaxChars": s["bash_chars"], "env": {
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(window),
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": str(s["compact_percent"]),
        "BASH_MAX_OUTPUT_LENGTH": str(s["bash_chars"]),
        "DISABLE_AUTO_COMPACT": "0", "DISABLE_COMPACT": "0"}}


def capture(workspace=None, *, enable_index=True, root_role='executor', backend='codex', coding_model=None):
    """One launch decision, retained by native calls for side-effect-free replay."""
    from ._code_index import prepare
    observation = {'status': 'disabled', 'fallback': 'scoped-direct-reads', 'reason': 'review-direct-reads'}
    server = prepare(workspace, observation=observation) if enable_index else None
    limits = settings(backend)
    return {"settings": limits, "server": server,
            "index_policy": "prepare" if enable_index else "review-direct-reads",
            "index_observation": observation,
            "tool_discipline": {"version": 6, "index_available": server is not None, "index_workspace": str(Path(workspace).resolve()) if workspace else None, 'root_role': root_role,
                **({'recorded_coding_model': coding_model} if coding_model else {}),
                'reading_budget': reading_budget(backend, limits)}}


def bind_coding_model(launch, selected):
    """Add an explicit resumed assignment; keep policy and prior exclusions frozen."""
    policy = launch.get('tool_discipline', {})
    if policy.get('version', 1) < 5 or not selected:
        return
    previous = policy.get('recorded_coding_model')
    if selected == previous:
        return
    models = set(policy.get('recorded_coding_models', []))
    models.update(m for m in (previous, selected) if m)
    policy['recorded_coding_model'] = selected
    policy['recorded_coding_models'] = sorted(models)


def claude_args(workspace=None, *, launch=None, aliases=None):
    launch = capture(workspace, backend="claude") if launch is None else launch
    server = launch["server"]
    config = claude_settings(launch)
    if aliases:
        config['env'].update({'ANTHROPIC_DEFAULT_' + tier.upper() + '_MODEL': value
                              for tier, value in aliases.items()})
    # Scope input detachment to Bash, not the CLI's prompt or MCP/RPC channels.
    command = shlex.join([sys.executable.replace('\\', '/'), '-I', '-m',
                          'foundry_runtime._claude_tool_input'])
    config['hooks'] = {'PreToolUse': [{'matcher': 'Bash', 'hooks': [
        {'type': 'command', 'command': command}]}]}
    if launch.get('tool_discipline'):
        from ._tool_discipline import command as discipline_command
        matcher = 'Bash|PowerShell|Grep|Search|Agent|Task|Write|Edit'
        if launch['tool_discipline'].get('version', 1) >= 3:
            matcher += '|Read'
        config['hooks'] = {'PreToolUse': [{'matcher': matcher,
            'hooks': [{'type': 'command', 'command': discipline_command(launch['tool_discipline'], 'claude')}]}]}
    return ["--settings", json.dumps(config),
            *(["--mcp-config", json.dumps({"mcpServers": {"foundry_codegraph": server}})] if server else [])]


def codex_settings(workspace=None, *, launch=None):
    launch = capture(workspace) if launch is None else launch
    s, server = launch["settings"], launch["server"]
    # Native compaction owns summarization; frozen settings retain their recorded
    # override (or absence). Leave unrelated user codegraph entries untouched.
    # Zero leaves output sizing to the native client and its selected settings.
    # Retained launches still carry their original explicit override.
    result = (["tool_output_token_limit=" + str(s["tool_tokens"])]
              if s["tool_tokens"] > 0 else [])
    if s.get('compact_tokens', 0) > 0:
        result.append('model_auto_compact_token_limit=' + str(s['compact_tokens']))
    if launch.get('tool_discipline'):
        from ._tool_discipline import codex_hooks, toml
        result += ['features.hooks=true', 'hooks=' + toml(codex_hooks(launch['tool_discipline']))]
    if server:
        # Use the frozen server surface; resuming an explore-only launch must not
        # silently gain tools from the currently installed runtime.
        names = server['env'].get('CODEGRAPH_MCP_TOOLS', 'codegraph_explore')
        enabled = ['codegraph_' + name.strip().removeprefix('codegraph_')
                   for name in names.split(',') if name.strip()]
        result.append('mcp_servers.foundry_codegraph={enabled=true,default_tools_approval_mode="approve",command=' + json.dumps(server["command"]) +
            ",args=" + json.dumps(server["args"]) + ",env={" +
            ",".join(k+"="+json.dumps(v) for k,v in server["env"].items()) +
            '},enabled_tools=' + json.dumps(enabled) + '}')
    return result


def compaction_observation(launch, backend):
    """Project frozen launch policy, never infer a native threshold from capacity."""
    value = (launch or {}).get('settings', {}).get('compact_tokens')
    if backend == 'claude':
        limits = (launch or {}).get('settings', {})
        window, percent = limits.get('context_tokens'), limits.get('compact_percent')
        bound = window * percent // 100 if type(window) is int and type(percent) is int else None
        if type(value) is int and value > 0:
            bound = min(bound, value) if bound is not None else value
        return {'backend': backend, 'mode': 'configured_window' if bound is not None else 'unrecorded',
                'window_tokens': window, 'compact_percent': percent, 'token_limit': bound,
                'source': 'frozen_launch_context',
                'note': 'Configured target; native model capacity and CLI behavior determine actual compaction.'}
    if backend != 'codex':
        return {'backend': backend, 'mode': 'unrecorded', 'token_limit': None}
    if type(value) is not int:
        return {'backend': backend, 'mode': 'unrecorded', 'token_limit': None}
    return {'backend': backend, 'mode': 'explicit_override' if value > 0 else 'native_default',
            'token_limit': value if value > 0 else None,
            'source': 'frozen_launch_context',
            'note': 'Native default leaves the threshold to Codex; context capacity is not a compaction threshold.'}
