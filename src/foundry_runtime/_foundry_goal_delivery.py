"""Goal delivery projections: retained worker handoffs, never acceptance verdicts."""
from pathlib import Path
import hashlib
import json
import sys

# Presentation only. Continuation eligibility belongs to the original execution
# journal and measured cleanup, never this finite list of vendor error names.
STOP_OUTCOMES = {'input_required': ('coding needs an operator answer', 'awaiting_input'),
               'provider_limit': ("coding stopped at the provider's usage limit; continue it in the original "
                                  'session after the limit resets', 'coding_incomplete'),
               'provider_capacity': ('coding stopped because the provider model was at capacity; continue it '
                                     'in the original session', 'coding_incomplete')}

# Native CodexErrorInfo string variants (see _app_server_terminal) that end a failed turn,
# not the task: the provider refused or shed the request and the thread is intact.
from ._provider_stop import reported_stop


def review_feedback(outcome_path):
    """Read a candidate's feedback and present its existing correction entry."""
    from . import foundry_goal as owner
    from ._foundry_cli_help import amendment_invocation

    path = Path(outcome_path)
    outcome = json.loads(path.read_bytes())
    manifest_path = path.with_name('manifest.json')
    manifest = json.loads(manifest_path.read_bytes())
    source = owner._fact(path)
    prior = {'prior_outcome': source, 'prior_manifest': owner._fact(manifest_path),
             'base_commit': manifest.get('base_commit'), 'candidate_commit': outcome.get('candidate_commit'),
             'reviews': outcome.get('reviews', []), 'host_findings': outcome.get('host_findings', [])}
    focus = owner._review_focus(prior)
    view = {'task': {key: manifest.get(key) for key in ('order', 'run_id')},
            'candidate_commit': outcome.get('candidate_commit'), 'outcome_status': outcome['status'],
            'findings': focus['findings'],
            'predecessor': (manifest.get('amendment') or {}).get('prior_outcome') or
                ((outcome.get('delivery_report') or {}).get('delivery', {}).get('context') or {}).get('continues'),
            'artifacts': {'outcome': source, 'manifest': prior['prior_manifest'],
                          'coding': (outcome.get('coding') or {}).get('evidence', []),
                          'review_checkpoint': manifest.get('review_checkpoint'),
                          'rounds': manifest.get('rounds', []),
                          'usage': outcome.get('task_usage'), 'delivered': outcome.get('artifacts', [])},
            'continuation': {'status': 'no_recorded_findings'}}
    if not focus['findings']:
        return view
    if outcome['status'] not in {'review_blocked', 'candidate_verified'} or len(prior['reviews']) != 2:
        view['continuation'] = {'status': 'review_completion_required'}
        return view
    session = outcome.get('coding_session')
    if not session:
        view['continuation'] = {'status': 'coding_session_required'}
        return view
    try:
        # The amendment owner selects the final round's retained settings.
        settings = owner.amendment_manifest(path, session)
        view['artifacts']['review_checkpoint'] = settings.get('review_checkpoint')
        view['continuation'] = {'status': 'available', 'coding_session': session,
            'invocation': amendment_invocation(settings['order']['path'], path, session, settings)}
    except (OSError, ValueError, KeyError, TypeError, owner.GoalBlocked) as exc:
        view['continuation'] = {'status': 'unavailable', 'reason': str(exc)}
    return view


def candidate(owner, tree, base=None):
    """Observe a committed handoff without refreshing the index or rewriting files.

    BASERED1: status can report an M for CRLF bytes whose repository-normalized
    content is unchanged. Git diffs distinguish that from actual edits. Compare
    index and worktree separately: staged work undone on disk is still unfinished.
    """
    git = owner._git
    head = git(tree, 'rev-parse', 'HEAD').decode().strip()
    changes = git(tree, '-c', 'core.quotePath=false', 'status', '--porcelain=v1',
                  '--untracked-files=all').decode('utf-8')
    if changes.strip():
        diff = ('diff', '--no-ext-diff', '--no-textconv', '--ignore-submodules=none', '--name-only', '-z')
        if not (git(tree, *diff) or git(tree, *diff, '--cached')
                or git(tree, 'ls-files', '--others', '--exclude-standard', '-z')):
            changes = ''
    workspace = {'head': head, 'status_porcelain': changes}
    if changes.strip():
        raise owner.GoalBlocked('candidate worktree is dirty', 'coding_incomplete', workspace=workspace)
    if base:
        if head == base:
            raise owner.GoalBlocked('coding produced no committed candidate', 'coding_incomplete', workspace=workspace)
        git(tree, 'merge-base', '--is-ancestor', base, head)
    return head


