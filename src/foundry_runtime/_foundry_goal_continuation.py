"""Coding continuation and provider handoff through the existing goal lifecycle."""
import json
from pathlib import Path

from ._model_catalogue import model


def _matches_manager_assignment(path, record, decision):
    """Read a legacy assignment from its bound native input and frozen digest."""
    import hashlib
    from ._execution_plan_prompt import execution_plan_prompt
    binding = record.get('turn_binding') or {}
    if not binding or not decision.get('manager'):
        return False
    log = path.with_name(path.name.removesuffix('.package.json') + '.log')
    heading = 'Continue the same task from the retained worker result.'
    marker = heading + '\n\nRECORDED EXECUTION DECISION\n'
    try:
        with log.open('rb') as stream:
            lines = stream.read(1024 * 1024).decode('utf-8', 'replace').splitlines()
        for line in lines:
            # Thread/resume includes old turns. Only this attempt's input binds
            # its assignment; do not parse the accumulated conversation response.
            if not line.startswith('stdout: ') or '"method"' not in line[:100] or '"userMessage"' not in line[:250]:
                continue
            row = json.loads(line[8:])
            params = row.get('params') or {}
            item = params.get('item') or {}
            if (row.get('method') != 'item/started' or item.get('type') != 'userMessage'
                    or params.get('threadId') != binding.get('thread_id')
                    or params.get('turnId') != binding.get('turn_id')):
                continue
            text = ''.join(part['text'] for part in item.get('content', []) if part.get('type') == 'text')
            if marker not in text:
                return False
            retained, _ = json.JSONDecoder().raw_decode(text.split(marker, 1)[1])
            source = execution_plan_prompt(heading, retained)
            return (retained == decision and source in text
                    and hashlib.sha256(source.encode('utf-8')).hexdigest() == record['frozen']['order_sha256'])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
    return False


def retained_dispatch(owner, folder, manifest):
    """Read the exact pending dispatch for both status and explicit recovery."""
    from ._project_execution import from_record
    from ._foundry_dispatch import _project_adapter
    from ._dispatch_package import read_intent
    project = from_record(manifest['project_record']) if manifest.get('project_record') is not None else None
    adapter = _project_adapter(project)
    reference = manifest.get('pending_dispatch')
    if reference:
        owner.goal_usage._reference(reference)
        paths = [Path(reference['path'])]
    else:
        # Declared reader for older goals that did not persist the package link.
        # Never pick by recency when several invocations could own the boundary.
        paths = list(adapter.evidence(manifest['tag']).glob('dispatch*.package.json'))
    candidates = []
    previous = None
    prior = manifest.get('coding_checkpoint')
    if prior:
        # Preserve the manifest's input across a crash between checkpoint and
        # manifest publication. This is immutable evidence, not another owner.
        archived = folder / ('coding-checkpoint-' + prior['sha256'] + '.json')
        source = prior if owner._fact(prior['path']) == prior else {**prior, 'path': str(archived)}
        raw = owner.goal_usage._reference(source)
        previous = json.loads(raw)
    prior_logs = {Path(fact['path']).resolve() for fact in (previous or {}).get('evidence', []) if fact['path'].endswith('.log')}
    for path in paths:
        if '.reconciled.' in path.name:
            continue
        package = json.loads(path.read_bytes())
        journal, record = read_intent(package, project_record=manifest.get('project_record'))
        frozen = record['frozen']
        log = path.with_name(path.name.removesuffix('.package.json') + '.log')
        if (package.get('tag') == manifest['tag'] and record.get('launcher') == 'app-server'
                and Path(frozen['worktree']).resolve() == Path(manifest['worktree']).resolve()
                and package.get('attempt_id') == record.get('attempt_id')
                and (reference or frozen.get('order_sha256') == manifest['order']['sha256']
                     or previous and frozen.get('session_id') == previous.get('coding_session'))
                and log.resolve() not in prior_logs
                and record['state'] in ('active', 'unknown', 'completed')):
            candidates.append((path, journal, record))
    if len(candidates) > 1 and manifest.get('execution_recovery'):
        # Legacy manifests lack pending_dispatch. Their verified recovery grant
        # already names the exact interrupted attempt; never select by timestamp.
        fact = manifest['execution_recovery']
        decision = json.loads(owner.goal_usage._reference(fact))
        predecessor = decision['predecessor']
        selected = [row for row in candidates
                    if row[2]['intent_id'] == predecessor['intent_id']
                    and row[2].get('attempt_id') == predecessor['attempt_id']
                    and row[2].get('recovery_authorization', {}).get('decision_sha256') == fact['sha256']]
        if decision.get('task_key') == manifest['tag'] and len(selected) == 1:
            candidates = selected
    if (len(candidates) > 1 and not reference and not manifest.get('execution_recovery')
            and manifest.get('execution_decision', {}).get('manager')):
        selected = [row for row in candidates
                    if _matches_manager_assignment(row[0], row[2], manifest['execution_decision'])]
        if len(selected) == 1:
            candidates = selected
    if len(candidates) != 1:
        raise owner.GoalBlocked('manager hand-back needs one exact retained dispatch; found ' + str(len(candidates))
                                + '; assignment unmatched within the first 1 MiB of the native log')
    path, journal, record = candidates[0]
    return path, journal, record, previous


