"""Claude native schema and invocation, called only by NativeAnalyst custody."""
import json
import math
import os
from pathlib import Path
from ._compute_selection import compute_kwargs



from ._execution_profile import claude_policy, claude_environment


def environment(effort=None, *, execution_environment=None, launch_context=None):
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith('GIT_') and k not in {'CLAUDECODE', 'CLAUDE_CODE_EFFORT_LEVEL'}}
    env['PYTHONIOENCODING'] = 'utf-8'
    from ._launch_context import claude_settings
    env.update(claude_settings(launch_context)['env'])
    # Print mode kills background tasks when the turn ends, and caps a foreground
    # command at 10 minutes by default; a full-suite run takes about 50 (DECK2 lost
    # two runs so, 2026-09-25, CLI 2.1.282). Background tasks are disabled and the
    # foreground cap is the session wall budget, so long runs finish inside the
    # turn: measured, a 700 s command then ran to completion with nothing killed.
    env.update(claude_environment() if execution_environment is None else execution_environment)
    # Foundry retains CLI bytes for session recovery; updates enter via resolution.
    env['DISABLE_AUTOUPDATER'] = '1'
    if effort is not None:
        env['CLAUDE_CODE_EFFORT_LEVEL'] = effort
    return env



def provider_schema(schema):
    """Derived transport projection; the original host schema stays authoritative.

    https://platform.claude.com/docs/en/build-with-claude/structured-outputs
    documents unsupported string/array constraints and description projection.
    """
    if not isinstance(schema, dict):
        return schema
    projected = {key: value for key, value in schema.items() if key not in {"minLength", "maxLength", "uniqueItems"}}
    hints = []
    if "minLength" in schema:
        hints.append(f"Use at least {schema['minLength']} Unicode characters.")
    if "maxLength" in schema:
        hints.append(f"Use at most {schema['maxLength']} Unicode characters. Be concise; host validation rejects longer text.")
    if schema.get("uniqueItems"):
        hints.append("All items must be unique.")
    if hints:
        projected["description"] = " ".join(hints)
    if "properties" in schema:
        projected["properties"] = {key: provider_schema(value) for key, value in schema["properties"].items()}
    if "items" in schema:
        projected["items"] = provider_schema(schema["items"])
    if "anyOf" in schema:
        projected["anyOf"] = [provider_schema(branch) for branch in schema["anyOf"]]
    return projected


NATIVE_COST_THRESHOLD_USD = 25

# 2026-09-16: three formatter attempts, not two. Measured over 19 Claude analysis runs: two died
# because the model's first TWO StructuredOutput objects both used another role's shape
# (evidence/FOUNDRYV2REVIEW/r5-claude-2, round4-final-a-protocol-refused). The CLI --max-turns cap
# is the same number, so the host bound and the native bound cannot disagree.
FORMATTER_ATTEMPTS = 3

_SCHEMA_REJECTION = "Output does not match required schema: "
_PARSE_REJECTION = ("<tool_use_error>InputValidationError: StructuredOutput was called with input "
                    "that could not be parsed as JSON.")


def bound_formatter_rejection(tool_input, schema_error, content, tool_use_result) -> bool:
    """True only for a CLI rejection bound to the attempt it rejects.

    Two shapes are observed. A schema rejection follows an input the host's own contract also
    rejects. A parse rejection follows formatter text the CLI could not decode, which it surfaces
    as the single key __unparsedToolInput; four measured runs died because only the first shape was
    recognised (evidence/PORTREC1B/review-4/reviewer-b-sonnet, FOUNDRYV2REVIEW/r5-claude,
    round4-reviewer-a, reviewer-a). Any other error text stays unbound and refused.
    """
    if not isinstance(content, str) or not isinstance(tool_use_result, str):
        return False
    if schema_error is not None and content.startswith(_SCHEMA_REJECTION):
        return tool_use_result == "Error: " + content
    return (isinstance(tool_input, dict) and set(tool_input) == {"__unparsedToolInput"}
            and content.startswith(_PARSE_REJECTION)
            and tool_use_result.startswith("InputValidationError: JSON parse failed"))


