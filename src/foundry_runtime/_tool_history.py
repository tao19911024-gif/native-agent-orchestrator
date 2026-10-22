"""Read-only projections of native transcripts for the shared tool hook.

No counter file or alternate journal: missing history stays incomplete. The
bounded suffix avoids scanning a growing conversation for every tool call.
"""
import json
import re
from pathlib import Path

def returned_text(value, *, command_envelope=False):
    """Unwrap native transport fields once; never decode returned source JSON."""
    if isinstance(value, list):
        if not all(isinstance(v, dict) and v.get('type') in ('text', 'input_text')
                   and isinstance(v.get('text'), str) for v in value):
            return None
        value = '\n'.join(v['text'] for v in value)
    if command_envelope and isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            pass
        else:
            # exec_command owns this envelope. Its output is opaque source,
            # including when that source itself looks like another envelope.
            if isinstance(decoded, dict) and isinstance(decoded.get('output'), str) and any(
                    k in decoded for k in ('exit_code', 'wall_time_seconds', 'session_id', 'metadata')):
                value = decoded['output']
    return value if isinstance(value, str) else None


def search_request(name, value):
    if name in ('Grep', 'Search'):
        return True
    if name not in ('Bash', 'PowerShell', 'powershell', 'exec_command', 'shell_command'):
        return False
    command = value.get('command', value.get('cmd', ''))
    # Mixed shell and variables are not positive read evidence. The usage
    # observer can still report them as candidates without denying execution.
    return isinstance(command, str) and bool(re.fullmatch(
        r'\s*(?:rg|grep|Select-String)\s+[^;|&<>$`\r\n]+\s*', command, re.I))


def observed_read(name, value, *, followup=False):
    """Recognize standalone read pipelines, never a word in mixed execution."""
    if name in ('Read', 'Grep', 'Search'):
        return True
    if name not in ('Bash', 'PowerShell', 'powershell', 'exec_command', 'shell_command'):
        return False
    command = value.get('command', value.get('cmd', ''))
    if not isinstance(command, str):
        return False
    if read_request(name, value, '.', measure=False, expanded=True, followup=followup):
        return True
    command = command.split('\n#', 1)[0].strip()
    # Shell operators inside literal arguments (awk programs, search patterns)
    # are not compound shell execution. Do not evaluate or expand those strings.
    command = re.sub(r'''"[^"\r\n]*"|'[^'\r\n]*' ''', 'literal', command, flags=re.X)
    return bool(re.fullmatch(r'git\s+(?:show|diff)', command, re.I) or re.fullmatch(
        r'(?:cat|Get-Content|sed|head|tail|awk|grep|rg|Select-String|git\s+(?:show|diff|grep' + ('|log' if followup else '') + r'))'
        r'\s+[^;|&<>$`\r\n]+(?:\|\s*(?:head|tail)\s+(?:-n\s*|-)\d+\s*)?', command, re.I))


def transcript(path, *, limit=2 * 1024 * 1024):
    rows, complete = [], True
    try:
        with Path(path).open('rb') as stream:
            header = json.loads(stream.readline())
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - limit))
            if size > limit:
                stream.readline()
                complete = False
            for raw in stream.read().splitlines():
                try:
                    rows.append(json.loads(raw))
                except (ValueError, UnicodeError):
                    complete = False
        return header, rows, complete
    except (OSError, ValueError, TypeError):
        return {}, [], False


def current_turn(rows):
    start = None
    for i, row in enumerate(rows):
        payload = row.get('payload', {})
        content = row.get('message', {}).get('content')
        if ((row.get('type') == 'event_msg' and payload.get('type') == 'task_started')
                or (row.get('type') == 'user' and not row.get('isMeta') and
                    (isinstance(content, str) or isinstance(content, list) and
                     any(c.get('type') == 'text' for c in content) and
                     not any(c.get('type') == 'tool_result' for c in content)))):
            start = i
    return (rows[start:], True) if start is not None else (rows, False)


def calls(rows):
    """Join native calls and exact single-command wrappers; mixed JS is unknown."""
    pending, results = {}, []
    for row in rows:
        p = row.get('payload', {})
        items = row.get('message', {}).get('content', [])
        if p.get('type') == 'function_call':
            try:
                pending[p['call_id']] = (p.get('name', '').split('.')[-1], json.loads(p['arguments']))
            except (ValueError, KeyError):
                pass
        elif p.get('type') == 'custom_tool_call':
            # Parse only a complete literal wrapper. Never infer permission from
            # the heuristic code-mode read candidates in usage statistics.
            raw = p.get('input', '')
            match = re.fullmatch(r'\s*text\(await tools\.exec_command\((\{.*\})\)\);?\s*', raw, re.S)
            if match:
                try:
                    args = json.loads(re.sub(r'(?<=[{,])\s*(cmd|workdir|max_output_tokens|yield_time_ms)\s*:', r'"\1":', match[1]))
                    if isinstance(args.get('cmd'), str):
                        pending[p['call_id']] = ('exec_command', args)
                except (ValueError, KeyError, TypeError):
                    pass
        elif p.get('type') in ('function_call_output', 'custom_tool_call_output') and p.get('call_id') in pending:
            name, value = pending.pop(p['call_id'])
            output = returned_text(p.get('output', ''), command_envelope=name == 'exec_command')
            if output is not None and not re.search(r'blocked by PreToolUse|permission.*denied', output, re.I):
                results.append((name, value, output))
        for item in items if isinstance(items, list) else []:
            if item.get('type') == 'tool_use':
                pending[item['id']] = (item.get('name', ''), item.get('input', {}))
            elif item.get('type') == 'tool_result' and item.get('tool_use_id') in pending:
                name, value = pending.pop(item['tool_use_id'])
                if not item.get('is_error'):
                    output = returned_text(item.get('content', ''))
                    if output is not None:
                        results.append((name, value, output))
    return results


