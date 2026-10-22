"""Explicit assignments and named profiles, not a learned router or availability list."""

import json
import os
import re
from pathlib import Path

from ._model_catalogue import BACKEND, model
from ._execution_plan_prompt import WAIT_GUIDANCE, EVIDENCE_GUIDANCE

ROLE_BY_KIND = {"plan": "planning", "analyse": "investigation", "synthesize": "synthesis",
                "solution": "solution", "review": "review"}
ROLES = (*ROLE_BY_KIND.values(), "coding")
MEMBER_TIERS = {
    'claude': {'worker': 'sonnet', 'investigator': 'haiku', 'mechanical': 'haiku', 'expert': 'fable'},
    'codex': {'worker': 'sol', 'investigator': 'luna', 'mechanical': 'luna', 'expert': 'astra'},
}
ESCALATION_TIERS = {'claude': ('haiku', 'sonnet', 'opus'), 'codex': ('luna', 'sol', 'astra')}


def execution_evidence(coding):
    """Project frozen observations; never infer implementers from coordinator role."""
    models, failures, seen, gaps = set(), [], set(), []
    def visit(row):
        identity = (row.get('session_id'), row.get('turn_id'), row.get('attempt_id'))
        if any(identity) and identity in seen:
            return
        if any(identity):
            seen.add(identity)
        if row.get('managed_turns') is not None:
            for child in row['managed_turns']:
                project(child)
            return
        work = row.get('work_observation') or {}
        observed = work.get('models', [])
        if not isinstance(observed, list) or not all(isinstance(m, str) for m in observed):
            raise ValueError('unrecognized implementation model observation')
        models.update(observed)
        signals = row.get('reading_signals') or {}
        declared = (row.get('model_observation') or {}).get('declarations') or []
        if signals.get('implementation_calls') and len(declared) == 1:
            models.add(declared[0]['model'])
        observed_model = declared[0]['model'] if len(declared) == 1 else None
        failures.extend({**f, 'session_id': row.get('session_id'), 'model': f.get('model', observed_model)} for f in
                        [*work.get('test_failures', []), *signals.get('test_failures', [])])
        for child in (row.get('child_agents') or {}).get('observations', []):
            project(child)
    def project(row):
        try:
            visit(row)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            gaps.append(str(exc))
    project(coding or {})
    # Keep recurrence across managed turns only for assertions still failing in
    # the latest turn. Authorship remains cumulative for independent review.
    turns = (coding or {}).get('managed_turns') or []
    active = failures
    if turns:
        active, unresolved = [], None
        for turn in reversed(turns):
            recent = execution_evidence(turn)['active_test_failures']
            ids = {f.get('test_id') for f in recent}
            unresolved = ids if unresolved is None else unresolved & ids
            if not unresolved:
                break
            active.extend(f for f in recent if f.get('test_id') in unresolved)
    return {'implementation_models': sorted(models), 'test_failures': failures,
            'active_test_failures': active,
            'gaps': gaps,
            'basis': 'observed editing participants; coordinator model is not substituted for missing authorship'}


def escalation(current, evidence, *, prior_findings=(), findings=()):
    """One-tier decision for the existing next-attempt owner, never a retry loop."""
    from ._model_catalogue import tier_for_model
    counts = {}
    seen = set()
    for row in evidence.get('active_test_failures', evidence.get('test_failures', [])):
        if not row.get('test_id') or not row.get('call_id') or not row.get('model') or row['model'] != current:
            continue
        identity = (row.get('session_id'), row.get('call_id'), row['test_id'])
        if identity in seen:
            continue
        seen.add(identity)
        counts[row['test_id']] = counts.get(row['test_id'], 0) + 1
    signals = ['test:' + key for key, count in counts.items() if count >= 2]
    def keys(values):
        return {(f['file'], f['summary'].strip()) for f in values
                if f.get('severity') in ('P0', 'P1') and isinstance(f.get('file'), str)
                and isinstance(f.get('summary'), str)}
    signals += ['finding:' + file + ':' + summary for file, summary in sorted(keys(prior_findings) & keys(findings))]
    contradictions = [{'file': f['file'], 'summary': f['summary'].strip(), 'matched_phrase': match[0]}
                      for f in findings if f.get('file')
                      if (match := re.search(r'contradict(?:ing|ory) evidence|evidence conflict|证据矛盾|证据冲突', f.get('summary', ''), re.I))]
    signals += ['contradiction:' + f['file'] + ':' + f['summary'] for f in contradictions]
    tier = tier_for_model(current)
    ladder = ESCALATION_TIERS.get(BACKEND.get(tier), ())
    if not signals or tier not in ladder:
        return None
    target = ladder[min(ladder.index(tier) + 1, len(ladder) - 1)]
    return {'from_model': current, 'model': model(target), 'effort': 'high', 'signals': signals,
            'contradiction_matches': contradictions,
            'action': 'escalate' if target != tier else 'expert_consultation_required',
            'reason': 'Repeated named assertion, blocking finding, or reviewer-identified evidence contradiction; infrastructure failures are excluded.'}


