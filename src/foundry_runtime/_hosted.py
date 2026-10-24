"""Durable launch envelope around the existing CLI and its evidence owners."""
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import sys
import subprocess

active_directory = ContextVar('foundry_hosted_directory', default=None)
ENVIRONMENT = ('PATH', 'CODEX_HOME', 'CODEX_THREAD_ID', 'CLAUDE_CONFIG_DIR',
               'PDCL_MODEL_CATALOGUE', 'FOUNDRY_CONTEXT_TOKENS', 'FOUNDRY_COMPACT_PERCENT', 'FOUNDRY_COMPACT_TOKENS',
               'FOUNDRY_BASH_CHARS', 'FOUNDRY_TOOL_TOKENS', 'FOUNDRY_WAIT_INTERVAL_SECONDS', 'FOUNDRY_CODEGRAPH_ARGV',
               'FOUNDRY_CODEGRAPH_STATE_DIR')
INPUT_FLAGS = ('--project-preflight-input', '--project-resume-preflight-input')


def write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def capture(report):
    directory = active_directory.get()
    if directory is not None:
        write(directory / 'execution.json', report)
        return True
    return False


def submit(directory, argv, *, entry=None):
    from . import _windows_host
    from ._followup import origin
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    inputs = {}
    for index, argument in enumerate(argv):
        name, _, inline = argument.partition('=')
        if name in INPUT_FLAGS:
            source = inline if '=' in argument else argv[index + 1]
            # These per-call values are already retained by ProjectExecution.
            # Preserve them across wrapper temp-file deletion, not project policy.
            inputs[name] = json.loads(Path(source).read_text(encoding='utf-8-sig'))
    content = {'python': sys.executable, 'cwd': str(Path.cwd()), 'argv': list(argv), 'inputs': inputs,
               'entry': entry, 'environment': {key: os.environ[key] for key in ENVIRONMENT if key in os.environ},
               'originating_session': origin()}
    run_id = hashlib.sha256(json.dumps([str(directory), content], sort_keys=True).encode()).hexdigest()
    request = dict(content, run_id=run_id, task_name='Foundry-' + run_id[:24])
    # A directory is one invocation, not a reusable queue slot. Exclusive creation
    # records even an ambiguous submission; another call only observes it.
    try:
        with (directory / 'request.json').open('x', encoding='utf-8') as stream:
            json.dump(request, stream, ensure_ascii=False, indent=2)
    except FileExistsError:
        if read(directory / 'request.json') != request:
            raise ValueError('Hosted directory already binds different invocation arguments; inspect --hosted-status')
    else:
        try:
            observed = _windows_host.call('register', _windows_host.scheduler_request(directory, request))
            write(directory / 'submission.json', observed)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            write(directory / 'submission.json', {'error': str(exc), 'outcome': 'unconfirmed'})
    return status(directory)


def status(directory, *, plan=False):
    from ._followup_host import enrich
    from . import _windows_host
    directory = Path(directory).resolve(strict=True)
    request = read(directory / 'request.json')
    report = {'hosted': str(directory), 'run_id': request['run_id'],
              'originating_session': request['originating_session'], 'read_only': True,
              'python': request['python'], 'task_name': request['task_name'],
              'log_path': str(directory / 'controller.log'), 'task_acceptance': 'not_evaluated',
              'status': 'pending', 'event_id': None}
    try:
        report['scheduler'] = _windows_host.call('inspect', {'name': request['task_name']})
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        report['scheduler'] = {'error': str(exc)}
    try:
        if (directory / 'started.json').exists():
            from ._executor_lifecycle import process_identity_exited
            started = read(directory / 'started.json')
            report['controller'] = dict(started, exited=process_identity_exited(started['pid'], started['creation_time']))
        if (directory / 'execution.json').exists():
            from ._followup import _goal_status, _dispatch_status
            execution = read(directory / 'execution.json')
            observed = (_goal_status(execution['goal']) if execution.get('goal') else
                        _dispatch_status(execution['dispatch']))
            if observed['run_id'] != execution['run_id']:
                raise ValueError('Hosted execution identity changed')
            report['execution'] = observed
            for key in ('status', 'event_id', 'outcome_path', 'outcome_status', 'result_path',
                        'returncode', 'evidence', 'reason', 'next_action', 'project_action', 'coding_recovery', 'input_request', 'feedback'):
                if key in observed:
                    report[key] = observed[key]
        if (directory / 'finished.json').exists():
            report['completion'] = read(directory / 'finished.json')
            if report['status'] == 'pending':
                report.update(status='launch_finished', reason='CLI exited; see completion returncode and controller log.')
        elif report.get('controller', {}).get('exited'):
            report.update(status='execution_stopped', reason='Hosted controller exited without a final receipt; child cleanup is unconfirmed.')
        elif 'controller' not in report:
            scheduler = report['scheduler']
            # Registered service work may be queued/running before Python writes
            # its first receipt; neither a registration nor pending proves life.
            if not scheduler.get('exists') or scheduler.get('state') not in (2, 4):
                report.update(status='reconciliation_required', reason='No controller receipt and no queued/running service action; inspect submission and scheduler evidence before retrying.')
        if report['status'] != 'pending':
            # Inner completion and outer process custody are distinct facts.
            # Bind both to this launch/chat; changing state must change the ID.
            report['event_id'] = hashlib.sha256(json.dumps(
                [request['run_id'], request['originating_session'], report['status'],
                 report['event_id'], report.get('completion') if report['status'] == 'launch_finished' else None],
                sort_keys=True).encode()).hexdigest()
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        report.update(status='reconciliation_required', reason=str(exc),
                      event_id=hashlib.sha256((request['run_id'] + str(exc)).encode()).hexdigest())
    argv = [request['python'], '-m', 'foundry_runtime', '--hosted-status', str(directory)]
    report['status_argv'] = argv
    return enrich(report, argv, plan=plan)
