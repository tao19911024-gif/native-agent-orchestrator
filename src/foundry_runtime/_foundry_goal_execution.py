"""One goal coding lifecycle; Foundry goal owns shared contracts and review."""
from __future__ import annotations
from pathlib import Path
from ._runtime_provenance import observe
from ._task_deadline import goal_scope

@goal_scope
def run_owned(order: Path, evidence_dir: Path, executable: Path, *, target_worktree=None,
             backend="codex", analyst_model=None, analyst_effort=None,
             coding_model=None, coding_effort=None, role_compute=None,
             coding_launcher="app-server", app_server_policy=None,
             predispatch_receipt=None, review_range=None, amend_from=None, coding_session=None, amend_prompt=None,
             compute_assignment=None, native_team=None, continuation=None, execution_decision=None, launch_context=None,
                  reviewers=None, recovery_file=None, manager_state=None, retained=None,
                  task_staffing=None, handover=None, project_record=None, manager_answer=None,
                  follow_up_goal=None, migrate_recovered_manager=False, worker_recovery=False, task_deadline_at=None,
                  review_author_models=None, initializing=False, disposable_scratch=False, **_legacy):
    """Execute the three-part task, then judge the frozen commit twice."""
    from . import foundry_goal as owner
    from .foundry_goal import (
        STOP_OUTCOMES, GoalBlocked, Path, _amendment, _candidate,
        _fact, _finish_review, _git, _json_bytes, _record_usage,
        _review_focus, _review_range, _session_from_log, _task_identity,
        _write, amendment_prompt, artifacts, compute_for_kind, freeze_roles,
        goal_continuation, goal_usage, incomplete, json, resolve_executable,
        resolve_goal_compute, review_assignments, stop_cause, sys,
        team_kwargs, uuid,
    )
    from . import _foundry_dispatch as dispatch
    from ._project_execution import from_record, AdmissionRefused
    project = from_record(project_record) if project_record is not None else None
    adapter = dispatch._project_adapter(project)
    plan_prompt = adapter.execution_plan_prompt
    order = Path(order).resolve(strict=True)
    evidence_dir = Path(evidence_dir).resolve()
    executable = Path(executable).resolve(strict=True)
    reviewers = review_assignments(reviewers)
    if reviewers is not None:
        reviewers = [{**row, "executable": str(Path(row["executable"]).resolve(strict=True)
                       if row.get("executable") else resolve_executable(row["backend"]))} for row in reviewers]
    tag = _task_identity.normalized_line(order)
    if (not continuation and retained is None and bool(amend_from) != bool(coding_session)) or (amend_from and review_range is not None):
        raise ValueError("amend-from and coding-session belong together in goal mode")
    amendment, recovery = None, None
    if review_range is None:
        authority = project if project else adapter.goal_authority()
        tree = Path(target_worktree or (project.workspace if project else authority.resolve_task(tag).worktree)).resolve(strict=True)
        if recovery_file is not None:
            from ._recovery_authorization import RecoveryAuthorization
            recovery = RecoveryAuthorization.load(recovery_file, task_key=tag, worktree=tree,
                session_id=coding_session, candidate_head=None, amendment=None,
                source_verifier=adapter.recovery_source_verifier)
        if retained is not None:
            base = retained['base_commit']
            amendment = retained['manifest'].get('amendment')
            if retained['returncode'] is None:
                mode = 'resume' if amendment or coding_session else 'dispatch'
                authority.require_task(tag, tree, mode=mode, **({'recovery': recovery} if recovery else {}))
        elif continuation:
            authority.require_task(tag, tree, mode='resume' if coding_session else 'dispatch')
            base = continuation["base_commit"]
            _git(tree, "merge-base", "--is-ancestor", base, "HEAD")
        elif amend_from:
            authority.require_task(tag, tree, mode="resume", **({'recovery': recovery} if recovery else {}))
            amendment = _amendment(order, tree, amend_from, coding_session, operator_additions=amend_prompt)
            base = amendment["base_commit"]
        else:
            authority.require_task(tag, tree)
            base = _candidate(tree)
    else:
        tree = Path(target_worktree).resolve(strict=True)
        base, candidate = _review_range(tree, *review_range)
    if project is not None and tree != project.workspace.resolve():
        raise ValueError("goal workspace differs from project descriptor")
    if backend not in {"codex", "claude"} or coding_launcher not in {"exec", "app-server", "claude"}:
        raise ValueError("unknown Foundry backend or coding transport")

    coding_backend = "claude" if coding_launcher == "claude" else "codex"
    analyst_compute, coding_compute, role_policy = resolve_goal_compute(
        role_compute, coding_backend=coding_backend, backend=backend, analyst_model=analyst_model,
        analyst_effort=analyst_effort, coding_model=coding_model,
        coding_effort=coding_effort)

    evidence_dir.mkdir(parents=True, exist_ok=retained is not None or initializing)
    receipt_path = (Path(predispatch_receipt).resolve(strict=True)
                    if amendment is not None and predispatch_receipt else None)
    if project is None and review_range is None and amendment is None and continuation is None and retained is None:
        receipt_path, admitted = adapter.ensure_goal_admission(
            order, tree, evidence_dir, predispatch_receipt, coding_backend)
        if not admitted:
            result = {"status": "admission_blocked", "predispatch": _fact(receipt_path)}
            _write(evidence_dir / "outcome.json", result)
            return result
    run_id = retained['run_id'] if retained is not None else uuid.uuid4().hex
    manifest = {"schema_version": 1, "run_id": run_id, "order": _fact(order),
                "predispatch": _fact(receipt_path) if receipt_path else None, "worktree": str(tree),
                "base_commit": base, "tag": tag, "runtime_provenance": observe(adapter=adapter),
                **({"coding_launcher": coding_launcher} if review_range is None else {}),
                "requested_compute": {
                    "coding": ({"backend": coding_backend, **coding_compute} if coding_compute else None)
                              if review_range is None else None,
                    "review": {"backend": backend, **compute_for_kind(
                        freeze_roles(role_policy), "review", analyst_compute)},
                    "scope": "requested settings; omitted model/effort inherit native defaults"}}
    from ._followup import origin, announce
    manifest['originating_session'] = origin()
    if task_deadline_at is not None:
        manifest['task_deadline_at'] = task_deadline_at
    if follow_up_goal is not None:
        manifest['follow_up_goal'] = str(Path(follow_up_goal).resolve())
    if project_record is not None:
        manifest["project_record"] = project_record
    if compute_assignment is not None:
        manifest["compute_assignment"] = compute_assignment
    if task_staffing is not None:
        manifest["task_staffing"] = task_staffing
    if handover is not None:
        manifest["handover"] = handover
    if reviewers is not None:
        manifest["reviewers"] = reviewers
        manifest["requested_compute"]["review"] = None
        manifest["requested_compute"]["reviews"] = reviewers
    if native_team is not None:
        manifest["native_team"] = native_team
    if launch_context is not None:
        manifest["launch_context"] = launch_context
    if execution_decision is not None:
        manifest["execution_decision"] = execution_decision
    if manager_state is not None:
        if task_deadline_at is not None:
            manager_state = {**manager_state, 'task_deadline_at': task_deadline_at}
        manifest['manager_state'] = manager_state
    execution_options = {
        **({'task_deadline_at': task_deadline_at} if task_deadline_at is not None else {}),
        **({'amend_from': str(amend_from)} if amend_from is not None else {}),
        **({'amend_prompt': amend_prompt} if amend_prompt is not None else {}),
        'backend': backend, 'analyst_model': analyst_model, 'analyst_effort': analyst_effort,
        **({'reviewers': reviewers} if reviewers is not None else {}),
        'coding_model': coding_model, 'coding_effort': coding_effort, 'role_compute': role_policy,
        'coding_launcher': coding_launcher, 'app_server_policy': app_server_policy,
        'compute_assignment': compute_assignment, 'native_team': native_team,
        **({'launch_context': launch_context} if launch_context is not None else {}),
        **({'migrate_recovered_manager': True} if migrate_recovered_manager else {}),
        **({'project_record': project_record} if project_record is not None else {})}
    if manager_state is not None:
        manifest['manager_phase'] = 'assignment_pending'
        manifest['manager_start'] = {'executable': str(executable), 'coding_session': coding_session,
            'options': {**execution_options, **({'recovery_file': str(recovery_file)} if recovery_file else {})}}
    if review_range is not None:
        manifest["candidate_commit"] = candidate
    if amendment is not None:
        manifest["amendment"] = amendment
    if recovery_file is not None:
        manifest['execution_recovery'] = _fact(recovery_file)
    if continuation is not None:
        manifest["continuation"] = continuation
        manifest['continuation_start'] = {
            'executable': str(executable),
            'options': {**execution_options, 'execution_decision': execution_decision,
                        'handover': handover}}
    manifest["delivery_context"] = goal_usage.delivery_context(
        "review-only" if review_range is not None else "amendment" if amendment else "goal",
        order=manifest["order"], run_id=run_id, amendment=amendment,
        requested_compute=manifest["requested_compute"])
    if continuation is not None:
        manifest["delivery_context"].update(mode="continuation", continues=continuation["prior_record"])
    if retained is not None:
        manifest = dict(retained['manifest'])
        if recovery_file is not None:
            manifest['execution_recovery'] = _fact(recovery_file)
        if manifest.get('predispatch'):
            goal_usage._reference(manifest['predispatch'])
            receipt_path = Path(manifest['predispatch']['path'])
    elif manager_state is None and ((native_team and native_team.get('expert_consultation')) or
            (execution_decision or {}).get('tier_escalation', {}).get('action') == 'expert_consultation_required'):
        from ._compute_selection import member_roles
        manifest['expert_state'] = {'native_dir': str(evidence_dir / 'expert-owner'), 'context_reuse': 1,
            'expert': {'backend': coding_backend, **((native_team or {}).get('member_roles') or member_roles(coding_backend))['expert']}}
        if project_record:
            manifest['expert_state']['execution_profiles'] = project_record['context']['execution_profiles']
        manifest['expert_packet'] = {'prior_review': amendment['prior_outcome'] if amendment else None,
            'tier_escalation': (execution_decision or {}).get('tier_escalation')}
        manifest['manager_start'] = {'executable': str(executable), 'coding_session': coding_session,
                                    'options': execution_options}
    if migrate_recovered_manager:
        manifest['worker_recovery_migration'] = True
    if manager_state is not None:
        from ._goal_intervention import input_root
        manifest['intervention'] = {'schema_version': 1,
            'root': str(input_root(evidence_dir, manifest))}
    continued_coding = None
    if (continuation or {}).get('manager_input') and retained is None:
        previous = json.loads(goal_usage._reference(continuation['manager_input']))
        packet, _ = owner.order_revision.manager_input(owner, previous, previous['manager_packet'])
        manifest['manager_packet'] = {**packet, 'operator_answer': continuation['answer'], 'run_id': run_id,
                                      'order': manifest['order']}
        if previous.get('coding_checkpoint'):
            continued_coding = {**json.loads(goal_usage._reference(previous['coding_checkpoint'])),
                'run_id': run_id, 'order': manifest['order'], 'options': {**execution_options, 'manager_state': manager_state,
                                             'execution_decision': execution_decision}}
            _write(evidence_dir / 'coding-checkpoint.json', continued_coding)
            manifest['coding_checkpoint'] = _fact(evidence_dir / 'coding-checkpoint.json')
            manifest['manager_phase'] = 'feedback_pending'
    from . import _goal_scratch
    if disposable_scratch and retained is None and not continuation and not amend_from and review_range is None:
        _goal_scratch.register(evidence_dir, manifest)
    project_plan_prompt = plan_prompt
    def plan_prompt(body, decision):
        return _goal_scratch.prompt(project_plan_prompt(body, decision), manifest)
    _write(evidence_dir / "manifest.json", manifest)
    if retained is not None or follow_up_goal is None:
        announce(evidence_dir, manifest)

    policy = {"launcher": coding_launcher, **team_kwargs(native_team),
              **({'launch_context': launch_context} if launch_context is not None else {}),
              **({"project": project} if project is not None else {})}
    if coding_launcher == "app-server" and app_server_policy is not None:
        policy["app_server_policy"] = app_server_policy
    coding = goal_usage._unknown("dispatch did not return a receipt path")
    native, outcome = None, None
    migrated_manager = False
    def capture_live_events(events):
        from ._goal_intervention import retain_events
        manifest.pop('undelivered_live_decision', None)
        retain_events(manifest, events)
        _write(evidence_dir / 'manifest.json', manifest)

    def retain_live_decision(unchanged):
        guidance = manifest.get('live_manager_decision') or {}
        if not unchanged or guidance.get('action') != 'delegate':
            return False
        manifest['undelivered_live_decision'] = {
            'guidance': guidance, 'observed_head': manifest['live_manager_packet']['observed_head'],
            'manager_call_id': manager_state.get('last_call_id')}
        _write(evidence_dir / 'manifest.json', manifest)
        return True

    def live_decision(events):
        from ._foundry_manager import live_budget
        with live_budget() as policy:
            return bounded_live_decision(events, policy)

    def bounded_live_decision(events, live_policy):
        nonlocal manifest, manager_state, manager_packet, reviewers, migrated_manager
        from time import time, monotonic
        from . import _foundry_manager
        from ._goal_intervention import incorporate, carry_events, answer_events
        from ._foundry_goal_transitions import manager as transfer_manager, restore_opus
        from contextlib import nullcontext
        started = monotonic()
        observation = {'started_at': time(), 'state': 'running', 'stage_seconds': {},
            'automatic_timeout_seconds': live_policy['total_seconds'],
            'ordinary_timeout_seconds': live_policy['ordinary_seconds'],
            'expert_timeout_seconds': live_policy['expert_seconds'],
            'soft_warning_seconds': live_policy['soft_warning_seconds']}
        manifest['live_manager_call'] = observation
        def measured(stage, invoke):
            observation['stage'] = stage
            _write(evidence_dir / 'manifest.json', manifest)
            begin = monotonic()
            try:
                return invoke()
            finally:
                observation['stage_seconds'][stage] = observation['stage_seconds'].get(stage, 0) + monotonic() - begin
        capture_live_events(events)
        from ._context_signals import CONTEXT_RENEWAL_GUIDANCE
        packet = {'stage': 'worker_running', 'run_id': run_id, 'order': manifest['order'],
                  'observed_head': _git(tree, 'rev-parse', 'HEAD').decode().strip(),
                  'events': events, 'ownership': 'Manager decides design/readiness; current dispatch still owns worker execution.',
                  'context_renewal': CONTEXT_RENEWAL_GUIDANCE}
        packet = carry_events(manifest, packet)
        packet['unanswered_instruction'] = 'Address every unanswered_live_events entry, or explicitly request the required commander ruling.'
        manifest['live_manager_packet'] = packet
        _write(evidence_dir / 'manifest.json', manifest)
        try:
            packet, queued = incorporate(manifest['intervention']['root'], packet)
            manifest['live_manager_packet'] = packet
            manifest = measured('restore_opus', lambda: restore_opus(owner, evidence_dir, manifest, live=True))
            manager_state = manifest['manager_state']
            while True:
                try:
                    with owner._run_lock(manager_state['native_dir'], reuse_owned=True) if manager_state.get('handoff') else nullcontext():
                        response = measured('coordinate', lambda: _foundry_manager.turn(manager_state, tree, packet, operator_update=queued))
                    break
                except _foundry_manager.ManagerQuotaUnavailable as exc:
                    manifest = measured('transfer_manager', lambda: transfer_manager(owner, evidence_dir, manifest, provider='codex', migrate=False,
                        answer=None, source_locked=True, quota_receipt=exc.receipt, live=True, worker_feedback=packet))
                    manager_state = manifest['manager_state']
                    reviewers = manifest['reviewers']
                    execution_options['reviewers'] = reviewers
                    migrated_manager = True
            guidance, manager_state, usage, manager_packet = response
            manifest.update(manager_state=manager_state, manager_packet=manager_packet,
                live_manager_decision=guidance, manager_phase='dispatch_pending')
            manifest['coordination_models'] = list(dict.fromkeys(m for m in [*manifest.get('coordination_models', []), manager_state.get('model')] if m))
            manifest['coordination'] = {**manifest.get('coordination', {}), 'status': 'opus_coordination' if manager_state['backend'] == 'claude' else 'codex_quota_fallback',
                'last_decision': guidance['evidence'], 'during_worker_turn': True}
            if guidance['action'] == 'await_input':
                manifest['coordination']['commander_question'] = guidance['instruction']
            _write(evidence_dir / 'manager-usage.json', usage)
            answer_events(manifest, manager_packet, guidance)
            observation['state'] = 'decision_available'
            _write(evidence_dir / 'manifest.json', manifest)
            return ('Foundry manager decision (not an operator instruction):\n' + guidance['instruction'] +
                    ('\nReturn your written result now for the manager readiness boundary.'
                     if guidance['action'] in ('review', 'deliver_artifacts') else '') +
                    ('\nThe manager requests a commander ruling; continue only independent assigned work.'
                     if guidance['action'] == 'await_input' else ''))
        except Exception as exc:
            observation['state'] = 'failed'
            manifest['live_manager_error'] = {'message': str(exc), 'events': events}
            _write(evidence_dir / 'manifest.json', manifest)
            raise
        finally:
            observation['elapsed_seconds'] = monotonic() - started
            manifest['live_manager_call'] = observation
            _write(evidence_dir / 'manifest.json', manifest)

    phase = "coding"
    project_review = None
    def observe_project_review(head):
        nonlocal project_review
        if project is not None:
            project_review = project.review_context(evidence_dir,
                task={"tag": tag, "order": manifest["order"], "run_id": run_id},
                subject={"base_commit": base, "candidate_commit": head,
                         "working_changes": _git(tree, "status", "--porcelain=v1").decode("utf-8", errors="replace")},
                env=dispatch._utf8_env())
            if project_review["status"] != "ready":
                raise GoalBlocked("project review context requires owner action", "coding_incomplete",
                                  workspace={"project_review": project_review})

    def candidate_for_review():
        # Project delivery/handoff is observed before requiring an executor commit.
        if project is None:
            return _candidate(tree, base)
        observed_head = _git(tree, "rev-parse", "HEAD").decode().strip()
        observe_project_review(observed_head)
        candidate = _candidate(tree, base)
        if candidate != observed_head:
            raise GoalBlocked("candidate changed during project observation; review the owner's final candidate",
                              "coding_incomplete", workspace={"project_review": project_review,
                              "observed_head": observed_head, "current_head": candidate})
        return candidate

    def review_or_feedback():
        nonlocal manager_packet
        try:
            return candidate_for_review()
        except GoalBlocked as exc:
            if not manager_state or not (exc.workspace or {}).get('status_porcelain', '').strip():
                raise
            manager_packet = {**manager_packet, 'stage': 'worker_returned',
                'review_refusal': {'reason': str(exc), 'workspace': exc.workspace,
                    'instruction': 'Review requires a committed, clean candidate. Resolve the project handoff/commit prerequisites through the worker, or request an operator decision; then route again.'}}
            manifest.update(manager_packet=manager_packet, manager_phase='feedback_pending')
            _write(evidence_dir / 'manifest.json', manifest)
            return None

    try:
        rc, coding_evidence, coding_turns = None, [], []
        if review_range is None:
            if manager_state is None and manifest.get('expert_state'):
                from . import _expert_consultation as experts, _foundry_manager
                question = experts.review_request(manifest['expert_packet'])
                if question:
                    if manager_answer is not None:
                        execution_decision = {**(execution_decision or {}), 'operator_answer': manager_answer}
                    manifest['manager_phase'] = 'expert_pending'
                    _write(evidence_dir / 'manifest.json', manifest)
                    answer = experts.consult(_foundry_manager._turn, manifest['expert_state'], tree,
                                             question, manifest['expert_packet']['prior_review'])
                    execution_decision = {**(execution_decision or {}), 'expert_consultation': answer}
                    manifest.update(execution_decision=execution_decision, manager_phase='delegation_ready')
                    _write(evidence_dir / 'manifest.json', manifest)
            manager_repeat = False
            manager_packet = {"stage": "assign", "run_id": run_id, "order": manifest["order"],
                'tier_escalation': (execution_decision or {}).get('tier_escalation'),
                "prior_review": amendment['prior_outcome'] if amendment else None,
                "operator_answer": continuation["answer"] if continuation else (amendment or {}).get('operator_additions'),
                **({'question_checkpoint': continuation['checkpoint']} if continuation else {}),
                "coding_session": coding_session}
            if (manager_state or {}).get('review_convergence') == 1:
                from ._expert_consultation import review_history
                manager_packet['review_history'] = review_history(amendment)
            if manager_state is not None and not manager_state.get('last_call_id'):
                manager_packet['original_order'] = project.prompt(order.read_text(encoding='utf-8-sig')) if project else order.read_text(encoding='utf-8-sig')
            if (continuation or {}).get('manager_input'):
                manager_packet = manifest['manager_packet']
                if continued_coding:
                    rc, coding_evidence = continued_coding['returncode'], list(continued_coding['evidence'])
                    coding = continued_coding['coding_usage']
                    coding_turns = list(coding.get('managed_turns', []))
                    manager_repeat = True
            if retained is not None:
                rc, coding_evidence = retained['returncode'], list(retained['evidence'])
                coding = retained['coding_usage']
                coding_turns = list(coding.get('managed_turns', []))
                manager_repeat = rc is not None
                manager_packet = manifest.get('manager_packet', manager_packet)
                if manager_answer is not None:
                    manager_packet = {**manager_packet, 'operator_answer': manager_answer}
            if migrate_recovered_manager and (continuation or {}).get('manager_input'):
                from ._foundry_goal_transitions import manager as transfer_manager
                manifest = transfer_manager(owner, evidence_dir, manifest, provider=None,
                    migrate=True, answer=continuation['answer'], source_locked=True, worker_feedback=manager_packet)
                manager_state, manager_packet = manifest['manager_state'], manifest['manager_packet']
                migrate_recovered_manager = False
                migrated_manager = True
            while True:
                if manifest.get('manager_phase') == 'artifacts_pending' and not owner.order_revision.pending(manifest):
                    phase = 'artifacts'
                    break
                if manager_state is not None and not ((recovery_file or migrate_recovered_manager) and not manager_repeat):
                    if manifest.get('manager_phase') == 'awaiting_input' and not owner.order_revision.pending(manifest):
                        raise GoalBlocked(manifest['execution_decision']['manager']['instruction'], 'awaiting_input')
                    if manifest.get('manager_phase') == 'review_pending' and not owner.order_revision.pending(manifest):
                        phase = 'candidate'
                        candidate = review_or_feedback()
                        if candidate is not None:
                            break
                        phase = 'coding'
                    from . import _foundry_manager
                    if (compute_assignment or {}).get('profile_version', 0) >= 5 and manifest.get('manager_phase') != 'delegation_ready':
                        from ._compute_selection import execution_evidence, escalation
                        tier = escalation((coding_compute or {}).get('model'), execution_evidence(coding))
                        if tier and tier['action'] == 'expert_consultation_required' and manager_state.get('expert'):
                            key = [tier['from_model'], tier['signals']]
                            if key not in manager_state.get('tier_consultations', []):
                                manager_packet = {**manager_packet, 'tier_escalation': tier}
                    from ._foundry_goal_transitions import restore_opus
                    manifest = restore_opus(owner, evidence_dir, manifest)
                    if manager_state != manifest['manager_state']:
                        manager_state = manifest['manager_state']
                        migrated_manager = True
                    try:
                        # Launch metadata is not manager input: changing the latter
                        # would turn a retained decision replay into a new paid turn.
                        manager_state = {**manager_state, 'recorded_coding_model': (coding_compute or {}).get('model')}
                        from ._goal_intervention import incorporate, carry_events, answer_events
                        manager_packet = carry_events(manifest, manager_packet)
                        manager_packet, queued_update = incorporate(manifest['intervention']['root'], manager_packet)
                        manager_packet, revision_update = owner.order_revision.manager_input(owner, manifest, manager_packet)
                        from contextlib import nullcontext
                        with owner._run_lock(manager_state['native_dir'], reuse_owned=True) if migrated_manager else nullcontext():
                            retained_live = manifest.pop('undelivered_live_decision', None)
                            if (retained_live and not manager_answer and not queued_update and not revision_update
                                    and not manager_packet.get('unanswered_live_events')
                                    and retained_live['manager_call_id'] == manager_state.get('last_call_id')
                                    and retained_live.get('observed_head') == manager_packet.get('observed_head')
                                    and retained_live.get('observed_head') is not None
                                    and not manager_packet.get('tier_escalation')
                                    and not any(row.get('status') == 'manager_answer_required'
                                                for row in manager_packet.get('member_tier_decisions', []))):
                                manager_result = (retained_live['guidance'], manager_state,
                                    json.loads((evidence_dir / 'manager-usage.json').read_bytes()), manager_packet)
                            else:
                                manager_result = _foundry_manager.turn(manager_state, tree, manager_packet,
                                    operator_update=manager_answer is not None or queued_update or revision_update or bool(manager_packet.get('unanswered_live_events')),
                                    pending_only=manifest.get('manager_phase') == 'delegation_ready' and manager_answer is None
                                        and not queued_update and not revision_update and not manager_packet.get('unanswered_live_events'))
                    except BaseException as exc:
                        if getattr(exc, 'manager_packet', None) is not None:
                            manifest['manager_packet'] = exc.manager_packet
                            if manifest.get('manager_phase') == 'delegation_ready':
                                manifest['manager_phase'] = 'feedback_pending'
                            _write(evidence_dir / 'manifest.json', manifest)
                        if getattr(exc, 'manager_usage', None) is not None:
                            _write(evidence_dir / 'manager-usage.json', exc.manager_usage)
                        elif getattr(exc, 'analyst', None) is not None:
                            _write(evidence_dir / 'manager-usage.json', exc.analyst.evidence_snapshot())
                        if isinstance(exc, _foundry_manager.ManagerQuotaUnavailable) and coding_backend == 'codex':
                            from ._foundry_goal_transitions import manager as transfer_manager
                            manifest = transfer_manager(owner, evidence_dir, manifest, provider='codex',
                                migrate=False, answer=None, source_locked=True, quota_receipt=exc.receipt)
                            manager_state, manager_packet = manifest['manager_state'], manifest['manager_packet']
                            reviewers = manifest['reviewers']
                            execution_options['reviewers'] = reviewers
                            migrated_manager = True
                            continue
                        raise
                    manager_answer = None
                    if manager_result is not None:
                        guidance, manager_state, manager_usage, manager_packet = manager_result
                        if revision_update:
                            manifest['manager_order_revision'] = manager_packet['order_revision']['evidence']
                        answer_events(manifest, manager_packet, guidance)
                        manifest['coordination_models'] = list(dict.fromkeys(m for m in [*manifest.get('coordination_models', []), manager_state.get('model')] if m))
                        tier = manager_packet.get('tier_escalation') or {}
                        if tier and guidance.get('expert_consultation'):
                            key = [tier['from_model'], tier['signals']]
                            manager_state['tier_consultations'] = [*manager_state.get('tier_consultations', []), key]
                        execution_decision = {**(execution_decision or {}), 'manager': guidance}
                        if manager_state.get('live_coordination') == 1:
                            manifest['coordination'] = {**manifest.get('coordination', {}),
                                'status': ('codex_quota_fallback' if manager_state.get('quota_fallback') else
                                           'opus_coordination' if manager_state['backend'] == 'claude' else 'codex_coordination'),
                                'last_decision': guidance['evidence']}
                        manifest.update(manager_state=manager_state, manager_packet=manager_packet, execution_decision=execution_decision,
                                        manager_phase={'review': 'review_pending', 'deliver_artifacts': 'artifacts_pending',
                                                       'await_input': 'awaiting_input'}.get(guidance['action'], 'delegation_ready'))
                        _write(evidence_dir / 'manager-usage.json', manager_usage)
                        _write(evidence_dir / 'manifest.json', manifest)
                        if guidance['action'] == 'await_input':
                            raise GoalBlocked(guidance['instruction'], 'awaiting_input')
                        if guidance['action'] == 'deliver_artifacts':
                            phase = 'artifacts'
                            break
                        if guidance['action'] == 'review':
                            phase = 'candidate'
                            candidate = review_or_feedback()
                            if candidate is not None:
                                break
                            phase = 'coding'
                            continue
                if manager_repeat:
                    if not coding_session:
                        phase = 'coding'
                        raise GoalBlocked('manager continuation needs the original coding session', 'coding_incomplete')
                    # Observe the dispatcher's existing task status before marking
                    # a launch uncertain. If the owner must update that status,
                    # the exact completed manager decision remains resumable.
                    authority.require_task(tag, tree, mode='resume')
                if manager_state is not None and (compute_assignment or {}).get('profile_version', 0) >= 5:
                    from ._compute_selection import execution_evidence, escalation, tier_state
                    observed = execution_evidence(coding)
                    decision = escalation((coding_compute or {}).get('model'), observed)
                    execution_decision = tier_state(execution_decision, coding_compute or {}, decision, evidence=observed)
                    manifest['execution_decision'] = execution_decision
                    decision = decision or execution_decision.get('tier_reset')
                    if decision:
                        if decision['action'] in ('escalate', 'return_to_baseline'):
                            coding_compute = {'model': decision['model'], 'effort': decision['effort']}
                            manifest['requested_compute']['coding'] = {**(manifest['requested_compute'].get('coding') or {}), **coding_compute}
                            if role_policy:
                                role_policy = {**role_policy, 'roles': {**role_policy['roles'],
                                    'coding': {**role_policy['roles']['coding'], **coding_compute}}}
                                execution_options['role_compute'] = role_policy
                            else:
                                execution_options.update(coding_model=decision['model'], coding_effort=decision['effort'])
                            if manifest.get('manager_start'):
                                manifest['manager_start']['options'].update({k: execution_options[k] for k in
                                    ('role_compute', 'coding_model', 'coding_effort') if k in execution_options})
                if manager_state is not None or manifest.get('expert_state'):
                    manifest['manager_phase'] = 'dispatch_pending'
                    _write(evidence_dir / 'manifest.json', manifest)
                from ._goal_intervention import live_scope
                try:
                    with live_scope(live_decision if (manager_state or {}).get('live_coordination') == 1 else None,
                                    capture=capture_live_events, undelivered=retain_live_decision,
                                    claude=(manager_state or {}).get('claude_live_events') == 1):
                        if worker_recovery and not manager_repeat:
                            result = dispatch.resume_order(coding_session, tree, tag, manager_answer,
                                recovery_file=recovery_file, **policy, **(coding_compute or {}))
                        elif manager_repeat:
                            result = dispatch.resume_order(coding_session, tree, tag,
                                plan_prompt("Continue the same task from the retained worker result.", execution_decision),
                                **policy, **(coding_compute or {}))
                        elif handover is not None:
                            message = (continuation["answer"] if continuation is not None else
                                       amendment_prompt(order, amendment, execution_decision, project=project))
                            result = goal_continuation.dispatch_handoff(owner, order, handover,
                                message, execution_decision, policy, coding_compute)
                        elif continuation is not None and coding_session:
                            # Dispatch deduplicates resume text. Bind the answer to its
                            # predecessor so identical replies to different questions are
                            # distinct turns, while retrying the same reply stays stable.
                            message = ("Foundry answer to coding checkpoint "
                                       + continuation["checkpoint"]["sha256"] + "\n\n"
                                       + continuation["answer"])
                            if "team_override" in continuation:
                                # The dispatcher freezes compute against resume text. Bind
                                # an explicit update even for older runs without a plan.
                                message += "\n\nExplicit team configuration for this continuation:\n" + _json_bytes(
                                    continuation["team_override"]).decode("utf-8")
                            result = dispatch.resume_order(coding_session, tree, tag,
                                                           plan_prompt(message, execution_decision),
                                                           **policy, **(coding_compute or {}))
                        elif amendment is None:
                            dispatch_decision = {**(execution_decision or {}),
                                **({'disposable_scratch': manifest['disposable_scratch']}
                                   if manifest.get('disposable_scratch') else {})}
                            result = dispatch.dispatch_order(order, predispatch_receipt=receipt_path,
                                                             **({"execution_decision": dispatch_decision} if dispatch_decision else {}),
                                                             **policy, **(coding_compute or {}))
                        else:
                            result = dispatch.resume_order(coding_session, tree, tag,
                                                           amendment_prompt(order, amendment, execution_decision,
                                                               recovery.data['predecessor']['intent_id'] if recovery_file else None, project=project),
                                                           **({'recovery_file': recovery_file} if recovery_file else {}),
                                                           **policy, **(coding_compute or {}))
                except AdmissionRefused as exc:
                    if manager_state is None:
                        raise
                    from ._goal_intervention import retain_events, carry_events, refusal_signature
                    event = exc.feedback
                    import hashlib
                    event['assignment_sha256'] = hashlib.sha256(
                        execution_decision['manager']['instruction'].encode('utf-8')).hexdigest()
                    history = manifest.setdefault('refused_assignments', [])
                    event['refusal_signature'] = refusal_signature(event, tree)
                    repeated = any(
                        row['refusal_signature'] == event['refusal_signature']
                        if row.get('refusal_signature') and event['refusal_signature'] else
                        row['assignment_sha256'] == event['assignment_sha256'] for row in history)
                    history.append(event)
                    retain_events(manifest, [event])
                    manager_packet = carry_events(manifest, {**manager_packet, 'stage': 'worker_returned',
                        'admission_refused': event, 'worker_started': False})
                    manifest.update(manager_packet=manager_packet, manager_phase='feedback_pending')
                    if repeated:
                        question = ('Manager repeated an identical refused assignment or the same objection without new evidence. Commander ruling required. '
                                    + event['judgement'])
                        manifest['manager_phase'] = 'awaiting_input'
                        manifest['assignment_refusal_question'] = {'question': question,
                            'source': 'repeated_admission_refusal', 'receipt': event['receipt']}
                        _write(evidence_dir / 'manifest.json', manifest)
                        raise GoalBlocked(question, 'awaiting_input')
                    _write(evidence_dir / 'manifest.json', manifest)
                    continue
                rc, log, output = result
                if rc:
                    manifest.pop('undelivered_live_decision', None)
                executions = getattr(result, 'capacity_executions', [result])
                coding_turns.extend(goal_usage.capture(row[1], tag) for row in executions)
                coding = {'managed_turns': coding_turns} if manager_state is not None or len(coding_turns) > 1 else coding_turns[-1]
                coding_session = _session_from_log(log) if handover else coding_session or _session_from_log(log)
                turn_evidence = [fact for row in executions for fact in
                    (_fact(row[1]), _fact(row[2]), _fact(Path(row[1]).with_suffix('.cleanup.json')))]
                coding_evidence.extend(turn_evidence)
                messages = Path(log).with_suffix('.messages.jsonl')
                if messages.is_file():
                    turn_evidence.append(_fact(messages))
                    coding_evidence.append(turn_evidence[-1])
                terminal_path = Path(log).with_suffix(".terminal.json")
                terminal = json.loads(terminal_path.read_bytes()) if terminal_path.exists() else {}
                # A returned dispatch is a continuation point, not acceptance. The
                # dispatcher still owns session identity, admission and execution.
                checkpoint = {"schema_version": 1, "run_id": run_id, "order": manifest["order"],
                    "worktree": str(tree), "base_commit": base, "coding_session": coding_session,
                    "returncode": rc, "expiration_cause": stop_cause(terminal),
                    "evidence": coding_evidence + ([_fact(terminal_path)] if terminal else []),
                    "executable": str(executable), "options": {
                        **execution_options,
                        "execution_decision": execution_decision,
                    **({"manager_state": manager_state} if manager_state is not None else {})}}
                if rc == 0:
                    checkpoint['options'].pop('migrate_recovered_manager', None)
                if manager_state is not None:
                    checkpoint['coding_usage'] = coding
                    manager_packet = {"stage": "worker_returned", "coding_session": coding_session,
                        "worker_evidence": turn_evidence, "returncode": rc,
                        "observed_head": _git(tree, 'rev-parse', 'HEAD').decode().strip(), "run_id": run_id}
                    from ._goal_intervention import carry_events
                    manager_packet = carry_events(manifest, manager_packet)
                    if (compute_assignment or {}).get('profile_version', 0) >= 5:
                        from ._compute_selection import execution_evidence, escalation
                        observed = execution_evidence(coding)
                        models = sorted({f['model'] for f in observed['test_failures'] if f.get('model')})
                        manager_packet['execution_observation'] = observed
                        member_decisions = [decision for m in models
                            if m != (coding_compute or {}).get('model')
                            if (decision := escalation(m, observed)) is not None]
                        if member_decisions:
                            import hashlib
                            manager_packet['member_tier_decisions'] = [{**decision,
                                'decision_id': hashlib.sha256(json.dumps(decision, sort_keys=True).encode()).hexdigest()[:16],
                                'status': 'manager_answer_required'} for decision in member_decisions]
                    manifest.update(manager_phase='feedback_pending', manager_packet=manager_packet)
                    if manager_answer is not None:
                        manager_packet['operator_answer'] = manager_answer
                _write(evidence_dir / "coding-checkpoint.json", checkpoint)
                manifest["coding_checkpoint"] = _fact(evidence_dir / "coding-checkpoint.json")
                manifest.pop('pending_dispatch', None)
                _write(evidence_dir / "manifest.json", manifest)
                if migrate_recovered_manager and rc == 0:
                    from ._foundry_goal_transitions import manager as transfer_manager
                    manifest = transfer_manager(owner, evidence_dir, manifest, provider=None,
                        migrate=True, answer=manager_answer, source_locked=True, worker_feedback=manager_packet)
                    manager_state = manifest['manager_state']
                    manager_packet = manifest['manager_packet']
                    migrate_recovered_manager = False
                    migrated_manager = True
                if rc:
                    if getattr(result, 'outcome', None) == 'provider_capacity_exhausted':
                        raise GoalBlocked('provider capacity retries exhausted; continue the same session '
                                          'after capacity returns', 'provider_capacity_exhausted')
                    if not checkpoint['expiration_cause']:
                        from ._recovery_reconciliation import completed_coding
                        from ._execution_intent import IntentConflict
                        try:
                            completed_coding(checkpoint)
                        except (OSError, ValueError, KeyError, TypeError, IntentConflict) as exc:
                            raise GoalBlocked('coding executor returned nonzero; completion evidence is unresolved: ' + str(exc)) from exc
                    raise GoalBlocked(*STOP_OUTCOMES.get(checkpoint["expiration_cause"],
                        ('coding execution failed; inspect the original error, then explicitly continue with operator instructions',
                         'coding_incomplete')))
                if manager_state is None:
                    phase = "candidate"
                    candidate = candidate_for_review()
                    break
                manager_repeat = True
        else:
            prior_coding = retained or continued_coding or {}
            coding = prior_coding.get('coding_usage') or goal_usage._unknown("coding not run: existing candidate review")
            coding_evidence = list(prior_coding.get('evidence', []))
            rc = prior_coding.get('returncode')
        if phase == 'artifacts':
            outcome = artifacts(owner, execution_decision['manager'], tree, rc, coding_session, coding_evidence)
            return outcome
        if project is not None and project_review is None:
            phase = "candidate"
            observe_project_review(candidate)
        phase = "context"

        changed_files = [item for item in _git(tree, "diff", "--name-only", "-z", base, candidate)
                         .decode("utf-8").split("\0") if item]
        context = {"original_order": project.prompt(order.read_text(encoding="utf-8-sig")) if project else order.read_text(encoding="utf-8-sig"),
                   "base_commit": base, "candidate_commit": candidate, **({"project_record": project_record} if project else adapter.review_facts(tree, base, candidate)),
                   "changed_files": changed_files, "coding_evidence": coding_evidence, "review_evidence_policy": 1}
        if review_author_models:
            context['review_authorship'] = {'models': list(review_author_models), 'basis': 'operator-supplied design authors; not native model observation'}
        if project_review is not None:
            context["project_review"] = project_review
        if ((compute_assignment or {}).get("correction_policy") == "review-guided"
                or (manager_state or {}).get('expert') or manifest.get('expert_state')):
            context["correction_routing"] = True
        if execution_decision is not None:
            context["execution_decision"] = execution_decision

        from ._candidate_closure import verify as verify_closure
        closure = verify_closure(owner, adapter, tree, candidate, evidence_dir, dispatch._utf8_env())
        context['candidate_closure'] = closure
        manifest['candidate_closure'] = closure
        _write(evidence_dir / 'manifest.json', manifest)
        if closure['status'] not in ('passed', 'not_configured'):
            phase = 'candidate'
            raise GoalBlocked('fresh candidate closure failed; inspect its retained receipt',
                              'coding_incomplete', workspace={'candidate_closure': closure})

        previous_review = amendment or (continuation or {}).get("prior_review")
        if previous_review:
            # Historical continuation receipts may only retain findings and a
            # reference, without the candidate needed to calculate a delta.
            complete = {"prior_outcome", "prior_manifest", "candidate_commit", "base_commit", "reviews"} <= previous_review.keys()
            context["prior_review"] = _review_focus(previous_review) if complete else previous_review
            if previous_review.get("candidate_commit"):
                previous = previous_review["candidate_commit"]
                context["review_delta"] = {"base_commit": previous, "candidate_commit": candidate,
                    "changed_files": [p for p in _git(tree, "diff", "--name-only", "-z", previous, candidate)
                                      .decode("utf-8").split("\0") if p],
                    "scope": "Focus for follow-up; original base-to-candidate acceptance remains in scope."}

        if owner._referenced_fields(context):
            context['review_field_reads'] = True

        checkpoint = {"schema_version": 1, "context_delivery": "reference-v1", "review_read_policy": 1,
                      "run_id": run_id, "order": manifest["order"],
                      **({'task_deadline_at': task_deadline_at} if task_deadline_at is not None else {}),
                      "worktree": str(tree), "context": context,
                      "coding": {"returncode": rc, "evidence": coding_evidence},
                      "coding_usage": coding, "coding_session": coding_session,
                      "delivery_context": manifest["delivery_context"],
                      "analyst": {"executable": str(executable), "backend": backend,
                                  "role_compute": role_policy, **(analyst_compute or {})}}
        if (compute_assignment or {}).get('profile_version', 0) >= 5:
            from ._compute_selection import execution_evidence, independent_reviewers
            checkpoint['implementation_evidence'] = execution_evidence(coding)
            if not checkpoint['implementation_evidence']['implementation_models'] and (coding_compute or {}).get('model'):
                checkpoint['implementation_evidence']['review_exclusion_models'] = [coding_compute['model']]
                checkpoint['implementation_evidence']['review_exclusion_basis'] = 'recorded coding assignment fallback; editing authorship unverified'
            checkpoint['implementation_evidence']['review_exclusion_models'] = list(dict.fromkeys([
                *checkpoint['implementation_evidence'].get('review_exclusion_models', checkpoint['implementation_evidence']['implementation_models']),
                *manifest.get('coordination_models', [])]))
            current_reviews = reviewers or [{'backend': backend, **(analyst_compute or {})}] * 2
            quota_fallback = bool((manager_state or {}).get('quota_fallback'))
            review_source = (manifest.get('task_staffing') or {}).get('review_source_detail') or {}
            explicit = (review_source.get('model') == 'explicit' if review_source else
                        ((manifest.get('task_staffing') or {}).get('sources', {}).get('reviewers') or
                         (compute_assignment or {}).get('selection', {}).get('source')) == 'explicit') and not quota_fallback
            if not explicit:
                executables = {r['backend']: r.get('executable') for r in current_reviews if r.get('executable')}
                permitted = (('codex',) if quota_fallback else tuple({r['backend'] for r in current_reviews})
                             if review_source.get('backend') == 'explicit' else ('claude', 'codex'))
                for provider in permitted:
                    if provider not in executables:
                        try:
                            executables[provider] = str(resolve_executable(provider))
                        except (OSError, ValueError, RuntimeError):
                            pass
                try:
                    reviewers = independent_reviewers(current_reviews,
                        checkpoint['implementation_evidence'].get('review_exclusion_models', checkpoint['implementation_evidence']['implementation_models']), available_backends=executables)
                    reviewers = [{**r, 'executable': r.get('executable') or executables.get(r['backend'])} for r in reviewers]
                except ValueError as exc:
                    reviewers = current_reviews
                    checkpoint['review_selection_gap'] = str(exc)
            else:
                reviewers = current_reviews  # Conflict is checked after saving the review checkpoint.
            reviewers = [{**r, 'executable': r.get('executable') or str(executable)} for r in reviewers]
            checkpoint['model_independence'] = ('observed_editing_participants_excluded' if
                checkpoint['implementation_evidence']['implementation_models'] else
                'recorded_coding_assignment_excluded' if checkpoint['implementation_evidence'].get('review_exclusion_models') else 'unverified_missing_participant_evidence')
            manifest['reviewers'] = reviewers
            manifest['requested_compute']['reviews'] = reviewers
        if reviewers is not None:
            checkpoint["reviewers"] = reviewers
        if compute_assignment is not None:
            checkpoint["compute_assignment"] = compute_assignment
        if native_team is not None:
            checkpoint["native_team"] = native_team
        checkpoint_path = evidence_dir / "review-checkpoint.json"
        _write(checkpoint_path, checkpoint)
        manifest["review_checkpoint"] = _fact(checkpoint_path)
        _write(evidence_dir / "manifest.json", manifest)
        phase = "review"
        return _finish_review(checkpoint, evidence_dir)
    except GoalBlocked as exc:
        if not ((phase in {"coding", "candidate", "artifacts"} and exc.status == "coding_incomplete")
                or (phase == "coding" and exc.status in ("awaiting_input", "provider_capacity_exhausted"))):
            raise
        outcome = incomplete(exc, rc, coding_evidence, coding_session, manager_state, manifest)
        if project and phase == "candidate" and not (exc.workspace or {}).get('candidate_closure'):
            outcome.update(project_action="prepare_candidate", project_acceptance={"status": "not_evaluated"},
                next_action="Project owner completes its handoff and commit under project rules, then uses --foundry-review on that candidate; no new coding turn is required.")
            if rc == 0 and (exc.workspace or {}).get('status_porcelain', '').strip():
                outcome['status'] = 'project_handoff'
    except (Exception, SystemExit) as exc:
        from ._dispatch_package import DispatchNotStarted
        if getattr(exc, 'dispatch_package', None) and manifest.get('manager_phase') == 'dispatch_pending':
            manifest['pending_dispatch'] = _fact(exc.dispatch_package)
            _write(evidence_dir / 'manifest.json', manifest)
        elif ((manager_state is not None or manifest.get('expert_state')) and manifest.get('manager_phase') == 'dispatch_pending'
                and isinstance(getattr(exc, 'dispatch_stop', None), DispatchNotStarted)):
            # The dispatcher still owns proof that no child started. Preserve
            # the same decision for an explicit retry after resolving its refusal.
            manifest['manager_phase'] = 'delegation_ready'
            _write(evidence_dir / 'manifest.json', manifest)
        raise
    finally:
        if phase != "review":
            _record_usage(evidence_dir, coding, native, outcome, native_team=native_team,
                          context=manifest["delivery_context"])
            if phase == "artifacts" and outcome is not None:
                owner._publish_outcome(evidence_dir, outcome)
    _write(evidence_dir / "outcome.json", outcome)
    return outcome
