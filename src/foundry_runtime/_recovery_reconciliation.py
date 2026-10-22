"""Reconciliation owned by the existing execution journal.

An unknown event remains unknown. The exception to blocking new work exists only
while its complete original recovery and completion evidence still reconcile.
Explicit goal recovery can settle a retained terminal after measured late cleanup;
ordinary admission scans remain read-only.
"""
import json
import os
import re
from pathlib import Path


def settle_terminal(intent, log):
    """Promote a bound, retained completed turn after measured late cleanup.

    Called only by explicit goal recovery while the intent mutex is held. Original
    transport evidence stays unchanged; the same journal owns the final outcome.
    """
    from ._execution_intent import IntentConflict, _digest
    from .app_server_transport import AppServer
    record = intent.record
    if (record['state'] not in ('active', 'unknown') or record.get('launcher') != 'app-server'
            or 'recovery_authorization' in record or 'recovery_consumption' in record):
        raise IntentConflict('retained turn requires its original unresolved app-server attempt')
    binding = record['turn_binding']
    terminal, final_text, stopped = AppServer.retained_terminal(log, binding, record['frozen']['app_server_policy'])
    if terminal['params']['turn']['status'] != 'completed':
        raise IntentConflict('retained worker turn did not complete')
    old_cleanup = json.loads(log.with_suffix('.cleanup.json').read_bytes())
    if (old_cleanup.get('job_active_processes') != 0 or any(old_cleanup.get(k) != binding[k]
            for k in ('attempt_id', 'root_pid', 'root_creation_time'))):
        raise IntentConflict('original cleanup does not establish the bound zero-process Job')
    prefix = log.with_suffix('.reconciled')
    cleanup_path = Path(str(prefix) + '.cleanup.json')
    intent.reconcile_abandoned(cleanup_path)
    from ._executor_lifecycle import CleanupReceipt
    cleanup = CleanupReceipt(**intent.record['cleanup'])
    terminal_path = Path(str(prefix) + '.terminal.json')
    result_path = Path(str(prefix) + '.result.md')
    terminal = {'schema_version': 'pdcl-app-server-terminal-1', 'binding': binding,
        'terminal': terminal, 'returncode': 0, 'returncode_source': 'turn_status',
        'native_returncode': None, 'app_server_policy': record['frozen']['app_server_policy'],
        'expiration_cause': stopped.get('expiration_cause'),
        'interrupt_result': stopped.get('interrupt_result', 'not_requested')}
    package = log.with_suffix('.package.json').read_bytes()
    for path, raw in ((terminal_path, json.dumps(terminal, sort_keys=True).encode()),
                      (result_path, final_text.encode()),
                      (Path(str(prefix) + '.package.json'), package)):
        if path.exists():
            if path.read_bytes() != raw:
                raise IntentConflict('reconciled evidence differs: ' + str(path))
        else:
            with path.open('xb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    evidence = {key: {'path': str(path.resolve()), 'sha256': _digest(path)} for key, path in
                (('log', log), ('result', result_path), ('terminal', terminal_path), ('cleanup', cleanup_path))}
    intent.finish(cleanup, returncode=0, evidence=evidence)
    return intent.record


def completed_coding(checkpoint):
    """Project the dispatch owner's completion proof; error vocabulary is irrelevant.

    This is the same journal/terminal/cleanup verification used by dispatch, not
    a new retry ledger. A later dispatch still performs its normal live admission.
    """
    from ._execution_intent import ExecutionIntent, IntentConflict
    cleanups = [fact for fact in checkpoint['evidence'] if fact['path'].endswith('.cleanup.json')]
    if not cleanups:
        raise IntentConflict('coding completion has no cleanup reference; reconcile its original dispatch')
    cleanup = cleanups[-1]
    package_path = Path(cleanup['path'].removesuffix('.cleanup.json') + '.package.json')
    package = json.loads(package_path.read_bytes())
    from ._dispatch_package import read_intent
    journal, record = read_intent(package, project_record=checkpoint['options'].get('project_record'))
    session = (record.get('turn_binding') or {}).get('thread_id') or record.get('session_id')
    if record['state'] != 'completed':
        raise IntentConflict('original dispatch is ' + record['state'] + '; use its existing reconciliation/recovery route')
    if (record['intent_id'] != package['intent_id'] or record['attempt_id'] != package['attempt_id']
            or (session is not None and session != checkpoint['coding_session'])
            or Path(record['frozen']['worktree']).resolve() != Path(checkpoint['worktree']).resolve()
            or record['returncode'] != checkpoint['returncode']
            or record['evidence']['cleanup'] != cleanup):
        raise IntentConflict('coding checkpoint and original dispatch completion differ')
    _execution_events(journal)
    ExecutionIntent._verify_completion(record)
    if record.get('terminal_evidence_class') == 'legacy_unbound_app_server':
        raise IntentConflict('legacy app-server result has no bound terminal; use existing reconciliation')
    return {'state': 'completed', 'intent_id': record['intent_id'], 'attempt_id': record['attempt_id'],
            'outcome': record['outcome'], 'returncode': record['returncode'],
            'cleanup_verified': True, 'journal': str(journal)}