def read_request(name, value, cwd, *, measure=True, expanded=False, followup=False):
    """Recognize exact file slices only; never run shell text to estimate it."""
    path, offset, count = None, 0, None
    if not isinstance(value, dict):
        return None
    cwd = value.get('workdir') or cwd
    if name == 'Read':
        path = value.get('file_path', value.get('path'))
        if type(value.get('offset', 1)) is not int:
            return None
        offset = max(0, value.get('offset', 1) - 1)
        count = value.get('limit')
    elif name in ('Bash', 'PowerShell', 'powershell', 'exec_command', 'shell_command'):
        command = value.get('command', value.get('cmd', '')).split('\n#', 1)[0].strip()
        # Exact single-read grammar; compounds/globs/variables stay unknown.
        match = re.fullmatch(r'(?:cat|Get-Content(?:\s+-LiteralPath)?)\s+["\']?([^"\'|;&<>$`*?]+?)["\']?(?:\s*\|\s*Select-Object\s+-Skip\s+(\d+)\s+-First\s+(\d+))?', command, re.I)
        if match:
            path, skip, first = match.groups()
            offset, count = int(skip or 0), int(first) if first else None
        else:
            match = re.fullmatch(r"sed\s+-n\s+['\"]?(\d+),(\d+)p['\"]?\s+['\"]?([^'\"|;&<>$`*?]+)['\"]?", command)
            if match:
                first, last, path = match.groups()
                offset, count = int(first) - 1, int(last) - int(first) + 1
        if followup and path is None:
            match = re.fullmatch(r'''tail\s+-n\s*\+(\d+)\s+["']?([^"'|;&<>$`*?]+?)["']?''', command)
            if match:
                first, path = match.groups()
                offset, count = max(0, int(first) - 1), None
        if expanded and path is None:
            match = re.fullmatch(r'''(head|tail)\s+(?:(?:-n\s*|-)?(\d+)\s+)?["']?([^"'|;&<>$`*?]+?)["']?''', command)
            if match:
                kind, amount, path = match.groups()
                count = int(amount or 10)
                offset = -count if kind == 'tail' else 0
    if not isinstance(path, str) or not path or count is not None and (type(count) is not int or count < 0):
        return None
    path = Path(path.strip())
    path = path if path.is_absolute() else Path(cwd) / path
    result = {'path': str(path.resolve()), 'offset': offset, 'count': count}
    if not measure:
        return result
    try:
        with path.open('rb') as stream:
            raw = stream.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            return None  # Large/unknown reads are observable gaps, never denials.
        lines = raw.decode('utf-8-sig').splitlines()
        selected = lines[offset:offset + count if count is not None and offset >= 0 else None] if count != 0 else []
        return {**result,
                'lines': len(selected), 'chars': sum(len(s) + 1 for s in selected), 'text': '\n'.join(selected)}
    except (OSError, UnicodeError):
        return None


def member_review_models(path):
    """Conservative exclusions from recorded implementation assignments only."""
    from ._tool_discipline import declared_activity
    _, rows, _ = transcript(path)
    models = set()
    for name, args, _ in calls(rows):
        if name in ('Agent', 'Task', 'spawn_agent') and isinstance(args.get('model'), str):
            text = '\n'.join(str(args.get(k, '')) for k in ('prompt', 'message', 'description'))
            activity = declared_activity(text)
            # A reviewer mentioning a fix or a prohibition on edits is not an
            # implementation assignment. Unknown free text needs positive evidence.
            positive = re.sub(r'\b(?:do not|don\x27t|never|without)\s+(?:edit|modify|fix|implement)\b[^.;\n]*|(?:不要|不得|禁止)(?:修改|编辑|修复|实现)[^。；\n]*', '', text, flags=re.I)
            if activity == 'execute' or (activity is None and re.search(
                    r'^\s*(?:please\s+)?(?:implement|fix|edit|modify)\b|^\s*(?:实现|修复|修改)', positive, re.I)):
                models.add(args['model'])
    return models