def restore_completed_feedback(owner, folder, manifest, *, checkpoint_path=None, publish=True):
    """Close a transport-cleanup race and restore the existing manager boundary."""
    from ._execution_intent import ExecutionIntent
    path, journal, record, previous = retained_dispatch(owner, folder, manifest)
    log = path.with_name(path.name.removesuffix('.package.json') + '.log')
    record = ExecutionIntent.settle_completed_turn(journal.parents[3], manifest['tag'], record['frozen'],
        intent_id=record['intent_id'], log=log)
    # Completed means the execution owner verified terminal and cleanup. Its
    # result can be a provider failure and must reach the manager as that failure.
    rc = record['returncode']
    facts = [v for v in record['evidence'].values() if v is not None]
    session = record['turn_binding']['thread_id']
    options = dict(manifest['manager_start']['options'])
    # The settled dispatch is the replacement worker. Its next turn resumes
    # this session even if publication stopped before consuming the transfer.
    options.pop('handover', None)
    checkpoint = {**{k: manifest[k] for k in ('order', 'run_id', 'worktree', 'base_commit')},
        **manifest['manager_start'], 'options': options, 'schema_version': 1, 'coding_session': session,
        'returncode': rc, 'evidence': [*(previous or {}).get('evidence', []), *facts],
        'coding_usage': {'managed_turns': [*((previous or {}).get('coding_usage') or {}).get('managed_turns', []),
                                         owner.goal_usage.capture(log, manifest['tag'])]}}
    from ._goal_intervention import carry_events
    manifest.update(manager_phase='feedback_pending', manager_packet=carry_events(manifest, {
            'stage': 'worker_returned', 'run_id': manifest['run_id'], 'coding_session': session,
            'worker_evidence': facts, 'returncode': rc,
            'observed_head': owner._git(manifest['worktree'], 'rev-parse', 'HEAD').decode().strip()}))
    owner._publish_coding_checkpoint(folder, manifest, checkpoint, checkpoint_path=checkpoint_path, publish=publish)
    return manifest


