"""Bounded concurrency for the independent review pair, and nothing else.

WHY A GROUP OWNER RATHER THAN A THREAD POOL.

The two independent reviewers of one candidate do not depend on each other:
same frozen candidate, same frozen packet, no communication, no mutation, and
separate native sessions. That makes them concurrent in principle. It does not
make `ThreadPoolExecutor(role, ...)` safe in practice, because the goal engine's
recovery contract is stricter than its execution contract:

  * `_foundry_goal_progress.reconcile` accepts AT MOST ONE stopped phase
    (`stopped != {frontier_phase}` is a refusal). A review phase CAN stop - a
    budget pause writes `state: stopped` through the journal. Two reviewers
    journalling concurrently can therefore leave two stopped receipts, and the
    next resume refuses the whole run with "observed stop is not the
    checkpoint's current phase". Serial review can never produce that state.

  * The read-only reconcile walk raises `UncrossedPhase` for the FIRST phase it
    cannot cross. Racing that walk makes the recovery frontier depend on thread
    scheduling.

So this owner splits the phase into the part that is expensive and the part
that is authoritative:

    EXPENSIVE (concurrent)   the native model call for each pending reviewer
    AUTHORITATIVE (serial)   journal prepare/complete, receipt reconciliation,
                             session independence, candidate re-verification

Each reviewer's native call is issued once, concurrently, through the analyst -
which already owns a per-call reservation folder, an RLock-guarded admission
path, a durable call ledger and its own concurrency ceiling. The caller then
journals the reviewers strictly in index order, consuming the value (or the
exception) this owner captured. The analyst is invoked exactly once per
reviewer: `prewarm` holds the outcome and the serial pass replays it rather
than calling again, so no external effect is repeated and no receipt is
re-derived.

Consequences that keep recovery identical to serial review:

  * at most one stopped phase receipt, because only one journal write is ever
    in flight;
  * a deterministic failure - the lowest-index reviewer that failed is the one
    that blocks, regardless of which thread finished first;
  * an already-completed reviewer is never prewarmed and never re-run;
  * the read-only reconcile walk never runs concurrently at all.
"""
from __future__ import annotations

import threading
import json
import hashlib
import os
from pathlib import Path
import time
from contextvars import copy_context

from . import _foundry_goal_telemetry as _telemetry