def independent_reviewers(assignments, implementation_models, *, explicit=False, available_backends=None):
    """Select through existing catalogue only; explicit conflicting slots are refused."""
    def canonical(value):
        value = value.removesuffix('[1m]')
        return model(value) if value in BACKEND else value
    excluded = {canonical(m) for m in implementation_models}
    rows = review_assignments(assignments)
    if not excluded:
        return rows  # Missing observation is not evidence of a prohibited reviewer.
    if not any(canonical(row['model']) in excluded for row in rows):
        return rows
    if explicit:
        raise ValueError('Reviewer model matches an observed coding participant; select another model, preferably another provider.')
    from ._model_catalogue import tier_for_model
    providers = {BACKEND.get(tier_for_model(m)) for m in excluded}
    tiers = ('opus', 'sonnet', 'astra', 'sol', 'fable', 'terra', 'luna') if providers == {'codex'} else ('astra', 'sol', 'opus', 'sonnet', 'fable', 'terra', 'luna')
    result = []
    for tier in tiers:
        if available_backends is not None and BACKEND[tier] not in available_backends:
            continue
        chosen = model(tier)
        if canonical(chosen) not in excluded and chosen not in {r['model'] for r in result}:
            result.append({'backend': BACKEND[tier], 'model': chosen, 'effort': 'high'})
        if len(result) == 2:
            return result
    raise ValueError('No independent review pair is available in the current model catalogue.')


def tier_state(prior, current, decision, *, evidence=None, findings=()):
    """One escalation/reset projection shared by managed and auto-amend rounds."""
    saved = dict(prior or {})
    if decision:
        saved.setdefault('tier_baseline', dict(current))
        saved['tier_escalation'] = decision
        saved.pop('tier_reset', None)
    elif saved.get('tier_escalation') and saved.get('tier_baseline'):
        evidence = evidence or {}
        active = {'test:' + f['test_id'] for f in evidence.get('active_test_failures', evidence.get('test_failures', [])) if f.get('test_id')}
        active.update(prefix + f['file'] + ':' + f['summary'].strip() for f in findings
                      if f.get('file') and f.get('summary') for prefix in ('finding:', 'contradiction:'))
        if active.intersection(saved['tier_escalation'].get('signals', [])):
            return saved  # A tier change alone does not resolve its failure.
        saved.pop('tier_escalation')
        saved['tier_reset'] = {**saved['tier_baseline'], 'action': 'return_to_baseline',
                              'reason': 'The latest completed round no longer reports the escalation signal.'}
    return saved


def member_roles(backend):
    """Staffing and spawn admission share one role-to-tier authority."""
    return {role: {'model': model(tier), **({} if backend == 'claude' and role in
            ('investigator', 'mechanical') else {'effort': 'high'})}
            for role, tier in MEMBER_TIERS[backend].items()}

# Versioned starting points. Tables name vendor tiers; _model_catalogue resolves each
# to the vendor's current model when new work is staffed (operator 2026-09-29).
# Changing the definition for a new run must not reroute a recorded recovery.
DEFAULT_GOAL_PROFILE = "sol-astra"
DEFAULT_TASK_KIND = "small"
TASK_KINDS = {
    "small": {"compute_profile": "sol-claude", "manager_profile": "opus",
              "team_profile": "none", "review_profile": "claude"},
    "cross-module": {"compute_profile": "sol-claude", "manager_profile": "opus",
                     "team_profile": "adaptive", "review_profile": "claude"},
    "ui-live": {"compute_profile": "claude", "manager_profile": None,
                "team_profile": "adaptive", "review_profile": "claude"},
}


REVIEW_PROFILES = {
    "claude": (("opus", "high"), ("sonnet", "high")),
    "opus-astra": (("opus", "high"), ("astra", "high")),
}


def missing_manager_profile(manifest):
    """Fill absent coordination from the existing kind; retain recorded fallback."""
    if manifest.get('manager_state'):
        return None
    kind = (manifest.get('task_staffing') or {}).get('kind') or DEFAULT_TASK_KIND
    return TASK_KINDS[kind]['manager_profile']


def review_assignments(value):
    """Freeze the two review slots without changing legacy single-backend records."""
    if value is None:
        return None
    if isinstance(value, str):
        if value not in REVIEW_PROFILES:
            raise ValueError("unknown review profile: " + value)
        value = [{"backend": BACKEND[tier], "model": model(tier), "effort": effort}
                 for tier, effort in REVIEW_PROFILES[value]]
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("review assignment needs two independent slots")
    rows = []
    for item in value:
        row = dict(item)
        if set(row) - {"backend", "model", "effort", "executable"} or row.get("backend") not in ("claude", "codex"):
            raise ValueError("unknown review assignment")
        pair = compute_kwargs(row.get("model"), row.get("effort"), backend=row["backend"])
        if not pair:
            raise ValueError("each reviewer needs its recorded model and effort")
        rows.append({**row, **pair})
    return rows

# Ordinary single execution uses Sonnet (operator revision 2026-10-10).
# Adaptive coordination/critical task kinds retain Opus at profile expansion.
GOAL_PROFILES = {
    "claude": {"coding": ("sonnet", "high"), "review": ("opus", "high"), "correction": ("sonnet", "high")},
    "sol-astra": {"coding": ("sol", "high"), "review": ("astra", "high"), "correction": ("astra", "high")},
    "sol-claude": {"coding": ("sol", "high"), "review": ("opus", "high"), "correction": ("sol", "high")},
    "terra-astra": {"coding": ("terra", "high"), "review": ("astra", "high"), "correction": ("astra", "high")},
    "luna-astra": {"coding": ("luna", "high"), "review": ("astra", "high"), "correction": ("astra", "high")},
    "astra": {"coding": ("astra", "high"), "review": ("astra", "high"), "correction": ("astra", "high")},
}