def stop_cause(terminal):
    """Why a coding turn stopped, read from its transport's terminal record.

    App-server records its own expirations; a turn it reports as failed with a provider
    CodexErrorInfo also ended the turn, not the task: AXBFG1 (2026-10-05) failed with
    serverOverloaded ("Selected model is at capacity") right after writing its handoff, with
    the thread intact. A Claude turn that the provider refused with HTTP 429 (the session or
    usage limit) is the same case: AMD3LAND2 (2026-10-01) stopped that way after four minutes
    with its session intact and nothing to review. A Claude turn that exited 0 has no stop
    cause; verify_terminal refuses an error result with exit 0.
    """
    if terminal.get('schema_version') == 'pdcl-app-server-terminal-1':
        return terminal.get('expiration_cause') or _app_server_provider_cause(terminal)
    if terminal.get('schema_version') != 'pdcl-claude-terminal-1' or not terminal.get('native_returncode'):
        return terminal.get('expiration_cause')
    from ._dispatch_claude import verify_terminal
    result = verify_terminal(terminal, session=terminal['session_id'], returncode=terminal['native_returncode'],
                             attempt_id=terminal['attempt_id'])
    stop = reported_stop(result)
    return stop['category'] if stop else 'provider_error'


def _app_server_provider_cause(terminal):
    turn = ((terminal.get('terminal') or {}).get('params') or {}).get('turn') or {}
    if turn.get('status') != 'failed':
        return None
    stop = reported_stop(turn)
    return stop['category'] if stop else 'provider_error'


def coding_recovery(folder, manifest):
    """Read-only recovery projection shared by status and refusal advice."""
    reference = manifest.get('coding_checkpoint')
    if manifest.get('manager_state') and manifest.get('manager_phase') == 'dispatch_pending':
        report = {'status': 'manager_handback_reconciliation_required', 'automatic_retry': False,
                'pending_dispatch': manifest.get('pending_dispatch'),
                'next_action': 'Use explicit --recover-goal with --prompt-file on a runtime supporting '
                    'completed-turn reconciliation. It verifies the original terminal and exited processes '
                    'before returning to the manager; it does not repeat the worker. Historical native '
                    'manager runtime changes require the existing --migrate-runtime option.'}
        try:
            from ._foundry_cli_help import recovery_invocation
            invocation = recovery_invocation(manifest['order']['path'], folder, manifest,
                                             prompt_file='<prompt-file>')
            migration = recovery_invocation(manifest['order']['path'], folder, manifest,
                python=sys.executable, prompt_file='<prompt-file>', extra_args=['--migrate-runtime'])
            report['manager_recovery'] = {**invocation, 'preferred': True,
                'migration_argv': migration['argv'],
                'migration_powershell': migration['powershell'],
                'note': 'Run on a runtime supporting completed-turn reconciliation (a52 or later). '
                        'The recorded interpreter is shown, never silently replaced by current. '
                        '--migrate-runtime explicitly migrates native manager provenance; it does not '
                        'install or upgrade the Foundry interpreter. No separate worker authorization is '
                        'needed for a verified completed-turn handback.'}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report['manager_recovery'] = {'status': 'unavailable', 'reason': str(exc)}
        try:
            from . import foundry_goal as owner
            from ._foundry_goal_continuation import retained_dispatch
            from .recovery import command_plan
            path, journal, record, _ = retained_dispatch(owner, Path(folder), manifest)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report['recovery_commands'] = {'status': 'unavailable', 'reason': str(exc)}
            report['manager_recovery'] = {'status': 'unavailable', 'reason': str(exc), 'preferred': False}
            report['next_action'] = 'Resolve the retained dispatch selection before requesting manager recovery: ' + str(exc)
        else:
            try:
                report['recovery_commands'] = {**command_plan(path, journal, record),
                    'role': 'worker_resume_fallback', 'preferred': False,
                    'warning': 'Starts a worker without manager coordination. For a completed turn use manager_recovery first.'}
            except (OSError, ValueError, KeyError, TypeError) as exc:
                report['recovery_commands'] = {'status': 'unavailable', 'reason': str(exc)}
        return report
    if not reference or manifest.get('review_checkpoint'):
        return None
    report = {'automatic_retry': False, 'provider_current_health': 'not_observed', 'controller_python': sys.executable}
    from ._execution_intent import IntentConflict
    try:
        from ._recovery_reconciliation import completed_coding
        path = Path(reference['path'])
        if path.resolve() != (Path(folder) / 'coding-checkpoint.json').resolve():
            raise ValueError('coding checkpoint names another goal directory')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != reference['sha256']:
            raise ValueError('coding checkpoint changed')
        checkpoint = json.loads(raw)
        if any(checkpoint.get(key) != manifest.get(key) for key in ('run_id', 'order', 'worktree', 'base_commit')):
            raise ValueError('coding checkpoint names another goal')
        if not checkpoint.get('returncode'):
            return None  # Successful coding follows the existing handoff/review route.
        report['execution'] = completed_coding(checkpoint)
        terminals = [fact for fact in checkpoint['evidence'] if fact['path'].endswith('.terminal.json')]
        if terminals:
            terminal = json.loads(Path(terminals[-1]['path']).read_bytes())
            report['stop_cause'] = stop_cause(terminal)
            report['provider_error'] = (((terminal.get('terminal') or {}).get('params') or {}).get('turn') or {}).get('error')
        from ._foundry_cli_help import recovery_invocation
        try:
            report['manager_recovery'] = {**recovery_invocation(manifest['order']['path'], folder, manifest,
                prompt_file='<prompt-file>'), 'preferred': True}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report['manager_recovery'] = {'status': 'unavailable', 'reason': str(exc)}
        report.update(status='explicit_continuation_available',
            next_action='Use the existing --recover-goal with --prompt-file containing the operator decision; '
                        'retain the task session and project preflight. This does not claim the provider has recovered.')
    except (OSError, ValueError, KeyError, TypeError, IntentConflict) as exc:
        report.update(status='reconciliation_required', reason=str(exc),
                      next_action='Inspect the original dispatch evidence and use its existing recovery route; no automatic replay.')
    return report