def _execution_events(path, *, predecessor=False):
    """Validate each execution projection, not only the merged last state.

The generic reader authenticates the event chain and transition order. Recovery
also needs to rule out a later event overwriting identity or a claimed finish
that did not preserve the original bound root/cleanup evidence.
"""
    from ._execution_intent import IntentConflict
    allowed = {
        'prepared': {'frozen', 'attempt_id', 'root_pid', 'root_creation_time',
                     'recovery_predecessor_intent_id', 'recovery_decision_sha256'},
        'start': {'attempt_id', 'owner_pid', 'owner_creation_time', 'root_pid',
                  'root_creation_time', 'session_id', 'worktree_lock', 'launcher'},
        'active': {'root_pid', 'root_creation_time', 'session_id'},
        'turn_bound': {'turn_binding'},
        'not_started': {'cleanup'},
        'unknown': {'reason', 'cleanup'},
        'finish': {'cleanup', 'returncode', 'evidence', 'verification', 'outcome'},
        'recovery_authorized': {'recovery_authorization'},
        'recovery_consumed': {'recovery_consumption'},
    }
    projected = {}
    for event in sorted(path.glob('*.json')):
        row = json.loads(event.read_bytes())
        kind, data = row['kind'], row['data']
        fields = allowed.get(kind, set())
        modern = row['schema_version'] == 2
        if kind == 'finish' and modern:
            fields = fields | {'returncode_source', 'native_returncode'}
        if kind not in allowed or not set(data) <= fields:
            raise IntentConflict('recovery event changes execution identity outside its owner')
        if kind in ('start', 'active', 'turn_bound', 'not_started', 'finish') and set(data) != fields:
            raise IntentConflict('recovery execution event is incomplete')
        if kind == 'prepared':
            if any(data.get(k) is not None for k in ('attempt_id', 'root_pid', 'root_creation_time')):
                raise IntentConflict('recovery prepared event claims an existing execution')
        if kind == 'start':
            # start() records the frozen session and the lease's path. Check
            # them here, before an active event can overwrite the session.
            if (not re.fullmatch(r'[0-9a-f-]{36}', str(data['attempt_id']))
                    or any(type(data[k]) is not int or data[k] <= 0
                           for k in ('owner_pid', 'owner_creation_time'))
                    or data['root_pid'] is not None or data['root_creation_time'] is not None
                    or data['session_id'] != projected['frozen']['session_id']
                    or not isinstance(data['worktree_lock'], str)
                    or os.path.normcase(data['worktree_lock']) != os.path.normcase(
                        os.path.join(projected['frozen']['worktree'], '.pdcl-executor.lock'))
                    or data['launcher'] != projected['frozen']['launcher']):
                raise IntentConflict('recovery start identity is invalid')
        if kind == 'active':
            if (any(type(data[k]) is not int or data[k] <= 0
                    for k in ('root_pid', 'root_creation_time'))
                    or (projected['frozen']['session_id'] is not None
                        and data['session_id'] != projected['frozen']['session_id'])):
                raise IntentConflict('recovery active root/session is invalid')
        if kind == 'turn_bound':
            from ._execution_intent import ExecutionIntent
            if not modern:
                raise IntentConflict('legacy execution cannot bind an app-server turn')
            ExecutionIntent._check_turn_binding({**projected, '_history_version': row['schema_version']}, data['turn_binding'])
        if kind == 'unknown' and 'cleanup' in data:
            # An earlier uncertain cleanup remains an observation. The final
            # predecessor cleanup is checked by _admit_recovery; requiring every
            # earlier observation to be clean would erase recovery's purpose.
            if (not isinstance(data['cleanup'], dict)
                    or data['cleanup'].get('attempt_id') != projected.get('attempt_id')):
                raise IntentConflict('recovery unknown cleanup names another attempt')
        elif 'cleanup' in data:
            cleanup = data['cleanup']
            if (not isinstance(cleanup, dict)
                    or any(cleanup.get(k) != projected.get(k)
                           for k in ('attempt_id', 'root_pid', 'root_creation_time'))
                    or cleanup.get('cleanup_verified') is not True
                    or cleanup.get('root_exited') is not True
                    or type(cleanup.get('job_active_processes')) is not int
                    or cleanup['job_active_processes'] != 0):
                raise IntentConflict('recovery execution cleanup does not match the bound root')
        if kind == 'finish':
            # finish() needs a previously bound root even after unknown. Equal
            # null roots in the projection and cleanup are not execution proof.
            if any(type(projected.get(k)) is not int or projected[k] <= 0
                   for k in ('root_pid', 'root_creation_time')):
                raise IntentConflict('recovery completion requires a bound execution root')
            outcomes = ({'turn_completed'} if data['returncode'] == 0 else
                        {'turn_failed', 'turn_interrupted'}) if modern else {
                        'process_completed' if data['returncode'] == 0 else 'process_failed'}
            if (predecessor or type(data['returncode']) is not int
                    or data['verification'] != 'not_evaluated'
                    or data['outcome'] not in outcomes
                    or (modern and (data['returncode_source'] != 'turn_status' or data['native_returncode'] is not None))):
                raise IntentConflict('recovery completion classification is invalid')
        projected.update(data)