def profile_pair(name, role, *, team_profile=None, task_kind=None):
    """One profile slot as the (model, effort) its vendor currently serves."""
    tier, effort = GOAL_PROFILES[name][role]
    if (name == 'claude' and role in ('coding', 'correction')
            and (team_profile == 'adaptive' or task_kind in ('cross-module', 'ui-live'))):
        tier = 'opus'
    return model(tier), effort


# Both providers use native members, not a Foundry-owned member scheduler.
# Freeze this guidance with each new team; old sessions keep their own contract.
_EXPERT_HANDOFF = (
    "Keep a compact progress note of decisions, evidence paths/revisions, open questions and next action "
    "before native compaction. Recover existing work from that note and verify only changed or missing facts; "
    "do not rebuild context in successive owner agents. Experts decide from investigator file:line excerpts; "
    "request missing facts through the manager, keeping decisive direct reads with #decide: reason available. "
    "Tool rules apply to all providers and mixed teams. Query the available index before repository searches. "
    "For unsuitable, failed, stale or already-inspected index results, use scoped direct reads and record "
    "#idx: concrete reason in the command comment or tool description. Reuse a known failure reason rather "
    "than repeatedly querying the failed index. Empty/partial results never prove source absence. "
    "Code fact gathering (finding files/symbols/call sites, extracting relevant ranges, mapping a "
    "bounded call chain or collecting test/log facts) belongs to the investigator: Haiku for Claude, "
    "Luna for Codex. This is the default for a separate retrieval deliverable, not a target utilization percentage. "
    "Do not call routine source discovery expert work merely because the overall project is complex. "
    "The manager requests a bounded evidence packet: path:line references, source revision or working-tree "
    "state, relevant excerpts, coverage and unresolved questions; reuse it instead of repeating the search. "
    "Keep retrieval separate from causal diagnosis, architectural judgment and implementation. Use Sonnet "
    "or Terra for analysis that requires those inferences, the implementation worker for edits, and Fable/Astra "
    "for named difficult decisions. Escalate with the unresolved question and gathered evidence; index/tool "
    "failure alone is not evidence that a stronger model is needed. A worker may read the code needed for its "
    "own implementation, and a manager/expert may inspect decisive passages or do a tiny lookup when a new "
    "handoff would cost more; state the reason for substantial high-tier retrieval. Do not spawn a separate "
    "reader for every file or send full transcripts. Report retrieval role/model and any escalation reason "
    "in the normal handoff. Propagate this rule to nested and mixed-provider members. "
    "Select each new member's model explicitly. Describe its deliverable; standard markers are "
    "[activity: investigate], [activity: mechanical], [activity: execute], [activity: review], "
    "[activity: decision] or [activity: expert]. Implementation including necessary reading is execute, "
    "not a separate investigation. New or unclassified activities remain allowed; do not force them into a wrong category. "
    "Use the lowest capable tier for difficulty and risk. "
    "When a member cannot resolve a named failure with its evidence, use the existing member mechanism to escalate "
    "one tier with [decision: failed attempt and unresolved question]; do not replay the same failed request indefinitely. "
    "A Fable expert brief must include [expert: specific question]; "
    "an Opus/Astra decision brief must include [decision: specific decision or difficult final diff]. "
    "A retrieval brief must explicitly request file:line references plus quotes/excerpts. "
    "Keep each Haiku retrieval job below 100k total prompt tokens, including inherited context and tool results; "
    "split larger evidence into scoped jobs or return a compact index-backed handoff before that boundary. "
    "Unknown context size is not proof of being below the boundary. "
    "When planned total inputs are known, include [planned_input_tokens: N] in the reader brief. "
    "The hook warns at 100k declared tokens or a large brief estimate; estimates exclude unknown inherited/tool context. "
    "Code fact gathering uses the investigator tier "
    "and implementation uses the worker tier. "
    "Read discipline: brief each member with relevant paths, line ranges, symbol signatures, decisions "
    "and open questions. Manager direct reading is metered by the frozen launch policy; index queries remain available. "
    "Use #decide: specific reason for substantial decision reads, or #reread: specific reason for required repeats. "
    "Missing or unfamiliar observation formats do not prevent execution. For cross-file discovery or relationships, use codegraph_explore with query "
    "naming exact files or symbols; maxFiles bounds source files, not response tokens. Do not pass "
    "unsupported prompt or max_output_tokens arguments. When codegraph_node is available, use "
    "file plus symbolsOnly=true for a requested outline, file plus symbol plus includeCode=true "
    "for one known definition, or file plus offset/limit for a needed line range. Choose the query "
    "that answers the question directly; do not automatically run outline then source for every file. "
    "Check returned paths: a symbol lookup may fall back to same-name definitions in other files. "
    "A retained shared index process may reject node as disabled even when the client lists it; "
    "fall back once to explore or scoped direct reads, without retrying or restarting that process. "
    "If the precise tool is not listed, use the available explore tool or scoped direct reads. "
    "Follow each tool's advertised schema. Explore returns source fragments, not an outline. "
    "Reuse returned unchanged source ranges; "
    "do not re-read them merely to obtain the same source. Fetch omitted, truncated or changed ranges "
    "with a narrower query or scoped direct reads (rg -n -C 3 or line ranges). If the index is unavailable "
    "or irrelevant, use scoped direct reads. Check index warnings and source coverage; "
    "an absent symbol is not proof of absence. After edits, refresh stale pointers. A truncated tool "
    "result is incomplete: retain the full result in task evidence and request the missing range; "
    "report the native truncation marker and omitted count when provided, never invent a count. "
    "Read all material needed for correctness, in slices rather than repeated whole-file dumps. "
    "Member lifecycle: assign one member per deliverable. Retire a member when its deliverable is done; "
    "use the native completion or shutdown mechanism, not a polling loop. Give a different deliverable "
    "to a fresh member with a short brief naming relevant files, decisions and open questions. "
    "Keep or re-engage a member only for follow-up fixes to its own just-finished work; do not carry "
    "a completed member across different deliverables. Preserve its results in task evidence files. "
    "Assign experts a specific decision, disputed evidence or difficult final diff, with relevant "
    "source references and acceptance criteria. Include these handoff rules in member assignments. "
    "Consult the configured expert before continuing an unresolved architecture/ownership decision, "
    "contradictory correctness evidence, or a recurring reasoning defect after an attempted correction. "
    "A reviewer explicitly requesting expert reasoning is also an escalation trigger. Carry the named "
    "question, attempted explanations and contrary evidence to that expert; record its answer in the "
    "normal handoff. Do not treat cross-module scope, quota exhaustion, an index outage or a mechanical "
    "test failure alone as proof that expert reasoning is needed. "
    "Experts return the decision, supporting evidence and unresolved questions when that assignment "
    "is answered; they do not stay active to poll workers or await routine test completion. "
    "The implementation owner collects test results; the manager handles dependencies and waits. "
    "Re-engage the same expert with changed facts only for a correction to that deliverable. Do not send "
    "acknowledgment-only messages or unchanged status back to a completed expert. Escalate immediately "
    "when needed; there is no cheaper-model prerequisite or expert-call quota. "
    "Internal expert consultation resolves named questions; it need not duplicate the final independent "
    "candidate review unless the task requires both. Keep evidence in existing task files: identify "
    "the examined commit or uncommitted diff, source/dependency assumptions, checked criteria, exact "
    "test scope/results and open findings. Share those references with the candidate handoff. Reuse "
    "applicable observations, not somebody else's verdict; changed or conflicting facts need checking. "
    "Assign routine checkpoint execution, failure-only reruns and log collection to an implementation "
    "worker; the manager evaluates failures and acceptance from the resulting evidence. Keep one owner "
    "for each running check and reuse its operation handle and completed results. Repeat or broaden "
    "checks for changed code, affected dependencies, contradictory evidence or required final acceptance, "
    "not merely to confirm a worker's report again. Delegate independent deliverables in parallel; "
    "keep tightly coupled edits with one owner. Do not add a coordination layer just to relay messages. "
)


