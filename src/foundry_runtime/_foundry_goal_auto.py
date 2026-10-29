"""Automatic correction controller for a Foundry goal's recorded rounds.

The goal runner owns one native round; this controller sequences those rounds.
The owner parameter supplies the runner's persistence and dispatch seams.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from ._compute_selection import resolve_native_team, is_managed
from ._model_catalogue import model
from ._foundry_response_contract import _BLOCKING_SEVERITIES
from . import _foundry_goal_usage as goal_usage


def active_round(root, manifest, *, consumed=False):
    """Resolve the existing round hierarchy, including explicit upgraded rounds."""
    root = Path(root).resolve()
    while manifest.get('active_round'):
        child = Path(manifest['active_round']).resolve(strict=True)
        if child.parent != root:
            raise ValueError('active round must be a direct child of its goal: ' + str(child))
        root, manifest = child, json.loads((child/'manifest.json').read_bytes())
    if consumed:
        from . import foundry_goal as owner
        from ._foundry_goal_continuation import consumed_continuation
        root, manifest = consumed_continuation(owner, root, manifest)
    return root, manifest


def outcome_path(root, manifest):
    """The round pointer selects new outcomes; legacy root outcomes stay intact."""
    root = Path(root)
    return (Path(manifest['active_round']) if manifest.get('runtime_upgrades') else root)/'outcome.json'


def block_identity(owner, outcome):
    findings = {owner._json_bytes(f).decode("utf-8") for r in [*outcome["reviews"], {"findings": outcome.get("host_findings", [])}]
                for f in r["findings"] if f["severity"] in _BLOCKING_SEVERITIES}
    return outcome["candidate_commit"], tuple(sorted(findings))



def correction_options(options, correction):
    options = dict(options)
    assignment = options.get("compute_assignment") or {}
    decisions = options.get('execution_decision') or {}
    decision = decisions.get('tier_escalation') or decisions.get('tier_reset')
    if assignment.get('profile_version', 0) >= 5 and decision:
        pair = {'model': decision['model'], 'effort': decision['effort']}
        if options.get('role_compute'):
            policy = options['role_compute']
            options['role_compute'] = {**policy, 'roles': {**policy['roles'],
                'coding': {**policy['roles']['coding'], **pair}}}
            return options
        return {**options, 'coding_model': pair['model'], 'coding_effort': pair['effort']}
    choice, reason, guidance = "expert", "Explicit or legacy correction model assignment.", []
    if assignment.get("correction_policy") == "fixed":
        reason = "The frozen task route selects its correction model."
    elif assignment.get("correction_policy") == "review-guided":
        prior = json.loads(Path(options["amend_from"]).read_bytes())
        guidance = [r.get("correction_guidance") for r in prior.get("reviews", []) if r.get("findings")]
        # No text classification: the independent reviewers already examined the
        # defect. Historical outcomes without advice retain their old routing.
        if guidance and all(isinstance(g, dict) and g.get("model") == "retain" for g in guidance):
            choice, reason = "retain", "Independent reviewers recommend the existing worker for the remaining work."
        elif not guidance and prior.get("host_findings"):
            choice, reason = "retain", "Only host-recorded mechanical findings; the existing worker applies them."
        else:
            reason = "Expert requested by a reviewer, or historical correction advice unavailable."
    if (assignment.get('correction_policy') == 'review-guided'
            and ((options.get('manager_state') or {}).get('expert')
                 or (options.get('native_team') or {}).get('expert_consultation'))
            and any(isinstance(g, dict) and g.get('model') == 'expert' for g in guidance)):
        choice, reason = 'retain', 'Recorded expert consultation precedes correction by the original worker.'
    decision = {"model_action": choice, "reason": reason, "review_guidance": guidance}
    options["execution_decision"] = {**(options.get("execution_decision") or {}), "correction": decision}
    if choice == "retain":
        return options
    if options.get("compute_assignment") is not None:
        options["compute_assignment"] = {**options["compute_assignment"], "coding_role": "correction"}
    if options.get("role_compute") is not None:
        policy = options["role_compute"]
        options["role_compute"] = {**policy, "roles": {**policy["roles"],
            "coding": {"backend": "claude" if options.get("coding_launcher") == "claude" else "codex",
                       "effort": correction.get("effort"), **correction}}}
    else:
        options.update(coding_model=correction["model"], coding_effort=correction.get("effort"))
    return options


def run(owner, order, evidence_dir, executable, correction, options):
    """Sequence the existing rounds; dispatch still owns every native continuation."""
    with owner._run_lock(evidence_dir):
        return run_owned(owner, order, evidence_dir, executable, correction, options)


def round_record(owner, folder):
    record = {"outcome": owner._fact(folder / "outcome.json")}
    if (folder / "usage.json").exists():
        record["usage"] = owner._fact(folder / "usage.json")
    return record


def finish(owner, root, manifest, result):
    """Publish the same terminal view after normal execution or review recovery."""
    usage = {"schema_version": 1, "mode": "auto-amend", "rounds": manifest["rounds"],
             "usage_scope": "per-round references, not a cumulative or full-account total"}
    owner._write(root / "usage.json", usage)
    result = {**result, "rounds": manifest["rounds"], "task_usage": owner._fact(root / "usage.json")}
    if manifest.get('review_tier_decisions'):
        decisions = list(manifest['review_tier_decisions'])
        for row in result.get('review_tier_decisions', []):
            # Keep distinct parent rounds; omit only their un-enriched child copy.
            if not any(all(saved.get(key) == value for key, value in row.items()) for saved in decisions):
                decisions.append(row)
        result['review_tier_decisions'] = decisions
    try:
        report = {"delivery": goal_usage.delivery(result, manifest.get("delivery_context")),
                  "usage_reference": result["task_usage"], "actual_account_charge": "unknown"}
        rows = goal_usage.round_reports(manifest["rounds"])
        report["round_reports"] = rows
        modes = [row.get("delivery", {}).get("context", {}).get("mode") for row in rows]
        report["round_count"] = len(rows)
        report["correction_round_count"] = modes.count("amendment") if all(modes) else None
        result["delivery_report"] = report
    except Exception as exc:
        result["delivery_report"] = goal_usage._unknown(exc)
    owner._write(root / "outcome.json", result)
    manifest["state"] = "completed"
    manifest.pop("error", None)
    return result


def run_owned(owner, order, evidence_dir, executable, correction, options):
    root = Path(evidence_dir).resolve()
    root.mkdir(parents=True, exist_ok=False)
    rounds, seen = [], set()
    block_window = []
    if options.get("amend_from"):
        predecessor = Path(options["amend_from"])
        prior_result = json.loads(predecessor.read_bytes())
        if options.get("task_staffing") and prior_result.get("status") == "review_blocked":
            block_window.append({"round": "predecessor", "outcome": owner._fact(predecessor),
                "result": prior_result})
        options = correction_options(options, correction)
    from ._runtime_provenance import observe
    context = (options.get("project_record") or {}).get("context")
    manifest = {"schema_version": 1, "mode": "auto-amend", "order": owner._fact(order),
                "run_id": uuid.uuid4().hex,
                "runtime_provenance": observe(project_context=context),
                "correction_compute": correction, "rounds": rounds, "state": "running"}
    upgrade_goal = ((options.get('continuation') or {}).get('runtime_upgrade') or {}).get('goal')
    if upgrade_goal:
        manifest['follow_up_goal'] = upgrade_goal
    if options.get("project_record") is not None:
        manifest["project_record"] = options["project_record"]
    manifest["delivery_context"] = goal_usage.delivery_context("auto-amend", order=manifest["order"])
    if options.get("compute_assignment") is not None:
        manifest["compute_assignment"] = options["compute_assignment"]
    if options.get("native_team") is not None:
        manifest["native_team"] = options["native_team"]
    if options.get("execution_decision") is not None:
        manifest["execution_decision"] = options["execution_decision"]
    if options.get("task_staffing") is not None:
        manifest["task_staffing"] = options["task_staffing"]
    from ._followup import origin, announce
    manifest['originating_session'] = origin()
    owner._write(root / 'manifest.json', manifest)
    announce(root, manifest)
    options = {**options, 'follow_up_goal': upgrade_goal or str(root)}
    result = None
    try:
        while True:
            folder = root / ("round-" + str(len(rounds)))
            manifest["active_round"] = str(folder)
            manifest['round_start'] = {'executable': str(executable),
                'options': json.loads(json.dumps(options, default=str))}
            owner._write(root / "manifest.json", manifest)
            result = owner._run(order, folder, executable, **options)
            options.pop('continuation', None)
            options = {k: v for k, v in options.items() if k != 'recovery_file'}
            child_manifest = json.loads((folder / "manifest.json").read_bytes()) if (folder / "manifest.json").exists() else {}
            if child_manifest.get('manager_state') is not None:
                options['manager_state'] = child_manifest['manager_state']
                # The completed round may have made a measured quota transfer.
                # Its retained manager now owns continuation, not the initial default.
                options.pop('manager_profile', None)
            if child_manifest.get("reviewers") is not None:
                options = {**options, "reviewers": [
                    {key: value for key, value in
                     ((child_manifest.get('review_transfers') or {}).get('review-' + str(index)) or [row])[-1].items()
                     if key in ('backend', 'model', 'effort', 'executable')}
                    for index, row in enumerate(child_manifest['reviewers'])]}
            if child_manifest.get('review_tier_decisions'):
                manifest.setdefault('review_tier_decisions', []).extend(
                    {'after_round': len(rounds), **decision} for decision in child_manifest['review_tier_decisions'])
            manifest.update({k: child_manifest[k] for k in ("worktree", "base_commit", "tag") if k in child_manifest})
            rounds.append(round_record(owner, folder))
            if result["status"] != "review_blocked":
                break
            block_window = (block_window + [{"round": len(rounds)-1,
                "outcome": rounds[-1]["outcome"], "result": result}])[-2:]
            promoted = False
            if (options.get('compute_assignment') or {}).get('profile_version', 0) >= 5:
                from ._compute_selection import escalation, tier_state
                current = (child_manifest.get('requested_compute', {}).get('coding') or {}).get('model') or options.get('coding_model') or (options.get('role_compute') or {}).get('roles', {}).get('coding', {}).get('model')
                findings = [f for r in result['reviews'] for f in r['findings']]
                previous = [f for r in block_window[0]['result']['reviews'] for f in r['findings']] if len(block_window) == 2 else []
                decision = escalation(current, result.get('implementation_evidence', {}),
                                      prior_findings=previous, findings=findings)
                if decision:
                    baseline = {'model': current,
                        'effort': (child_manifest.get('requested_compute', {}).get('coding') or {}).get('effort') or options.get('coding_effort') or 'high'}
                    options['execution_decision'] = tier_state(options.get('execution_decision'), baseline, decision)
                    previous_decisions = manifest.setdefault('tier_escalations', [])
                    fresh_consultation = not any(d['action'] == decision['action'] and d['from_model'] == decision['from_model']
                        and d['signals'] == decision['signals'] for d in previous_decisions)
                    manifest.setdefault('tier_escalations', []).append({'after_round': len(rounds)-1, **decision})
                    if decision['action'] == 'escalate':
                        promoted = True
                    else:
                        # One expert-assisted attempt before the existing unchanged
                        # candidate loop rule applies again; no new retry scheduler.
                        promoted = fresh_consultation
                elif (options.get('execution_decision') or {}).get('tier_escalation'):
                    saved = tier_state(options['execution_decision'], {}, None,
                        evidence=result.get('implementation_evidence', {}), findings=findings)
                    options['execution_decision'] = saved
                    if saved.get('tier_reset'):
                        manifest.setdefault('tier_escalations', []).append({'after_round': len(rounds)-1, **saved['tier_reset']})
                review_source = (options.get('task_staffing') or {}).get('review_source_detail') or {}
                if (options.get('compute_assignment') or {}).get('profile_version', 0) >= 6 and review_source.get('model') in ('kind', 'project', 'provider-default'):
                    states = dict((options.get('execution_decision') or {}).get('review_tiers') or {})
                    reviewers = [dict(row) for row in options.get('reviewers', [])]
                    for index, reviewer in enumerate(reviewers):
                        if reviewer['backend'] != 'codex':
                            continue
                        name = 'review-' + str(index)
                        decision = escalation(reviewer['model'], result.get('implementation_evidence', {}),
                                              prior_findings=previous, findings=findings)
                        saved = tier_state(states.get(name), reviewer, decision,
                                           evidence=result.get('implementation_evidence', {}), findings=findings)
                        change = decision if decision and decision['action'] == 'escalate' else saved.get('tier_reset')
                        if change and change['model'] != reviewer['model']:
                            reviewer.update(model=change['model'], effort=change['effort'])
                            promoted = True
                            manifest.setdefault('review_tier_decisions', []).append({
                                'after_round': len(rounds)-1, 'slot': name, **change})
                        states[name] = saved
                    options['reviewers'] = reviewers
                    options['execution_decision'] = {**(options.get('execution_decision') or {}), 'review_tiers': states}
            if len(block_window) == 2 and (options.get('manager_state') or {}).get('quota_fallback'):
                manifest['handover_deferred'] = {
                    'reason': 'measured Claude quota unavailable; retain Codex execution until manager recovery',
                    'quota_evidence': options['manager_state']['quota_fallback'],
                    'quota_return_at': options['manager_state'].get('quota_return_at')}
                owner._write(root / 'manifest.json', manifest)
            if (options.get("task_staffing") and options.get("coding_launcher") != "claude"
                    and not (options.get('manager_state') or {}).get('quota_fallback')
                    and (result.get("coding_session") or options.get("coding_session"))
                    and len(block_window) == 2):
                session = result.get("coding_session") or options.get("coding_session")
                transition = {"after_rounds": [row["round"] for row in block_window],
                    "verdicts": [row["result"]["reviews"] for row in block_window],
                    "prior_coding_session": session, "worktree": manifest["worktree"],
                    "base_commit": manifest["base_commit"],
                    "candidate_commit": result.get("candidate_commit"),
                    "current_head": owner._git(manifest["worktree"], "rev-parse", "HEAD").decode().strip(),
                    "original_order": owner._fact(order),
                    "predispatch": json.loads((root / "round-0" / "manifest.json").read_bytes()).get("predispatch"),
                    "round_results": [row["result"] for row in block_window],
                    "outcomes": [row["outcome"] for row in block_window],
                    "staffing": options["task_staffing"]}
                if options.get("project_record") is not None:
                    transition["project_record"] = options["project_record"]
                manifest["handover"] = transition
                manifest.pop('handover_deferred', None)
                correction = {"model": model("opus"), "effort": "high"}
                options = {**options, "amend_from": folder / "outcome.json", "coding_session": session,
                    "coding_launcher": "claude", "coding_model": model("opus"),
                    "coding_effort": "high", "native_team": resolve_native_team("adaptive", backend="claude",
                        managed=is_managed(state=options.get('manager_state'))),
                    "manager_state": (options.get('manager_state') if
                        (options.get('manager_state') or {}).get('backend') == 'claude' else None), "manager_profile": None,
                    "handover": transition, "predispatch_receipt": None,
                    "execution_decision": {key: value for key, value in (options.get("execution_decision") or {}).items()
                                           if key not in ("correction", "tier_escalation", "tier_reset")}}
                options.pop("role_compute", None)
                options.pop("recovery_file", None)
                options["compute_assignment"] = {**options["compute_assignment"], "coding_role": "handover",
                    "roles": {**options["compute_assignment"]["roles"],
                        "correction": {"backend": "claude", **correction, "source": "handover"}, "handover": {
                        "backend": "claude", "model": model("opus"), "effort": "high", "source": "handover"}}}
                continue
            identity = block_identity(owner, result)
            if options.get("amend_from"):
                prior = json.loads(Path(options["amend_from"]).read_bytes())
                if prior["status"] == "review_blocked":
                    seen.add(block_identity(owner, prior))
            if identity in seen and not promoted:
                result = {**result, "auto_amend_stop": "unchanged candidate and blocking findings repeated"}
                break
            seen.add(identity)
            session = result.get("coding_session") or options.get("coding_session")
            if not session:
                result = {**result, "auto_amend_stop": "coding session unavailable; continue explicitly with its session ID"}
                break
            options = {**options, "amend_from": folder / "outcome.json", "coding_session": session,
                       "predispatch_receipt": None}
            # Only the first Claude round crosses providers. Later BLOCK rounds
            # resume the Claude session returned by that handover.
            options.pop("handover", None)
            options = correction_options(options, correction)
        return finish(owner, root, manifest, result)
    except BaseException as exc:
        manifest.update(state="interrupted", error=str(exc))
        raise
    finally:
        owner._write(root / "manifest.json", manifest)