def recover_worker(owner, order, folder, manifest, answer, recovery_file, *, migrate_runtime=False):
    """Recover one uncertain worker through its goal, never its older rc0 turn."""
    from ._recovery_authorization import RecoveryAuthorization
    from ._project_execution import from_record
    if (folder / 'outcome.json').exists():
        return json.loads((folder / 'outcome.json').read_bytes())
    if not isinstance(answer, str) or not answer.strip():
        raise owner.GoalBlocked('worker recovery requires the original pending dispatch and explicit answer')
    # The goal may have written a new checkpoint before its manifest pointer.
    # Recovery configuration belongs only to the producer's archived source;
    # the dispatcher determines whether its successor already completed.
    declared = json.loads(Path(recovery_file).read_bytes())
    project = from_record(manifest.get('project_record'))
    recovery = RecoveryAuthorization.load(recovery_file, task_key=manifest['tag'], worktree=manifest['worktree'],
        session_id=declared['successor']['session_id'], candidate_head=None, amendment=answer.encode('utf-8'))
    from ._execution_intent import ExecutionIntent
    intents = project.common_dir / 'pdcl-dispatch' / ('task-' + manifest['tag']) / 'intents'
    predecessor, _, _ = ExecutionIntent.read_history(intents / declared['predecessor']['intent_id'], task_key=manifest['tag'])
    authorization = predecessor.get('recovery_authorization')
    completed = False
    if authorization and authorization['decision_sha256'] == recovery.decision_sha256:
        successor, _, _ = ExecutionIntent.read_history(intents / authorization['successor_intent_id'], task_key=manifest['tag'])
        if successor['state'] == 'completed':
            ExecutionIntent._verify_completion(successor)
            completed = True
    # A published authorization can precede a failed admission. The old successful
    # checkpoint must not turn that still-unstarted recovery into ordinary resume.
    if completed and manifest.get('execution_recovery') == owner._fact(recovery_file) and manifest.get('manager_phase') != 'dispatch_pending':
        current = json.loads(owner.goal_usage._reference(manifest['coding_checkpoint']))
        facts = [value for value in successor['evidence'].values() if value is not None]
        if (current.get('coding_session') == declared['successor']['session_id']
                and current.get('returncode') == successor['returncode']
                and all(fact in current.get('evidence', []) for fact in facts)):
            return owner._recover_manager_feedback(order, folder, manifest, answer=answer)
    saved = json.loads(owner.goal_usage._reference(recovery.data['successor']['foundry_manifest']))
    checkpoint = json.loads(owner.goal_usage._reference(saved['coding_checkpoint']))
    for key in ('order', 'run_id', 'worktree', 'base_commit'):
        if saved.get(key) != manifest.get(key):
            raise owner.GoalBlocked('worker recovery belongs to another retained goal: ' + key)
    if any(saved.get(key) != manifest.get(key) for key in ('manager_state', 'execution_decision')):
        state = manifest.get('manager_state') or {}
        handoff = state.get('handoff') or {}
        source = {key: saved['manager_state'].get(key) for key in ('backend', 'model', 'effort', 'native_dir', 'last_call_id')}
        if manifest.get('execution_recovery') != owner._fact(recovery_file) or handoff.get('source_state') != source:
            raise owner.GoalBlocked('worker recovery manager differs without its recorded transfer')
        owner.goal_usage._reference(handoff['native_manifest'])
        from ._foundry_goal_transitions import manager_history
        _, latest, history = manager_history(owner, state, manifest['worktree'], allow_empty=True)
        decision = manifest['execution_decision']['manager']
        valid_decision = (decision == saved['execution_decision']['manager'] if not history else
                          decision.get('evidence') == history[-1]['receipt'] and
                          all(decision.get(key) == value for key, value in history[-1]['result'].items()))
        if (latest != state.get('last_call_id') or not valid_decision
                or {k:v for k,v in saved['execution_decision'].items() if k != 'manager'} !=
                   {k:v for k,v in manifest['execution_decision'].items() if k != 'manager'}):
            raise owner.GoalBlocked('worker recovery manager decision differs from its retained receipt')
    if (checkpoint.get('coding_session') != recovery.data['successor']['session_id']
            or any(checkpoint[key] != manifest[key] for key in ('order', 'run_id', 'worktree', 'base_commit'))):
        raise owner.GoalBlocked('worker recovery checkpoint belongs to another retained goal')
    project.require_task(manifest['tag'], Path(manifest['worktree']), mode='resume', recovery=recovery)
    options = dict(checkpoint['options'])
    options.pop('migrate_recovered_manager', None)
    options.update(project_record=manifest['project_record'], manager_state=manifest['manager_state'],
                   execution_decision=manifest['execution_decision'])
    with owner._run_lock(manifest['manager_state']['native_dir']):
        return owner.goal_execution.run_owned(order, folder, Path(checkpoint['executable']),
            target_worktree=Path(manifest['worktree']), coding_session=checkpoint['coding_session'],
            retained={**checkpoint, 'returncode': None, 'manifest': manifest},
            recovery_file=recovery_file, manager_answer=answer, worker_recovery=True,
            migrate_recovered_manager=migrate_runtime or manifest.get('worker_recovery_migration', False), **options)