def is_managed(profile=None, state=None):
    return bool(profile or (state or {}).get('live_coordination'))


def native_wait_config():
    """Freeze native wait returns, never execution deadlines or cancellation.

    Keep the value in native_team.config, which existing recovery replays verbatim.
    Invalid optional calibration uses the default; zero preserves native defaults.
    Native V2 currently supports up to one hour. Do not reject a runnable task.
    """
    raw = os.environ.get('FOUNDRY_WAIT_INTERVAL_SECONDS', '1800')
    seconds = int(raw) if raw.isdecimal() else 1800
    if seconds == 0:
        return {}
    interval = min(seconds, 3600) * 1000
    return {
        'features.multi_agent_v2': {'enabled': True,
            'min_wait_timeout_ms': interval, 'default_wait_timeout_ms': interval,
            'max_wait_timeout_ms': 3600000, 'non_code_mode_only': True},
    }


def resolve_native_team(profile, *, backend="codex", managed=False):
    """Freeze native delegation guidance/settings, not a second task planner.

    Codex 0.153.4: agents capacity excludes the root; V2 capacity includes it.
    Explicit spawn model/effort overrides are exposed by the native V2 feature.
    """
    if profile is None:
        return None
    if backend == "claude":
        if profile not in {"none", "adaptive"}:
            raise ValueError("unknown Claude team profile")
        team = {"profile": profile, "version": 15 if profile == "adaptive" else 1, "backend": "claude",
            "worker_capacity": 10 if profile == "adaptive" else 0,
            "lead_role": "manager" if profile == "adaptive" and not managed else "executor",
            "config": {},
            "member_roles": member_roles('claude'),
            "instructions": (EVIDENCE_GUIDANCE + WAIT_GUIDANCE + ("Execute the Foundry manager's bounded assignment with your native workers. "
                "Return slice results and unresolved design questions to that manager; it owns design "
                "and readiness decisions. " if managed else
                "Manage this task's native Claude team. Delegate substantive implementation "
                "and its focused tests to workers; retain requirements, architectural decisions, "
                "conflicting evidence and final acceptance. ") + "While workers execute, resolve dependencies "
                "instead of duplicating their investigation, edits or test loops. Direct work is appropriate "
                "for trivial changes or decisions needing your expertise; briefly state why. "
                "Assign code fact gathering/investigator and mechanical extraction to Haiku, implementation "
                "and causal analysis to Sonnet, and difficult "
                "expert reasoning to Fable. The manager retains Opus. Explicitly select the role model, "
                "do not inherit the leader model. "
                "Give each member a concrete deliverable, relevant source references, ownership and acceptance "
                "criteria. Resume existing agents with SendMessage and changed facts only for fixes to their own deliverable; "
                "use fresh context for independent review, unrelated work or a needed model change. "
                "Prefer direct workers over layers of coordinators unless decomposition has a concrete benefit. "
                "Size parallel work across the whole team, including nested members: ten useful workers is "
                "a planning reference, not a required headcount or an enforced limit. "
                "Ask for a concise result, changed files, exact test scope/results, evidence locations and "
                "unresolved issues; keep long reports and logs in retrievable files. Reuse evidence while "
                "its sources, dependencies and assumptions remain valid; inspect relevant passages when "
                "evidence is missing, changed or conflicting. Required independent review remains independent. "
                "Quality first: escalate unresolved reasoning to Fable, never hide uncertainty to save tokens, "
                "and report actual role/model choices.\n" if profile == "adaptive" else
                "Complete implementation as one executor; independent review remains separate.\n")}
        if profile == "adaptive":
            team['expert_consultation'] = True
            team["member_role_briefs"] = {"investigator": {
                "description": "Read-only code fact gathering: files, symbols, bounded call chains and evidence",
                "prompt": "Perform read-only code fact gathering for the assigned question. Do not implement "
                          "edits or make architectural decisions. Return path:line references, source revision "
                          "or working-tree state, relevant excerpts, coverage and unresolved questions. Use "
                          "the available index and scoped fallback; report missing evidence without guessing. "
                          "Escalate causal reasoning to the parent with the collected facts."},
                "expert": {"description": "Resolve a named difficult decision or conflicting correctness evidence",
                           "prompt": "Resolve the assigned expert decision using the supplied source references, "
                                     "attempted explanations, contrary evidence and acceptance criteria. Read decisive "
                                     "source as needed. Return the decision, evidence and remaining uncertainty; "
                                     "do not take over routine discovery, implementation or test polling."}}
            team["instructions"] += _EXPERT_HANDOFF
            team["calibration_prompt_format"] = "tables-v2"
            # Frozen with the team so amendments retain their original guidance.
            # The transport applies it to built-in and nested agents as well as roles.
            team["member_instructions"] = (
                "Own and complete your assigned deliverable, including edits and focused tests when assigned. "
                "Do bounded work yourself; delegate further only when independent subtasks justify another "
                "context and coordination cost. Tell your parent the subdivision and reuse existing members "
                "with SendMessage only for fixes to their own deliverable, sending changed facts rather than whole transcripts. "
                "When delegating, explicitly choose Haiku for code fact gathering/investigator and mechanical "
                "work, Sonnet for implementation/causal analysis or Fable for difficult expert reasoning; do not "
                "inherit a costly parent model by accident. "
                "Reuse valid evidence; revisit it when relevant sources, dependencies or assumptions change, "
                "or evidence conflicts. Escalate uncertainty or missing context to the parent without guessing. "
                "Return a concise conclusion, changed files, exact test scope/results, evidence references "
                "and unresolved issues. Store long reports/logs in task evidence files and cite relevant "
                "sections rather than copying them into messages. Keep necessary reasoning and contrary "
                "evidence; brevity must not reduce correctness or required review coverage. " + _EXPERT_HANDOFF)
        return team
    if profile == "none":
        return {"profile": "none", "version": 3, "worker_capacity": 0, "lead_role": "executor",
                "process_completion_wait": 1,
                "config": {"agents.enabled": True, "features.multi_agent_v2": {"enabled": True}, **native_wait_config()},
                "instructions": WAIT_GUIDANCE + "Complete this task as one executor using the recorded model and test plan. "
                                "Do the implementation yourself rather than forming an implementation team. "
                                "This choice does not prohibit independent review required by the target repository. "
                                "Use separate fresh or bounded review contexts where required; reuse valid review "
                                "evidence for its actual scope. Foundry candidate review remains a separate stage.\n"}
    if profile != "adaptive":
        raise ValueError("unknown native team profile: " + str(profile))
    wait_config = native_wait_config()
    return {"profile": profile, "version": 20, "worker_capacity": 10, "expert_consultation": True,
            "process_completion_wait": 1,
            "calibration_prompt_format": "tables-v2",
            "lead_role": "executor" if managed else "manager",
            "member_roles": {k: v for k, v in member_roles('codex').items() if not managed or k != "expert"},
            "config": {
                **wait_config,
                "agents.enabled": True,
                "agents.max_concurrent_threads_per_session": 10,
                "agents.default_subagent_model": model(MEMBER_TIERS['codex']['worker']),
                "agents.default_subagent_reasoning_effort": "high",
                "features.multi_agent_v2": {
                    **wait_config.get('features.multi_agent_v2', {}),
                    "enabled": True, "expose_spawn_agent_model_overrides": True,
                    "max_concurrent_threads_per_session": 11}},
            "instructions": (EVIDENCE_GUIDANCE + WAIT_GUIDANCE +
                ("You execute the Foundry manager's bounded assignment. Delegate substantive "
                "implementation and its tests to native workers with fresh or bounded context. "
                "The Foundry manager owns task decomposition, architectural decisions and readiness. "
                "Return results and unresolved design questions at the assigned handback boundary. "
                if managed else
                "You are the manager of this task's native agent team. Delegate substantive "
                "implementation and its tests to a native worker with fresh or bounded context. "
                "Keep requirements, decomposition, architectural decisions, conflicting evidence "
                "and final acceptance in your own context. ") + (
                "While workers execute, relay design questions to the Foundry manager; "
                "do not decide architecture, summon native experts or declare readiness yourself. "
                if managed else "While workers execute, resolve dependencies and decision questions; ") +
                "Do not duplicate worker coding or tool loops. "
                "For genuinely trivial work or a problem needing your own expertise, direct work "
                "is appropriate: briefly explain the reason rather than creating a token quota. "
                + ("Follow the manager's decomposition; choose execution staffing for the assigned slice. " if managed else
                   "Decide the decomposition and team size from the actual problem. ") + "Ten worker slots "
                "are available, not a requirement to fill them. Solving the task to its "
                "acceptance criteria comes first; optimize tokens/time only without reducing "
                "quality. Use Sol/high as the normal implementation and correction worker, "
                "Luna/high for code fact gathering/investigator and mechanical work; Terra/high for causal analysis. "
                + ("The Foundry manager consults its configured expert for design decisions. " if managed else
                "Use Astra/high for expert decisions and difficult cross-module reasoning. ")
                + ('' if managed else _EXPERT_HANDOFF) +
                "These are starting points, not capability guarantees "
                "or a requirement to try a cheaper model first. Select an available model "
                "and reasoning effort explicitly when delegating; use stronger reasoning "
                "when the evidence warrants it. Child defaults are Sol/high. Current model IDs: "
                + ", ".join(model(tier) for tier in ("astra", "sol", "terra", "luna")) + ". "
                "Older generations remain explicit compatibility choices "
                "when justified by existing work or evidence. "
                "Give each member a concrete question, relevant source references and the "
                "needed result, including the relevant acceptance criteria and decisions. "
                "Tell implementation workers to own the edits, relevant tests and committed "
                "candidate handoff, not merely investigate or return a plan. Ask for findings, "
                "evidence locations, changed files and unresolved "
                "questions, keeping lengthy logs in retrievable files. When selecting a different model, use a fresh or bounded "
                "context rather than a full-history fork that inherits parent settings. "
                "Use native messages/follow-up to exchange relevant findings and conflicting "
                "evidence; continue existing members with deltas only for fixes to their own deliverable where supported. Avoid "
                "broadcasting transcripts or redoing valid completed work. Reuse available task "
                "notes and evidence while their source files, relevant dependencies, environment "
                "and assumptions still apply; include uncommitted edits when checking applicability. "
                "An unrelated commit alone does not invalidate a finding. Reopen sources when "
                "references are missing, facts changed or evidence conflicts. Search for relevant "
                "sections and query needed JSON fields before loading whole files; expand when "
                "needed for understanding or required coverage. Retrieve omitted evidence needed "
                "for the task when output is truncated. When useful, update existing task notes with decisions, source "
                "versions, evidence locations and open questions rather than replaying raw logs. "
                "For ongoing operations, reuse the existing handle and supported event/completion "
                "wait; inspect changed status or relevant output instead of repeatedly dumping "
                "the same logs. Follow the runtime's wait and progress-update requirements. "
                "Coordinate assigned edit ownership and dependencies. "
                + ("Return member conclusions and supporting evidence to the manager for acceptance; " if managed else
                   "Validate member conclusions against acceptance and their supporting evidence; ") +
                "Do not repeat their entire investigation "
                "just because it was delegated. Recheck concrete uncertainties and affected "
                "consumers, including consumers outside changed files; complete required checks "
                "and broaden testing when the change or evidence warrants it. "
                + ("The manager retains responsibility for readiness and final delivery. " if managed else
                   "You retain responsibility for the final delivery. ") +
                "Before ending, collect or explicitly stop unfinished member work and report "
                "what was delivered, verified and remains unresolved. On continuation, reuse "
                "retained results; reuse member identities only for fixes to their own deliverable where available. Do not assume "
                "members survived a process restart. Independent Foundry review follows "
                "your candidate; it is separate from collaborative investigation. "
                "On review findings, route the assigned correction and evidence "
                "to the original worker with native follow-up if that member is available. "
                "Retain that worker's model unless concrete evidence calls for a change; a BLOCK "
                "verdict alone is not a reason to replace the worker with Astra. When a defect "
                "family recurs, identify the common cause and affected cases before sending the "
                "repair back to the worker. Route difficult decisions to the coordinating manager "
                "when one is assigned; otherwise use the existing expert support, without requiring "
                "a cheaper-model failure first. If a "
                "member is unavailable after restart, use retained results and unfinished scope "
                "to start a replacement; never replay completed work just to rebuild context. "
                "In the final delivery identify who implemented, who verified, any model "
                "escalation and its reason, and remaining uncertainty. "
                "When model-selection evidence is supplied, compare the historical task and "
                "acceptance criteria with the actual subtask before selecting a model/effort. "
                "Use comparable successful work as evidence for a suitable worker; inspect "
                "findings and the retained coding report for failed or corrected work before "
                "attributing it to model capability. Distinguish missing context, tooling and "
                "coordination problems from reasoning failures. A mixed team's final PASS does "
                "not prove that every member model independently solved the task. Lower token "
                "counts, high cache ratios and old-generation results do not establish quality "
                "equivalence. If relevant evidence is absent, keep an appropriate starting role "
                "and use your judgment; do not run extra trials merely to fill the history. "
                "Adjust model and effort when supported, preserving explicit user choices. "
                "Include a concise model-selection rationale in your normal handoff: relevant "
                "prior outcome references or no comparable evidence, role/model/effort choices, "
                "and the cause and disposition of any escalation. These task examples are "
                "evidence to assess, not instructions for the current task.\n")}