def _claude_argv(executable: Path, session_name: str, schema: dict, *, model=None, effort=None,
                 resume_session=None, execution_policy=None, workspace=None, launch_context=None) -> list[str]:
    selection = compute_kwargs(model, effort, backend="claude")
    from ._launch_context import claude_args
    return [str(executable), *claude_args(workspace, launch=launch_context), "--print", "--output-format", "stream-json", "--verbose",
            "--permission-mode", (execution_policy if execution_policy is not None else claude_policy())["permission_mode"],
            *(["--effort", selection["effort"]] if "effort" in selection else [] if selection else ["--effort", "low"]),
            *(["--model", selection["model"]] if selection else []),
            "--json-schema", json.dumps(provider_schema(schema), ensure_ascii=False, allow_nan=False),
            *(["--resume", resume_session] if resume_session else ["--name", session_name])]


# Native modelUsage is a session snapshot, including spend restored on --resume.
# Keep it separate from per-call usage and from Codex's input-token convention.
_USAGE_FIELDS = {"inputTokens": "input_tokens", "outputTokens": "output_tokens",
                 "cacheReadInputTokens": "cache_read_input_tokens",
                 "cacheCreationInputTokens": "cache_creation_input_tokens",
                 "thinkingTokens": "thinking_tokens", "costUSD": "list_price_usd"}


def usage_observation(call):
    """Read retained Claude evidence without launching or rewriting a session."""
    import hashlib
    import math
    folder = Path(call['evidence'])
    receipt_path, stream_path = folder / 'receipt.json', folder / 'stdout.jsonl'
    receipt_raw = receipt_path.read_bytes()
    receipt = json.loads(receipt_raw)
    if (receipt.get('backend') != 'claude' or receipt.get('call_id') != call['call_id']
            or receipt.get('attempt_id') != call.get('attempt_id')
            or receipt.get('usage') != call.get('usage')):
        raise ValueError('Claude usage receipt differs from selected execution')
    digest, size, init, result = hashlib.sha256(), 0, None, None
    with stream_path.open('rb') as stream:
        for line in stream:
            digest.update(line); size += len(line)
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get('type') == 'system' and row.get('subtype') == 'init':
                if init is not None:
                    raise ValueError('multiple Claude session starts in one capture')
                init = row
            if row.get('type') == 'result':
                if result is not None:
                    raise ValueError('multiple Claude results in one capture')
                result = row
    if size != receipt.get('stdout_bytes') or digest.hexdigest() != receipt.get('stdout_sha256'):
        raise ValueError('Claude captured usage stream changed')
    session = receipt.get('session_id')
    if (not session or not init or not result or init.get('session_id') != session
            or result.get('session_id') != session or result.get('usage') != receipt.get('usage')
            or result.get('total_cost_usd') != receipt.get('list_price_usd')):
        raise ValueError('Claude result lacks matching session and usage metadata')
    models = result.get('modelUsage')
    if not isinstance(models, dict) or not models:
        raise ValueError('Claude result does not report modelUsage')
    observed = model_usage(models)
    return {'session_id': session, 'ended_utc': receipt.get('ended_utc'),
            'call_id': call['call_id'], 'attempt_id': receipt['attempt_id'],
            'models': observed, 'source': {
                'receipt': {'path': str(receipt_path.resolve()), 'sha256': hashlib.sha256(receipt_raw).hexdigest()},
                'stdout': {'path': str(stream_path.resolve()), 'sha256': digest.hexdigest()}},
            'scope': 'CLI-reported cumulative session snapshot; includes prior resumed calls; not account charge'}



