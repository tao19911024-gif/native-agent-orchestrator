#!/usr/bin/env python3
"""Custody for one Codex app-server stdio session and its live steer handle.

The model still performs the work.  This module owns only transport: JSON-RPC
framing, the active turn precondition, one loopback control socket, evidence,
and process lifetime.
"""

from __future__ import annotations

import json
import copy
import os
import queue
import socket
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, TextIO

from ._executor_lifecycle import CleanupReceipt, ExecutorLease, OwnedProcess, SpawnFailure
from ._codex_command import coding_argv, codex_env
from ._execution_profile import coding_policy
from ._compute_selection import compute_kwargs
from ._compute_selection import team_kwargs
from ._app_server_terminal import validate_terminal
from ._app_server_policy import normalize_policy
from ._app_server_activity import activity_key

# Received and acted on, never written to the wire log: the completed item repeats them.
_UNLOGGED_METHODS = frozenset({"item/commandExecution/outputDelta"})


class RpcError(RuntimeError):
    def __init__(self, error: dict):
        super().__init__(str(error.get("message", "app-server request failed")))
        self.error = error


class TransportCleanupError(RuntimeError):
    """Keep uncertain native custody reachable by the invoking dispatcher."""

    def __init__(self, process, cleanup):
        super().__init__("app-server cleanup remains unknown: " + cleanup.reason)
        self.process, self.cleanup = process, cleanup


class UncertainExecution(RuntimeError):
    """Transport stopped without trustworthy evidence of this turn's outcome."""