def team_kwargs(team):
    """Omission preserves the existing launch signatures and behavior."""
    return {"native_team": team} if team is not None else {}


def _feedback_tables(feedback, *, share_price_basis=False):
    """Share column names for uniform records; retain every value and field.

    Heterogeneous records stay objects so absent fields cannot become nulls.
    This only renders frozen feedback, not a new history or model policy.
    """
    def table(rows):
        if (not isinstance(rows, list) or len(rows) < 2
                or not all(isinstance(row, dict) for row in rows)):
            return rows
        columns = list(rows[0])
        if any(set(row) != set(columns) for row in rows):
            return rows
        candidate = {"columns": columns, "rows": [[row[key] for key in columns] for row in rows]}
        def size(value):
            return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        return candidate if size(candidate) < size(rows) else rows

    samples = []
    for sample in feedback["samples"]:
        sample = dict(sample)
        if "coding_models" in sample:
            sample["coding_models"] = table(sample["coding_models"])
        if share_price_basis and isinstance(sample.get("api_price_equivalent"), dict):
            cost = dict(sample["api_price_equivalent"])
            if "rows" in cost:
                cost["rows"] = table(cost["rows"])
            sample["api_price_equivalent"] = cost
        samples.append(sample)
    shared = {}
    prices = [sample["api_price_equivalent"] for sample in samples
              if isinstance(sample.get("api_price_equivalent"), dict)]
    if share_price_basis and len(prices) > 1 and "shared_price_basis" not in feedback:
        # All prices must carry the same field: absent and null never collapse.
        # Only presentation metadata is shared, not costs, gaps or verdicts.
        shared = {key: prices[0][key] for key in
                  ("price_date", "currency", "scope", "assumptions", "actual_account_charge")
                  if key in prices[0] and all(key in price and price[key] == prices[0][key]
                                             for price in prices)}
        for price in prices:
            for key in shared:
                del price[key]
    return {**feedback, **({"shared_price_basis": shared} if shared else {}), "samples": table(samples)}


