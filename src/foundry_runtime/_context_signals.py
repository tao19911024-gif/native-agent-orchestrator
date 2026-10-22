"""Observed per-response context signals, never billing totals or stop conditions."""
from datetime import datetime
from bisect import bisect_left, bisect_right
import hashlib
import json
from pathlib import Path
import re


CONTEXT_RENEWAL_GUIDANCE = (
    'Act only on the measured_thread_id and thread_role of the context event. '
    'A root residual is root context pressure, not evidence that any member needs replacement. '
    'For the root, reduce repeated standing context and reuse concise source/evidence references; '
    'if it remains high, report that fact and arrange a root handoff only at the existing continuation boundary. '
    'Do not restart a running root or renew an idle/new member because another thread is full. '
    'Only member_renewal_recommended with a matching renewal_target recommends a written handoff '
    'and fresh-member takeover of that measured member remaining scope through existing team controls. '
    'Unknown or historical events without role identity require identifying the measured thread before choosing a remedy. '
    'Do not inherit the old transcript (fork_turns=none where supported); preserve uncommitted work, '
    'evidence and ownership, and do not replay completed work.'
)


def test_failures(text):
    """Named test assertions only; quota, timeout, collection ERROR are not tests."""
    return sorted(set(re.findall(r'^FAILED\s+(\S+::\S+)\s+-\s+(?:AssertionError|assert\b)', text, re.M)))


class CompactionResidual:
    """Correlate completed compaction with the next measured native request."""
    def __init__(self, threshold=None, share=.8, rearm_share=.6):
        self.threshold, self.share = threshold, share
        self.rearm_share = rearm_share
        self.members, self.seen = {}, set()

    def observe(self, event):
        params = event.get('params') or {}
        thread = params.get('threadId')
        if not isinstance(thread, str):
            return None
        row = self.members.setdefault(thread, {'last_input_tokens': None, 'residuals': []})
        item = params.get('item') or {}
        if event.get('method') == 'item/completed' and item.get('type') == 'contextCompaction':
            key = (thread, item.get('id'))
            if key in self.seen:
                return None
            self.seen.add(key)
            row['pending'] = {'compaction_id': item.get('id'), 'before_input_tokens': row['last_input_tokens'],
                              'timestamp_ms': event.get('emittedAtMs')}
        if event.get('method') != 'thread/tokenUsage/updated':
            return None
        usage = params.get('tokenUsage') or {}
        value = (usage.get('last') or {}).get('inputTokens')
        if type(value) is not int or value <= 0:
            return None
        row['last_input_tokens'] = value
        row['model_context_window'] = usage.get('modelContextWindow')
        pending = row.pop('pending', None)
        if pending is None:
            return None
        ratio = value / self.threshold if self.threshold and self.threshold > 0 else None
        sample = {**pending, 'after_input_tokens': value, 'residual_share': ratio,
                  'headroom_tokens': self.threshold - value if self.threshold else None,
                  'renewal_recommended': ratio is not None and ratio >= self.share}
        baseline = row.get('renewal_baseline')
        sample['renewal_signal'] = sample['renewal_recommended'] and baseline is None
        if baseline is not None:
            sample['renewal_followup'] = {
                'recommended_at_compaction': baseline['compaction_id'],
                'baseline_input_tokens': baseline['after_input_tokens'],
                'residual_fell': value < baseline['after_input_tokens'],
                'rearmed': ratio is not None and ratio <= self.rearm_share,
                'handoff_execution': 'not_verified_by_token_sample'}
            if sample['renewal_followup']['rearmed']:
                row.pop('renewal_baseline', None)
        elif sample['renewal_signal']:
            row['renewal_baseline'] = {'compaction_id': sample['compaction_id'], 'after_input_tokens': value}
        row['residuals'].append(sample)
        return {'member_id': thread, **sample}


