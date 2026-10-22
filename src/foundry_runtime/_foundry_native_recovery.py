"""Read-only native restoration; durable writes belong to admitted execution."""
import hashlib
import threading
from ._provider_stop import CONTINUABLE


def _stop(analyst, reason):
    analyst._cancelled.set()
    analyst._cancel_reason = reason


def reconcile_manager(analyst):
    """Under the goal's controller lock, settle missing receipts through the existing cleanup owner.

    A complete but invalid receipt is never replaced. A missing process identity
    cannot prove a dead process tree. No recovered stream is promoted to success.
    """
    import json
    from ._foundry_review_group import interrupted_review
    from .foundry_native import _now, compute_owner, NativeError
    from .foundry_goal import _write
    changed = False
    for row in analyst._contexts.values():
        folder, allocation = row['folder'], row['allocation']
        if row['state'] != 'unknown' or (folder / 'receipt.json').exists():
            continue
        if allocation.get('kind') != 'coordinate' or not (folder / 'process.json').exists():
            continue
        try:
            analyst._validate_allocation(allocation, folder, stable_only=True)
            invocation = json.loads((folder / 'invocation.json').read_bytes())
            if any(invocation.get(key) != allocation.get(key) for key in
                   ('call_id', 'attempt_id', 'stdin_sha256', 'input_bytes')):
                raise ValueError('invocation differs from reserved call')
            raw = (folder / 'stdin.txt').read_bytes()
            if hashlib.sha256(raw).hexdigest() != allocation['stdin_sha256']:
                raise ValueError('captured request differs from reserved call')
            interrupted = interrupted_review(folder, {'backend': analyst.backend})
            observation = json.loads((folder / 'interrupted.json').read_bytes())
            streams = observation['streams']
            schema = invocation['response_contract']
            record = {'schema_version': 2, 'outcome': 'failure',
                'error': 'controller interrupted before receipt publication; cleanup measured on recovery',
                'attempt_id': allocation['attempt_id'], 'call_id': allocation['call_id'],
                'input_bytes': allocation['input_bytes'], 'backend': analyst.backend,
                **compute_owner.call_compute_metadata(analyst, allocation['kind']),
                'response_contract_sha256': hashlib.sha256(json.dumps(schema, sort_keys=True,
                    separators=(',', ':')).encode()).hexdigest(),
                'cleanup': observation['cleanup'], 'returncode': None, 'truncated': True,
                'ended_utc': None, 'reconciled_utc': _now(), 'reconciliation': interrupted['receipt'],
                'usage': None, 'session_id': None, 'model': None,
                'accounting_observations': [], 'observed_formatter_attempts': None}
            for name, filename in (('stdout', 'stdout.jsonl'), ('stderr', 'stderr.txt')):
                record[name + '_bytes'] = streams[filename]['bytes']
                record[name + '_sha256'] = streams[filename]['sha256']
            analyst._inspect_receipt(row, record=record)
            if not analyst._latest_cancellation()[1].exists():
                analyst._cancelled.clear()  # The restored stop has no durable cancellation yet.
                analyst.cancel('reconciled interrupted manager; explicit goal recovery may continue its request')
                if analyst._cancel_persistence_error:
                    raise OSError(analyst._cancel_persistence_error)
            _write(folder / 'receipt.json', record)
            changed = True
        except (OSError, ValueError, RuntimeError) as exc:
            row['error'] = str(exc)
            raise NativeError('native reconciliation requires owner action: call_id=%s kind=%s path=%s: %s' % (
                allocation.get('call_id'), allocation.get('kind'), folder, exc), analyst=analyst) from exc
    if changed:
        analyst._contexts.clear()
        analyst._calls = 0
        analyst._restore_allocations()