def continue_coding(owner, order, folder, manifest, answer, *, team_profile=None, target_provider=None, migrate_runtime=False,
                    manager_profile=None, manager_migration=False):
    """One explicit answer through the existing resume and review owners."""
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("coding continuation needs a nonempty operator answer")
    prior_path = folder / "outcome.json"
    prior_status = json.loads(prior_path.read_bytes()).get('status') if prior_path.exists() else None
    if prior_status is not None and prior_status not in {"coding_incomplete", "provider_capacity_exhausted", "awaiting_input", "artifacts_delivered", "project_handoff", 'execution_unknown'}:
        raise owner.GoalBlocked("answer continuation is for unfinished coding; use recorded review recovery or amendment")
    if manifest.get("review_checkpoint"):
        raise owner.GoalBlocked("coding already completed; recover review without an answer")
    manager_input = manifest.get('manager_phase') == 'awaiting_input'
    path = folder / "coding-checkpoint.json"
    if manager_input:
        checkpoint = owner._manager_checkpoint(folder, manifest, allow_failed=True)
    elif not manifest.get("coding_checkpoint"):
        raise owner.GoalBlocked("legacy run has no coding checkpoint; use its recorded --resume then --foundry-review path")
    elif manifest["coding_checkpoint"] != owner._fact(path):
        raise owner.GoalBlocked("coding checkpoint differs from recorded run")
    else:
        checkpoint = json.loads(path.read_bytes())
    if prior_status == 'execution_unknown' and not checkpoint.get('returncode'):
        raise owner.GoalBlocked('uncertain goal has no failed coding completion; reconcile its existing execution')
    if migrate_runtime and target_provider is None:
        target_provider = 'claude' if checkpoint['options'].get('coding_launcher') == 'claude' else 'codex'
    if ((not manager_input and checkpoint.get("schema_version") != 1) or any(checkpoint.get(k) != manifest.get(k)
            for k in ("order", "run_id", "worktree", "base_commit"))):
        raise owner.GoalBlocked("coding checkpoint does not describe this run")
    if not checkpoint.get("coding_session") and not manager_input:
        raise owner.GoalBlocked("original coding session is not recorded")
    if not manager_input and not prior_path.exists() and not checkpoint["returncode"]:
        raise owner.GoalBlocked("coding returned successfully before its handoff was classified; "
                          "inspect the retained candidate and use --foundry-review, not another coding turn")
    for fact in checkpoint["evidence"]:
        if fact != owner._fact(fact["path"]):
            raise owner.GoalBlocked("retained coding evidence changed")
    if not target_provider and checkpoint['returncode']:
        from ._recovery_reconciliation import completed_coding
        from ._execution_intent import IntentConflict
        try:
            completed_coding(checkpoint)
        except (OSError, ValueError, KeyError, TypeError, IntentConflict) as exc:
            raise owner.GoalBlocked('coding continuation needs settled execution evidence: ' + str(exc)) from exc
    prior = owner._fact(prior_path if prior_path.exists() else folder / "manifest.json")
    continuation = {"prior_record": prior, "base_commit": checkpoint["base_commit"],
                    "checkpoint": manifest.get("coding_checkpoint") or owner._fact(folder / 'manifest.json'), "answer": answer}
    options = dict(checkpoint["options"])
    if manifest.get('task_staffing'):
        options['task_staffing'] = dict(manifest['task_staffing'])
    if manager_input or (target_provider and manifest.get('manager_state') is not None):
        continuation['manager_input'] = owner._fact(folder / 'manifest.json')
        options.update(manager_state=manifest['manager_state'], execution_decision=manifest['execution_decision'])
    if (manifest.get('project_record') is not None
            and manifest['project_record'] != options.get('project_record')):
        options['project_record'] = manifest['project_record']
        continuation['project_record'] = manifest['project_record']
    if manifest.get('manager_phase') == 'artifacts_pending':
        options['manager_state'] = manifest['manager_state']
    if target_provider:
        continuation["target_provider"] = target_provider
    if migrate_runtime or manager_migration:
        from ._runtime_provenance import observe
        continuation['runtime_transfer'] = observe()
    if manager_migration:
        options['migrate_recovered_manager'] = True
    if team_profile is not None:
        team = owner.resolve_native_team(team_profile, backend=target_provider or ("claude" if options.get("coding_launcher") == "claude" else "codex"))
        continuation["team_override"] = team
        options["native_team"] = team
        decision = dict(options.get("execution_decision") or {})
        decision["team"] = {"profile": team_profile, "source": "explicit",
                            "reason": "Operator selected the team configuration for this answer continuation."}
        options["execution_decision"] = decision
    prior_review = manifest.get("amendment") or (manifest.get("continuation") or {}).get("prior_review")
    if prior_review:
        continuation["prior_review"] = prior_review
    parent = continuation_parent(folder)
    # Sibling invocations keep long conversations at a constant path depth and
    # remain visible to the existing per-order evidence discovery.
    target = parent / ("continue-" + owner._sha(owner._json_bytes(continuation))[:16])
    if target.exists():
        saved = json.loads((target / "manifest.json").read_bytes())
        if saved.get("continuation") != continuation:
            raise owner.GoalBlocked("existing continuation belongs to different inputs")
        if (target / "outcome.json").exists():
            result = json.loads((target / "outcome.json").read_bytes())
        elif not any(saved.get(key) for key in ('coding_checkpoint', 'review_checkpoint', 'manager_state')):
            # Admission may fail after the continuation manifest was written.
            # Retry through the same dispatch owner, which still reconciles any
            # live/unknown intent; directory existence is not coding completion.
            frozen = saved.get('continuation_start', {}).get('options')
            if frozen is None:
                frozen = dict(options)
                coding = saved.get('requested_compute', {}).get('coding') or {}
                if frozen.get('role_compute'):
                    roles = frozen['role_compute']
                    frozen['role_compute'] = {**roles, 'roles': {**roles['roles'], 'coding': coding}}
                    frozen.update(coding_model=None, coding_effort=None)
                else:
                    frozen.update(coding_model=coding.get('model'), coding_effort=coding.get('effort'))
                for key in ('coding_launcher', 'reviewers', 'native_team', 'compute_assignment',
                            'execution_decision', 'handover', 'project_record'):
                    if key in saved:
                        frozen[key] = saved[key]
            executable = saved.get('continuation_start', {}).get('executable')
            if executable is None:
                executable, frozen = fresh_executables(frozen)
            if manager_profile and not (frozen.get('manager_state') or frozen.get('manager_profile')):
                frozen = {**frozen, 'manager_profile': manager_profile}
            result = owner._run(order, target, Path(executable),
                target_worktree=Path(checkpoint['worktree']), coding_session=checkpoint['coding_session'],
                continuation=continuation, retained={'manifest': saved, 'run_id': saved['run_id'],
                    'base_commit': saved['base_commit'], 'returncode': None, 'evidence': [],
                    'coding_usage': owner.goal_usage._unknown('continuation admission retry')}, **frozen)
        else:
            result = owner.recover_goal(order, target)
    else:
        if target_provider:
            options = handoff_options(owner, folder, manifest, checkpoint, continuation, options, target_provider,
                                      migrate_runtime=migrate_runtime)
        if manager_profile and not (options.get('manager_state') or options.get('manager_profile') or manifest.get('manager_state')):
            options['manager_profile'] = manager_profile
        executable, options = fresh_executables(options)
        result = owner._run(order, target, Path(executable),
            target_worktree=Path(checkpoint["worktree"]), coding_session=checkpoint["coding_session"],
            continuation=continuation, **options)
    return {**result, "continuation_evidence": str(target),
            "result_outcome": owner._fact(target / 'outcome.json')}