def compaction_fill(events, *, threshold=None):
    """Visible completed-item text by member/window; not tokenizer or billing data.

    Completed items avoid counting streamed deltas twice. Hidden reasoning,
    images, pre-existing history and system prompts cannot be inferred here.
    """
    members, seen = {}, set()
    residual = CompactionResidual(threshold)
    fields = {'commandExecution': ('tool_output', 'aggregatedOutput'),
              'mcpToolCall': ('tool_output', 'result'),
              'dynamicToolCall': ('tool_output', 'contentItems'),
              'agentMessage': ('messages', 'text'),
              'userMessage': ('messages', 'content'),
              'reasoning': ('visible_reasoning', 'content'),
              'collabAgentToolCall': ('collaboration', 'agentsStates')}
    def characters(value):
        if isinstance(value, str):
            return len(value)
        if isinstance(value, list):
            return sum(characters(v) for v in value)
        if isinstance(value, dict):
            return sum(characters(v) for v in value.values())
        return 0
    for event in events:
        residual.observe(event)
        if event.get('method') != 'item/completed':
            continue
        params = event.get('params') or {}
        item = params.get('item') or {}
        thread, item_id = params.get('threadId'), item.get('id')
        if not thread or not item_id or (thread, item_id) in seen:
            continue
        seen.add((thread, item_id))
        row = members.setdefault(thread, {'first_ms': event.get('emittedAtMs'),
            'last_ms': event.get('emittedAtMs'), 'windows': [], 'current': {}, 'unclassified_items': {}})
        row['last_ms'] = event.get('emittedAtMs')
        kind = item.get('type')
        if kind == 'contextCompaction':
            row['windows'].append(row['current'])
            row['current'] = {}
            continue
        if kind in fields:
            category, field = fields[kind]
            size = characters(item.get(field))
            if kind == 'reasoning' and not size:
                size = characters(item.get('summary'))
            row['current'][category] = row['current'].get(category, 0) + size
        else:
            unknown = row['unclassified_items']
            unknown[kind] = unknown.get(kind, 0) + 1
    for row in members.values():
        row['compactions'] = len(row['windows'])
        row['trailing_partial_window'] = row.pop('current')
        row['visible_chars'] = {}
        for window in [*row['windows'], row['trailing_partial_window']]:
            for category, size in window.items():
                row['visible_chars'][category] = row['visible_chars'].get(category, 0) + size
    return {'members': members, 'residuals': residual.members, 'threshold_tokens': threshold,
            'renewal_share': residual.share, 'rearm_share': residual.rearm_share,
            'unit': 'visible characters, not tokens',
            'coverage': 'completed native items only; first and trailing windows may be partial; '
                        'hidden reasoning, system prompts, images and inherited history unavailable'}


def _output_text(value, depth=0):
    """Unwrap observed JSON tool envelopes; stop at source text or depth bound."""
    if depth >= 8:
        return str(value)
    if isinstance(value, list):
        return '\n'.join(_output_text(v, depth + 1) for v in value)
    if isinstance(value, dict):
        for key in ('output', 'content', 'text', 'result', 'value'):
            if key in value:
                return _output_text(value[key], depth + 1)
        return json.dumps(value, ensure_ascii=False)
    if not isinstance(value, str):
        return str(value)
    parts = []
    for line in value.splitlines():
        try:
            decoded = json.loads(line)
        except ValueError:
            parts.append(line)
        else:
            parts.append(_output_text(decoded, depth + 1) if isinstance(decoded, (dict, list, str)) else line)
    return '\n'.join(parts)