def restore_allocations(self):
    from .foundry_native import NativeError, NativeSchemaError, _call_key, _json
    # A readback preserves an existing stop; it is not a new cancellation.
    _, prior_stop = self._latest_cancellation()
    if prior_stop.exists():
        persisted = _json(prior_stop.read_bytes())
        if not isinstance(persisted, dict) or not isinstance(persisted.get('reason'), str):
            raise NativeError('persisted native cancellation is invalid')
        self._cancelled.set()
        self._cancel_reason = persisted['reason']
    recovered_failure = False
    for folder in sorted(self.evidence_dir.glob("call-*")):
        allocation = {"call_id": folder.name[5:], "input_bytes": self.max_input_bytes,
                      "capture_allowance_bytes": self.max_output_bytes}
        row = {"folder": folder, "allocation": allocation, "state": "unknown", "process": None,
               "done": threading.Event(), "capture_bytes": None}
        row["done"].set()
        try:
            saved = _json((folder / "reservation.json").read_bytes())
            self._validate_allocation(saved, folder, stable_only=True)
            row["allocation"] = saved
            key = _call_key(saved['call_id']) if saved.get('call_id') else folder.name
            if key in self._contexts:
                raise NativeError('duplicate native allocation call identity')
            self._contexts[key] = row
            self._calls += 1
            record, streams, total = self._inspect_receipt(row)
            if record["outcome"] in ("success", "schema_failure"):
                self._decode_receipt(record, streams, saved)
            row.update(state="settled", capture_bytes=total, cleanup=record["cleanup"],
                       accounting_observations=record.get("accounting_observations", []), usage=record.get("usage"),
                       session_id=record.get('session_id'), ended_utc=record.get('ended_utc'),
                       outcome=record.get("outcome"), error=record.get("error"),
                       root_pid=record["cleanup"]["root_pid"], root_creation_time=record["cleanup"]["root_creation_time"])
            if record["outcome"] not in {"success", "schema_failure", *CONTINUABLE}:
                recovered_failure = True
        except NativeSchemaError as exc:
            # Schema failure is a completed native call, not an unknown one.
            # Keep the same receipt facts as the live worker retained so a
            # later bounded repair cannot change the terminal evidence view.
            row.update(state="settled", capture_bytes=total, cleanup=record["cleanup"],
                       accounting_observations=record.get("accounting_observations", []),
                       session_id=record.get('session_id'), ended_utc=record.get('ended_utc'),
                       usage=record.get("usage"), outcome=record.get("outcome"), error=str(exc),
                       root_pid=record["cleanup"]["root_pid"],
                       root_creation_time=record["cleanup"]["root_creation_time"])
        except Exception as exc:
            if not any(existing is row for existing in self._contexts.values()):
                self._contexts[folder.name] = row
            self._calls = len(self._contexts)
            row["error"] = str(exc)
            # Recorded ownership remains useful evidence after controller
            # death; it is never treated as a recovered live Job handle.
            try:
                identity = _json((folder / "process.json").read_bytes())
                if (isinstance(identity, dict) and identity.get("attempt_id") == row["allocation"].get("attempt_id")
                        and all(type(identity.get(key)) is int and identity[key] > 0
                                for key in ("root_pid", "root_creation_time"))):
                    row.update(root_pid=identity["root_pid"], root_creation_time=identity["root_creation_time"])
            except (OSError, NativeError):
                pass
            _stop(self, "native recovery contains unknown calls")
    snapshot = self.evidence_snapshot()
    resources_exhausted = ((self.max_total_input_bytes is not None and snapshot["input_bytes"] > self.max_total_input_bytes)
            or (self.max_total_output_bytes is not None
                and snapshot["capture_bytes"] + snapshot["capture_reserved_bytes"] > self.max_total_output_bytes))
    if resources_exhausted:
        _stop(self, "persisted aggregate native resource limit exceeded")
    _, cancellation = self._latest_cancellation()
    if cancellation.exists():
        persisted = _json(cancellation.read_bytes())
        if not isinstance(persisted, dict) or not isinstance(persisted.get("reason"), str):
            raise NativeError("persisted native cancellation is invalid")
        failures = {key for key, row in self._contexts.items() if row.get('outcome') != 'success'}
        retries = [row['allocation']['retry_of'] for row in self._contexts.values()
                   if row['state'] == 'settled' and 'retry_of' in row['allocation']]
        superseded = (bool(retries) and failures <= {'call-' + ref['call_id'] for ref in retries}
            and all(row['state'] == 'settled' for row in self._contexts.values())
            and hashlib.sha256(cancellation.read_bytes()).hexdigest() in
                {ref['cancellation_sha256'] for ref in retries})
        if superseded and not resources_exhausted:
            self._cancelled.clear()
            self._cancel_reason = None
        else:
            self._cancelled.set()
            self._cancel_reason = persisted["reason"]
    elif recovered_failure:
        _stop(self, "previous native call failed; no automatic retry")
    unresolved = [row for row in self._contexts.values() if row['state'] == 'unknown']
    if unresolved:
        _stop(self, 'native recovery contains unknown calls: ' + '; '.join(
            'call_id=%s kind=%s path=%s cause=%s root_pid=%s root_creation_time=%s' % (
                row['allocation'].get('call_id'), row['allocation'].get('kind', 'unreadable'),
                row['folder'], row.get('error'), row.get('root_pid'), row.get('root_creation_time'))
            for row in unresolved))


