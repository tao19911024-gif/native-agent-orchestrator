"""Native member observations; liveness and waiting are not evidence of progress."""
import json
import sys
import os
import time
from datetime import datetime
from pathlib import Path


def native_call_status(folder, backend, *, now=None, threshold=300, max_bytes=1024 * 1024):
    """Bounded read of an existing call, not a heartbeat writer or cancellation owner."""
    folder = Path(folder)
    now = time.time() if now is None else now
    result = {'evidence': str(folder), 'backend': backend, 'read_only': True,
              'process_state': 'unknown', 'activity': 'unknown',
              'observation': 'Output silence and process existence do not prove a deadlock or progress.'}
    try:
        if (folder / 'receipt.json').exists():
            result['activity'] = 'receipt_available'
            return result
        allocation = json.loads((folder / 'reservation.json').read_bytes())
        result['call_id'] = allocation.get('call_id')
        if not (folder / 'process.json').exists():
            result['activity'] = 'process_not_observed'
            result['observation'] = 'Call reserved; process identity not published. Startup or local preparation may still be in progress.'
            if (folder / 'receipt.json').exists():
                result['activity'] = 'receipt_available'
            return result
        identity = json.loads((folder / 'process.json').read_bytes())
        if identity.get('attempt_id') != allocation.get('attempt_id'):
            raise ValueError('process and allocation identity differ')
        result.update(call_id=allocation.get('call_id'), root_pid=identity['root_pid'])
        result['process_observed_utc'] = identity.get('observed_utc')
        if identity.get('observed_utc'):
            result['process_elapsed_seconds'] = max(0, now - datetime.fromisoformat(identity['observed_utc']).timestamp())
        if os.name == 'nt':
            from ._executor_lifecycle import process_identity_exited
            try:
                result['process_state'] = ('exited' if process_identity_exited(
                    identity['root_pid'], identity['root_creation_time']) else 'exists')
            except OSError as exc:
                result['process_observation_error'] = str(exc)
        stream_path = folder / 'stdout.jsonl'
        with stream_path.open('rb') as stream:
            stat = os.fstat(stream.fileno())
            offset = max(0, stat.st_size - max_bytes)
            stream.seek(offset)
            data = stream.read(stat.st_size - offset)
        lines = data.split(b'\n')
        if offset:
            lines = lines[1:]  # A tail boundary may split a provider event.
        partial = bool(lines[-1])
        lines = lines[:-1]  # Never interpret an in-flight partial event.
        result.update(stream_updated_at=stat.st_mtime,
                      no_output_seconds=max(0, now - stat.st_mtime),
                      snapshot_bytes=len(data), truncated_history=bool(offset), partial_event=partial)
        tools, malformed, last_kind, terminal = {}, 0, None, False
        for raw in lines:
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
                if not isinstance(row, dict):
                    raise ValueError('not an event')
            except (ValueError, UnicodeError):
                malformed += 1
                continue
            last_kind = row.get('type')
            if last_kind in ('result', 'turn.completed', 'turn.failed'):
                terminal = True
                tools.clear()
                continue
            if backend == 'claude':
                blocks = (row.get('message') or {}).get('content', [])
                for block in blocks if isinstance(blocks, list) else []:
                    if not isinstance(block, dict):
                        continue
                    if block.get('type') == 'tool_use' and block.get('id'):
                        tools[block['id']] = {'id': block['id'], 'name': block.get('name')}
                    elif block.get('type') == 'tool_result':
                        tools.pop(block.get('tool_use_id'), None)
                if row.get('type') == 'tool_progress':
                    key = (row.get('tool_use_id') or '').split('-heartbeat-')[0] or row.get('parent_tool_use_id')
                    if key:
                        tool = tools.setdefault(key, {'id': key, 'name': row.get('tool_name')})
                        tool['provider_elapsed_seconds'] = row.get('elapsed_time_seconds')
                        tool['heartbeat_only'] = bool(row.get('heartbeat'))
            elif backend == 'codex':
                item = row.get('item') or {}
                if item.get('type') in ('command_execution', 'mcp_tool_call', 'collab_tool_call') and item.get('id'):
                    if row.get('type') in ('item.started', 'item.updated'):
                        tools[item['id']] = {'id': item['id'], 'name': item['type']}
                    elif row.get('type') == 'item.completed':
                        tools.pop(item['id'], None)
        heartbeat_wait = any(tool.get('heartbeat_only') and
            type(tool.get('provider_elapsed_seconds')) in (int, float) and
            tool['provider_elapsed_seconds'] >= threshold for tool in tools.values())
        result.update(active_tools=list(tools.values()), last_event_type=last_kind,
                      malformed_events=malformed, activity='terminal_observed' if terminal else 'tool_pending' if tools else
                      'stream_output' if last_kind and not offset and not malformed else 'unknown',
                      attention_required=bool(tools) and (result['no_output_seconds'] >= threshold or heartbeat_wait),
                      attention_reason='tool silence or prolonged heartbeat without result; inspect before intervention')
        # Receipt publication can race this read; never present a completed call as pending.
        if (folder / 'receipt.json').exists():
            result.update(activity='receipt_available', active_tools=[], attention_required=False)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        result['observation_error'] = str(exc)
    return result