def completed_recovery(owner, path, record):
    from ._execution_intent import ExecutionIntent, IntentConflict
    from ._recovery_authorization import RecoveryAuthorization
    from ._recovery_provenance import original_decision
    if (record['state'] != 'unknown' or 'recovery_authorization' not in record
            or 'recovery_consumption' not in record):
        return False
    try:
        visited = set()
        while True:
            if path.name in visited:
                raise IntentConflict('cyclic recovery history')
            visited.add(path.name)
            authorization = record['recovery_authorization']
            decision_path, raw = original_decision(authorization, source_verifier=owner.source_verifier)
            if json.loads(raw) != authorization['decision']:
                raise IntentConflict('recovery embedded decision differs from original bytes')
            successor_id = authorization['successor_intent_id']
            if not re.fullmatch(r'[0-9a-f]{64}', str(successor_id)) or successor_id == path.name:
                raise IntentConflict('recovery successor identity is invalid')
            key = record['task_key']
            successor_path = path.parent / successor_id
            successor, sequence, digest = owner._read(successor_path, task_key=key)
            frozen = successor['frozen']
            if (frozen['repository_id'] != record['frozen']['repository_id']
                    or frozen['repository_id'] != owner.frozen['repository_id']):
                raise IntentConflict('recovery successor is incomplete or in another repository')
            recovery = RecoveryAuthorization.load_historical(
                decision_path, task_key=key, worktree=frozen['worktree'],
                session_id=frozen['session_id'], candidate_head=frozen['base_commit'],
                source_verifier=owner.source_verifier)
            # This unentered reader acquires no lock, appends no event and consumes
            # nothing. The calling scan retains its own task mutex / worktree lease.
            reader = ExecutionIntent(owner.common_dir, key, frozen, intent_id=successor_id,
                                     recovery=recovery, read_only=True)
            reader._record, reader._sequence, reader._last_hash = successor, sequence, digest
            reader._history_version = successor['_history_version']
            prior = owner._read(path, task_key=key)
            _execution_events(path, predecessor=True)
            _execution_events(successor_path)
            reader._admit_recovery((path, *prior))
            reader._verify_recovery_link(require_completed=False)
            if successor['state'] == 'completed':
                reader._verify_completion(successor)
                break
            # An explicitly authorized next recovery may end at its own cleaned
            # predecessor. Normal admission still validates that decision; this
            # readback neither completes history nor authorizes a new execution.
            if (owner._recovery is not None and key == owner.task_key
                    and successor_id == owner._recovery.data['predecessor']['intent_id']
                    and successor['state'] == 'unknown'):
                break
            if (successor['state'] != 'unknown' or 'recovery_authorization' not in successor
                    or 'recovery_consumption' not in successor):
                raise IntentConflict('recovery successor is incomplete or in another repository')
            path, record = successor_path, successor
        # Closing the historical chain does not transfer its task to a new
        # author. Both admission scans must compare the actual request, not the
        # historical reader we constructed to verify that chain. A different
        # LINE keeps its own identity under the dispatcher's current authority.
        if owner.task_key == key:
            request = owner.frozen
            if (request['session_id'] != frozen['session_id']
                    or os.path.normcase(request['worktree']) != os.path.normcase(frozen['worktree'])
                    or request['operation'] != 'resume'
                    or request.get('launcher') != recovery.launcher):
                raise IntentConflict('recovered task request requires the original session/worktree and ordinary resume')
        return True
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise IntentConflict('incomplete or corrupt completed recovery chain: ' + str(exc)) from exc