def continuation_parent(folder):
    parent = folder.parent
    path = parent / 'manifest.json'
    if path.exists():
        record = json.loads(path.read_bytes())
        if record.get('mode') == 'auto-amend' and Path(record.get('active_round', '')).resolve() == folder:
            parent = parent.parent
    return parent


def consumed_continuation(owner, folder, manifest):
    """Resolve an answered question by its retained native chain, never mtime.

    Historical continuations intentionally leave the question outcome immutable.
    Its caller must follow the recorded answer when recovering without a new answer
    or upgrading; a newer native call alone is not proof of a consumed decision.
    """
    state = manifest.get('manager_state') or {}
    if not state.get('native_dir'):
        return folder, manifest
    records = []
    for path in continuation_parent(folder).glob('continue-*/manifest.json'):
        saved = json.loads(path.read_bytes())
        prior = (saved.get('continuation') or {}).get('prior_record') or {}
        if not prior.get('path'):
            continue
        if ((saved.get('manager_state') or {}).get('native_dir') != state['native_dir']
                or saved.get('worktree') != manifest.get('worktree')
                or saved.get('order') != manifest.get('order')):
            continue
        records.append((path.parent, saved, prior))
    # Several answers may form a chain of sibling continuations. Follow their
    # declared prior records; neither lexicographic names nor timestamps order it.
    reachable, candidates = {folder}, []
    while True:
        linked = [(path, saved, prior) for path, saved, prior in records
                  if path not in reachable and Path(prior['path']).resolve().parent in reachable
                  and Path(prior['path']).name == 'outcome.json']
        if not linked:
            break
        for path, saved, prior in linked:
            owner.goal_usage._reference(prior)
            reachable.add(path)
            candidates.append((path, saved))
    if not candidates:
        return folder, manifest
    from ._foundry_goal_transitions import manager_history
    _, latest, _ = manager_history(owner, state, manifest['worktree'])
    if latest == state.get('last_call_id'):
        return folder, manifest
    consumed = [(path, saved) for path, saved in candidates
                if saved.get('manager_state', {}).get('last_call_id') == latest]
    if len(consumed) != 1:
        raise owner.GoalBlocked('manager history has no unique consumed continuation; inspect the retained continuation paths')
    return consumed[0]


