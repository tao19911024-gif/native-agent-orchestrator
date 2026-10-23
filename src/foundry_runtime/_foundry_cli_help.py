"""Advisory recovery syntax shared by project entries; no routing authority."""
import json
from pathlib import Path
import sys

RECOVERY_USAGE = "--order <order> --foundry-goal --recover-goal <directory> [--prompt-file <answer>]"


def continuation_advice(result):
    """Name the delivered outcome, never its historical continuation source."""
    fact = result.get('result_outcome') or {}
    if not result.get('continuation_evidence') or not fact.get('path'):
        return None
    path = fact['path']
    note = ('Current continuation result: ' + str(path) + '\n'
            'delivery.context.continues is historical provenance, not this result. '
            'Keep the original outcome unchanged.')
    if result.get('status') in ('review_blocked', 'candidate_verified'):
        quoted = "'" + str(path).replace("'", "''") + "'"
        note += ('\nFor corrections, retain the recorded runtime/project entry and order; use '
                 '--foundry-goal --amend-from ' + quoted +
                 ' --coding-session <existing-coding-session>. '
                 'Append --prompt-file <additions.md> for new instructions; '
                 'PASS with no findings requires additions. These are argument fragments, not a full command.')
    return note


def _read_manifest(path):
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("manifest must be an object")
    return value


def recovery_invocation(order, folder, manifest, *, python=None, prompt_file=None, extra_args=()):
    """One goal recovery syntax for status and CLI advice; never dispatch here."""
    return _goal_invocation(order, manifest, ['--recover-goal', str(folder)],
                            python=python, prompt_file=prompt_file, extra_args=extra_args)


def amendment_invocation(order, outcome, session, manifest):
    """Use the same recorded entry and environment for explicit review correction."""
    invocation = _goal_invocation(order, manifest,
        ['--amend-from', str(outcome), '--coding-session', session])
    invocation['verification'] = 'syntax_only; task and candidate checked by the existing amendment owner'
    return invocation


def _goal_invocation(order, manifest, mode_args, *, python=None, prompt_file=None, extra_args=()):
    provenance = manifest.get('runtime_provenance') or {}
    # Admission and native execution can have different producers. The native
    # interpreter wins for replay; preflight verifies it can read the contract.
    revision_python = (manifest.get('order_revision_runtime') or {}).get('python')
    native = (manifest.get('manager_state') or {}).get('native_dir')
    native_policy = Path(native) / 'native-manifest.json' if native else None
    policy = _read_manifest(native_policy) if native_policy and native_policy.exists() else {}
    explicit_transition = '--upgrade-runtime' in extra_args or '--migrate-runtime' in extra_args
    python = ((python or revision_python) if explicit_transition else
              (policy.get('runtime_provenance') or {}).get('python') or revision_python or python) or provenance.get('python')
    if not python:
        raise ValueError('goal recovery requires recorded runtime_provenance.python')
    context = (manifest.get('project_record') or {}).get('context') or {}
    if context.get('descriptor'):
        argv = [python, '-m', 'foundry_runtime', '--project-config', context['descriptor']]
    else:
        adapter = provenance.get('adapter') or {}
        if adapter.get('module') != '_pdcl_project' or not adapter.get('path'):
            raise ValueError('goal recovery requires the recorded project descriptor or PDCL adapter entry')
        argv = [python, str(Path(adapter['path']).parent / 'dispatch.py')]
    argv += ['--order', str(order), '--foundry-goal', *mode_args]
    if prompt_file is not None:
        argv += ['--prompt-file', prompt_file]
    argv.extend(extra_args)
    powershell = '& ' + ' '.join('$PromptFile' if v == '<prompt-file>' else
                                "'" + str(v).replace("'", "''") + "'" for v in argv)
    from ._foundry_recovery_preflight import retained_environment
    environment = {} if explicit_transition else retained_environment(policy)
    if environment:
        # Scope assignments to this invocation and restore the shell even when
        # the child fails. The argv remains machine-readable beside its env.
        quoted = lambda value: "'" + str(value).replace("'", "''") + "'"
        save = '; '.join('$prior[' + quoted(key) + '] = [Environment]::GetEnvironmentVariable(' + quoted(key) + ", 'Process')" for key in environment)
        assign = '; '.join('[Environment]::SetEnvironmentVariable(' + quoted(key) + ', ' + quoted(value) + ", 'Process')" for key, value in environment.items())
        powershell = '& { $prior = @{}; ' + save + '; try { ' + assign + '; ' + powershell + "; $result = $LASTEXITCODE } finally { foreach ($key in $prior.Keys) { [Environment]::SetEnvironmentVariable($key, $prior[$key], 'Process') } }; $global:LASTEXITCODE = $result }"
    return {'runtime_python': python, 'argv': argv, 'environment': environment, 'powershell': powershell,
            'verification': 'syntax_only; use --recovery-preflight before manual execution'}