def reading_role(event, policy, header, rows):
    """Resolve member identity before slicing usage to the current turn.

    The bounded native transcript remains the authority: absent or ambiguous
    assignments stay unknown, rather than inheriting the parent's manager role.
    """
    child = bool(event.get('agent_id') or header.get('isSidechain') or
                 isinstance(header.get('payload', {}).get('source'), dict) and
                 bool(header['payload']['source'].get('subagent')))
    if not child:
        return policy.get('root_role')
    if policy.get('version', 1) < 6:
        return None
    if event.get('agent_type') in ('expert', 'reviewer'):
        return event['agent_type']
    from ._tool_discipline import ACTIVITY, declared_activity
    for row in reversed(rows):
        text = None
        if row.get('type') == 'user' and not row.get('isMeta'):
            text = returned_text(row.get('message', {}).get('content'))
        elif row.get('type') == 'response_item':
            payload = row.get('payload', {})
            if payload.get('type') == 'message' and payload.get('role') == 'user':
                text = returned_text(payload.get('content'))
        if text and ACTIVITY.search(text):
            return declared_activity(text)  # Latest assignment, including ambiguity.
    return None


def reading_check(event, policy):
    """Count completed reads; allow decisive, stale, missing and index evidence."""
    name, value = event.get('tool_name', ''), event.get('tool_input') or {}
    if name.endswith(('codegraph_explore', 'codegraph_node')):
        return None  # Index access is always available, even after its charge.
    reason = '\n'.join(str(value.get(k, '')) for k in ('command', 'cmd', 'description'))
    if re.search(r'#(?:decide|reread):\s*\S.+', reason, re.I):
        return None
    header, rows, complete = transcript(event.get('transcript_path'))
    role = reading_role(event, policy, header, rows)
    rows, boundary = current_turn(rows)
    cwd = event.get('cwd', '.')
    expanded = policy.get('version', 1) >= 4
    followup = policy.get('version', 1) >= 5
    requested = read_request(name, value, cwd, expanded=expanded, followup=followup)
    history = calls(rows) if boundary else []
    # Re-read proof uses the actual previously returned text, not a current
    # file hash masquerading as the historical hash. Truncated output is exempt.
    if requested and requested['text']:
        for tool, args, output in history:
            previous = read_request(tool, args, cwd, measure=False, expanded=expanded, followup=followup)
            if (previous and all(previous[k] == requested[k] for k in ('path', 'offset', 'count'))
                    and not re.search(r'truncat|omitted|not shown', output, re.I)):
                plain = '\n'.join(output.splitlines())
                if tool == 'Read':
                    numbered = [re.fullmatch(r'\s*(\d+)(?:→|\t)(.*)', line) for line in output.splitlines()]
                    if numbered and all(m and int(m[1]) == previous['offset'] + i + 1 for i, m in enumerate(numbered)):
                        plain = '\n'.join(m[2] for m in numbered)
                if plain == requested['text']:
                    return 'This unchanged range was already returned in this turn. Reuse it; for missing or changed evidence give #reread: concrete reason.'
    if role != 'manager' and not (role == 'expert' and policy.get('version', 1) >= 6):
        return None
    retrieval = ('Delegate broad retrieval to the configured investigator' if role == 'manager' else
                 'Ask the manager for an investigator evidence packet')
    budget = policy.get('reading_budget', {})
    unit = budget.get('unit', 'lines')
    spent = 0
    oversized = False
    for tool, args, output in history:
        if tool.endswith(('codegraph_explore', 'codegraph_node')):
            spent += budget.get('index_charge', 0)
        elif (observed_read(tool, args, followup=followup) if expanded else
              tool == 'Read' or read_request(tool, args, cwd, measure=False) or search_request(tool, args)):
            amount = len(output.splitlines()) if unit == 'lines' else len(output)
            spent += amount
            oversized |= amount > budget.get('single', float('inf'))
    if expanded and observed_read(name, value, followup=followup) and (oversized or spent >= budget.get('turn', float('inf'))):
        return (role.capitalize() + ' reading budget reached by actual prior output. ' + retrieval + ', '
                'use the index, or #decide: specific reason; no provider request needs blind replay. '
                f'Observed {spent} {unit}; history ' + ('complete.' if complete and boundary else 'partial lower bound.'))
    if requested and (requested[unit] > budget.get('single', float('inf')) or
                      spent + requested[unit] > budget.get('turn', float('inf'))):
        return (role.capitalize() + ' reading budget reached. ' + retrieval + ', '
                'query the index, or add #decide: specific decision requiring direct evidence. '
                f'Observed {spent} {unit}; requested {requested[unit]}; history '
                + ('complete.' if complete and boundary else 'partial (observed lower bound).'))
    # Search result sizes are only known after execution. Once spent is over
    # budget, further direct searches require a reason; index stays available.
    search = search_request(name, value)
    if search and spent >= budget.get('turn', float('inf')):
        return role.capitalize() + ' reading budget reached. ' + retrieval + ', use the index or #decide: specific reason.'
    return None