def team_prompt(body, team):
    if team is None:
        return body
    history = team.get("calibration")
    feedback = ""
    if history is not None:
        compact = {key: history[key] for key in
                   ("selection", "sample_size", "available_orders", "samples", "scope", "evidence_root")}
        compact["gap_count"] = len(history["gaps"])
        if team.get("calibration_prompt_format") in {"tables-v1", "tables-v2"}:
            baseline = _feedback_tables(compact)
            compact = (_feedback_tables(compact, share_price_basis=True)
                       if team["calibration_prompt_format"] == "tables-v2" else baseline)
            shared_note = ("; shared_price_basis fields apply to every api_price_equivalent object"
                           if "shared_price_basis" in compact else "")
            encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
            baseline_encoded = json.dumps(baseline, ensure_ascii=False, separators=(",", ":"))
            if len((encoded + shared_note).encode("utf-8")) >= len(baseline_encoded.encode("utf-8")):
                encoded, shared_note = baseline_encoded, ""
            feedback = ("\nModel-selection evidence (frozen at task start; table rows map positionally "
                        "to columns, including null/unknown values" + shared_note + "):\n"
                        + encoded)
        else:
            # Preserve exact rendering for already frozen teams and continuations.
            feedback = "\nModel-selection evidence (frozen at task start):\n" + json.dumps(compact, ensure_ascii=False)
    return team["instructions"] + feedback + "\nTask:\n" + body