def fresh_executables(options):
    """A coding continuation has no admitted review calls; select new CLI custody.

    Preserve model/effort assignments. The new continuation_start freezes paths
    for admission retries; worker sessions and admitted manager calls are separate.
    Historical checkpoints and native receipts stay intact.
    """
    from .foundry_native_support import resolve_executable
    backend = options.get('backend', 'codex')
    selected = {name: str(resolve_executable(name)) for name in dict.fromkeys(
        [backend, *(row['backend'] for row in options.get('reviewers') or [])])}
    options = dict(options)
    if options.get('reviewers') is not None:
        options['reviewers'] = [{**row, 'executable': selected[row['backend']]}
                                for row in options['reviewers']]
    return selected[backend], options



def _admission(owner, folder, manifest):
    """Follow existing continuation/amendment references to the original receipt."""
    seen = set()
    while str(folder) not in seen:
        seen.add(str(folder))
        receipt = manifest.get("predispatch") or (manifest.get("handover") or {}).get("predispatch")
        if receipt:
            owner.goal_usage._reference(receipt)
            return receipt
        prior = ((manifest.get("continuation") or {}).get("prior_record")
                 or (manifest.get("amendment") or {}).get("prior_outcome"))
        if not prior:
            break
        owner.goal_usage._reference(prior)
        folder = Path(prior["path"]).parent
        manifest = json.loads((folder / "manifest.json").read_bytes())
    raise owner.GoalBlocked("handoff needs the retained original admission receipt")


