"""Read provider capacity events from one bound attempt; never control execution."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def execution_activity(log, launcher, *, session=None, limit=1024 * 1024):
    """Bounded, read-only app-server evidence; absence is not a liveness verdict."""
    result = {'status': 'unobserved', 'scope': 'bounded log tail; not a liveness or request-health verdict',
              'last_output': None, 'last_tool_completion': None, 'compaction': None,
              'last_usage': None, 'communication_error': None, 'last_native_event': None}
    if not log or launcher != 'app-server':
        return result
    path = Path(log)
    try:
        with path.open('rb') as stream:
            size = stream.seek(0, 2)
            start = max(0, size - limit)
            stream.seek(start)
            raw = stream.read(size - start)
        if start:
            raw = raw.partition(b'\n')[2]
        result.update(status='observed', source=str(path), observed_bytes=size,
                      tail_truncated=bool(start), read_limit_bytes=limit,
                      observed_utc=datetime.now(timezone.utc).isoformat())
        compactions = {}
        for line in raw.splitlines(keepends=True):
            if not line.endswith(b'\n'):
                continue
            if line.startswith(b'stderr: ') and b'failed to refresh available models' in line:
                # Do not copy arbitrary stderr (which may contain credentials).
                match = re.search(rb'\d{4}-\d{2}-\d{2}T[\d:.]+Z', line)
                timestamp = match[0].decode('ascii') if match else None
                result['communication_error'] = {'kind': 'model_catalog_refresh_failed',
                    'timestamp': timestamp, 'request_timeout': b'request timed out' in line,
                    'scope': 'background model catalogue; generation request cause unknown'}
                continue
            if not line.startswith(b'stdout: '):
                continue
            try:
                event = json.loads(line[8:])
                params = event.get('params') or {}
                if not isinstance(params, dict) or (session and params.get('threadId') != session):
                    continue
                method = event.get('method')
                item = params.get('item') or {}
                if not isinstance(item, dict):
                    continue
                at = params.get('completedAtMs') or params.get('startedAtMs') or event.get('emittedAtMs')
                if type(at) not in (int, float):
                    continue
                stamp = datetime.fromtimestamp(at / 1000, timezone.utc).isoformat()
                result['last_native_event'] = {'timestamp': stamp, 'method': method,
                                               'item_type': item.get('type')}
                if method == 'thread/tokenUsage/updated':
                    usage = params.get('tokenUsage') or {}
                    if isinstance(usage, dict):
                        result['last_usage'] = {'timestamp': stamp, 'usage': usage.get('last')}
                elif item.get('type') == 'contextCompaction' and method in ('item/started', 'item/completed'):
                    key = (params.get('threadId'), item.get('id'))
                    row = compactions.setdefault(key, {'thread_id': key[0], 'item_id': key[1]})
                    row['started_at_ms' if method == 'item/started' else 'completed_at_ms'] = at
                    row['status'] = 'started' if method == 'item/started' else 'completed'
                    row['duration_seconds'] = ((at - row['started_at_ms']) / 1000
                        if method == 'item/completed' and 'started_at_ms' in row else None)
                    result['compaction'] = dict(row)
                elif method == 'item/completed':
                    row = {'timestamp': stamp, 'type': item.get('type'), 'item_id': item.get('id'),
                           'thread_id': params.get('threadId')}
                    if item.get('type') in ('agentMessage', 'fileChange'):
                        result['last_output'] = {**row, 'status': item.get('status')}
                    elif item.get('type') == 'commandExecution':
                        result['last_tool_completion'] = {**row, 'exit_code': item.get('exitCode'),
                                                         'duration_ms': item.get('durationMs')}
            except (ValueError, TypeError, AttributeError, OverflowError, OSError):
                continue
        return result
    except OSError as exc:
        return {**result, 'status': 'unobserved', 'error': str(exc), 'source': str(path)}


def observe(log, launcher):
    unknown = {'status': 'unobserved'}
    if not log or launcher != 'claude':
        return unknown
    path = Path(log).with_suffix('.claude.jsonl')
    try:
        sessions = {}
        with path.open('rb') as stream:
            size = path.stat().st_size
            while stream.tell() < size:
                offset = stream.tell()
                line = stream.readline(size - offset)
                if not line.endswith(b'\n'):
                    break  # The writer may still be publishing this event.
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(event, dict) or event.get('type') != 'rate_limit_event':
                    continue
                info = event.get('rate_limit_info') or {}
                if not isinstance(info, dict):
                    continue
                state = info.get('status')
                if state not in ('allowed', 'allowed_warning', 'rejected'):
                    continue
                session = event.get('session_id')
                if session is not None and not isinstance(session, str):
                    continue
                sessions[session] = dict(session_id=session, status=state,
                                         resets_at=info.get('resetsAt'),
                                         windows=info.get('unifiedWindows'),
                                         limit_type=info.get('rateLimitType'), byte_offset=offset)
        blocked = [s for s in sessions.values() if s['status'] == 'rejected']
        return dict(status='quota_blocked' if blocked else 'allowed' if sessions else 'unobserved',
                    blocked_sessions=blocked, latest_events=list(sessions.values()),
                    source=str(path), observed_bytes=size,
                    observed_utc=datetime.now(timezone.utc).isoformat(),
                    scope='provider capacity events only; heartbeat or reset time is not resumed model progress')
    except OSError as exc:
        return {**unknown, 'source': str(path), 'error': str(exc)}


def short_window_reset(observation, session):
    """Only a bound, rejected short window can schedule recovery, never prose."""
    import math
    rows = observation.get('latest_events', [])
    row = next((r for r in rows if r.get('session_id') == session), None)
    if not row or row.get('status') != 'rejected' or row.get('limit_type') != 'five_hour':
        return None
    windows = row.get('windows') or {}
    if not isinstance(windows, dict):
        return None
    for name, value in windows.items():
        if name.startswith('seven_day') and isinstance(value, dict):
            used = value.get('utilization')
            if type(used) in (int, float) and used >= 1:
                return None
    short = windows.get('five_hour') or {}
    reset = row.get('resets_at') or (short.get('resetsAt') if isinstance(short, dict) else None)
    return reset if type(reset) in (int, float) and math.isfinite(reset) else None


def codex_reset(snapshot):
    """Normalize provider window data, retaining unknown/weekly stops as stops."""
    import math
    if not isinstance(snapshot, dict):
        return None
    blocked = []
    for name in ('primary', 'secondary'):
        window = snapshot.get(name)
        if not isinstance(window, dict):
            continue
        used = window.get('usedPercent', window.get('used_percent'))
        duration = window.get('windowDurationMins', window.get('window_minutes'))
        reset = window.get('resetsAt', window.get('resets_at'))
        if type(used) not in (int, float) or not math.isfinite(used):
            return None
        if used < 100:
            continue
        if (type(duration) not in (int, float) or not 0 < duration < 1440
                or type(reset) not in (int, float) or not math.isfinite(reset)):
            return None
        blocked.append(reset)
    return max(blocked) if blocked else None


def stream_reset(path, backend, session, *, since=None):
    """Read once on a terminal stop, not a provider health poll."""
    latest = None
    buckets = {}
    try:
        with Path(path).open('rb') as stream:
            for raw in stream:
                if not raw.endswith(b'\n'):
                    continue
                if b'stdout: ' in raw and not raw.lstrip().startswith(b'{'):
                    raw = raw.split(b'stdout: ', 1)[1]
                try:
                    event = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                if since is not None:
                    try:
                        timestamp = datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00')).timestamp()
                    except (ValueError, TypeError, KeyError):
                        continue
                    if timestamp < since:
                        continue
                if backend == 'claude':
                    if event.get('type') == 'rate_limit_event' and event.get('session_id') == session:
                        info = event.get('rate_limit_info') or {}
                        if not isinstance(info, dict):
                            latest = None
                            continue
                        latest = short_window_reset({'latest_events': [dict(session_id=session,
                            status=info.get('status'), limit_type=info.get('rateLimitType'),
                            resets_at=info.get('resetsAt'), windows=info.get('unifiedWindows'))]}, session)
                else:
                    if event.get('method') == 'account/rateLimits/updated':
                        params = event.get('params')
                        value = params.get('rateLimits') if isinstance(params, dict) else None
                        if isinstance(value, dict):
                            key = value.get('limitId') or 'codex'
                            if not isinstance(key, str):
                                latest = None
                                continue
                            buckets[key] = {**buckets.get(key, {}), **value}
                            resets = []
                            unknown = False
                            for bucket in buckets.values():
                                blocked = any(isinstance(w, dict) and type(w.get('usedPercent')) in (int, float)
                                              and w['usedPercent'] >= 100 for w in (bucket.get('primary'), bucket.get('secondary')))
                                due = codex_reset(bucket)
                                unknown |= blocked and due is None
                                if due is not None:
                                    resets.append(due)
                            latest = max(resets) if resets and not unknown else None
                    payload = event.get('payload') or {}
                    if event.get('type') == 'event_msg' and isinstance(payload, dict) and payload.get('type') == 'token_count':
                        latest = codex_reset(payload.get('rate_limits'))
        return latest
    except OSError:
        return None


def native_reset(path, backend, session, workspace, *, since=None):
    value = stream_reset(path, backend, session)
    if value is not None or backend != 'codex' or since is None:
        return value
    # Exec does not always emit quota snapshots. Its existing session journal
    # can supply one from this attempt, after exact identity/workspace binding.
    from ._native_session import read_session_worktree, _session_root
    try:
        if read_session_worktree(session) != Path(workspace).resolve():
            return None
        paths = list(_session_root().glob('[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/rollout-*-' + session + '.jsonl'))
        return stream_reset(paths[0], backend, session, since=since) if len(paths) == 1 else None
    except (OSError, ValueError, TypeError):
        return None