def resolve_goal_profile(name, *, backend="codex", role_compute=None,
                         coding_model=None, coding_effort=None, analyst_model=None,
                         analyst_effort=None, auto_amend_model=None, auto_amend_effort=None, coding_backend="codex",
                         team_profile=None, task_kind=None):
    """Expand one opt-in profile into existing goal options plus provenance.

    Complete explicit pairs override individual slots. Profiles include automatic
    BLOCK correction; they never classify tasks or change native configuration.
    """
    if name not in GOAL_PROFILES:
        raise ValueError("unknown compute profile: " + str(name))
    if (name == "claude") != (coding_backend == "claude"):
        raise ValueError("compute profile must match the coding provider")
    if role_compute is not None:
        raise ValueError("compute-profile and role-compute are alternative assignment sources")
    if backend not in ("codex", "claude"):
        raise ValueError("unknown analyst backend")
    options, roles = {}, {}
    for role, prefix, model, effort in (
        ("coding", "coding", coding_model, coding_effort),
        ("review", "analyst", analyst_model, analyst_effort),
        ("correction", "auto_amend", auto_amend_model, auto_amend_effort),
    ):
        provider = backend if role == "review" else coding_backend
        pair = compute_kwargs(model, effort, backend=provider)
        if provider == "claude" and not pair and BACKEND[GOAL_PROFILES[name][role][0]] != "claude":
            raise ValueError("a compute profile with Claude requires an explicit analyst model and effort")
        source = "explicit" if pair else "profile"
        if not pair:
            pair = dict(zip(("model", "effort"), profile_pair(name, role, team_profile=team_profile, task_kind=task_kind)))
        options.update({prefix + "_" + key: value for key, value in pair.items()})
        roles[role] = {"backend": provider, **pair, "source": source}
    return options, {"profile": name, "profile_version": 5, "roles": roles,
                     "correction_policy": ("fixed" if name == "sol-claude" and
                         roles["correction"]["source"] == "profile" else
                         "review-guided" if roles["correction"]["source"] == "profile" else "explicit"),
                     "coding_role": "coding"}