def tool_record(payload):
    """Compact native observations; never execute code-mode text or retain source.

    Shell/code-mode classification is deliberately a candidate signal. Mixed
    commands and unrecognised JavaScript remain visible in the coverage counters.
    It must not be reused as a read-permission predicate.
    """
    kind = payload.get('type')
    identity = payload.get('call_id')
    if not identity:
        return None
    if kind in ('function_call', 'custom_tool_call'):
        name = payload.get('name', '').split('.')[-1]
        raw = payload.get('arguments', payload.get('input', ''))
        if not isinstance(raw, str):
            return None
        commands, args = [], {}
        try:
            args = json.loads(raw)
            if isinstance(args, dict):
                command = args.get('cmd', args.get('command'))
                if isinstance(command, str):
                    commands.append(command)
        except ValueError:
            # Literal cmd fields only. Variables, templates and JS expressions
            # are unclassified; this is not a JavaScript parser.
            for match in re.finditer(r'\bcmd\s*:\s*("(?:\\.|[^"\\])*")', raw):
                try:
                    commands.append(json.loads(match[1]))
                except ValueError:
                    pass
        pattern = r'\b(?:rg|cat|head|tail|sed|awk|grep|Get-Content|Select-String)\b|\bgit\s+(?:diff|show|grep)\b'
        candidate = name in ('Read', 'Grep', 'Search') or any(re.search(pattern, c, re.I) for c in commands)
        index = name.endswith(('codegraph_explore', 'codegraph_node')) or bool(re.search(
            r'\btools\.\w*codegraph_(?:explore|node)\s*\(', raw))
        return {'kind': 'call', 'call_id': identity, 'tool': name,
                **wait_parameters(name, args),
                # Reviewer exclusion needs an actual tool invocation, not a
                # write keyword inside a search or quoted example. Mixed JS and
                # shell edits remain unknown until their native evidence exists.
                'implementation': name in ('apply_patch', 'Write', 'Edit') or bool(re.fullmatch(
                    r'\s*text\(await tools\.apply_patch\("(?:\\.|[^"\\])*"\)\);?\s*', raw, re.S)),
                'signature': hashlib.sha256((name + '\n' + raw).encode()).hexdigest(),
                'read_candidate': bool(candidate), 'index_candidate': index,
                'code_mode': kind == 'custom_tool_call', 'literal_commands': len(commands)}
    if kind in ('function_call_output', 'custom_tool_call_output'):
        output = payload.get('output', '')
        raw = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        text = _output_text(output)
        wait = wait_result(output)
        return {'kind': 'output', 'call_id': identity, 'chars': len(text), 'lines': len(text.splitlines()),
                **({'wait_result': wait} if wait else {}),
                'test_failures': test_failures(text),
                'output_sha256': hashlib.sha256(text.encode()).hexdigest(),
                'blocked': bool(re.search(r'blocked by PreToolUse|permission.*denied', raw, re.I)),
                'truncated': bool(re.search(r'output truncated|tokens truncated|original_token_count.*output.*truncated|omitted \d+', raw, re.I))}
    return None