def handoff_options(owner, folder, manifest, checkpoint, continuation, options, provider, *, migrate_runtime=False):
    source = "claude" if options.get("coding_launcher") == "claude" else "codex"
    if source == provider and not migrate_runtime:
        raise ValueError("handoff target is the current provider; omit --handoff-provider to resume")
    cleanup = [json.loads(Path(f["path"]).read_bytes()) for f in checkpoint["evidence"]
               if f["path"].endswith(".cleanup.json")]
    if not cleanup or not all(c.get("cleanup_verified") is True and c.get("root_exited") is True
                              and c.get("job_active_processes") == 0 for c in cleanup):
        raise owner.GoalBlocked("original coding execution has not settled; reconcile its existing dispatch first")
    tree = Path(checkpoint["worktree"])
    # Observe the actual unfinished workspace; no reset, staging or implicit commit.
    work = {"head": owner._git(tree, "rev-parse", "HEAD").decode().strip(),
            "status": owner._git(tree, "status", "--porcelain=v1").decode("utf-8", errors="replace")}
    packet = {"source_provider": source, "target_provider": provider,
              "prior_coding_session": checkpoint["coding_session"], "worktree": str(tree),
              "base_commit": checkpoint["base_commit"], "workspace": work,
              "original_order": checkpoint["order"], "checkpoint": continuation["checkpoint"],
              "evidence": checkpoint["evidence"], "predispatch": None if manifest.get("project_record") else _admission(owner, folder, manifest),
              **({"project_record": manifest["project_record"]} if manifest.get("project_record") else {})}
    if "team_override" in continuation:
        packet["team_override"] = continuation["team_override"]
    if source == provider:
        coding = ((options.get('role_compute') or {}).get('roles', {}).get('coding') or
                  {'backend': provider, 'model': options.get('coding_model'), 'effort': options.get('coding_effort')})
    else:
        coding = {"backend": provider, "model": model("opus" if provider == "claude" else "sol"), "effort": "high"}
    options = dict(options)
    options.update(coding_launcher=options.get('coding_launcher', 'exec') if source == provider else
                   "claude" if provider == "claude" else "app-server",
                   app_server_policy=options.get('app_server_policy') if source == provider else None, handover=packet,
                   native_team=continuation.get("team_override") or (options.get('native_team') if source == provider else owner.resolve_native_team(
                       "adaptive" if options.get("native_team") and options["native_team"].get("profile") == "adaptive" else "none",
                       backend=provider)))
    if options.get("role_compute"):
        roles = options["role_compute"]
        options["role_compute"] = {**roles, "roles": {**roles["roles"], "coding": coding}}
        options.update(coding_model=None, coding_effort=None)
    else:
        options.update(coding_model=coding["model"], coding_effort=coding["effort"])
    assignment = options.get("compute_assignment")
    if assignment:
        options["compute_assignment"] = {**assignment, "coding_role": "handover",
            "roles": {**assignment.get("roles", {}), "handover": {**coding, "source": "explicit-handoff"}}}
    decision = dict(options.get("execution_decision") or {})
    decision.pop("correction", None)
    options["execution_decision"] = {**decision, "handover": packet}
    return options


def dispatch_handoff(owner, order, handover, message, decision, policy, coding):
    """One dispatch consumer for automatic BLOCK handover and explicit recovery."""
    from . import _foundry_dispatch as dispatch
    receipt = handover.get("predispatch")
    if policy.get("project") is None and (not isinstance(receipt, dict) or not receipt.get("path")):
        raise owner.GoalBlocked("handover requires the original admission receipt; amendment lineage has none")
    return dispatch.dispatch_order(order, **policy, **(coding or {}),
        predispatch_receipt=receipt["path"] if receipt else None,
        execution_decision={**(decision or {}), "handover": handover},
        continuation_prompt=message + "\n\nProvider handover (new native session; retain completed work and inspect referenced evidence):\n"
                            + json.dumps(handover, ensure_ascii=False))