class AppServer:
    """A single JSON-RPC client over one app-server subprocess's stdio."""

    INPUT_WAIT_SECONDS = 900

    def __init__(self, process: OwnedProcess, log_handle: TextIO, *, deadline: float | None, policy=None, observation_only=False):
        self.observation_only = observation_only
        self._log_handle = log_handle
        self._log_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._pending_lock = threading.Lock()
        self._next_id = 1
        self._turn_started = threading.Event()
        self.completed = threading.Event()
        self.outcome_ready = threading.Event()
        self.reader_done = threading.Event()
        self._turn_lock = threading.RLock()
        self._early_events = []
        self._starting = False
        self._protocol_error = ""
        self._terminal = None
        self._response_status = ""
        self.thread_id = ""
        self.turn_id = ""
        self.turn_status = ""
        self.session_id = ""
        self.agent_text = ""
        self._final_messages: list[str] = []
        self.process = process
        self.deadline = deadline
        self.policy = normalize_policy(policy)
        self.last_activity = None
        self._activity_states = set()
        from ._member_activity import CodexMemberActivity
        self.member_activity = CodexMemberActivity()
        self._stopping = threading.Event()
        self.expiration_cause = None
        self.grace_deadline = None
        self.interrupt_result = 'not_requested'
        self._interrupt_sent = False
        self._writers = []
        self._input_requests = {}
        self._answered_requests = set()
        self._unanswered_input = False
        self._message_items = set()
        self.process_completion_wait = False
        self._process_waiting = {}
        self._process_completed = {}
        self._process_seen = set()
        from ._goal_intervention import LiveEvents
        self.live_events = LiveEvents()
        self._stdout_thread = threading.Thread(
            target=self._read_stdout, name="pdcl-app-server-stdout", daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, name="pdcl-app-server-stderr", daemon=True
        )
    def start_readers(self) -> None:
        # Caller retains this object before either thread can be started.
        self._stdout_thread.start()
        self._stderr_thread.start()

    def remaining(self, limit: float = 60) -> float:
        left = limit if self.deadline is None else min(limit, self.deadline - time.monotonic())
        if left <= 0:
            raise TimeoutError("app-server attempt deadline expired")
        return left

    def expiration(self):
        with self._turn_lock:
            now = time.monotonic()
            if self.expiration_cause:
                return self.expiration_cause
            if self._terminal is not None:
                return None
            if self.deadline is not None and now >= self.deadline:
                return 'overall'
            if any(now >= row['deadline'] for row in self._input_requests.values()):
                return 'input_required'
            if (self.last_activity is not None and self.policy['inactivity_seconds'] is not None
                    and now >= self.last_activity + self.policy['inactivity_seconds']):
                return 'inactivity'
            return None

    def begin_stop(self, cause):
        with self._turn_lock:
            if self.expiration_cause is None:
                self.expiration_cause = cause
                self.grace_deadline = time.monotonic() + self.policy['interrupt_grace_seconds']
                self._stopping.set()

    def interrupt_and_wait(self):
        # Only the existing supervisor calls this. The same deadline covers lock,
        # pipe send, acknowledgment and terminal observation; activity cannot renew it.
        with self._turn_lock:
            if (not self.turn_id or self._response_status != 'inProgress'
                    or self._terminal is not None or self.reader_done.is_set()
                    or self.interrupt_result != 'not_requested'):
                return
            self.interrupt_result = 'requested'
            params = {'threadId': self.thread_id, 'turnId': self.turn_id}
        try:
            response = self.request('turn/interrupt', params, _deadline=self.grace_deadline)
            self.interrupt_result = ('acknowledged' if response == {} else
                                     'unacknowledged: malformed interrupt acknowledgment')
        except (RpcError, TimeoutError, OSError) as error:
            self.interrupt_result = 'unacknowledged: ' + str(error)
        # Rejected or missing acknowledgment does not discard a racing terminal.
        self.outcome_ready.wait(max(0, self.grace_deadline - time.monotonic()))

    def _log(self, line: str) -> None:
        with self._log_lock:
            self._log_handle.write(line.rstrip("\n") + "\n")
            self._log_handle.flush()

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        try:
            for line in self.process.stderr:
                self._log("stderr: " + line)
        except (OSError, ValueError):
            pass  # Owned cleanup closes pipes after terminating the Job.

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                observed_at = time.monotonic()
                try:
                    message = json.loads(line)
                except (TypeError, ValueError):
                    self._log("stdout: " + line)
                    continue
                if not isinstance(message, dict):
                    self._log("stdout: " + line)
                    continue
                # Output deltas still renew the inactivity clock below, but the
                # completed commandExecution item carries the same bytes as
                # aggregatedOutput; logging each chunk too doubled every log.
                if message.get("method") not in _UNLOGGED_METHODS:
                    self._log("stdout: " + line)
                if "id" in message and ("result" in message or "error" in message):
                    with self._pending_lock:
                        waiter = self._pending.pop(message["id"], None)
                    if waiter is not None:
                        waiter.put(message)
                    continue
                self._notification(message, observed_at=observed_at)
        except (OSError, ValueError) as error:
            # Owned Job cleanup may close the read pipe. Like EOF, this is
            # transport loss, never an observation of a different turn outcome.
            self.reader_error = str(error)
        except Exception as error:
            with self._turn_lock:
                self._invalidate("stdout reader failed: " + str(error))
        finally:
            self.outcome_ready.set()
            with self._pending_lock:
                self.reader_done.set()
                waiters = list(self._pending.values())
                self._pending.clear()
            for waiter in waiters:
                waiter.put({"error": {"code": -32000, "message": "app-server exited before responding"}})

    def _notification(self, message: dict, *, observed_at=None) -> None:
        observed_at = time.monotonic() if observed_at is None else observed_at
        with self._turn_lock:
            if self._starting:
                # The RPC response, never the first notification, selects the turn.
                self._early_events.append((copy.deepcopy(message), observed_at))
            else:
                self._observe(message, observed_at)

    def _invalidate(self, reason):
        self._protocol_error = self._protocol_error or reason
        self.completed.clear()
        self.outcome_ready.set()

    def _observe(self, message, observed_at=None):
        observed_at = time.monotonic() if observed_at is None else observed_at
        if self._terminal is None:
            self.member_activity.observe(message, observed_at, thread_id=self.thread_id, turn_id=self.turn_id)
            if self.deadline is not None and observed_at >= self.deadline:
                self.begin_stop('overall')
            elif (self.last_activity is not None and self.policy['inactivity_seconds'] is not None
                  and observed_at >= self.last_activity + self.policy['inactivity_seconds']):
                self.begin_stop('inactivity')
        key = activity_key(message, self.thread_id, self.turn_id)
        if (key and self._terminal is None and not self._stopping.is_set()
                and (key[0] == 'delta' or (key not in self._activity_states
                     and ('state', key[1], 'item/completed') not in self._activity_states))):
            if key[0] == 'state':
                self._activity_states.add(key)
            self.last_activity = observed_at if self.last_activity is None else max(self.last_activity, observed_at)
        method = message.get("method")
        params = message.get("params", {})
        if self.process_completion_wait and not self.observation_only:
            try:
                notice = self._process_wait_notice(message)
                if notice is not None:
                    self._deliver_process_wait_notice(notice)
            except Exception as exc:
                self._log('native process wait observation unavailable: ' + str(exc))
        if hasattr(self, 'live_events') and not self.observation_only:
            try:
                residual = self.live_events.observe(message, self.thread_id)
                if residual is not None:
                    self._log(json.dumps({'event': 'foundry_context_residual', **residual}))
            except Exception as exc:
                self._log('live manager event observation unavailable: ' + str(exc))
        if (method == 'item/completed' and isinstance(params, dict)
                and params.get('threadId') == self.thread_id and params.get('turnId') == self.turn_id):
            item = params.get('item') or {}
            if isinstance(item, dict) and item.get('type') == 'agentMessage':
                if not self.observation_only:
                    self._project_message(params, item)
        if method == 'serverRequest/resolved' and isinstance(params, dict):
            if params.get('threadId') == self.thread_id:
                if self._input_requests.pop(params.get('requestId'), None) is not None:
                    self._unanswered_input = True
                    self.begin_stop('input_required')
            return
        if method == 'item/tool/requestUserInput':
            if (not isinstance(params, dict) or params.get('threadId') != self.thread_id
                    or params.get('turnId') != self.turn_id):
                return
            request_id = message.get('id')
            questions = params.get('questions')
            if (type(request_id) not in (str, int) or not isinstance(questions, list) or not questions
                    or any(not isinstance(q, dict) or not isinstance(q.get('id'), str)
                           or not q['id'] or not isinstance(q.get('question'), str) for q in questions)
                    or len({q['id'] for q in questions}) != len(questions)):
                self._invalidate('malformed native input request')
                return
            if (request_id not in self._answered_requests and self._terminal is None
                    and not self._stopping.is_set()):
                self._input_requests.setdefault(request_id, {
                    'request': copy.deepcopy(message),
                    'deadline': observed_at + self.INPUT_WAIT_SECONDS})
            return
        if method not in {"turn/started", "turn/completed", "item/agentMessage/delta", "item/completed"}:
            return
        if not self.thread_id or not self.turn_id:
            return  # No admitted turn yet; outside turn/start's buffered interval.
        params = message.get("params")
        if not isinstance(params, dict) or not self._id(params.get("threadId")):
            self._invalidate("notification lacks a valid thread identity")
            return
        if params["threadId"] != self.thread_id:
            return
        turn = params.get("turn") if method.startswith("turn/") else None
        turn_id = turn.get("id") if isinstance(turn, dict) else params.get("turnId")
        if not self._id(turn_id):
            self._invalidate("notification lacks a valid turn identity")
            return
        if turn_id != self.turn_id:
            return
        if method == "turn/started":
            if turn.get("status") != "inProgress" or self._terminal is not None:
                self._invalidate("contradictory turn/started status or ordering")
                return
            self.turn_status = "inProgress"
            self._turn_started.set()
        elif method == "item/agentMessage/delta":
            if isinstance(params.get("delta"), str) and self._terminal is None:
                self.agent_text += params["delta"]
        elif method == "item/completed":
            item = params.get("item")
            if (isinstance(item, dict) and item.get("type") == "agentMessage"
                    and isinstance(item.get("text"), str) and self._terminal is None):
                text = item["text"]
                if item.get("phase") == "final_answer":
                    self._final_messages.append(text)
        elif method == "turn/completed":
            try:
                status = validate_terminal(message, self.thread_id, self.turn_id)
            except ValueError as error:
                self._invalidate(str(error))
                return
            if status == 'completed' and (self._input_requests or self._unanswered_input):
                self._invalidate('native turn completed with unanswered input')
                return
            if ((self._response_status != "inProgress" and self._response_status != status)
                    or (self._terminal is not None and self._terminal != message)):
                self._invalidate("malformed or contradictory terminal status")
                return
            self._terminal = copy.deepcopy(message)
            self.turn_status = status
            if not self._protocol_error:
                self.completed.set()
            self.outcome_ready.set()

    @staticmethod
    def _id(value):
        return isinstance(value, str) and bool(value.strip())

    def _process_wait_notice(self, message):
        """Existing native event stream wakes only its recorded waiting owner.

        Completion may precede wait subscription; retain it until a native wait
        consumes it. Repeated terminal notifications do not create new input.
        No OS scan, timer, process cancellation, manager decision or task resume.
        """
        params = message.get('params') or {}
        if not isinstance(params, dict):
            return
        thread, turn = params.get('threadId'), params.get('turnId')
        method = message.get('method')
        if method == 'turn/completed':
            terminal = params.get('turn') or {}
            if not isinstance(terminal, dict):
                return
            turn = terminal.get('id')
            key = (thread, turn)
            self._process_waiting.pop(key, None)
            self._process_completed.pop(key, None)
            self._process_seen = {row for row in self._process_seen if row[:2] != key}
            return
        if (not self._id(thread) or not self._id(turn)
                or self._terminal is not None or self._stopping.is_set()
                or (thread == self.thread_id and turn != self.turn_id)
                or (thread != self.thread_id and thread not in self.member_activity.members)):
            return
        item = params.get('item') or {}
        if not isinstance(item, dict):
            return
        identity = item.get('id')
        if not self._id(identity):
            return
        key = (thread, turn)
        if item.get('type') == 'collabAgentToolCall' and item.get('tool') == 'wait':
            if method == 'item/started':
                self._process_waiting[key] = identity
            elif method == 'item/completed' and self._process_waiting.get(key) == identity:
                self._process_waiting.pop(key, None)
        elif method == 'item/completed' and item.get('type') == 'commandExecution':
            # An empty write_stdin interaction is not process termination.
            # Require a native process handle and measured exit code; retain
            # unknown shapes in the log for the bounded native-handle fallback.
            if (item.get('source') == 'unifiedExecInteraction'
                    or not self._id(item.get('processId')) or type(item.get('exitCode')) is not int):
                return
            seen = (*key, identity)
            if seen not in self._process_seen:
                self._process_seen.add(seen)
                self._process_completed.setdefault(key, []).append({
                    'item_id': identity, 'process_id': item['processId'],
                    'status': item.get('status'), 'exit_code': item.get('exitCode')})
        if key in self._process_waiting and self._process_completed.get(key):
            return {'thread_id': thread, 'turn_id': turn,
                    'wait_id': self._process_waiting[key],
                    'commands': self._process_completed.pop(key)}

    def _deliver_process_wait_notice(self, notice):
        # The reader must remain free to receive this RPC's response. This is
        # an owned delivery thread, joined with existing RPC writers on close.
        # Multi-agent V2 rejects direct AppServer input to a child. Its existing
        # parent owns native member messaging; route through that root instead.
        # Root acknowledgment is not evidence that the member received a relay.
        member_relay = notice['thread_id'] != self.thread_id
        destination_thread = self.thread_id if member_relay else notice['thread_id']
        destination_turn = self.turn_id if member_relay else notice['turn_id']
        text = ('Foundry native process completion: ' + json.dumps(notice) +
                '. If already collected, reuse that retained result; otherwise collect the original native handle/result. '
                'This is execution evidence, not acceptance; do not restart the command. '
                'Continue waiting for other unfinished work using its same owner/handle.')
        if member_relay:
            text = ('Relay this native completion evidence to your existing member with thread ID ' +
                    notice['thread_id'] + ' using collaboration.send_message (resolve its existing '
                    'member name from your spawn result if needed). Do not collect or restart its '
                    'process yourself, create a replacement member, or treat this input as member '
                    'completion. Then wait for that member to verify its original handle/result. '
                    'Message to relay: ' + text)
        def deliver():
            try:
                if self._terminal is not None or self._stopping.is_set():
                    self._log(json.dumps({'event':'foundry_process_wait_delivery_unconfirmed',
                        **notice, 'error':'owning executor is ending'}))
                    return
                result = self.request('turn/steer', {
                    'threadId': destination_thread, 'expectedTurnId': destination_turn,
                    'input': [{'type':'text', 'text':text}]}, stop_on_timeout=False)
                if result.get('turnId') != destination_turn:
                    raise RpcError({'message':'process wait input acknowledgment names a different turn'})
                self._log(json.dumps({'event':('foundry_process_wait_relay_delivered_to_parent'
                    if member_relay else 'foundry_process_wait_delivered'), **notice,
                    'destination_thread_id':destination_thread, 'destination_turn_id':destination_turn,
                    'result':result}))
            except Exception as exc:
                # Ambiguous RPC delivery is not permission to duplicate input.
                self._log(json.dumps({'event':'foundry_process_wait_delivery_unconfirmed',
                                      **notice, 'error':str(exc)}))
        worker = threading.Thread(target=deliver, name='foundry-process-wait-input', daemon=True)
        self._writers.append(worker)
        worker.start()

    def terminal_observation(self):
        with self._turn_lock:
            if self._unanswered_input and self.turn_status == 'completed':
                raise UncertainExecution('native turn completed with unanswered input')
            if not self.completed.is_set() or self._protocol_error or self._terminal is None:
                raise UncertainExecution(self._protocol_error or "uncertain execution: no matching terminal event")
            return copy.deepcopy(self._terminal)

    def notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message, time.monotonic() + self.remaining(self.policy['rpc_seconds']))

    def _send(self, message: dict, deadline: float, *, stopping=False) -> None:
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        assert self.process.stdin is not None
        finished, errors = threading.Event(), []
        def write():
            acquired = False
            try:
                acquired = self._write_lock.acquire(timeout=max(0, deadline - time.monotonic()))
                if not acquired or time.monotonic() >= deadline:
                    raise TimeoutError('app-server send deadline expired')
                if self._stopping.is_set() and not stopping:
                    raise TimeoutError('app-server stopping; work admission closed')
                self.process.stdin.write(line)
                self.process.stdin.flush()
            except BaseException as error:
                errors.append(error)
            finally:
                if acquired:
                    self._write_lock.release()
                finished.set()
        writer = threading.Thread(target=write, name='pdcl-app-server-write', daemon=True)
        with self._turn_lock:
            self._writers = [t for t in self._writers if t.is_alive()] + [writer]
            writer.start()
        if (not finished.wait(max(0, deadline - time.monotonic()))
                or time.monotonic() > deadline):
            raise TimeoutError('app-server send deadline expired')
        if errors:
            raise errors[0]

    def request(self, method: str, params: dict, timeout=None, *, _deadline=None, stop_on_timeout=True) -> dict:
        stopping = _deadline is not None
        if stopping or method == 'turn/interrupt':
            with self._turn_lock:
                if (not stopping or method != 'turn/interrupt' or not self._stopping.is_set()
                        or _deadline != self.grace_deadline or self._interrupt_sent
                        or not self.thread_id or not self.turn_id
                        or params != {'threadId': self.thread_id, 'turnId': self.turn_id}):
                    raise RpcError({'message': 'interrupt requires the single admitted stop request'})
                self._interrupt_sent = True
        deadline = _deadline if stopping else time.monotonic() + self.remaining(
            min(self.policy['rpc_seconds'], timeout if timeout is not None else self.policy['rpc_seconds']))
        if self._stopping.is_set() and not stopping:
            raise TimeoutError('app-server stopping; work admission closed')
        with self._pending_lock:
            if self.reader_done.is_set():
                raise RpcError({'message': 'app-server transport exited before request registration'})
            request_id = self._next_id
            self._next_id += 1
            waiter: queue.Queue = queue.Queue(maxsize=1)
            self._pending[request_id] = waiter
        try:
            self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                deadline, stopping=stopping,
            )
            response = waiter.get(timeout=max(0, deadline - time.monotonic()))
            if time.monotonic() > deadline:
                raise TimeoutError('app-server response arrived after request deadline')
        except (queue.Empty, TimeoutError) as exc:
            if not stopping and stop_on_timeout:
                self.begin_stop(self.expiration() or ('rpc:' + method))
            raise TimeoutError("app-server request timed out: " + method) from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
        # Native JSONRPCErrorError requires integer code and string message.
        # Presence, not truthiness, distinguishes errors: result/error together
        # cannot acknowledge an interrupt (JSON-RPC 5/5.1). Keep malformed replies
        # on the RpcError path so the supervisor still observes remaining grace.
        if "error" in response:
            if "result" in response:
                raise RpcError({'message': 'malformed app-server response: result and error coexist'})
            error = response['error']
            if (not isinstance(error, dict) or type(error.get('code')) is not int
                    or not isinstance(error.get('message'), str)):
                raise RpcError({'message': 'malformed app-server error response'})
            raise RpcError(error)
        result = response.get("result")
        if stopping and not isinstance(result, dict):
            raise RpcError({'message': 'malformed interrupt acknowledgment'})
        return result if isinstance(result, dict) else {}

    def _check_compute(self, selection):
        """Use the native catalogue when it describes this model, never a static list.

        An unlisted private model remains for the native launch to diagnose.
        The catalogue is capability evidence, not a provider identity observation.
        """
        cursor, seen = None, set()
        while True:
            page = self.request('model/list', {'includeHidden': True, 'limit': 100,
                                              **({'cursor': cursor} if cursor else {})})
            for row in page.get('data', []):
                if selection['model'] not in (row.get('id'), row.get('model')):
                    continue
                supported = row.get('supportedReasoningEfforts')
                if isinstance(supported, list) and selection['effort'] not in [
                        item.get('reasoningEffort') for item in supported]:
                    raise ValueError('native model/list reports unsupported effort for ' + selection['model'])
                return
            cursor = page.get('nextCursor')
            if not cursor:
                return
            if cursor in seen:
                raise RpcError({'message': 'model/list repeated its pagination cursor'})
            seen.add(cursor)

    def start_turn(self, body: str, worktree: Path, *, resume_thread_id=None,
                   model=None, effort=None, native_team=None, execution_policy=None) -> None:
        selection = compute_kwargs(model, effort)
        policy = execution_policy if execution_policy is not None else coding_policy()
        self.request(
            "initialize",
            {"clientInfo": {"name": "pdcl-dispatch", "version": "1"},
             "capabilities": {"experimentalApi": True,
                 # Full cumulative diffs duplicate retained fileChange items and
                 # grow with every edit. Use the native connection subscription;
                 # received events, terminal evidence and usage stay untouched.
                 "optOutNotificationMethods": ["turn/diff/updated"]}},
        )
        self.notify("initialized")
        if selection:
            self._check_compute(selection)
        thread_method = "thread/resume" if resume_thread_id is not None else "thread/start"
        config = dict(native_team['config']) if native_team is not None else {}
        config['features.default_mode_request_user_input'] = True
        if selection:
            config['model_reasoning_effort'] = selection['effort']
        thread_params = {
            "cwd": str(worktree),
            "approvalPolicy": policy['approval_policy'],
            "sandbox": policy['sandbox_mode'],
            **({'threadId': resume_thread_id} if resume_thread_id is not None else {}),
            **({'model': selection['model']} if selection else {}),
            **({'config': config} if config else {}),
        }
        started = self.request(thread_method, thread_params)
        thread = started.get("thread")
        if not isinstance(thread, dict) or not self._id(thread.get("id")):
            raise UncertainExecution(thread_method + " returned no valid thread id")
        if resume_thread_id is not None and thread["id"] != resume_thread_id:
            raise UncertainExecution("thread/resume returned a different thread identity")
        self.thread_id = thread["id"]
        self.session_id = str(thread.get("sessionId") or self.thread_id)
        with self._turn_lock:
            self._starting = True
        turn = self.request(
            "turn/start",
            {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": body +
                    ("\nThis Foundry AppServer delivers native command-completion events to a waiting "
                     "owner through its existing input channel. For a pending process, retain its "
                     "exec session_id, then call collaboration.wait_agent directly while it runs "
                     "(even without a child agent). Do not block in a long write_stdin call: that "
                     "delays operator input. When notified of process completion, collect the original "
                     "session/result once. If other processes remain, wait again. Member owners use "
                     "the same procedure. If the configured mailbox interval expires without a "
                     "completion notice, check only the original process session with a short empty "
                     "write_stdin, then return to mailbox waiting if still pending; notification loss "
                     "is not a reason to restart or wait forever without reconciling that handle. "
                     "Parents wait for their member result. Keep this process-wait "
                     "instruction in member briefs. No process is cancelled by a mailbox timeout."
                     if self.process_completion_wait else "") +
                    "\n\nIf progress depends on information only the commander has, ask early with "
                    "request_user_input and wait for the answer before dependent work. Do not end "
                    "the task merely to ask. An answer must actually settle the question; otherwise "
                    "ask again. Ordinary investigation remains your responsibility."}],
                **selection,
            },
        ).get("turn")
        with self._turn_lock:
            if (not isinstance(turn, dict) or not self._id(turn.get("id"))
                    or turn.get("status") not in ("inProgress", "completed", "failed", "interrupted")):
                raise UncertainExecution("turn/start returned malformed identity/status")
            self.turn_id = turn["id"]
            self._response_status = turn["status"]
            self._starting = False
            # Start the observed clock only once identity is admitted. Buffered
            # activity keeps its original receive time, never its replay time.
            self.last_activity = None
            early, self._early_events = self._early_events, []
            for message, observed_at in early:
                self._observe(message, observed_at)
            if self.last_activity is None:
                self.last_activity = time.monotonic()
        # The response admits the turn; a redundant started notification is not
        # required to prove its eventual terminal outcome (and can race EOF).

    def steer(self, prompt: str, expected_turn_id: str) -> dict:
        return self.request(
            "turn/steer",
            {
                "threadId": self.thread_id,
                "expectedTurnId": expected_turn_id,
                "input": [{"type": "text", "text": prompt}],
            }, stop_on_timeout=False,
        )

    def _project_message(self, params, item):
        """Small view of root messages/questions; native log remains authoritative."""
        name = getattr(self._log_handle, 'name', None)
        ident = item.get('id')
        if not isinstance(name, str) or not isinstance(ident, str) or not ident or ident in self._message_items:
            return
        path = Path(name).with_suffix('.messages.jsonl')
        try:
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'thread_id': params['threadId'], 'turn_id': params['turnId'],
                    'item_id': ident, 'phase': item.get('phase'), 'text': item.get('text'),
                    'questions': item.get('questions'), 'source_log': name}, ensure_ascii=False) + '\n')
            self._message_items.add(ident)
        except Exception as exc:
            self._log('message projection unavailable: ' + str(exc))

    def input_requests(self) -> list[dict]:
        with self._turn_lock:
            return [copy.deepcopy(row['request']) for row in self._input_requests.values()]

    def answer_input(self, request_id, response: dict) -> dict:
        """Reply to the native server request on the same stdio, thread and turn."""
        with self._turn_lock:
            if type(request_id) not in (str, int):
                raise RpcError({'message': 'input request ID must be a string or integer'})
            row = self._input_requests.get(request_id)
            if (row is None or self._terminal is not None or self._stopping.is_set()
                    or self.reader_done.is_set() or self.expiration()):
                raise RpcError({'message': 'input request is no longer awaiting an answer'})
            questions = row['request']['params']['questions']
            answers = response.get('answers') if isinstance(response, dict) else None
            if (not isinstance(answers, dict) or set(answers) != {q['id'] for q in questions}
                    or any(not isinstance(v, dict) or not isinstance(v.get('answers'), list)
                           or not v['answers'] or any(not isinstance(s, str) or not s.strip()
                                                     for s in v['answers']) for v in answers.values())):
                raise RpcError({'message': 'answer must supply nonempty answers for every question id'})
            # The control server serializes delivery. Mark uncertain sends consumed too:
            # a pipe timeout cannot justify sending the same RPC response twice.
            self._answered_requests.add(request_id)
            self._input_requests.pop(request_id)
        message = {'jsonrpc': '2.0', 'id': request_id, 'result': response}
        try:
            self._log('input_answer: ' + json.dumps(message, ensure_ascii=False))
            self._send(message, time.monotonic() + self.remaining(self.policy['rpc_seconds']))
        except (TimeoutError, OSError, ValueError):
            with self._turn_lock:
                self._unanswered_input = True
                self.begin_stop(self.expiration() or 'rpc:input_answer')
            raise
        return {'turnId': self.turn_id, 'requestId': request_id}

    def final_text(self) -> str:
        return self._final_messages[-1] if self._final_messages else self.agent_text

    @classmethod
    def retained_terminal(cls, log, binding, policy):
        """Replay evidence through the same terminal observer, with no I/O threads."""
        import io
        app = cls(None, io.StringIO(), deadline=None, policy=policy, observation_only=True)
        app.thread_id, app.turn_id = binding['thread_id'], binding['turn_id']
        stopped = None
        with Path(log).open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    row = json.loads(line.removeprefix('stdout: ').removeprefix('input_answer: '))
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if line.startswith('input_answer: '):
                    app._answered_requests.add(row.get('id'))
                    app._input_requests.pop(row.get('id'), None)
                elif line.startswith('stdout: '):
                    result = row.get('result')
                    turn = result.get('turn') if isinstance(result, dict) else None
                    turn = turn if isinstance(turn, dict) else {}
                    if turn.get('id') == app.turn_id:
                        app._response_status = turn.get('status')
                    if row.get('method'):
                        app._observe(row, observed_at=0)
                elif row.get('event') == 'executor_stopped':
                    stopped = row
        terminal = app.terminal_observation()
        if stopped is None:
            raise UncertainExecution('retained transport has no stopped observation')
        return terminal, app.final_text(), stopped

    def close(self) -> None:
        # Only RPC threads here. The caller has already stopped the owned Job;
        # closing stdin before stopping a blocked writer can itself deadlock.
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread.ident is not None:
                thread.join(timeout=2)
                if thread.is_alive():
                    raise RuntimeError("app-server reader cleanup remains unknown")
        for thread in self._writers:
            if thread.ident is not None:
                thread.join(timeout=2)
                if thread.is_alive():
                    raise RuntimeError("app-server reader cleanup remains unknown")