class MemberActivity:
    def __init__(self, threshold=300):
        self.threshold = threshold
        self.members = {}
        self.offset = 0
        self.pending = b''
        self.read_error = None

    def observe(self, row, now):
        member = row.get('parent_tool_use_id')
        if row.get('type') == 'tool_progress' and row.get('heartbeat') and row.get('tool_name') == 'Agent':
            member = member or (row.get('tool_use_id') or '').split('-heartbeat-')[0]
            if not member:
                return None
            state = self.members.setdefault(member, {'last': now, 'reported': False})
            return self._warning(member, state, now)
        if member and row.get('type') in ('assistant', 'user'):
            self.members[member] = {'last': now, 'reported': False}
        message = row.get('message')
        blocks = message.get('content', []) if isinstance(message, dict) else []
        if isinstance(blocks, list):
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'tool_use' and block.get('name') == 'Agent' and block.get('id'):
                    self.members[block['id']] = {'last': now, 'reported': False}
                elif block.get('type') == 'tool_result':
                    self.members.pop(block.get('tool_use_id'), None)
        if row.get('type') == 'system' and row.get('subtype') == 'task_notification':
            self.members.pop(row.get('tool_use_id'), None)
        return None

    def _warning(self, member, state, now):
        idle = now - state['last']
        if idle >= self.threshold and not state['reported']:
            state['reported'] = True
            return {'signal': 'member_no_transcript_progress', 'member': member,
                    'provider': 'claude', 'idle_seconds': int(idle), 'threshold_seconds': self.threshold,
                    'observation': 'member has no visible progress; stalled descendant, if any, is unidentified; not proof of deadlock',
                    'action': 'inspect member transcript and active tool; no automatic termination'}

    def warnings(self, now):
        return [signal for member, state in self.members.items()
                if (signal := self._warning(member, state, now))]

    def read(self, path, now, *, on_event=None):
        try:
            with path.open('rb') as stream:
                stream.seek(self.offset)
                data = stream.read()
                self.offset = stream.tell()
        except OSError as exc:
            error = str(exc)
            if error == self.read_error:
                return self.warnings(now)
            self.read_error = error
            return [{'signal': 'member_observation_unavailable', 'error': error,
                     'action': 'inspect native stream; execution continues'}, *self.warnings(now)]
        self.read_error = None
        lines = (self.pending + data).split(b'\n')
        self.pending = lines.pop()
        signals = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and (signal := self.observe(row, now)):
                signals.append(signal)
            if isinstance(row, dict) and on_event is not None:
                try:
                    on_event(row)
                except Exception as exc:
                    signals.append({'signal': 'live_event_observation_unavailable', 'error': str(exc),
                                    'action': 'native stream retained; execution continues'})
        return [*signals, *self.warnings(now)]


class CodexMemberActivity:
    """Consume installed app-server collab states; no new member scheduler."""
    def __init__(self, threshold=300):
        self.threshold = threshold
        self.members = {}
        self.seen = set()
        self.next_check = 0

    def publish(self, app, now):
        if now < self.next_check:
            return
        self.next_check = now + 1
        with app._turn_lock:
            signals = self.warnings(now) if app._terminal is None else []
        for signal in signals:
            notice = 'Foundry observation: ' + json.dumps(signal)
            try:
                app._log(notice)
                print(notice, file=sys.stderr, flush=True)
            except (OSError, ValueError):
                pass  # Observation failure does not change native execution.

    def observe(self, message, now, *, thread_id, turn_id):
        params = message.get('params')
        if not isinstance(params, dict):
            return
        member = params.get('threadId')
        if not isinstance(member, str):
            return
        if member in self.members and member != thread_id:
            # Only an actual child item/delta is progress, not a repeated running state.
            method = message.get('method', '')
            if ((method in ('item/started', 'item/completed') and isinstance(params.get('item'), dict))
                    or (method in ('item/agentMessage/delta', 'item/commandExecution/outputDelta', 'item/reasoning/textDelta')
                        and isinstance(params.get('delta'), str) and params['delta'].strip())):
                self.members[member].update(last=now, reported=False)
            if (method == 'turn/completed' and isinstance(params.get('turn'), dict)
                    and params['turn'].get('status') in ('completed', 'failed', 'interrupted')):
                self.members.pop(member, None)
            return
        if (member != thread_id or params.get('turnId') != turn_id
                or message.get('method') != 'item/completed'):
            return
        item = params.get('item')
        if not isinstance(item, dict) or item.get('type') not in ('collabAgentToolCall', 'subAgentActivity'):
            return
        ident = item.get('id')
        if not isinstance(ident, str) or ident in self.seen:
            return
        self.seen.add(ident)
        if item['type'] == 'subAgentActivity':
            child = item.get('agentThreadId')
            if not isinstance(child, str) or not child or child == thread_id:
                return
            if item.get('kind') == 'started':
                self.members[child] = {'last': now, 'reported': False, 'status': 'running'}
            elif item.get('kind') in ('completed', 'interrupted'):
                self.members.pop(child, None)
            return  # Parent sending a message does not prove child progress.
        states = item.get('agentsStates', {})
        if not isinstance(states, dict):
            return
        for child, state in states.items():
            if not isinstance(child, str) or not isinstance(state, dict):
                continue
            status = state.get('status')
            if status in ('completed', 'errored', 'interrupted', 'shutdown', 'notFound'):
                self.members.pop(child, None)
            elif status in ('pendingInit', 'running'):
                previous = self.members.setdefault(child, {'last': now, 'reported': False, 'status': status})
                if previous['status'] != status or item.get('tool') in ('spawnAgent', 'resumeAgent', 'followupTask'):
                    previous.update(last=now, reported=False, status=status)

    def warnings(self, now):
        result = []
        for member, state in self.members.items():
            if now - state['last'] >= self.threshold and not state['reported']:
                state['reported'] = True
                result.append({'signal': 'member_no_transcript_progress', 'provider': 'codex',
                    'member': member, 'idle_seconds': int(now - state['last']),
                    'threshold_seconds': self.threshold,
                    'observation': 'no new visible member activity; child events may be unavailable; not proof of deadlock',
                    'action': 'inspect member state and active tool; no automatic termination'})
        return result
