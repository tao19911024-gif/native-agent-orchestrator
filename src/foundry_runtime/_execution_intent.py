"""Dispatcher-owned replay protection, not a task/authorization database.

The caller supplies its already-validated canonical LINE and frozen authority.
All command worktrees share the Git common-dir runtime lock. The byte lock is
OS-released on crash, but immutable unresolved intent events still block replay.
No model, worker, PORTFOLIO reader, automatic stale cleanup, or CLI lives here.
"""
from __future__ import annotations

import copy
from dataclasses import asdict
from . import _recovery_authorization
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from ._executor_lifecycle import CleanupReceipt, ExecutorLease, _api, _creation, _local_ntfs
from ._app_server_terminal import validate_terminal
from ._execution_bindings import _bytes, _sha, _identity, _same_invocation, _frozen
from ._app_server_policy import normalize_policy


class IntentConflict(RuntimeError):
    """An existing event or binding prevents a safe new execution."""


class IntentBusy(IntentConflict):
    """Another live controller holds this task's runtime serialization lock."""


def _reference_bytes(reference, label, *, limit=8 * 1024 * 1024):
    if (not isinstance(reference, dict) or set(reference) != {"path", "sha256"}
            or not isinstance(reference["path"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(reference["sha256"]))):
        raise IntentConflict("invalid recovery evidence reference: " + label)
    path = Path(reference["path"])
    if not path.is_absolute():
        raise IntentConflict("recovery evidence paths must be absolute: " + label)
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
    except OSError as exc:
        raise IntentConflict("recovery evidence unavailable: " + label) from exc
    if len(raw) > limit:
        raise IntentConflict(f"recovery evidence exceeds {limit} byte read limit: {label}: {path}; "
                             "--session-header for app-server must name the intent's kind=turn_bound event JSON, not dispatch.log")
    if _sha(raw) != reference["sha256"]:
        raise IntentConflict("recovery evidence changed: " + label)
    return raw



def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_event(directory, task_key, intent_id, version, sequence, previous,
                 kind, state, data):
    row = {"schema_version": version, "task_key": task_key, "intent_id": intent_id,
           "sequence": sequence, "previous_sha256": previous,
           "created_utc": datetime.now(timezone.utc).isoformat(), "kind": kind,
           "state": state, "data": data}
    raw = _bytes(row)
    if len(raw) > 1024 * 1024:
        raise IntentConflict("intent event exceeds limit")
    with (directory / f"{sequence:08d}.json").open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return _sha(raw)


def _verify_evidence(evidence, cleanup=None, *, terminal=False):
    keys = {"log", "result", "cleanup"} | ({"terminal"} if terminal else set())
    if not isinstance(evidence, dict) or set(evidence) != keys:
        raise IntentConflict("completion needs explicit log/result/cleanup references")
    for kind, reference in evidence.items():
        if kind == "result" and reference is None:
            continue  # explicit missing result, not proof of successful repair
        if (not isinstance(reference, dict) or set(reference) != {"path", "sha256"}
                or not isinstance(reference["path"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", str(reference["sha256"]))):
            raise IntentConflict("invalid immutable evidence reference: " + kind)
        path = Path(reference["path"])
        if not path.is_absolute():
            raise IntentConflict("evidence paths must be absolute")
        try:
            if _digest(path) != reference["sha256"]:
                raise IntentConflict("evidence bytes changed: " + kind)
            if kind == "cleanup" and cleanup is not None:
                if path.stat().st_size > 64 * 1024 or json.loads(path.read_bytes()) != cleanup:
                    raise IntentConflict("cleanup reference does not carry this measured receipt")
        except (OSError, ValueError) as error:
            raise IntentConflict("evidence unavailable: " + kind) from error


def _verify_claude_terminal(record, evidence, returncode):
    from ._dispatch_claude import verify_terminal
    try:
        proof = json.loads(Path(evidence['terminal']['path']).read_bytes())
        verify_terminal(proof, session=record['session_id'], returncode=returncode,
                        attempt_id=record['attempt_id'])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise IntentConflict('invalid Claude terminal evidence: ' + str(error)) from error


def _verify_terminal(record, evidence, returncode):
    """Compare receipt semantics to prior admission, independently of file hash."""
    try:
        proof = json.loads(Path(evidence['terminal']['path']).read_bytes())
        binding = record['turn_binding']
        if proof['schema_version'] != 'pdcl-app-server-terminal-1' or proof['binding'] != binding:
            raise ValueError('terminal receipt differs from admitted turn binding')
        if 'app_server_policy' in record['frozen']:
            if (proof.get('app_server_policy') != record['frozen']['app_server_policy']
                    or normalize_policy(proof['app_server_policy']) != record['frozen']['app_server_policy']):
                raise ValueError('terminal policy differs from frozen intent')
        cleanup = record['cleanup']
        if (cleanup.get('cleanup_verified') is not True or cleanup.get('root_exited') is not True
                or type(cleanup.get('job_active_processes')) is not int or cleanup['job_active_processes'] != 0):
            raise ValueError('terminal execution cleanup is unverified')
        for key in ('attempt_id', 'root_pid', 'root_creation_time'):
            if binding[key] != record[key] or binding[key] != record['cleanup'][key]:
                raise ValueError('terminal/cleanup attempt identity mismatch')
        terminal = proof['terminal']
        status = validate_terminal(terminal, binding['thread_id'], binding['turn_id'])
        if (type(returncode) is not int or returncode != (0 if status == 'completed' else 1)
                or type(proof['returncode']) is not int or proof['returncode'] != returncode
                or proof['returncode_source'] != 'turn_status' or proof['native_returncode'] is not None):
            raise ValueError('terminal receipt identity/status/result mismatch')
        return status
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise IntentConflict('invalid bound terminal evidence: ' + str(error)) from error


class ExecutionIntent:
    @classmethod
    def open(cls, common_dir: Path, task_key: str, frozen: dict, *, intent_id=None,
             recovery=None, read_only=False, source_verifier=None):
        return cls(common_dir, task_key, frozen, intent_id=intent_id,
                   recovery=recovery, read_only=read_only, source_verifier=source_verifier)

    def __init__(self, common_dir, task_key, frozen, *, intent_id=None, recovery=None,
                 read_only=False, source_verifier=None):
        if not isinstance(task_key, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9_.-]{0,63}", task_key):
            raise ValueError("task_key must be the canonical uppercase LINE identity")
        self.common_dir = _local_ntfs(Path(common_dir))
        self.task_key, self._frozen = task_key, _frozen(frozen)
        self.intent_id = _identity(task_key, self._frozen)
        self._legacy_id = _identity(task_key, self._frozen, generation=1)
        self._expected_id = intent_id
        if intent_id is not None and intent_id not in {self.intent_id, self._legacy_id}:
            raise IntentConflict("explicit intent identity does not match frozen authority/inputs")
        if intent_id is not None:
            self.intent_id = intent_id
        # Canonical LINE is already path-safe; prefix avoids Windows device names.
        # Do not duplicate a 64-byte digest above the full execution identity.
        self._task_dir = self.common_dir / "pdcl-dispatch" / ("task-" + task_key)
        self.path = self._task_dir / "intents" / self.intent_id
        self._handle, self._locked, self._poisoned = None, False, False
        self._record, self._sequence, self._last_hash = {}, 0, None
        self._history_version = 2 if self._frozen.get('launcher') == 'app-server' else 1
        self._read_only = read_only
        if recovery is not None and (not isinstance(recovery, _recovery_authorization.RecoveryAuthorization)
                                     or self._frozen.get("operation") != "resume"):
            raise IntentConflict("recovery requires a validated resume authorization")
        if recovery is not None and not read_only:
            recovery.require_live_execution()
        self.source_verifier = source_verifier or (recovery.source_verifier if recovery else None)
        self._recovery = recovery
        self._recovery_predecessor = None
        if recovery is not None:
            recovery.revalidate()
            target = recovery.data['successor']
            if (recovery.data['task_key'] != task_key
                    or self._frozen.get('launcher') != recovery.launcher
                    or self._frozen['session_id'] != target['session_id']
                    or self._frozen['worktree'] != str(_local_ntfs(Path(target['worktree'])))
                    or self._frozen['base_commit'] != target['candidate_head']
                    or self._frozen['order_sha256'] != target['amendment_sha256']):
                raise IntentConflict('actual recovery successor differs from authorized bindings; expected '
                    f"--launcher {recovery.launcher} --resume {target['session_id']} --worktree {target['worktree']} "
                    'and the exact authorized --prompt-file content and candidate head')

    def _acquire(self):
        import msvcrt

        if self._handle is not None:
            raise IntentConflict("execution context cannot be entered twice")
        self._task_dir.mkdir(parents=True, exist_ok=True)
        self._handle = (self._task_dir / "owner.lock").open("a+b")
        try:
            if self._handle.seek(0, 2) == 0:
                self._handle.write(b"\0")
                self._handle.flush()
            self._handle.seek(0)
            try:
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise IntentBusy("task already held: " + self.task_key) from error
            self._locked = True
        except BaseException:
            self.__exit__(None, None, None)
            raise

    @classmethod
    def settle_abandoned(cls, common_dir, task_key, frozen, *, intent_id, output, close_reason=None):
        """Measure one existing attempt under its mutex; never admit execution."""
        intent = cls(common_dir, task_key, frozen, intent_id=intent_id)
        try:
            intent._acquire()
            record, intent._sequence, intent._last_hash = intent._read(intent.path)
            if record['frozen'] != intent.frozen:
                raise IntentConflict('intent frozen bindings changed')
            intent._record = record
            intent._history_version = record['_history_version']
            if close_reason is not None:
                if not isinstance(close_reason, str) or not close_reason.strip():
                    raise ValueError('closing an abandoned intent requires a reason')
                if intent.state == 'closed':
                    return intent.record
                if 'recovery_authorization' in record or 'recovery_consumption' in record:
                    raise IntentConflict('intent already has a recovery successor; close that attempt instead')
            intent.reconcile_abandoned(output)
            if close_reason is not None:
                intent._append('close', 'closed', {'closure_reason': close_reason.strip(),
                    'closure_basis': 'operator ended the task; execution outcome remains unknown'})
            return intent.record
        finally:
            intent.__exit__(None, None, None)

    @classmethod
    def settle_completed_turn(cls, common_dir, task_key, frozen, *, intent_id, log):
        """Explicit recovery reconciles a retained terminal under the original mutex."""
        intent = cls(common_dir, task_key, frozen, intent_id=intent_id)
        try:
            intent._acquire()
            record, intent._sequence, intent._last_hash = intent._read(intent.path)
            if record['frozen'] != frozen:
                raise IntentConflict('intent frozen bindings changed')
            intent._record, intent._history_version = record, record['_history_version']
            if intent.state == 'completed':
                intent._verify_completion(record)
                return intent.record
            from ._recovery_reconciliation import settle_terminal
            return settle_terminal(intent, Path(log))
        finally:
            intent.__exit__(None, None, None)

    def __enter__(self):
        self._acquire()
        try:
            histories = self._task_dir / "intents"
            histories.mkdir(exist_ok=True)
            legacy = None
            predecessor = None
            for path in sorted(histories.iterdir()):
                record, sequence, last_hash = self._read(path)
                if path == self.path:
                    if record["frozen"] != self.frozen:
                        raise IntentConflict("intent frozen bindings changed")
                    self._record, self._sequence, self._last_hash = record, sequence, last_hash
                    self._history_version = record['_history_version']
                elif (self._recovery is not None
                      and path.name == self._recovery.data["predecessor"]["intent_id"]):
                    predecessor = path, record, sequence, last_hash
                elif record["state"] in {"starting", "active", "unknown"}:
                    if not self._completed_recovery(path, record):
                        raise IntentConflict("unresolved intent for this task blocks another worktree: " + path.name)
                    if _same_invocation(record['frozen'], self._frozen):
                        raise IntentConflict('recovered invocation grants no replay; provide a new amendment')
                elif ('app_server_policy' not in record['frozen']
                      and 'app_server_policy' in self._frozen
                      and record['frozen'] == {k: v for k, v in self._frozen.items() if k != 'app_server_policy'}):
                    if record['state'] != 'completed' or self._expected_id is not None:
                        raise IntentConflict('policy-less history grants no new execution or explicit identity')
                    if legacy is None or record['_history_version'] > legacy[1]['_history_version']:
                        legacy = path, record, sequence, last_hash
                elif (path.name == self._legacy_id and record['_history_version'] == 1
                      and record['frozen'] == self.frozen):
                    legacy = path, record, sequence, last_hash
                elif record["state"] == "completed" and _same_invocation(record["frozen"], self._frozen):
                    raise IntentConflict(
                        "completed invocation has different frozen inputs; provide a new amendment "
                        "instead of replaying the same instructions: " + path.name)
                elif (_same_invocation(record["frozen"], self._frozen)
                      and record['frozen'].get('app_server_policy') != self._frozen.get('app_server_policy')):
                    raise IntentConflict('app-server policy changed for this invocation; use a new authorized run')
                elif (_same_invocation(record["frozen"], self._frozen)
                      and record["frozen"].get("requested_compute") != self._frozen.get("requested_compute")):
                    # A no-child receipt permits retry, not a replacement assignment.
                    raise IntentConflict("requested compute changed for this invocation; use a new authorized run")
            # Scan every history before selecting compatibility output: an old
            # completed record cannot hide unresolved modern work later in sort order.
            if self._read_only and self._record.get('state') != 'completed':
                raise IntentConflict('read-only recovery requires an existing completed successor')
            if self._recovery is not None:
                self._recovery.revalidate()
                self._admit_recovery(predecessor)
            elif legacy is not None and not self._record:
                if self._expected_id is not None:
                    raise IntentConflict('explicit intent identity has only legacy history')
                self.path, self._record, self._sequence, self._last_hash = legacy
                self.intent_id, self._history_version = self.path.name, self._record['_history_version']
                self._frozen = copy.deepcopy(self._record['frozen'])
            elif legacy is not None and self._record['state'] == 'not_started':
                raise IntentConflict('legacy history grants no new modern execution')
            if not self._record:
                if self._expected_id == self._legacy_id and self._frozen.get('launcher') == 'app-server':
                    raise IntentConflict('absent legacy identity grants no execution')
                self.path.mkdir(exist_ok=False)
                try:
                    data = {"frozen": self.frozen, "attempt_id": None,
                            "root_pid": None, "root_creation_time": None}
                    if self._recovery is not None:
                        data.update(
                            recovery_predecessor_intent_id=self._recovery.data["predecessor"]["intent_id"],
                            recovery_decision_sha256=self._recovery.decision_sha256,
                        )
                    self._append("prepared", "not_started", data)
                except BaseException:
                    # This controller just created it under the LINE mutex, before
                    # caller handoff. rmdir only succeeds if no event was created.
                    try:
                        self.path.rmdir()
                    except OSError:
                        pass  # partial/uncertain history stays visible and blocking
                    raise
            if self._record["state"] == "completed":
                self._verify_completion(self._record)
            if self._record['state'] == 'closed':
                raise IntentConflict('intent was closed without execution; use a new task or amendment')
            if self._recovery is not None:
                self._verify_recovery_link()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_args):
        import msvcrt

        if self._handle is not None:
            try:
                if self._locked:
                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self._handle.close()
                self._handle, self._locked = None, False

    def _require(self):
        if not self._locked or self._poisoned:
            raise IntentConflict("intent context is closed or durable state is uncertain")

    @property
    def frozen(self):
        return copy.deepcopy(self._frozen)

    @property
    def state(self):
        self._require()
        return self._record["state"]

    @property
    def record(self):
        self._require()
        return copy.deepcopy(self._record)

    def _read(self, directory, *, task_key=None):
        return self.read_history(directory, task_key=self.task_key if task_key is None else task_key)

    @staticmethod
    def recovery_spent_without_start(intents_dir, consumption, *, task_key):
        """True when this consumption's successor attempt ended not_started with no process.

        Such a consumption produced no execution, so the same authorization may be consumed
        again by the same successor. Judged from the successor's history, not its current
        state, so the answer does not change once a later attempt runs.
        """
        if not isinstance(consumption, dict) or not re.fullmatch(
                r"[0-9a-f]{64}", str(consumption.get('successor_intent_id'))):
            return False
        successor_dir = Path(intents_dir) / consumption['successor_intent_id']
        ExecutionIntent.read_history(successor_dir, task_key=task_key)
        started = False
        for event in sorted(successor_dir.iterdir()):
            row = json.loads(event.read_bytes())
            if row.get('kind') == 'start' and row.get('data', {}).get('attempt_id') == consumption.get('attempt_id'):
                started = True
            cleanup = row.get('data', {}).get('cleanup')
            if (row.get('kind') == 'not_started' and isinstance(cleanup, dict)
                    and cleanup.get('attempt_id') == consumption.get('attempt_id')
                    and cleanup.get('root_pid') is None and cleanup.get('root_creation_time') is None
                    and cleanup.get('root_exited') is True and cleanup.get('cleanup_verified') is True
                    and cleanup.get('job_active_processes') == 0):
                return True
        # Consumption can precede a failed admission with no start event. Test
        # this attempt's whole retained history, not the successor's current state:
        # the result must remain true after a later authorized attempt starts.
        if not started and isinstance(consumption.get('attempt_id'), str):
            from ._executor_lifecycle import process_identity_exited
            pid, created = consumption.get('owner_pid'), consumption.get('owner_creation_time')
            return (type(pid) is int and pid > 0 and type(created) is int and created > 0
                    and process_identity_exited(pid, created))
        return False

    @staticmethod
    def read_history(directory, *, task_key):
        """Read-only projection using the same chain checks as execution admission."""
        if not directory.is_dir() or not re.fullmatch(r"[0-9a-f]{64}", directory.name):
            raise IntentConflict("unrecognized intent history entry: " + str(directory))
        paths = sorted(directory.iterdir())
        if not paths or len(paths) > 4096:
            raise IntentConflict("empty or oversized intent history: " + str(directory))
        record, previous, version = {}, None, None
        transitions = {"prepared": ({None}, "not_started"),
                       "start": ({"not_started"}, "starting"),
                       "active": ({"starting"}, "active"),
                       "turn_bound": ({"active"}, "active"),
                       "not_started": ({"starting"}, "not_started"),
                       "unknown": ({"starting", "active", "unknown"}, "unknown"),
                       "finish": ({"active", "unknown"}, "completed"),
                       "close": ({"unknown"}, "closed"),
                       "recovery_authorized": ({"unknown"}, "unknown"),
                       "recovery_consumed": ({"unknown"}, "unknown")}
        for sequence, path in enumerate(paths, 1):
            try:
                if path.name != f"{sequence:08d}.json" or path.stat().st_size > 1024 * 1024:
                    raise IntentConflict("noncontiguous or oversized intent event")
                raw = path.read_bytes()
                row = json.loads(raw)
                if sequence == 1:
                    version = row.get('schema_version') if isinstance(row, dict) else None
                if (not isinstance(row, dict) or type(version) is not int or version not in (1, 2)
                        or row.get("schema_version") != version
                        or row.get("task_key") != task_key or row.get("intent_id") != directory.name
                        or row.get("sequence") != sequence or row.get("previous_sha256") != previous
                        or row.get("kind") not in transitions or not isinstance(row.get("data"), dict)):
                    raise IntentConflict("invalid intent event envelope")
                prior_states, next_state = transitions[row["kind"]]
                if record.get("state") not in prior_states or row.get("state") != next_state:
                    raise IntentConflict("illegal durable intent transition")
                if row['kind'] != 'turn_bound' and 'turn_binding' in row['data']:
                    raise IntentConflict('turn binding can only originate in turn admission')
                if row['kind'] == 'turn_bound':
                    ExecutionIntent._check_turn_binding(record, row['data'].get('turn_binding'))
                elif row['kind'] == 'recovery_authorized':
                    if (set(row['data']) != {'recovery_authorization'}
                            or 'recovery_authorization' in record):
                        raise IntentConflict('invalid or repeated recovery authorization')
                elif row['kind'] == 'recovery_consumed':
                    if (set(row['data']) != {'recovery_consumption'}
                            or 'recovery_authorization' not in record
                            or ('recovery_consumption' in record and not ExecutionIntent.recovery_spent_without_start(
                                directory.parent, record['recovery_consumption'], task_key=task_key))):
                        raise IntentConflict('invalid or repeated recovery consumption')
                elif row['kind'] == 'close':
                    if (not isinstance(row['data'].get('closure_reason'), str)
                            or not row['data']['closure_reason'].strip()
                            or not record.get('cleanup', {}).get('cleanup_verified')
                            or record['cleanup'].get('root_exited') is not True
                            or record['cleanup'].get('job_active_processes') != 0):
                        raise IntentConflict('closed intent lacks measured cleanup or a reason')
                record.update(row["data"])
                record.update(state=next_state, task_key=task_key, intent_id=directory.name)
                record['_history_version'] = version
                if sequence == 1 and _identity(task_key, record.get("frozen"), generation=version) != directory.name:
                    raise IntentConflict("intent history frozen identity mismatch")
                previous = _sha(raw)
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise IntentConflict("unreadable/partial intent history: " + str(path)) from error
        return record, len(paths), previous

    @staticmethod
    def _verify_completion(record):
        modern = record['_history_version'] == 2
        _verify_evidence(record['evidence'], record['cleanup'], terminal=modern or record.get('launcher') == 'claude')
        if record.get('launcher') == 'claude':
            _verify_claude_terminal(record, record['evidence'], record.get('returncode'))
        if modern:
            status = _verify_terminal(record, record['evidence'], record.get('returncode'))
            if (record.get('returncode_source') != 'turn_status' or 'native_returncode' not in record
                    or record['native_returncode'] is not None or record.get('outcome') != 'turn_' + status):
                raise IntentConflict('missing or contradictory terminal result classification')
            record['terminal_evidence_class'] = 'bound_app_server_turn'
        elif record.get('launcher') == 'app-server' or record['frozen'].get('launcher') == 'app-server':
            # Historical interpretation is a read view, never rewritten evidence
            # or authority to execute. Old mapped codes were not native exit proof.
            record['terminal_evidence_class'] = 'legacy_unbound_app_server'
            record['returncode_source'] = 'legacy_app_server_mapping'
            record['native_returncode'] = None

    def _append(self, kind, state, data):
        self._require()
        if self._read_only:
            raise IntentConflict('read-only intent cannot append events')
        try:
            last_hash = _write_event(self.path, self.task_key, self.intent_id,
                                     self._history_version, self._sequence + 1, self._last_hash,
                                     kind, state, data)
        except BaseException:
            self._poisoned = True
            raise  # partial file remains visible and blocking; never overwrite it
        self._sequence += 1
        self._last_hash = last_hash
        self._record.update(copy.deepcopy(data))
        self._record.update(state=state, task_key=self.task_key, intent_id=self.intent_id)
        self._record['_history_version'] = self._history_version

    def _admit_recovery(self, predecessor):
        if predecessor is None:
            raise IntentConflict("recovery predecessor history is absent")
        path, record, sequence, last_hash = predecessor
        expected = self._recovery.data["predecessor"]
        hashes = expected["event_sha256s"]
        paths = sorted(path.glob("*.json"))
        if len(paths) < len(hashes):
            raise IntentConflict("recovery predecessor history is incomplete")
        for name, digest in hashes.items():
            event = path / name
            if not event.is_file() or _digest(event) != digest:
                raise IntentConflict("recovery predecessor event changed: " + name)
        tail_kinds = [json.loads(event.read_bytes())['kind'] for event in paths[len(hashes):]]
        if tail_kinds[:1] not in ([], ['recovery_authorized']) or any(
                kind != 'recovery_consumed' for kind in tail_kinds[1:]):
            raise IntentConflict('recovery decision omits later original history')
        origin = record['frozen']
        successor = self._recovery.data['successor']
        allowed_launcher = (self._recovery.launcher if self._recovery.launcher in {'app-server', 'claude'} else
                            {'dispatch': 'exec', 'resume': 'exec-resume'}.get(origin['operation']))
        if (origin.get('launcher') != allowed_launcher or record.get('launcher') != allowed_launcher
                or (allowed_launcher == 'app-server' and
                    (record.get('turn_binding', {}).get('thread_id') != successor['session_id']
                     or origin.get('app_server_policy') != self.frozen.get('app_server_policy')))
                or origin['repository_id'] != self.frozen['repository_id']
                or origin['worktree'] != str(_local_ntfs(Path(successor['worktree'])))
                or any(bound is not None and bound != successor['session_id']
                       for bound in (origin.get('session_id'), record.get('session_id')))):
            raise IntentConflict('recovery predecessor execution/session/worktree is outside grant')
        try:
            original_tail = json.loads((path / list(hashes)[-1]).read_bytes())
        except (OSError, ValueError) as exc:
            raise IntentConflict("recovery unknown event is unreadable") from exc
        cleanup = record.get("cleanup")
        if (original_tail.get("kind") != "unknown" or original_tail.get("state") != "unknown"
                or record.get("state") != "unknown" or "returncode" in record
                or record.get("attempt_id") != expected["attempt_id"]
                or record.get("root_pid") != expected["root_pid"]
                or record.get("root_creation_time") != expected["root_creation_time"]
                or not isinstance(cleanup, dict) or cleanup.get("cleanup_verified") is not True
                or cleanup.get("root_exited") is not True or cleanup.get("job_active_processes") != 0
                or cleanup.get("attempt_id") != expected["attempt_id"]
                or cleanup.get("root_pid") != expected["root_pid"]
                or cleanup.get("root_creation_time") != expected["root_creation_time"]):
            raise IntentConflict("recovery predecessor is not the exact cleaned unknown attempt")
        try:
            if json.loads(_reference_bytes(expected["cleanup"], "cleanup", limit=64 * 1024)) != cleanup:
                raise IntentConflict("recovery cleanup evidence differs from unknown event")
        except (UnicodeError, ValueError) as exc:
            raise IntentConflict("recovery cleanup evidence is invalid") from exc
        authorization = {
            "decision_sha256": self._recovery.decision_sha256,
            "decision": self._recovery.data,
            "successor_intent_id": self.intent_id,
            "successor_frozen_sha256": _sha(_bytes(self.frozen)),
        }
        if self._recovery.data['schema_version'] != 'pdcl-execution-recovery-1':
            authorization['decision_reference'] = {
                'path': str(self._recovery.path), 'sha256': self._recovery.decision_sha256}
        existing = record.get("recovery_authorization")
        created = False
        if existing is None:
            if self._read_only:
                raise IntentConflict('read-only recovery cannot authorize a predecessor')
            if self._record:
                raise IntentConflict("recovery successor exists without predecessor authorization")
            self._recovery.require_execution_source()
            try:
                last_hash = _write_event(path, self.task_key, expected["intent_id"],
                                         record["_history_version"], sequence + 1, last_hash,
                                         "recovery_authorized", "unknown",
                                         {"recovery_authorization": authorization})
            except BaseException as exc:
                raise IntentConflict("recovery authorization persistence is uncertain") from exc
            sequence += 1
            record["recovery_authorization"] = copy.deepcopy(authorization)
            created = True
        elif existing != authorization:
            raise IntentConflict("recovery authorization or successor linkage changed")
        if not self._record and not created:
            raise IntentConflict("recovery authorization has no durable successor linkage")
        self._recovery_predecessor = [path, record, sequence, last_hash]

    def _verify_recovery_link(self, *, require_completed=True):
        record = self._record
        expected = self._recovery.data["predecessor"]["intent_id"]
        if (record.get("recovery_predecessor_intent_id") != expected
                or record.get("recovery_decision_sha256") != self._recovery.decision_sha256):
            raise IntentConflict("recovery successor linkage is missing or changed")
        consumption = self._recovery_predecessor[1].get("recovery_consumption")
        if (consumption is not None and record["state"] == "not_started"
                and ExecutionIntent.recovery_spent_without_start(
                    self._recovery_predecessor[0].parent, consumption, task_key=self.task_key)):
            consumption = None  # that consumption launched nothing; the successor awaits a real one
        if consumption is not None:
            expected_link = {"decision_sha256": self._recovery.decision_sha256,
                             "successor_intent_id": self.intent_id,
                             **{key: record.get(key) for key in (
                                 'attempt_id', 'owner_pid', 'owner_creation_time')}}
            if not isinstance(consumption, dict) or consumption != expected_link:
                raise IntentConflict("recovery was consumed with changed linkage")
            if require_completed and record["state"] != "completed":
                raise IntentConflict("recovery was consumed without a completed successor")
        elif record["state"] != "not_started":
            raise IntentConflict("recovery successor advanced without durable consumption")

    def consume_recovery(self, lease: ExecutorLease):
        if self._recovery is not None:
            self._recovery.require_live_execution()
        if self._read_only or self._recovery is None or self.state != "not_started":
            raise IntentConflict("only an unconsumed recovery successor can be consumed")
        self._recovery.require_execution_source()
        lease.require_reserved(Path(self.frozen["worktree"]), self.task_key, {self._recovery.launcher})
        path = self._recovery_predecessor[0]
        record, sequence, last_hash = self._read(path)
        self._admit_recovery((path, record, sequence, last_hash))
        self._verify_recovery_link()
        path, record, sequence, last_hash = self._recovery_predecessor
        if record.get("recovery_consumption") is not None and not ExecutionIntent.recovery_spent_without_start(
                path.parent, record["recovery_consumption"], task_key=self.task_key):
            raise IntentConflict("recovery authorization was already consumed")
        row = lease._payload
        consumption = {"decision_sha256": self._recovery.decision_sha256,
                       "successor_intent_id": self.intent_id,
                       "attempt_id": row["attempt_id"], "owner_pid": row["owner_pid"],
                       "owner_creation_time": row["owner_creation_time"]}
        try:
            last_hash = _write_event(path, self.task_key,
                                     self._recovery.data["predecessor"]["intent_id"],
                                     record["_history_version"], sequence + 1, last_hash,
                                     "recovery_consumed", "unknown",
                                     {"recovery_consumption": consumption})
        except BaseException as exc:
            self._poisoned = True
            raise IntentConflict("recovery consumption persistence is uncertain") from exc
        record["recovery_consumption"] = copy.deepcopy(consumption)
        self._recovery_predecessor = [path, record, sequence + 1, last_hash]

    def _same_controller(self):
        api = _api()
        if (self._record.get("owner_pid") != os.getpid()
                or self._record.get("owner_creation_time") != _creation(api, api.GetCurrentProcess())):
            raise IntentConflict("a restarted controller cannot assert the old attempt never spawned")

    def _completed_recovery(self, path, record):
        from ._recovery_reconciliation import completed_recovery
        return completed_recovery(self, path, record)

    def acquire_lease(self, worktree, tag, launcher):
        """Reserve through the existing lease, enriching only an occupied refusal."""
        try:
            return ExecutorLease.acquire(worktree, tag, launcher)
        except FileExistsError as exc:
            try:
                lock = json.loads((Path(worktree) / '.pdcl-executor.lock').read_bytes())
                key = lock['tag']
                if not re.fullmatch(r'[A-Z0-9][A-Z0-9_.-]{0,63}', key):
                    raise ValueError('invalid holder LINE')
                for path in (self._task_dir.parent / ('task-' + key) / 'intents').glob('*'):
                    record, _, _ = self._read(path, task_key=key)
                    if (record.get('attempt_id') == lock.get('attempt_id')
                            and all(record.get(k) == lock.get(k) for k in
                                    ('owner_pid', 'owner_creation_time', 'root_pid', 'root_creation_time'))):
                        raise self._predecessor_conflict('executor worktree lock is occupied', path, record)
            except IntentConflict:
                raise
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise IntentConflict('executor worktree lock is occupied; holder evidence unreadable: ' + str(error)) from exc
            raise IntentConflict('executor worktree lock is occupied; no matching retained intent; inspect ' + str(worktree)) from exc

    @staticmethod
    def _predecessor_conflict(reason, path, record):
        from .recovery import blocker_advice
        return IntentConflict(reason + '\n' + json.dumps(blocker_advice(path, record), ensure_ascii=False))

    def _require_resolved_worktree_history(self):
        # Called only after acquiring this worktree's existing exclusive lease.
        # A different LINE must not hide effects left unresolved on the same tree.
        # Recheck same-task reconciliations too: gate work may have changed their
        # external evidence since __enter__. Explicit recovery keeps its own
        # consumed-predecessor checks immediately above this call in start().
        if self._recovery is None:
            for path in sorted((self._task_dir / 'intents').iterdir()):
                if path == self.path:
                    continue
                record, _, _ = self._read(path)
                if (record['state'] in {'starting', 'active', 'unknown'}
                        and not self._completed_recovery(path, record)):
                    raise self._predecessor_conflict('unresolved intent for this task before start: ' + path.name, path, record)
        target = os.path.normcase(self.frozen["worktree"])
        for task in sorted(self._task_dir.parent.glob("task-*")):
            if task == self._task_dir:
                continue
            key = task.name[5:]
            if not re.fullmatch(r"[A-Z0-9][A-Z0-9_.-]{0,63}", key):
                raise IntentConflict("unrecognized task history: " + str(task))
            for directory in sorted((task / "intents").glob("*")):
                first = directory / "00000001.json"
                try:
                    if first.stat().st_size > 1024 * 1024:
                        raise ValueError("oversized first event")
                    row = json.loads(first.read_bytes())
                    frozen = row["data"]["frozen"]
                    if (row.get("task_key") != key or row.get("intent_id") != directory.name
                            or row.get("sequence") != 1 or row.get("previous_sha256") is not None
                            or row.get("kind") != "prepared" or row.get("state") != "not_started"
                            or row.get("schema_version") not in (1, 2)
                            or _identity(key, frozen, generation=row['schema_version']) != directory.name):
                        raise ValueError("invalid first event identity")
                    worktree = frozen["worktree"]
                    if not isinstance(worktree, str) or not os.path.isabs(worktree):
                        raise ValueError("invalid worktree identity")
                except (OSError, ValueError, KeyError, TypeError) as error:
                    raise IntentConflict("cannot establish worktree of prior history: " + str(directory)) from error
                if os.path.normcase(worktree) != target:
                    continue  # An unrelated tree's live/partial tail is not our blocker.
                record, _, _ = self._read(directory, task_key=key)
                if record["state"] in {"starting", "active", "unknown"}:
                    if not self._completed_recovery(directory, record):
                        raise self._predecessor_conflict("unresolved history for this worktree under another LINE: " + key, directory, record)

    def start(self, lease: ExecutorLease):
        if self._recovery is not None:
            self._recovery.require_live_execution()
        if self.state != "not_started":
            raise IntentConflict("existing attempt is completed or unresolved; do not replay it")
        allowed = ({"claude"} if self.frozen.get("launcher") == "claude" else {"app-server"} if self.frozen["operation"] == "resume"
                   and self.frozen.get("launcher") == "app-server" else
                   {"exec-resume"} if self.frozen["operation"] == "resume" else
                   {"exec", "app-server"})
        if lease._payload['launcher'] == 'app-server' and self._history_version != 2:
            raise IntentConflict('legacy app-server history is read-only; it grants no execution')
        if lease._payload['launcher'] == 'app-server' and 'app_server_policy' not in self.frozen:
            raise IntentConflict('policy-less app-server history is read-only; it grants no execution')
        lease.require_reserved(Path(self.frozen["worktree"]), self.task_key, allowed)
        if self._recovery is not None:
            consumed = self._recovery_predecessor[1].get("recovery_consumption")
            row = lease._payload
            if (not isinstance(consumed, dict) or consumed.get("attempt_id") != row["attempt_id"]
                    or consumed.get("owner_pid") != row["owner_pid"]
                    or consumed.get("owner_creation_time") != row["owner_creation_time"]):
                raise IntentConflict("recovery must be durably consumed by this controller before spawn")
        self._require_resolved_worktree_history()
        if "launcher" in self.frozen and lease._payload["launcher"] != self.frozen["launcher"]:
            raise IntentConflict("launcher differs from frozen intent")
        self._append("start", "starting", {
            "attempt_id": lease.attempt_id, "owner_pid": lease._payload["owner_pid"],
            "owner_creation_time": lease._payload["owner_creation_time"],
            "root_pid": None, "root_creation_time": None, "session_id": self.frozen["session_id"],
            "worktree_lock": str(lease.path), "launcher": lease._payload["launcher"],
        })

    def active(self, lease: ExecutorLease, session_id=None):
        if self.state != "starting":
            raise IntentConflict("only a durably starting attempt can become active")
        self._same_controller()
        row = lease._payload
        if (not lease._ours() or row["attempt_id"] != self._record["attempt_id"]
                or row["owner_pid"] != self._record["owner_pid"]
                or row["owner_creation_time"] != self._record["owner_creation_time"]
                or row["worktree"] != self.frozen["worktree"] or row["tag"] != self.task_key
                or row["state"] != "running" or not row["root_pid"] or not row["root_creation_time"]):
            raise IntentConflict("native root is not bound to the reserved attempt")
        if self.frozen["session_id"] is not None and session_id not in (None, self.frozen["session_id"]):
            raise IntentConflict("resume session identity changed")
        self._append("active", "active", {"root_pid": row["root_pid"],
                     "root_creation_time": row["root_creation_time"],
                     "session_id": session_id or self.frozen["session_id"]})

    @staticmethod
    def _check_turn_binding(record, binding):
        keys = {'attempt_id', 'root_pid', 'root_creation_time', 'thread_id', 'turn_id'}
        if (record.get('_history_version') != 2 or record.get('launcher') != 'app-server'
                or 'turn_binding' in record or not isinstance(binding, dict) or set(binding) != keys
                or any(binding[k] != record[k] for k in ('attempt_id', 'root_pid', 'root_creation_time'))
                or any(not isinstance(binding[k], str) or not binding[k].strip()
                       for k in ('thread_id', 'turn_id'))):
            raise IntentConflict('invalid or repeated admitted turn binding')

    def bind_turn(self, binding):
        if self.state != 'active':
            raise IntentConflict('turn admission requires an active native root')
        self._same_controller()
        self._check_turn_binding(self._record, binding)
        self._append('turn_bound', 'active', {'turn_binding': binding})

    def not_started(self, cleanup: CleanupReceipt):
        if self.state != "starting":
            raise IntentConflict("only the starting controller may establish no-spawn")
        self._same_controller()
        if (cleanup.attempt_id != self._record["attempt_id"] or cleanup.root_pid is not None
                or cleanup.root_creation_time is not None or not cleanup.cleanup_verified
                or not cleanup.root_exited or cleanup.job_active_processes != 0):
            raise IntentConflict("no-spawn requires a matching measured no-child cleanup receipt")
        self._append("not_started", "not_started", {"cleanup": asdict(cleanup)})

    def unknown(self, reason: str, cleanup: CleanupReceipt | None = None):
        if self.state not in {"starting", "active", "unknown"} or not isinstance(reason, str) or not reason:
            raise IntentConflict("unknown must refer to an existing unresolved attempt")
        data = {"reason": reason}
        if cleanup is not None:
            if cleanup.attempt_id != self._record["attempt_id"]:
                raise IntentConflict("unknown cleanup names another attempt")
            data["cleanup"] = asdict(cleanup)
        self._append("unknown", "unknown", data)

    def reconcile_abandoned(self, output):
        """Close a dead controller's occupancy, preserving its unknown outcome."""
        from ._executor_lifecycle import measure_abandoned
        self._require()
        output = Path(output).resolve()
        observation_path = output.with_suffix(output.suffix + '.observation.json')
        lock = Path(self._record['worktree_lock'])
        if (self.state == 'unknown' and not lock.exists() and output.is_file() and observation_path.is_file()
                and json.loads(output.read_bytes()) == self._record.get('cleanup')):
            return self.record  # completed reconciliation is not another event or execution
        if self.state not in {'active', 'unknown'} or 'recovery_authorization' in self._record:
            raise IntentConflict('reconciliation requires an active/unknown bound attempt before recovery')
        identity = {key: self._record[key] for key in (
            'attempt_id', 'owner_pid', 'owner_creation_time', 'root_pid', 'root_creation_time')}
        if lock.exists():
            raw = lock.read_bytes()
            payload = json.loads(raw)
            expected = {**identity, 'launcher': self._record['launcher'],
                        'worktree': self.frozen['worktree'], 'tag': self.task_key}
            if (payload.get('schema_version') != 'pdcl-executor-lock-2'
                    or any(payload.get(key) != value for key, value in expected.items())):
                raise IntentConflict('executor occupancy belongs to another attempt')
            lease = ExecutorLease(lock, payload)
        else:
            # The controller released its occupancy on exit without recording an outcome. The
            # intent's own start/active events carry the same identities the lock held.
            raw, lease = None, None
        cleanup, observation = measure_abandoned(identity)
        if lease is None:
            observation['occupancy'] = ('absent: ' + str(lock) + ' did not exist at reconciliation; '
                                        'identities read from the intent start and active events')
            if lock.exists():
                raise IntentConflict('executor occupancy appeared during observation')
        elif lock.read_bytes() != raw:
            raise IntentConflict('executor occupancy changed during observation')
        output.parent.mkdir(parents=True, exist_ok=True)
        for path, value in ((output, asdict(cleanup)), (observation_path, observation)):
            if path.exists():
                prior = json.loads(path.read_bytes())
                if path == output and prior != value:
                    raise IntentConflict('existing cleanup output describes another observation')
                if path == observation_path and any(prior.get(k) != observation[k] for k in identity if k in observation):
                    raise IntentConflict('existing observation describes another process')
            else:
                with path.open('xb') as stream:
                    stream.write(_bytes(value)); stream.flush(); os.fsync(stream.fileno())
        if self.state != 'unknown' or self._record.get('cleanup') != asdict(cleanup):
            self.unknown('External controller exit; effects remain unknown. Observation: ' + str(observation_path), cleanup)
        if lease is None:
            return self.record
        if lock.read_bytes() != raw:
            raise IntentConflict('cleanup recorded but executor occupancy changed before release')
        if not lease.release(cleanup):
            raise IntentConflict('cleanup recorded but executor occupancy could not be released')
        return self.record

    def finish(self, cleanup: CleanupReceipt, *, returncode: int, evidence: dict):
        if self.state not in {"active", "unknown"}:
            raise IntentConflict("completion requires an already bound native root")
        if (cleanup.attempt_id != self._record["attempt_id"]
                or cleanup.root_pid != self._record["root_pid"]
                or cleanup.root_creation_time != self._record["root_creation_time"]
                or cleanup.root_pid is None or not cleanup.cleanup_verified
                or not cleanup.root_exited or cleanup.job_active_processes != 0):
            raise IntentConflict("completion identity or owned cleanup is unverified")
        if type(returncode) is not int:
            raise ValueError("native returncode must be an integer")
        modern = self._history_version == 2
        if self._record.get('launcher') == 'app-server' and not modern:
            raise IntentConflict('legacy app-server history cannot assert new terminal proof')
        _verify_evidence(evidence, asdict(cleanup), terminal=modern or self._record.get("launcher") == "claude")
        if self._record.get("launcher") == "claude":
            _verify_claude_terminal(self._record, evidence, returncode)
        data = {"cleanup": asdict(cleanup), "returncode": returncode,
                "evidence": evidence, "verification": "not_evaluated",
                "outcome": "process_completed" if returncode == 0 else "process_failed"}
        if modern:
            status = _verify_terminal({**self._record, 'cleanup': asdict(cleanup)}, evidence, returncode)
            data.update(outcome='turn_' + status, returncode_source='turn_status', native_returncode=None)
        self._append("finish", "completed", data)