def recovery_advice(order, evidence):
    """Read historical stops and the recorded invocation; never infer live quota."""
    folder = Path(evidence)
    manifest = _read_manifest(folder / 'manifest.json')
    from ._foundry_goal_auto import active_round
    active, current = active_round(folder, manifest)
    native = (current.get('manager_state') or {}).get('native_dir')
    python = (current.get('runtime_provenance') or {}).get('python')
    stops, calls = [], []
    if native:
        native = Path(native)
        policy = native / 'native-manifest.json'
        if policy.exists():
            python = (_read_manifest(policy).get('runtime_provenance') or {}).get('python') or python
        for path in sorted(native.glob('cancellation*.json')):
            row = _read_manifest(path)
            stops.append({'record': str(path), 'observed_utc': row.get('observed_utc'),
                          'reason': row.get('reason'), 'observation': 'historical; not a current quota check'})
        for path in sorted(native.glob('call-*/receipt.json')):
            row = _read_manifest(path)
            if row.get('outcome') != 'success':
                calls.append({'call_id': row.get('call_id'), 'ended_utc': row.get('ended_utc'),
                              'outcome': row.get('outcome'), 'error': row.get('error'),
                              'provider_stop': row.get('provider_stop'), 'receipt': str(path)})
    from ._expert_consultation import observe as observe_experts
    from ._foundry_goal_delivery import coding_recovery
    try:
        invocation = recovery_invocation(order, active, current, python=python)
    except (ValueError, KeyError, TypeError) as exc:
        # Missing old provenance removes only the executable advice, not the
        # retained cancellation/call evidence needed to diagnose that old run.
        invocation = {'status': 'unavailable', 'reason': str(exc), 'argv': [], 'powershell': None}
    argv = invocation['argv']
    return {'evidence': str(active), 'historical_cancellations': stops,
            'unresolved_native_calls': unresolved_native_calls(active),
            'native_reconciliation': {'invocation': invocation,
                'scope': 'a56+ manager recovery measures missing-receipt process trees under the goal lock; '
                         'this command never kills a still-live root or repairs an invalid identity receipt'},
            'coding_recovery': coding_recovery(active, current),
            'expert_consultations': observe_experts(current.get('manager_state') or current.get('expert_state')),
            'historical_failed_calls': calls, 'recovery_argv': argv, 'recovery_invocation': invocation,
            'recovery_command_powershell': invocation['powershell'],
            'note': 'Historical failures may already have successful replacements. These records do not establish a current limit. '
                'Recover unfinished manager decisions with this recorded-runtime command; append --prompt-file for new instructions. '
                    'Resolve any reported identity/preflight refusal first; do not edit retained evidence.'}


def unresolved_native_calls(folder):
    """Expose the native owner's last failed restoration, without inferring liveness."""
    path = Path(folder) / 'manager-usage.json'
    if not path.exists():
        return []
    try:
        snapshot = _read_manifest(path)
    except (OSError, ValueError) as exc:
        return [{'role': 'manager', 'source': str(path), 'error': str(exc),
                 'observation': 'last restoration snapshot unreadable; no process conclusion'}]
    return [{**{key: row.get(key) for key in ('call_id', 'kind', 'evidence', 'error',
                                            'cleanup', 'root_pid', 'root_creation_time')},
             'role': 'manager', 'source': str(path),
             'observation': 'last retained restoration; not a current process or cleanup observation'}
            for row in snapshot.get('calls', []) if row.get('state') == 'unknown']


def announce_recovery_failure(order, evidence):
    try:
        advice = recovery_advice(order, evidence)
    except (OSError, ValueError, TypeError, KeyError):
        return
    print('Retained recovery evidence (historical, not a live quota observation):\n' +
          json.dumps(advice, ensure_ascii=True, indent=2), file=sys.stderr)


def announce_review_recovery(order, evidence):
    """Advisory only: the existing recover_goal owner validates and resumes it."""
    if evidence is None:
        return
    try:
        folder = Path(evidence)
        manifest = _read_manifest(folder / "manifest.json")
        active = Path(manifest["active_round"]) if manifest.get("active_round") else folder
        current = _read_manifest(active / "manifest.json") if active != folder else manifest
        if not current.get("review_checkpoint") or (active / "outcome.json").exists():
            return
    except (OSError, ValueError, TypeError):
        return
    print("Review interrupted. Keep completed reviews, coding and test evidence; "
          "retry only unfinished review slots using the existing checkpoint:\n"
          f'  --order "{order}" --foundry-goal --recover-goal "{folder}"\n'
          "Do not start a fresh --foundry-review for this interruption; "
          "no --prompt-file is needed for review-only recovery.", file=sys.stderr)