def reading_signals(rows, turn):
    """Join compact call/output evidence within one attributed native turn."""
    records = [r['payload'] for r in rows if r.get('type') == 'tool_observation'
               and r['payload'].get('turn_id') == turn]
    calls = {r['call_id']: r for r in records if r['kind'] == 'call'}
    outputs = {r['call_id']: r for r in records if r['kind'] == 'output'}
    reads, seen, repeats = [], set(), 0
    for identity, call in calls.items():
        out = outputs.get(identity)
        if not call['read_candidate'] or not out or out['blocked']:
            continue
        reads.append(out)
        signature = (call['signature'], out['output_sha256'])
        if signature in seen and not out['truncated']:
            repeats += 1
        seen.add(signature)
    lines = sorted(r['lines'] for r in reads)
    return {'tool_calls': len(calls), 'code_mode_calls': sum(c['code_mode'] for c in calls.values()),
            'implementation_calls': sum(c.get('implementation', False) for c in calls.values()),
            'test_failures': [{'call_id': identity, 'test_id': test} for identity, o in outputs.items()
                              if identity in calls and not o['blocked'] for test in o.get('test_failures', [])],
            'unclassified_code_mode_calls': sum(c['code_mode'] and not c['literal_commands'] and not c['index_candidate'] for c in calls.values()),
            'read_candidate_calls': sum(c['read_candidate'] for c in calls.values()),
            'index_candidate_calls': sum(c['index_candidate'] for c in calls.values()),
            'blocked_outputs': sum(o['blocked'] for o in outputs.values()),
            'missing_outputs': len(calls.keys() - outputs.keys()),
            'observed_read_outputs': len(reads), 'read_output_chars': sum(r['chars'] for r in reads),
            'read_output_lines': sum(lines), 'read_outputs_over_150_lines': sum(n > 150 for n in lines),
            'read_output_lines_p50': lines[len(lines) // 2] if lines else None,
            'read_output_lines_p90': lines[min(len(lines) - 1, int(len(lines) * .9))] if lines else None,
            'truncated_read_outputs': sum(r['truncated'] for r in reads),
            'identical_call_output_repeats': repeats,
            'scope': 'Heuristic read candidates, including mixed shell/code-mode calls; output wrappers/errors may be included. '
                     'Index calls are counted separately. Lines/chars are not tokens, repeated hashes do not prove unchanged files, '
                     'missing/unclassified calls remain unknown. Observational only; not read admission or a savings estimate.'}


def claude_work(path):
    """Model-bearing editing calls and test outcomes from the retained CLI stream."""
    models, failures, seen, owners = set(), [], set(), {}
    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            message = row.get('message') or {}
            content = message.get('content', [])
            for item in content if isinstance(content, list) else []:
                identity = item.get('id', item.get('tool_use_id'))
                if item.get('type') == 'tool_use':
                    owners[identity] = message.get('model')
                    obs = tool_record({'type': 'function_call', 'call_id': identity,
                        'name': item.get('name', ''), 'arguments': json.dumps(item.get('input', {}))})
                    if obs and obs['implementation'] and message.get('model'):
                        models.add(message['model'])
                elif item.get('type') == 'tool_result' and identity not in seen:
                    seen.add(identity)
                    failures += [{'call_id': identity, 'test_id': t, 'model': owners.get(identity)}
                                 for t in test_failures(_output_text(item.get('content', '')))]
    return {'models': sorted(models), 'test_failures': failures,
            'basis': 'model-bearing editing requests and named assertion outputs; not proof of every change author'}


def observe_work(path):
    try:
        return claude_work(path)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return {'models': [], 'test_failures': [], 'coverage': 'uncovered', 'gap': str(exc)}


def summarize(events, *, bound=None, previous=None):
    members, seen, gaps = {}, set(), []
    for e in sorted(events, key=lambda e: e.get("timestamp") or ""):
        identity = (e["member"], e["id"])
        if identity in seen:
            continue
        seen.add(identity)
        row = members.setdefault(e["member"], {"responses": 0, "max_context_tokens": None,
                    "context_over_bound": [], "large_cache_write_after_idle": []})
        row["responses"] += 1
        try:
            stamp = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
        except (ValueError, TypeError, KeyError, AttributeError):
            stamp = None
            gaps.append("missing timestamp: " + e["id"])
        context = e.get("context")
        if context is not None:
            row["max_context_tokens"] = max(row["max_context_tokens"] or 0, context)
            if bound is not None and context > bound:
                row["context_over_bound"].append({"response_id": e["id"], "tokens": context})
        last = row.get("_last")
        if last is None and previous and e["member"] in previous:
            last = datetime.fromisoformat(previous[e["member"]].replace("Z", "+00:00"))
        if stamp is not None and last is not None:
            seconds = (stamp - last).total_seconds()
            if seconds >= 300 and (e.get("cache_write") or 0) >= 100000:
                row["large_cache_write_after_idle"].append({"response_id": e["id"],
                    "idle_seconds": seconds, "cache_write_tokens": e["cache_write"],
                    "gap_bucket": "5m_to_1h" if seconds <= 3600 else "over_1h"})
        if stamp is not None:
            row["_last"] = stamp
        if e.get("cache_write") is None:
            gaps.append("cache write unavailable: " + e["id"])
    for row in members.values():
        row.pop("_last", None)
    return {"context_bound": bound, "members": members, "gaps": gaps,
            "scope": "observed request input, not exact resident memory; idle means gap between observed requests; first request has no proven idle gap"}


def merge_response(previous, current):
    """Merge snapshots of one response, never sum streamed content blocks.

    Input counters describe the same request; output grows during streaming.
    Conflicting request metadata stays visibly unavailable instead of guessing.
    """
    if previous is None:
        return {**current, 'usage': dict(current['usage'])}
    result = {**previous, 'usage': dict(previous['usage'])}
    conflicts = set(previous.get('conflicting_fields', [])) | set(current.get('conflicting_fields', []))
    for field in ('model', 'initial_assignment_activity'):
        old, new = previous.get(field), current.get(field)
        if old is not None and new is not None and old != new:
            conflicts.add(field)
        elif old is None:
            result[field] = new
    for field, new in current['usage'].items():
        old = previous['usage'].get(field)
        if old is None:
            result['usage'][field] = new
        elif new is not None:
            if field == 'output_tokens':
                result['usage'][field] = max(old, new)
            elif old != new:
                conflicts.add(field)
    for field in conflicts:
        if field in result['usage']:
            result['usage'][field] = None
        else:
            result[field] = None
    if conflicts:
        result['conflicting_fields'] = sorted(conflicts)
    return result


def wait_result(value):
    """Native structured wait evidence only; unknown results remain actionable."""
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict) and value[0].get('type') == 'text':
        value = value[0].get('text')
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, dict):
        return None
    # Errors, new output and decisions must never be suppressed by a timeout flag.
    # Native V2 includes a fixed timeout label (and optional clamp explanation).
    # Only that empty-result shape is a timeout; arbitrary messages remain evidence.
    timeout_label = value.get('timed_out') is True and isinstance(value.get('message'), str) and bool(
        re.fullmatch(r'Wait timed out\.(?:\n\nRequested timeout of \d+ms was clamped to the minimum of \d+ms\.)?', value['message']))
    if (any(value.get(key) for key in ('is_error', 'error', 'errors', 'output', 'content',
                                     'result', 'task', 'contentItems'))
            or (value.get('message') and not timeout_label)
            or value.get('status') in ('failed', 'errored', 'interrupted', 'cancelled')):
        return None
    states = value.get('agentsStates', value.get('status'))
    if isinstance(states, dict) and states:
        return {'state': 'observed', 'signature': hashlib.sha256(
            json.dumps(states, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
    if value.get('timed_out') is True or value.get('retrieval_status') == 'timeout':
        return {'state': 'timeout'}
    if value.get('agentsStates') == {}:
        return {'state': 'no_member_change'}
    return None


def wait_parameters(name, args):
    """Retain only wait control fields, never commands, stdin or message text."""
    if not isinstance(args, dict):
        return {}
    names = {'wait_agent': 'timeout_ms', 'wait_threads': 'timeoutMs',
             'TaskOutput': 'timeout', 'write_stdin': 'yield_time_ms', 'wait': 'yield_time_ms'}
    if name not in names:
        return {}
    result = {}
    requested = args.get(names[name])
    if type(requested) is int and requested >= 0:
        result['requested_wait_ms'] = requested
    if name == 'write_stdin' and args.get('chars', ''):
        result['wait_kind'] = 'interactive_write'
    elif name == 'TaskOutput' and args.get('block') is False:
        result['wait_kind'] = 'nonblocking_probe'
    elif name == 'wait' and args.get('terminate') is True:
        result['wait_kind'] = 'termination_request'
    else:
        result['wait_kind'] = 'blocking_wait'
    return result


def waiting_signals(rows, turn=None, *, response_events=None):
    """Tool identities establish wait purpose, not elapsed time or token cost."""
    calls, outputs, started, ended = {}, set(), {}, {}
    if response_events is None:
        response_events = [{'timestamp': r.get('timestamp'), 'id': r['payload'].get('response_id'),
                            'member': r['payload'].get('thread_id')}
                           for r in rows if r.get('type') == 'token_usage_record'
                           and (turn is None or r['payload'].get('turn_id') == turn)]
    # Parse/deduplicate once, not once per wait in a long-running transcript.
    response_times, missing_times = {}, set()
    for response in response_events:
        member, identity = response.get('member'), response.get('id')
        groups = (None,) if member is None else (None, member)
        try:
            if not identity:
                raise ValueError('missing response identity')
            observed = datetime.fromisoformat(response['timestamp'].replace('Z', '+00:00')).timestamp()
        except (AttributeError, TypeError, ValueError, KeyError):
            missing_times.update(groups)
            continue
        for group in groups:
            times = response_times.setdefault(group, {})
            key = (member, identity)
            times[key] = min(times.get(key, observed), observed)
    response_times = {k:sorted(v.values()) for k,v in response_times.items()}
    reasons = {'wait_agent': 'member_result', 'wait_threads': 'member_result',
               'TaskOutput': 'background_tool_result', 'write_stdin': 'process_io',
               'wait': 'operation_result', 'sleep': 'explicit_delay',
               'request_user_input': 'operator_input', 'AskUserQuestion': 'operator_input'}
    for row in rows:
        p = row.get('payload', {})
        if turn is not None and p.get('turn_id') != turn:
            continue
        identity = p.get('call_id')
        if not identity:
            continue
        if p.get('kind') == 'output':
            outputs.add(identity)
            ended.setdefault(identity, (row.get('timestamp'), p.get('wait_result')))
        elif p.get('kind') == 'call' and p.get('tool') in reasons:
            calls[identity] = {'call_id': identity, 'tool': p['tool'],
                               'reason': reasons[p['tool']],
                               **{k:p[k] for k in ('requested_wait_ms', 'wait_kind', 'member', 'thread_id') if k in p}}
            started.setdefault(identity, row.get('timestamp'))
    intervals = []
    for identity, call in calls.items():
        stamp, result = ended.get(identity, (None, None))
        if result:
            call['result'] = result['state']
        try:
            duration = (datetime.fromisoformat(stamp.replace('Z', '+00:00')) -
                        datetime.fromisoformat(started[identity].replace('Z', '+00:00'))).total_seconds()
            if duration >= 0:
                call['observed_seconds'] = duration
                if call.get('wait_kind') == 'blocking_wait':
                    first = datetime.fromisoformat(started[identity].replace('Z', '+00:00'))
                    last = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                    member = call.get('member', call.get('thread_id'))
                    times = response_times.get(member)
                    covered = times is not None and member not in missing_times
                    intervals.append({'call_id': identity, 'start': started[identity], 'end': stamp,
                        'observed_seconds': duration, 'tool': call['tool'],
                        'response_records_in_call_window': (bisect_left(times, last.timestamp()) -
                            bisect_right(times, first.timestamp())) if covered and duration > 0 else (0 if covered else None),
                        'additional_model_calls_caused_by_wait': None,
                        'response_scope': 'Record timestamps may include the response issuing this wait; not model start times or added wait cost.',
                        'requested_wait_ms': call.get('requested_wait_ms'),
                        'response_coverage': 'observed_native_records' if covered else 'unavailable'})
        except (AttributeError, TypeError, ValueError):
            pass
    return {'coverage': 'observed_tools', 'calls': [dict(call, returned=key in outputs)
            for key, call in calls.items()], 'intervals': intervals, 'elapsed_seconds': None,
            'scope': 'Observed tool purpose only; process_io may write input, not wait. '
                     'Nested code-mode waits and provider/user idle time may be unobserved. '
                     'A returned wait does not establish task completion; no wait-token attribution.'}


def claude_stream(path, bound=None):
    from ._tool_discipline import declared_activity
    events, responses, assignments, tools = [], {}, {}, []
    stamps, active, aliases = {}, {}, {}
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            r = json.loads(line)
            if r.get('agent_id') and r.get('parent_tool_use_id'):
                aliases.setdefault(r['parent_tool_use_id'], set()).add(r['agent_id'])
            member = r.get('agent_id') or r.get('parent_tool_use_id') or 'leader'
            if r.get('type') == 'stream_event':
                event = r.get('event') or {}
                if event.get('type') == 'message_start':
                    active[member] = event.get('message') or {}
                    r = {**r, 'type': 'assistant', 'message': active[member]}
                elif event.get('type') == 'message_delta' and member in active:
                    r = {**r, 'type': 'assistant', 'message': {
                        **active[member], 'content': [], 'usage': event.get('usage') or {}}}
                elif event.get('type') == 'message_stop':
                    active.pop(member, None)
            if r.get("type") not in ('assistant', 'user') or not isinstance(r.get("message"), dict):
                continue
            m = r["message"]
            for item in m.get('content', []) if isinstance(m.get('content'), list) else []:
                member = r.get('agent_id') or r.get('parent_tool_use_id') or 'leader'
                if item.get('type') in ('tool_use', 'tool_result'):
                    identity = item.get('id') if item['type'] == 'tool_use' else item.get('tool_use_id')
                    if identity:
                        wait = wait_result(item.get('content'))
                        tools.append({'timestamp': r.get('timestamp'), 'payload': {
                            'kind': 'call' if item['type'] == 'tool_use' else 'output',
                            **({'wait_result': wait} if wait else {}),
                            **(wait_parameters(item.get('name'), item.get('input')) if item['type'] == 'tool_use' else {}),
                            'member': member, 'call_id': identity, 'tool': item.get('name')}})
                if item.get('type') == 'tool_use' and item.get('name') in ('Agent', 'Task'):
                    value = item.get('input') or {}
                    assignments[item.get('id')] = declared_activity(str(value.get('prompt', '')))
            u = m.get("usage", {})
            if r.get('type') != 'assistant' or not m.get("id") or not isinstance(u, dict) or not u:
                continue
            member = r.get('agent_id') or r.get('parent_tool_use_id') or 'leader'
            response = {'response_id': m['id'], 'member_id': member, 'model': m.get('model'),
                'initial_assignment_activity': assignments.get(r.get('parent_tool_use_id')),
                'usage': {k: v if type(v) is int and v >= 0 else None for k, v in
                    ((k, u.get(k)) for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens'))}}
            key = (member, m['id'])
            responses[key] = merge_response(responses.get(key), response)
            if not stamps.get(key):
                stamps[key] = r.get('timestamp')
    canonical = {}
    for key, response in responses.items():
        matches = aliases.get(key[0], set())
        member = next(iter(matches)) if len(matches) == 1 else key[0]
        identity = (member, key[1])
        canonical[identity] = merge_response(canonical.get(identity), {**response, 'member_id': member})
        if not stamps.get(identity):
            stamps[identity] = stamps.get(key)
    responses = canonical
    for key, response in responses.items():
        u = response['usage']
        values = [u.get(k) for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')]
        events.append({"member": key[0], "id": key[1], "timestamp": stamps[key],
            "context": sum(values) if all(type(v) is int and v >= 0 for v in values) else None,
            "cache_write": u.get("cache_creation_input_tokens")})
    result = summarize(events, bound=bound)
    result["member_identity"] = "native agent_id when present, else parent tool invocation; not proof of one persistent member across invocations"
    result['response_usage'] = list(responses.values())
    for tool in tools:
        p = tool['payload']
        matches = aliases.get(p['member'], set())
        member = next(iter(matches)) if len(matches) == 1 else p['member']
        p['member'] = member
        p['call_id'] = str(member) + '/' + str(p['call_id'])
    result['waiting_signals'] = waiting_signals(tools, response_events=events)
    result['usage_scope'] = 'Observed response snapshots, not session billing; unfinished streams may be partial.'
    return result


def codex_rows(rows, turn, previous=None, bound=None):
    return summarize(({"member": r["payload"].get("thread_id", "unknown"),
        "id": r["payload"]["response_id"], "timestamp": r.get("timestamp"),
        "context": r["payload"]["usage"].get("input_tokens"),
        "cache_write": r["payload"]["usage"].get("cache_write_input_tokens")}
        for r in rows if r.get("type") == "token_usage_record" and r["payload"].get("turn_id") == turn), previous=previous, bound=bound)


def observe_claude(path, bound=None):
    try:
        return claude_stream(path, bound=bound)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"coverage": "uncovered", "members": {}, "gaps": [str(exc)]}


def recorded_bound(receipt, provider="codex"):
    """Use attempt-bound package observation; old runs get a labelled fixed reference."""
    try:
        log = next(r for r in receipt["evidence_refs"] if r["kind"] == "log")
        path = Path(log["path"]).with_suffix(".package.json")
        record = json.loads(path.read_bytes())
        policy = (record.get("launch_context") or {}).get("settings")
        if policy is None:
            policy = record["runtime_provenance"]["launch_context"]
        value = policy["context_tokens"]
        if record["attempt_id"] != receipt["attempt_id"] or type(value) is not int or value <= 0:
            raise ValueError("package observation does not bind this attempt")
        target = policy.get('compact_tokens')
        if provider == 'codex':
            return {'tokens': target if type(target) is int and target > 0 else None,
                    'source': str(path) if type(target) is int and target > 0 else 'model default; numeric compaction threshold unavailable'}
        percent = policy.get("compact_percent")
        if type(percent) is not int:
            raise ValueError("recorded compaction percent unavailable")
        bound = value * percent // 100
        if type(target) is int and target > 0:
            bound = min(bound, target)
        return {"tokens": bound, "source": str(path)}
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        return {"tokens": None, "source": "actual launch compaction threshold unavailable"}


def previous_for(rows, turn, inherited):
    previous = dict(inherited)
    selected = [r for r in rows if r.get("type") == "token_usage_record"]
    first = min((r["timestamp"] for r in selected if r["payload"].get("turn_id") == turn), default="")
    for r in sorted(selected, key=lambda r: r["timestamp"]):
        if r["timestamp"] < first:
            previous[r["payload"]["thread_id"]] = r["timestamp"]
    return previous


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Read-only bounded native context/compaction report')
    parser.add_argument('--log', type=Path, required=True)
    parser.add_argument('--threshold', type=int, required=True, help='Recorded launch threshold, not model capacity')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    limit = args.log.stat().st_size
    digest = hashlib.sha256()
    gaps = []
    def events():
        with args.log.open('rb') as stream:
            remaining = limit
            while remaining:
                raw = stream.readline(remaining)
                if not raw:
                    break
                remaining -= len(raw); digest.update(raw)
                text = raw.decode('utf-8', errors='replace')
                if not text.startswith('stdout: {'):
                    continue
                try:
                    yield json.loads(text[len('stdout: '):])
                except ValueError:
                    gaps.append({'byte_end': limit - remaining, 'reason': 'incomplete native event'})
    report = compaction_fill(events(), threshold=args.threshold)
    report.update(source=str(args.log.resolve()), snapshot_bytes=limit,
                  prefix_sha256=digest.hexdigest(), gaps=gaps)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
