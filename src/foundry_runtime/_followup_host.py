"""Codex host registration contract; the host alone writes and runs automations."""
import hashlib
import json
import os
from pathlib import Path
import tomllib

MARKER = 'Foundry follow-up binding: '


def binding(report):
    origin = report.get('originating_session') or {}
    if origin.get('host') != 'codex' or not origin.get('thread_id'):
        return None
    kind = 'hosted' if report.get('hosted') else 'goal' if report.get('goal') else 'dispatch'
    return {kind: str(Path(report[kind]).resolve()), 'run_id': report['run_id'],
            'thread_id': origin['thread_id']}


def inspect(report):
    """Observe the host registry, not a second Foundry delivery ledger."""
    bound = binding(report)
    if bound is None:
        return {'status': 'origin_unavailable', 'automations': []}
    root = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'automations'
    matches, errors = [], []
    try:
        try:
            root.stat()
        except FileNotFoundError:
            paths = []
        else:
            paths = list(root.glob('*/automation.toml'))
        for path in paths:
            try:
                record = tomllib.loads(path.read_text(encoding='utf-8-sig'))
                if record.get('kind') != 'heartbeat' or record.get('target_thread_id') != bound['thread_id']:
                    continue
                prompt = record.get('prompt', '')
                if not isinstance(prompt, str) or record.get('status') not in ('ACTIVE', 'PAUSED'):
                    raise ValueError('invalid heartbeat prompt or status')
                markers = [json.loads(line[len(MARKER):]) for line in prompt.splitlines()
                           if line.startswith(MARKER)]
                exact = bound in markers
                # Existing host prompts predate structured bindings. Surface a candidate
                # for adoption instead of silently registering a duplicate heartbeat.
                target = bound.get('hosted') or bound.get('goal') or bound.get('dispatch')
                legacy = not markers and bound['run_id'] in prompt and target.replace('\\', '/') in prompt.replace('\\', '/')
                if exact or legacy:
                    matches.append({'id': record['id'], 'status': record['status'],
                                    'rrule': record.get('rrule'),
                                    'binding': 'exact' if exact else 'legacy_candidate'})
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append({'path': str(path), 'error': str(exc)})
    except OSError as exc:
        errors.append({'path': str(root), 'error': str(exc)})
    state = ('unavailable' if errors else 'ambiguous' if len(matches) > 1 else
             'legacy_candidate' if matches and matches[0]['binding'] != 'exact' else
             'registered' if matches and matches[0]['status'] == 'ACTIVE' else
             'paused' if matches else 'unregistered')
    return {'status': state, 'automations': matches, 'errors': errors,
            'delivery': 'not_observed', 'registry': str(root)}


def registration_request(report, status_argv):
    """Ready host-tool input, never a claim that the tool has already been called."""
    bound = binding(report)
    if bound is None:
        return None
    key = hashlib.sha256(json.dumps(bound, sort_keys=True).encode()).hexdigest()[:12]
    prompt = (
        'Observe this authorized Foundry task and return its result to this originating chat. '
        'Run only the read-only status command, argv=' + json.dumps(status_argv) + '. '
        'Verify run_id and originating_session.thread_id against the binding below. '
        'Do not launch, resume, approve or modify business tasks or message other chats. '
        'Stay silent on pending or unchanged state; pending alone does not prove process liveness. '
        'For outcome_available read the referenced small outcome; for result_available read the '
        'referenced result and return code. Report completion, failure, quota blocking or the exact '
        'required decision, with evidence and next action. For launch_finished report the completion '
        'returncode and inspect the referenced controller log; CLI exit is not a business outcome. '
        'Execution completion is not project acceptance. '
        'For execution_stopped, superseded, reconciliation_required, identity mismatch or unreadable '
        'evidence, report the specific issue without automatic retry or assuming child cleanup. '
        'Deduplicate by event_id against prior notices in this chat. After reporting an actionable '
        'or terminal result, pause this exact heartbeat using automation_update, preserving all fields. '
        'Registration or pausing is not evidence that a message was delivered.\n'
        + MARKER + json.dumps(bound, ensure_ascii=True, sort_keys=True))
    return {'mode': 'create', 'kind': 'heartbeat', 'name': 'Foundry result ' + key,
            'prompt': prompt, 'rrule': 'FREQ=MINUTELY;INTERVAL=15', 'status': 'ACTIVE',
            'targetThreadId': bound['thread_id']}


def enrich(report, status_argv, *, plan=False):
    observed = inspect(report)
    observed.update(
        mechanism='heartbeat_polling' if observed['status'] == 'registered' else 'none',
        push_status='receiver_not_available',
        push_scope='No supported local Codex background receiver is exposed to Foundry.',
        automatic_registration=False,
        event_retention='Existing run, outcome, journal and hosted receipts; no delivery queue or acknowledgement.',
    )
    report['host_follow_up'] = observed
    report['delivery_status'] = ('host_polling_registered_delivery_unobserved'
                                if observed['status'] == 'registered' else 'automatic_delivery_unavailable')
    if plan:
        report['registration_request'] = registration_request(report, status_argv)
        report['registration_tool'] = 'mcp__codex_app.automation_update'
        report['registration_action'] = (
            'inspect_registry_error' if observed['status'] == 'unavailable' else
            'reuse_existing' if observed['status'] == 'registered' else
            'preserve_paused' if observed['status'] == 'paused' else
            'inspect_and_update_existing' if observed['automations'] else
            'create' if observed['status'] == 'unregistered' else 'origin_required')
        report['registration_instruction'] = (
            'This is an optional polling plan, not an event callback or authorization to register. '
            'Submit it only when the user explicitly requests periodic follow-up. Preserve cancellation, '
            'paused state and existing cadence; an authorized business continuation is not permission '
            'to restart a cancelled monitor. Missing registration cannot distinguish deletion from '
            'never registered. Verify any authorized host update through status; registration is not delivery.')
    return report