def retained_stop_cause(evidence):
    """A checkpointed turn's stop cause, re-read from its retained terminal record.

    Read from the evidence rather than the checkpoint's copy, so a turn checkpointed before its
    cause was classified continues the same way as a new one.
    """
    terminals = [fact['path'] for fact in evidence if fact['path'].endswith('.terminal.json')]
    return stop_cause(json.loads(Path(terminals[-1]).read_bytes())) if terminals else None


def artifacts(owner, decision, tree, rc, session, evidence):
    paths = decision.get('artifacts')
    if rc != 0 or not session or not isinstance(paths, list) or not paths:
        raise owner.GoalBlocked('artifact delivery needs a completed worker and named paths', 'coding_incomplete')
    facts = []
    try:
        for name in dict.fromkeys(paths):
            path = Path(name)
            if not path.is_absolute():
                path = tree / path
            path = path.resolve(strict=True)
            if path.is_dir():
                entries = []
                for member in sorted(path.rglob('*')):
                    if member.is_file():
                        with member.open('rb') as stream:
                            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                        entries.append({'path': member.relative_to(path).as_posix(), 'sha256': digest})
                raw = json.dumps(entries, ensure_ascii=False, sort_keys=True,
                                 separators=(',', ':')).encode('utf-8')
                facts.append({'path': str(path), 'kind': 'directory', 'entries': entries,
                              'sha256': hashlib.sha256(raw).hexdigest(),
                              'hash_basis': 'utf8-canonical-json-relative-file-manifest'})
            else:
                with path.open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                facts.append({'path': str(path), 'sha256': digest})
    except (OSError, ValueError, TypeError) as exc:
        raise owner.GoalBlocked('artifact handoff is unreadable: ' + str(exc), 'coding_incomplete') from exc
    return {'status': 'artifacts_delivered', 'acceptance_pending': True, 'integration_pending': False,
            'artifacts': facts, 'reason': decision['instruction'], 'reviews': [],
            'coding_session': session, 'coding': {'returncode': rc, 'evidence': evidence},
            'next_action': 'Inspect the named artifact hashes and acceptance evidence with the order owner. '
                           'This is a delivery receipt, not candidate_verified or independent review. '
                           'If work remains, answer through --recover-goal with --prompt-file in the original task.'}


def incomplete(exc, rc, coding_evidence, coding_session, manager_state, manifest):
    outcome = {"status": exc.status, "integration_pending": False,
               "reason": str(exc), "workspace": exc.workspace,
               "coding": {"returncode": rc, "evidence": coding_evidence},
               "reviews": [], "coding_session": coding_session,
               "next_action": "Read the coding report and resolve the unfinished handoff in the existing task; "
                              "continue with --order <original order> --foundry-goal "
                              "--recover-goal <this directory> --prompt-file <answer>. "
                              "An already committed candidate can use --foundry-review. "
                              "This result is not a review BLOCK and does not trigger automatic correction."}
    if (manifest.get('candidate_closure') or {}).get('status') in ('failed', 'not_evaluated'):
        outcome['next_action'] = (
            'Inspect the closure receipt and correct the verification environment or candidate. '
            'On a67 or later, --recover-goal without --prompt-file retries closure and review '
            'of the unchanged candidate without another coding or manager turn. '
            'Historical runtimes require an explicit settled-boundary upgrade; changed code '
            'uses the existing amendment or named-candidate review path.')
        return outcome
    if manifest.get('manager_phase') == 'awaiting_input':
        decision = manifest['execution_decision']['manager']
        outcome['input_request'] = {'question': decision['instruction'], 'call_id': decision['call_id'],
                                    'decision': decision['evidence']}
        if manifest.get('assignment_refusal_question'):
            outcome['input_request'].update(manifest['assignment_refusal_question'])
        outcome['next_action'] = ('Provide the actual operator answer through the original goal --recover-goal '
            'with --prompt-file. Without an answer recovery returns this same request without model calls. '
            'Question delivery/acceptance is not approval; no worker or manager is running to wait.')
        return outcome
    if manager_state is not None and not manifest.get('coding_checkpoint'):
        outcome['next_action'] = (
            "The manager requested review before coding in this invocation, but the candidate is not "
            "reviewable; no worker was dispatched by this invocation. This directory has no coding "
            "checkpoint to answer. Inspect the task and workspace: for a new task, start a new goal "
            "with clarified coding instructions; for an answer continuation, supply a clarified answer from "
            "its parent run; for an amendment, re-run --amend-from on the original review outcome. "
            "This result is not a review BLOCK.")
    return outcome