class Panel:
    """Independent slots; retain receipts and replace only unfinished reviews.

    Each explicit goal recovery can open one new attempt per failed slot.
    Historical shared-owner and per-slot layouts remain evidence, not live
    owners. New attempts use separate NativeAnalysts so one failure cannot
    cancel the other slot. No worker/coding/manager phase is repeated here.
    """
    max_concurrency = 2

    def __init__(self, settings, requests, factory, folder, workspace, *, resume=False, validate=None,
                 rejected_review=(), transfers=None):
        self.members, self.routes, self.history, self.results, self.pending = {}, {}, {}, {}, {}
        self.requests, self.active_requests = {}, {}
        self.factory, self.workspace = factory, workspace
        self.launch, self.launch_lock = {}, threading.Lock()
        self.validate = validate
        for index, spec in enumerate(settings):
            name = "review-" + str(index)
            accepted = [spec, *({**row, **({'execution_profiles': spec['execution_profiles']}
                if spec.get('execution_profiles') else {})} for row in (transfers or {}).get(name, []))]
            options = dict(accepted[-1])
            transition = options.pop('runtime_transition', None)
            executable = Path(options.pop("executable"))
            slot = folder / name
            attempts = sorted(slot.glob('attempt-*')) if resume else []
            self.history[name] = []
            predecessors = []
            selected_request = requests[name]
            previous_backend = None
            previous_transition = None
            if resume:
                # Read all history before starting either review. Incomplete
                # cleanup/receipts still require reconciliation, not duplicate work.
                for source in [folder, slot, *attempts]:
                    manifest = source / 'native-manifest.json'
                    if not manifest.exists():
                        continue
                    policy = json.loads(manifest.read_bytes())
                    saved_transition = (policy.get('shadow_identity') or {}).get('review_transition')
                    if saved_transition not in {None, *(row.get('runtime_transition') for row in accepted)}:
                        raise ValueError('review history transition differs from recorded assignments')
                    if previous_backend is not None and (previous_backend != policy['backend'] or previous_transition != saved_transition):
                        selected_request = requests[name]
                    previous_backend = policy['backend']
                    previous_transition = saved_transition
                    shadow = {'review_predecessors': predecessors, 'review_request_sha256': _request_hash(selected_request)}
                    if saved_transition is not None:
                        shadow['review_transition'] = saved_transition
                    if source in attempts and policy.get('shadow_identity') != shadow:
                        raise ValueError('review attempt predecessor records changed')
                    from .foundry_native import _call_folder
                    call = _call_folder(source, selected_request['call_id'])
                    if not call.exists():
                        continue  # A controller stopped before reserving this slot.
                    saved = _archived(factory, source, selected_request, workspace, accepted, policy)
                    self.history[name].append(saved['snapshot'])
                    predecessors.append(saved['receipt'])
                    if saved['schema_error'] and saved['persistent']:
                        repaired = factory.review_repair_request(selected_request, saved['schema_error'])
                        if _call_folder(source, repaired['call_id']).exists():
                            selected_request = repaired
                            saved = _archived(factory, source, selected_request, workspace, accepted, policy)
                            self.history[name].append(saved['snapshot'])
                            predecessors.append(saved['receipt'])
                    if saved['value'] is not None:
                        try:
                            if self.validate:
                                self.validate(saved['value'])
                        except rejected_review:
                            pass  # A host-rejected verdict is still unfinished work.
                        else:
                            self.results[name] = saved
                    selected_request = (factory.review_repair_request(selected_request, saved['schema_error'], source=source)
                        if saved['schema_error'] and saved['persistent'] else requests[name])
            if previous_backend is not None and (previous_backend != options.get('backend', 'codex') or previous_transition != transition):
                selected_request = requests[name]
            self.requests[name] = selected_request
            self.pending[name] = (executable, slot / ('attempt-%06d' % (len(attempts) + 1)),
                                  {**options, 'shadow_identity': {'review_predecessors': predecessors,
                                      'review_request_sha256': _request_hash(selected_request),
                                      **({'review_transition': transition} if transition else {})}})
            self.routes[requests[name]["call_id"]] = name

    def __call__(self, request):
        name = self.routes[request['call_id']]
        if name in self.results:
            return self.results[name]['value']
        if name not in self.members:
            executable, evidence, options = self.pending[name]
            backend = options.get('backend', 'codex')
            with self.launch_lock:
                if backend not in self.launch:
                    from ._launch_context import capture
                    self.launch[backend] = capture(self.workspace, backend=backend)
            self.members[name] = self.factory(executable, evidence, workspace=self.workspace,
                max_concurrency=1, timeout_seconds=None, launch_context=self.launch[backend], **options)
        from .foundry_native import NativeSchemaError
        actual = self.requests[name]
        self.active_requests[name] = actual
        try:
            value = self.members[name](actual)
        except NativeSchemaError as exc:
            if not exc.repairable or 'schema_repair' in actual:
                raise
            # One formatting turn, not another investigation. If it also fails,
            # retain both receipts and stop until the next explicit recovery.
            actual = self.factory.review_repair_request(actual, exc)
            self.active_requests[name] = actual
            value = self.members[name](actual)
        if self.validate:
            self.validate(value)
        return value

    def native_session(self, request):
        name = self.routes[request['call_id']]
        if name in self.results:
            return self.results[name]['session']
        return self.members[name].native_session(self.active_requests[name])

    def cancel(self, reason):
        for member in self.members.values():
            member.cancel(reason)

    def drain(self):
        snapshots = {}
        for name, history in self.history.items():
            attempts = history + ([self.members[name].drain()] if name in self.members else [])
            snapshots[name] = {
                'calls': [row for attempt in attempts for row in attempt.get('calls', [])],
                **{key: sum(attempt.get(key, 0) or 0 for attempt in attempts)
                   for key in ('input_bytes', 'capture_bytes', 'capture_reserved_bytes')},
                'unresolved_call_ids': [item for attempt in attempts
                                        for item in attempt.get('unresolved_call_ids', [])]}
        return {"reviewers": snapshots,
                "calls": [{**row, "reviewer_slot": name} for name, snapshot in snapshots.items()
                          for row in snapshot.get("calls", [])],
                **{key: sum(snapshot.get(key, 0) or 0 for snapshot in snapshots.values())
                   for key in ("input_bytes", "capture_bytes", "capture_reserved_bytes")},
                "unresolved_call_ids": [item for snapshot in snapshots.values()
                                        for item in snapshot.get("unresolved_call_ids", [])]}