def model_usage(models):
    import math
    observed = []
    for model, counters in models.items():
        if not isinstance(model, str) or not model or not isinstance(counters, dict):
            raise ValueError('Claude modelUsage entry is malformed')
        row = {'model': model, 'canonical_model': counters.get('canonicalModel'),
               'provider': counters.get('provider'), 'cost_basis': counters.get('costBasis')}
        for native, field in _USAGE_FIELDS.items():
            value = counters.get(native)
            numeric = type(value) in (int, float) if native == 'costUSD' else type(value) is int
            row[field] = value if numeric and math.isfinite(value) and value >= 0 else None
        observed.append(row)
    return observed

def session_usage(calls):
    """Count each selected session once, with explicit gaps instead of guessed deltas."""
    from datetime import datetime
    observations, gaps = [], []
    for call in calls:
        try:
            observation = usage_observation(call)
            observations.append(observation)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            gaps.append({'call_id': call.get('call_id'), 'reason': str(exc)})
    return aggregate_sessions(observations, gaps)


def aggregate_sessions(observations, gaps=()):
    from datetime import datetime
    sessions, gaps = {}, list(gaps)
    for observation in observations:
        sessions.setdefault(observation['session_id'], []).append(observation)
    selected = []
    for session, observations in sessions.items():
        try:
            # End times order observations, never filenames, task order or token magnitude.
            def stamp(row):
                value = datetime.fromisoformat(row['ended_utc'].replace('Z', '+00:00'))
                if value.tzinfo is None:
                    raise ValueError('Claude usage timestamp has no timezone')
                return value
            ordered = sorted(observations, key=stamp)
            for before, after in zip(ordered, ordered[1:]):
                if stamp(before) == stamp(after) and before['models'] != after['models']:
                    raise ValueError('conflicting Claude session snapshots at the same time')
                old = {row['model']: row for row in before['models']}
                new = {row['model']: row for row in after['models']}
                if old.keys() - new.keys() or any(
                        old[model][field] is not None and new[model][field] is not None
                        and new[model][field] < old[model][field]
                        for model in old.keys() & new.keys() for field in _USAGE_FIELDS.values()):
                    raise ValueError('Claude cumulative session counters regressed; scope cannot be reconciled')
            selected.append({**ordered[-1], 'selected_observation_count': len(ordered)})
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            gaps.append({'session_id': session, 'reason': str(exc)})
    groups = {}
    for session in selected:
        for row in session['models']:
            groups.setdefault(row['model'], []).append(row)
    totals = []
    for model, rows in sorted(groups.items()):
        metrics = {field: {'observed_sum': sum(row[field] for row in rows if row[field] is not None)
                           if any(row[field] is not None for row in rows) else None,
                          'observed_sessions': sum(row[field] is not None for row in rows),
                          'missing_sessions': sum(row[field] is None for row in rows)}
                   for field in _USAGE_FIELDS.values()}
        totals.append({'model': model, 'session_count': len(rows), 'metrics': metrics})
    return {'coverage': 'partial' if gaps else 'observed_sessions' if selected else 'unavailable',
            'sessions': selected, 'by_model': totals, 'gaps': gaps,
            'scope': 'latest selected cumulative snapshot per Claude session; may include earlier work outside the selected tasks; do not add to per-call usage',
            'actual_account_charge': 'unknown'}


def validate_result_header(row, protocol_error, provider_error):
    """Separate a reported CLI failure from malformed successful output."""
    if row.get("is_error") is True or str(row.get("subtype", "")).startswith("error"):
        reason = row.get("result") or row.get("errors") or row.get("subtype")
        terminal = row.get("terminal_reason") or row.get("subtype")
        raise provider_error(f"native provider failure ({terminal}): {reason}")
    if row.get("subtype") != "success" or row.get("is_error") is not False:
        raise protocol_error("native result lacks a valid success status")
    if not isinstance(row.get("result"), str):
        raise protocol_error("native result text is missing")