def inspect_call(cls, evidence_dir, request, workspace, settings):
    """Read an archived role call under its recorded policy without admission.

    A tool upgrade cannot supply a missing verdict, but must not prevent us
    from reading a completed receipt or recognizing a cleaned-up failure.
    New calls are owned by a fresh native generation, never this reader.
    """
    from pathlib import Path
    from .foundry_native import NativeError, NativeSchemaError, _call_folder, _json, compute_owner, compute_kwargs, message_recipients_for_request, _allocation_contract
    folder = Path(evidence_dir).resolve()
    policy = _json((folder / 'native-manifest.json').read_bytes())
    if (policy.get('schema_version') not in (6, 7) or request.get('kind') not in {'review', 'coordinate'}
            or policy.get('workspace') != str(Path(workspace).resolve())):
        raise NativeError('native history does not describe this workspace')
    reader = object.__new__(cls)
    reader.backend = policy['backend']
    reader._compute = policy['requested_compute'] or {}
    reader._role_compute = compute_owner.freeze_roles(policy.get('role_compute'))
    candidates = settings if isinstance(settings, list) else [settings]
    if not any(reader.backend == spec.get('backend', 'codex')
            and reader._compute == compute_kwargs(spec.get('model'), spec.get('effort'), backend=reader.backend)
            and reader._role_compute == compute_owner.freeze_roles(compute_owner.role_assignments(
                spec.get('role_compute'), backend=reader.backend, coding_backend=None)) for spec in candidates):
        raise NativeError('native history assignment differs from checkpoint')
    reader.executable = Path(policy['executable'])
    reader.workspace, reader.evidence_dir = Path(workspace).resolve(), folder
    reader.backend_identity = policy['backend_identity']
    reader.execution_profiles = policy['backend_identity'].get('execution_profiles')
    for key in ('max_input_bytes', 'max_output_bytes', 'max_total_input_bytes', 'max_total_output_bytes'):
        setattr(reader, key, policy[key])
    call = _call_folder(folder, request['call_id'])
    allocation = _json((call / 'reservation.json').read_bytes())
    # The immutable stdin hash is checked below. Compare its task envelope,
    # not today's prompt wording, when reading a pre-upgrade invocation.
    captured_request = _json((call / 'stdin.txt').read_bytes().rsplit(b'\n\n', 1)[-1])
    expected_request = {k: v for k, v in request.items() if k != 'call_id'}
    expected_request['response_contract'] = _allocation_contract(allocation)
    if (allocation.get('kind') != request['kind'] or allocation.get('call_id') != request['call_id']
            or allocation.get('message_recipients') != message_recipients_for_request(request)
            or captured_request != expected_request):
        raise NativeError('native history request differs from checkpoint')
    row = {'folder': call, 'allocation': allocation}
    record, streams, total = reader._inspect_receipt(row, archived=True)
    value, shape_error = None, None
    session = record.get('session_id')
    if record['outcome'] in {'success', 'schema_failure'}:
        try:
            value = reader._decode_receipt(record, streams, allocation)
        except NativeSchemaError as exc:
            shape_error = exc
    elif (request['kind'] == 'review' and record.get('truncated') is False
            and record['cleanup'].get('root_pid') is not None):
        # Execution outcome and a completed review are separate facts. Read the
        # entire authenticated stream; never rewrite the original failure receipt.
        from .foundry_native import _parsed
        try:
            value, init, result = _parsed(streams['stdout'], reader.max_output_bytes,
                _allocation_contract(allocation), reader.backend, receipt=record)
            session = init.get('session_id')
            if allocation.get('resume_session') and session != allocation['resume_session']:
                raise NativeError('native continuation returned another session')
            if any(record.get(key) is not None and record[key] != init.get(key)
                   for key in ('session_id', 'model')):
                raise NativeError('retained review identity differs from receipt')
            row['recovered_verdict'] = {
                'source': str(call / 'receipt.json'),
                'receipt_sha256': hashlib.sha256((call / 'receipt.json').read_bytes()).hexdigest(),
                'execution_outcome': record['outcome'], 'returncode': record.get('returncode'),
                'session_id': session, 'usage': result.get('usage'),
                'list_price_usd': result.get('total_cost_usd'),
                'accounting_observations': result.get('accounting_observations', [])}
        except NativeError:
            value, session = None, record.get('session_id')
    row.update(state='settled', process=None, capture_bytes=total,
               accounting_observations=record.get('accounting_observations', []),
               cleanup=record['cleanup'], usage=record.get('usage'),
               session_id=record.get('session_id'), ended_utc=record.get('ended_utc'),
               outcome=record['outcome'], error=record.get('error'),
               root_pid=record['cleanup']['root_pid'],
               root_creation_time=record['cleanup']['root_creation_time'])
    reader._contexts = {call.name: row}
    reader._call_lock, reader._cancelled = threading.RLock(), threading.Event()
    reader._cancel_reason = reader._cancel_persistence_error = None
    return {'value': value, 'session': session, 'schema_error': shape_error,
            'persistent': reader.backend == 'claude' or allocation.get('persistent_session') is True,
            'snapshot': reader.evidence_snapshot(),
            'receipt': {'path': str(call / 'receipt.json'),
                        'sha256': hashlib.sha256((call / 'receipt.json').read_bytes()).hexdigest()}}