def _archived(factory, source, request, workspace, spec, policy):
    """A reserved call's receipt, or, for a first or schema-repair call whose controller died first, its interruption."""
    from .foundry_native import _call_folder
    call = _call_folder(source, request['call_id'])
    if (call / 'receipt.json').exists():
        return factory.inspect_call(source, request, workspace, spec)
    return interrupted_review(call, policy)


def interrupted_review(call, policy):
    """Record, once, a reserved review call whose controller exited before its receipt.

    Recovery reaches this only under the round's controller lock (``foundry_goal._run_lock``),
    which the owning controller holds until it exits or leaves its review; a killed
    controller's kill-on-close Job ends the review's process tree. ``measure_abandoned``
    confirms the recorded root and its descendants are gone and refuses while any still runs. The measurement is written beside
    the call as ``interrupted.json`` and later recoveries read it back unchanged, so the next
    attempt's predecessor reference is stable. The call's outcome stays unknown; it is
    retained as evidence and never read as a verdict.
    """
    from ._executor_lifecycle import measure_abandoned
    record_path = call / 'interrupted.json'
    allocation = json.loads((call / 'reservation.json').read_bytes())
    if not record_path.exists():
        identity_path = call / 'process.json'
        if identity_path.exists():
            identity = json.loads(identity_path.read_bytes())
            if identity.get('attempt_id') != allocation.get('attempt_id'):
                raise ValueError('interrupted review process identity differs from its reservation')
            cleanup, observation = measure_abandoned(identity, roles=('root',))
            cleanup = {**cleanup.__dict__}
        else:  # No recorded process: any process it had spawned died with its kill-on-close Job.
            cleanup = {'attempt_id': allocation['attempt_id'], 'root_pid': None, 'root_creation_time': None,
                       'root_exited': True, 'job_active_processes': 0, 'cleanup_verified': True,
                       'reason': 'controller exited before recording a review process; '
                                 'its kill-on-close Job ended anything it had spawned'}
            observation = None
        streams = {}
        for filename in ('stdout.jsonl', 'stderr.txt'):
            path = call / filename
            data = path.read_bytes() if path.exists() else b''
            streams[filename] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        record = {'schema_version': 1, 'call_id': allocation['call_id'], 'attempt_id': allocation['attempt_id'],
                  'outcome': 'interrupted', 'cleanup': cleanup, 'observation': observation, 'streams': streams,
                  'measured_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
        # Atomic: a recovery killed mid-write leaves only the temporary file, rewritten next time.
        partial = record_path.with_name(record_path.name + '.partial')
        with partial.open('wb') as handle:
            handle.write(json.dumps(record, indent=1, sort_keys=True).encode())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, record_path)
    raw = record_path.read_bytes()
    record = json.loads(raw)
    if (record.get('outcome') != 'interrupted' or record.get('call_id') != allocation['call_id']
            or record.get('attempt_id') != allocation['attempt_id']):
        raise ValueError('interrupted review record does not describe its reservation')
    cleanup = record['cleanup']
    capture = sum(stream['bytes'] for stream in record['streams'].values())
    row = {'call_id': allocation['call_id'], 'kind': allocation.get('kind'), 'backend': policy.get('backend'),
           'evidence': str(call), 'state': 'interrupted', 'root_pid': cleanup['root_pid'],
           'root_creation_time': cleanup['root_creation_time'], 'cleanup': cleanup,
           'error': 'controller exited before the receipt', 'input_bytes': allocation['input_bytes'],
           'usage': None, 'provider_usage_observed': False, 'outcome': 'interrupted',
           'capture_bytes': capture, 'capture_allowance_bytes': allocation.get('capture_allowance_bytes')}
    return {'value': None, 'session': None, 'schema_error': None, 'persistent': False,
            'snapshot': {'calls': [row], 'input_bytes': row['input_bytes'], 'capture_bytes': capture,
                         'capture_reserved_bytes': 0, 'unresolved_call_ids': []},
            'receipt': {'path': str(record_path), 'sha256': hashlib.sha256(raw).hexdigest()}}