class CorrectionServer:
    """One loopback handle that serializes steer requests onto app-server stdio."""

    def __init__(self, app_server: AppServer, *, attempt_id: str = ""):
        self.app_server = app_server
        self.attempt_id = attempt_id
        self._stop = threading.Event()
        self._connection_lock = threading.Lock()
        self._connection: socket.socket | None = None
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self._listener.settimeout(0.5)
        self.host, self.port = self._listener.getsockname()
        self._thread = threading.Thread(
            target=self._serve, name="pdcl-steer-control", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    @staticmethod
    def _read_line(connection: socket.socket, *, timeout: float = 2) -> bytes:
        payload = b""
        limit = connection.gettimeout()
        # Request framing is short; a response must allow the caller's RPC wait.
        # Do not silently apply the request-frame limit to acknowledgment reads.
        deadline = time.monotonic() + min(timeout, limit if limit is not None else timeout)
        while not payload.endswith(b"\n") and len(payload) <= 1_048_576:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("control line deadline expired")
            connection.settimeout(remaining)
            chunk = connection.recv(65536)
            if not chunk:
                break
            payload += chunk
        if len(payload) > 1_048_576:
            raise ValueError("control line exceeds byte limit")
        return payload

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _address = self._listener.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                break
            with connection:
                with self._connection_lock:
                    self._connection = connection
                try:
                    if self._stop.is_set():
                        continue
                    payload = self._read_line(connection)
                    if self._stop.is_set():
                        continue
                    response = self._handle(payload)
                    connection.settimeout(2)
                    connection.sendall(
                        (json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8")
                    )
                except (OSError, ValueError):
                    pass
                finally:
                    with self._connection_lock:
                        self._connection = None

    def _handle(self, payload: bytes) -> dict:
        try:
            request = json.loads(payload.decode("utf-8"))
            expected = str(request["expected_turn_id"])
            thread_id = str(request["thread_id"])
            prompt = str(request["prompt"])
            attempt_id = request["attempt_id"]
            root_creation_time = request["root_creation_time"]
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
            return {"ok": False, "condition": "stale turn id", "message": str(exc)}
        if (not self.attempt_id or attempt_id != self.attempt_id
                or type(root_creation_time) is not int
                or root_creation_time != self.app_server.process.creation_time):
            return {"ok": False, "condition": "stale attempt", "message": "executor identity changed"}
        try:
            current = self.app_server.process.contains(
                self.app_server.process.pid, root_creation_time
            )
        except OSError:
            current = False
        if not current:
            return {"ok": False, "condition": "stale attempt", "message": "owned root is not live"}
        if thread_id != self.app_server.thread_id or expected != self.app_server.turn_id:
            return {
                "ok": False,
                "condition": "stale turn id",
                "message": "active turn handle changed before delivery",
            }
        try:
            if 'answer_request' in request:
                result = self.app_server.answer_input(request['answer_request'], json.loads(prompt))
            else:
                result = self.app_server.steer(prompt, expected)
        except (RpcError, TimeoutError, OSError, ValueError) as exc:
            return {"ok": False, "condition": "delivery unconfirmed",
                    "message": str(exc) + "; inspect native input/result evidence before resending"}
        return {"ok": True, "turn_id": str(result.get("turnId") or expected)}

    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        with self._connection_lock:
            if self._connection is not None:
                try:
                    self._connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self._connection.close()
        if self._thread.ident is not None:
            self._thread.join(timeout=3)
            if self._thread.is_alive():
                raise RuntimeError("app-server control cleanup remains unknown")


def send_correction(handle: dict, prompt: str, timeout: float = 10, *, answer_request=None) -> dict:
    """Deliver one correction through the live dispatcher's loopback handle."""
    control = handle["control"]
    request = {
        "prompt": prompt,
        "expected_turn_id": handle["turn_id"],
        "thread_id": handle["thread_id"],
        "attempt_id": handle["attempt_id"],
        "root_creation_time": handle["root_creation_time"],
    }
    if answer_request is not None:
        request['answer_request'] = answer_request
    with socket.create_connection(
        (str(control["host"]), int(control["port"])), timeout=timeout
    ) as connection:
        connection.settimeout(timeout)
        connection.sendall(
            (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
        )
        payload = CorrectionServer._read_line(connection, timeout=timeout)
    response = json.loads(payload.decode("utf-8"))
    if not isinstance(response, dict) or type(response.get("ok")) is not bool:
        raise ValueError("control acknowledgment is incomplete or malformed")
    if response["ok"] and response.get("turn_id") != handle["turn_id"]:
        raise ValueError("control acknowledgment names a different turn")
    return response


def _write_handle(path: Path, handle: dict, *, update=False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path
    if update:
        current = json.loads(path.read_text(encoding='utf-8'))
        if any(current.get(key) != handle.get(key) for key in (
                'attempt_id', 'root_pid', 'root_creation_time', 'thread_id', 'turn_id')):
            raise ValueError('active handle changed before question publication')
        target = path.with_suffix('.' + handle['attempt_id'] + '.tmp')
    with target.open("x", encoding="utf-8") as stream:
        json.dump(handle, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    if update:
        os.replace(target, path)


def require_fresh_launch(out: Path, active_path: Path, log: Path) -> None:
    """Refuse a launch whose output, active-turn handle or terminal receipt already exists."""
    for path in (out, active_path, log.with_suffix('.terminal.json')):
        if path.exists():
            raise FileExistsError(str(path))


def remove_attempt_handle(path: Path, attempt_id: str, root_pid, root_creation_time) -> bool:
    """Remove the active-turn handle only when it names exactly this attempt and root."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return False
    if (isinstance(record, dict) and record.get("attempt_id") == attempt_id
            and record.get("root_pid") == root_pid
            and record.get("root_creation_time") == root_creation_time):
        path.unlink()
        return True
    return False


def _remove_own_handle(path: Path, attempt_id: str, process) -> None:
    remove_attempt_handle(path, attempt_id, process.pid, process.creation_time)


def run_executor(
    codex: str,
    body: str,
    worktree: Path,
    log: Path,
    out: Path,
    tag: str,
    active_path: Path,
    on_spawn: Callable[[int], None],
    *,
    lease: ExecutorLease,
    on_cleanup: Callable[[CleanupReceipt], None],
    on_turn: Callable[[dict], None] | None = None,
    resume_thread_id=None,
    timeout_seconds: float | None = None,
    app_server_policy=None,
    process_factory=OwnedProcess.spawn,
    model=None,
    effort=None,
    native_team=None,
    execution_policy=None,
    launch_context=None,
) -> tuple[int, int]:
    """Run under a caller-owned lease; return only after measured Job cleanup.

After reservation admission, on_cleanup is attempted exactly once on every
exit. A rejected external lease grants no cleanup authority or receipt. The
dispatcher persists admitted receipts and decides whether its lease can be
released. A completed model turn is never proof of process cleanup.
"""
    # Custody begins only after this owner's reservation is admitted. Keeping
    # this outside the cleanup try prevents a refusal from manufacturing a
    # no-spawn receipt for another attempt or an already running executor.
    lease.require_reserved(worktree, tag, {"app-server"})
    app: AppServer | None = None
    control: CorrectionServer | None = None
    process = None
    log_handle = None
    failure = None
    cleanup_errors = []
    receipt = None
    cleanup_elapsed = None
    cleanup_lock = threading.Lock()
    done, expired, stopped = threading.Event(), threading.Event(), threading.Event()
    watcher = None

    def drain_output():
        if app is not None:
            deadline = time.monotonic() + 2
            for thread in (app._stdout_thread, app._stderr_thread):
                if thread.ident is not None:
                    thread.join(timeout=max(0, deadline - time.monotonic()))

    def stop_owned():
        nonlocal receipt, cleanup_elapsed
        with cleanup_lock:
            if receipt is None:
                started = time.monotonic()
                try:
                    receipt = process.stop_and_verify(drain_seconds=0, kill_seconds=2,
                                                      drain_output=drain_output)
                    if (not receipt.cleanup_verified and receipt.job_active_processes == 0
                            and not receipt.root_exited):
                        # Windows Job accounting can reach zero before the root
                        # handle signals exit. Keep custody while that exit settles;
                        # this is cleanup observation, not a model-call timeout.
                        receipt = process.stop_and_verify(drain_seconds=30, kill_seconds=0,
                                                          drain_output=drain_output)
                except BaseException as error:
                    receipt = CleanupReceipt(
                        lease.attempt_id, process.pid, process.creation_time,
                        False, None, False, "cleanup failed: " + str(error),
                    )
                finally:
                    cleanup_elapsed = time.monotonic() - started
            return receipt

    def watch(deadline):
        try:
            while not done.is_set():
                if (app is not None and app._stdout_thread.ident is not None
                        and app._stderr_thread.ident is not None):
                    # The root can exit during any startup RPC or admitted turn.
                    # Reader EOF may still depend on ending its owned descendants.
                    with cleanup_lock:
                        root_exited = process.poll() is not None
                    if root_exited:
                        stop_owned()
                        return
                cause = app.expiration() if app is not None else (
                    'overall' if deadline is not None and time.monotonic() >= deadline else None)
                if cause:
                    expired.set()
                    if app is not None:
                        app.begin_stop(cause)
                        app.interrupt_and_wait()
                    stop_owned()
                    return
                done.wait(.01)
        finally:
            stopped.set()

    try:
        selection = compute_kwargs(model, effort)
        policy = normalize_policy(app_server_policy, timeout_seconds=timeout_seconds)
        deadline = None if policy['overall_seconds'] is None else time.monotonic() + policy['overall_seconds']
        from ._task_deadline import remaining
        task_left = remaining()
        if task_left is not None:
            deadline = min(deadline, time.monotonic() + task_left) if deadline is not None else time.monotonic() + task_left
        log.parent.mkdir(parents=True, exist_ok=True)
        require_fresh_launch(out, active_path, log)
        log_handle = log.open("x", encoding="utf-8", errors="replace")
        log_handle.write(json.dumps({
            "event": "executor_reserved", "attempt_id": lease.attempt_id,
            "worktree": str(worktree), "launcher": "app-server",
            **({"resume_thread_id": resume_thread_id} if resume_thread_id is not None else {}),
            "app_server_policy": policy,
            "requested_compute": selection or None,
            **team_kwargs(native_team),
        }, ensure_ascii=False) + "\n")
        log_handle.flush()
        os.fsync(log_handle.fileno())
        from ._task_deadline import check
        from ._launch_context import worker_environment
        check()
        process = process_factory(
            coding_argv(codex, "app-server", workspace=worktree, **selection, **team_kwargs(native_team), execution_policy=execution_policy, launch_context=launch_context), cwd=str(worktree),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cleanup_path=log.with_suffix('.cleanup.json'),
            attempt_id=lease.attempt_id, env=worker_environment(codex_env(codex), launch_context),
        )
        app = AppServer(process, log_handle, deadline=deadline, policy=policy)
        app.process_completion_wait = (native_team or {}).get('process_completion_wait') == 1
        app.live_events.residual.threshold = ((launch_context or {}).get('settings') or {}).get('compact_tokens')
        watcher = threading.Thread(target=watch, args=(deadline,), name="pdcl-app-server-deadline", daemon=True)
        watcher.start()
        lease.bind_process(process.pid, process.creation_time)
        on_spawn(process.pid)
        app.start_readers()
        app.start_turn(body, worktree,
                       **({"resume_thread_id": resume_thread_id}
                          if resume_thread_id is not None else {}),
                       **selection, **team_kwargs(native_team), execution_policy=execution_policy)
        binding = {"attempt_id": lease.attempt_id, "root_pid": process.pid,
                   "root_creation_time": process.creation_time,
                   "thread_id": app.thread_id, "turn_id": app.turn_id}
        if on_turn is not None:
            on_turn(binding)
        log_handle.write("session id: %s\n" % app.session_id)
        log_handle.flush()
        control = CorrectionServer(app, attempt_id=lease.attempt_id)
        control.start()
        handle = {
            "schema_version": "pdcl-active-turn-2",
            "launcher": "app-server",
            "tag": tag,
            "worktree": str(worktree),
            "attempt_id": lease.attempt_id,
            "owner_pid": lease._payload["owner_pid"],
            "owner_creation_time": lease._payload["owner_creation_time"],
            "root_pid": process.pid,
            "root_creation_time": process.creation_time,
            "thread_id": app.thread_id,
            "turn_id": app.turn_id,
            "control": {"host": control.host, "port": control.port},
            "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "commander_messages": str(Path(log).with_suffix('.messages.jsonl')),
        }
        _write_handle(active_path, handle)
        visible_questions = []
        while not app.outcome_ready.wait(.01):
            if hasattr(app, 'live_events'):
                app.live_events.drain(app)
            app.member_activity.publish(app, time.monotonic())
            questions = app.input_requests()
            if questions != visible_questions:
                visible_questions = questions
                _write_handle(active_path, {**handle, 'input_requests': questions}, update=True)
            if expired.is_set():
                break
        if expired.is_set() or app.expiration_cause:
            stopped.wait(policy['interrupt_grace_seconds'] + 3)
        app.terminal_observation()
    except SpawnFailure as error:
        process, receipt = error.process, error.cleanup
        failure = error
    except BaseException as error:
        failure = error
        if isinstance(error, TimeoutError) and app is not None:
            app.begin_stop(app.expiration() or 'rpc:startup')
            stopped.wait(policy['interrupt_grace_seconds'] + 3)
    finally:
        done.set()
        # Stop the child before joining control/RPC threads: they can be blocked
        # in a pipe write whose only bounded release is owned Job termination.
        if process is not None:
            stop_owned()
        else:
            receipt = CleanupReceipt(lease.attempt_id, None, None, True, 0, True,
                                     "no process was created")
        if watcher is not None and watcher.ident is not None:
            watcher.join(timeout=3)
            if watcher.is_alive():
                cleanup_errors.append(RuntimeError("deadline watcher cleanup remains unknown"))
        if control is not None:
            try:
                control.close()
            except BaseException as error:
                cleanup_errors.append(error)
        if app is not None:
            try:
                app.close()
            except BaseException as error:
                cleanup_errors.append(error)
            try:
                if hasattr(app, 'live_events'):
                    app.live_events.close(app)
            except BaseException as error:
                cleanup_errors.append(error)
            try:
                with out.open("x", encoding="utf-8") as stream:
                    stream.write(app.final_text())
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException as error:
                cleanup_errors.append(error)
        if process is not None and receipt.cleanup_verified and not cleanup_errors:
            try:
                _remove_own_handle(active_path, lease.attempt_id, process)
            except BaseException as error:
                cleanup_errors.append(error)
        if log_handle is not None:
            try:
                log_handle.write(json.dumps({
                    'event': 'executor_stopped',
                    'expiration_cause': app.expiration_cause if app is not None else None,
                    'interrupt_result': app.interrupt_result if app is not None else 'not_requested',
                    'cleanup_elapsed_seconds': cleanup_elapsed,
                }) + '\n')
                log_handle.flush()
                os.fsync(log_handle.fileno())
                log_handle.close()
            except BaseException as error:
                cleanup_errors.append(error)
        if cleanup_errors:
            receipt = replace(receipt, cleanup_verified=False,
                              reason=receipt.reason + "; transport cleanup: " +
                              "; ".join(str(error) for error in cleanup_errors))
        try:
            on_cleanup(receipt)
        except BaseException as error:
            cleanup_errors.append(error)
    if not receipt.cleanup_verified:
        raise TransportCleanupError(process, receipt) from failure
    if app is not None and app.expiration_cause:
        # A work-clock expiry is not a native result. Keep any trustworthy
        # terminal received during grace, even when completion beat interruption.
        try:
            app.terminal_observation()
        except UncertainExecution as error:
            raise TimeoutError('app-server ' + app.expiration_cause + ' expired; ' + str(error)) from failure
        if isinstance(failure, (TimeoutError, UncertainExecution)):
            failure = None
    if failure is not None:
        for error in cleanup_errors:
            failure.add_note("additional cleanup failure: " + str(error))
        raise failure
    if cleanup_errors:
        raise cleanup_errors[0]
    # Recheck the notifications observed while owned cleanup drained the readers;
    # root exit alone never establishes a native terminal result.
    terminal = app.terminal_observation()
    rc = 0 if terminal["params"]["turn"]["status"] == "completed" else 1
    _write_handle(log.with_suffix(".terminal.json"), {
        "schema_version": "pdcl-app-server-terminal-1", "binding": binding,
        "terminal": terminal, "returncode": rc, "returncode_source": "turn_status",
        "native_returncode": None,
        "app_server_policy": policy, "expiration_cause": app.expiration_cause,
        "interrupt_result": app.interrupt_result,
    })
    return rc, process.pid