def compute_kwargs(model=None, effort=None, *, backend='codex') -> dict:
    """Omitting both inherits defaults; either override requires the whole pair.

    Model IDs are opaque native inputs. Codex validates its configuration and
    app-server can consult model/list. Do not infer availability from a name.
    """
    if model is None and effort is None:
        return {}
    if backend == "claude" and isinstance(model, str) and model.strip() == model and model and not any(ord(c) < 32 for c in model) and effort is None:
        return {"model": model}
    if any(not isinstance(v, str) or not v.strip() or v != v.strip()
           or any(ord(c) < 32 for c in v) for v in (model, effort)):
        raise ValueError('explicit compute requires both nonempty model and effort')
    # Native Claude 2.1.269 --help advertises these flag values. These are CLI
    # syntax, not model capabilities; native provider errors remain authoritative.
    # https://code.claude.com/docs/en/model-config
    if backend == 'claude' and effort not in ('low', 'medium', 'high', 'xhigh', 'max'):
        raise ValueError('Claude --effort supports low, medium, high, xhigh, max; model support is native')
    return {'model': model, 'effort': effort}


def role_assignments(value, *, backend="codex", coding_backend="codex"):
    """One complete explicit assignment; no inheritance or silent provider switch."""
    if value is None:
        return None
    if isinstance(value, (str, Path)):
        def unique(items):
            result = {}
            for key, item in items:
                if key in result:
                    raise ValueError("duplicate role-compute key: " + key)
                result[key] = item
            return result
        with Path(value).open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("role-compute file exceeds 64 KiB")
        value = json.loads(raw, object_pairs_hook=unique)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "mode", "roles"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["mode"] != "explicit" or not isinstance(value["roles"], dict)
            or set(value["roles"]) != set(ROLES) or backend not in ("codex", "claude")):
        raise ValueError("role-compute requires version 1, explicit mode and all six roles")
    roles = {}
    for role in ROLES:
        row = value["roles"][role]
        expected = (coding_backend or row.get("backend")) if role == "coding" and isinstance(row, dict) else backend
        if not isinstance(row, dict) or set(row) != {"backend", "model", "effort"}:
            raise ValueError("each role-compute entry needs backend, model and effort")
        if expected not in ("codex", "claude") or row["backend"] != expected:
            raise ValueError("role-compute analyst roles must match --analyst-backend; coding must match the coding provider")
        pair = compute_kwargs(row["model"], row.get("effort"), backend=expected)
        if not pair:
            raise ValueError("role-compute entries must specify model and effort")
        roles[role] = {"backend": expected, "effort": pair.get("effort"), **pair}
    return {"schema_version": 1, "mode": "explicit", "roles": roles}


def resolve_goal_compute(value, *, backend="codex", analyst_model=None, analyst_effort=None,
                         coding_model=None, coding_effort=None, coding_backend="codex"):
    analyst = compute_kwargs(analyst_model, analyst_effort, backend=backend)
    coding = compute_kwargs(coding_model, coding_effort, backend=coding_backend)
    policy = role_assignments(value, backend=backend, coding_backend=coding_backend)
    if policy is not None:
        if analyst or coding:
            raise ValueError("--role-compute conflicts with legacy analyst/coding model and effort options")
        coding = {k: policy["roles"]["coding"].get(k) for k in ("model", "effort")}
    return analyst, coding, policy


def freeze_roles(policy):
    return None if policy is None else tuple((role, row["model"], row.get("effort"))
                                             for role, row in policy["roles"].items())


def compute_for_kind(frozen_roles, kind, default):
    if frozen_roles is None or kind is None:
        return dict(default)
    role = ROLE_BY_KIND[kind]
    return next({"model": model, "effort": effort} for name, model, effort in frozen_roles if name == role)


def call_compute_metadata(analyst, kind):
    result = analyst._metadata()
    if analyst._role_compute is not None:
        result.update(compute_role=ROLE_BY_KIND[kind], role_compute_schema_version=1,
                      requested_compute=compute_for_kind(analyst._role_compute, kind, analyst._compute))
    return result


def goal_shadow_advice(inputs, analyst, coding, backend, policy):
    from .foundry_shadow import advise
    pair = lambda role: {k: policy["roles"][role][k] for k in ("model", "effort")} if policy else analyst or None
    result = {"research": advise(inputs, phase="research", actual=None if policy else analyst or None, backend=backend),
              "solution": advise(inputs, phase="solution", actual=pair("solution"), backend=backend),
              "coding": advise(inputs, phase="coding", actual=coding or None, backend="codex"),
              "review": advise(inputs, phase="review", actual=pair("review"), backend=backend)}
    if policy:
        result["research"]["actual_by_role"] = {role: pair(role) for role in ("planning", "investigation", "synthesis")}
    return result