def _request_hash(request):
    return hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def plan(names, *, completed, historical, analyst):
    """Which reviewers may share a concurrent group. Absent proof, none do.

    `completed` are the reviewers whose phase receipt already exists, so replay
    must not call a model for them. `historical` marks the read-only reconcile
    walk, whose frontier must stay deterministic.
    """
    if historical:
        return []
    pending = [name for name in names if name not in completed]
    if len(pending) < 2:
        return []
    ceiling = getattr(analyst, "max_concurrency", 1)
    if not isinstance(ceiling, int) or ceiling < len(pending):
        # Admission would refuse the second call as busy; serial is correct, not slower.
        return []
    return pending


def prewarm(pending, invocations, analyst, timing=None):
    """Issue each pending reviewer's native call at once; return outcome per name.

    Returns `{name: (value, exception)}`. Exactly one analyst invocation per
    name. Every thread is joined before returning, so no call outlives the
    group and no reviewer's effect is left unobserved.

    `timing`, when given, is filled with each member's wall time. The group must
    measure its own cost: once the native call moves here, the reviewer's phase
    receipt only spans the journal write, so a phase timer would report the
    review as nearly free and hide the number this whole change is judged on.
    """
    if not pending:
        return {}
    outcomes, threads = {}, []
    guard = threading.Lock()

    def work(name):
        started = _telemetry.started()
        try:
            value, error = analyst(invocations[name]), None
        except BaseException as exc:  # captured, re-raised by the serial pass in index order
            value, error = None, exc
        # THE OUTCOME IS RECORDED BEFORE ANY TIMING WORK, AND SEPARATELY FROM IT.
        # By this line the native call has happened and been paid for, so losing
        # its result is the most expensive thing this function can do. It used to
        # read the clock first, which put an unguarded call between the model
        # answering and the answer being kept.
        with guard:
            outcomes[name] = (value, error)
        row = _telemetry.measure(started)
        if timing is not None and row is not None:
            with guard:
                timing[name] = row

    for name in pending:
        thread = threading.Thread(target=copy_context().run, args=(work, name), name="foundry-review-" + name)
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes


def observe(folder, attempt, timing):
    """Append the group's own observation: member cost and true concurrent span.

    Never raises. This runs after both reviewers' native calls are complete, so
    an escaping error here would fail the run for a number nobody reads.
    """
    if not timing:
        return
    try:
        starts = [row["started_utc"] for row in timing.values()]
        ends = [row["completed_utc"] for row in timing.values()]
        members = {name: row["duration_s"] for name, row in timing.items()}
        _telemetry.record(folder, {"phase": "review-group-" + str(attempt), "group": True,
                                   "started_utc": min(starts), "completed_utc": max(ends),
                                   "duration_s": _telemetry.span(min(starts), max(ends)),
                                   "members": members, "review_concurrency": len(timing)})
    except Exception:
        return
