"""Native admission transport; project code owns verdicts and worker admission."""
import json
from pathlib import Path
import subprocess
from time import sleep

from . import _task_deadline as deadline
from ._app_server_policy import DEFAULT_CAPACITY_DELAYS
from ._provider_stop import reported_stop

# Admission has no worker side effects. Reuse the first two capacity delays,
# retaining a short bounded judgement cycle rather than the worker's full schedule.
RETRY_DELAYS = DEFAULT_CAPACITY_DELAYS[:2]


class AdmissionUnavailable(RuntimeError):
    def __init__(self, reason, attempts):
        self.reason = reason
        super().__init__(f'admission judgement unavailable: {reason} ({attempts} attempts)')


def _failure(stdout):
    for line in reversed(stdout.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get('type') == 'turn.completed' or (event.get('type') == 'result'
                and event.get('is_error') is False and event.get('subtype') == 'success'):
            return None
        if event.get('type') not in {'error', 'turn.failed'} and event.get('is_error') is not True:
            continue
        stop = reported_stop(event)
        if stop:
            return stop['category']
        error = event.get('error') if isinstance(event.get('error'), dict) else {}
        text = error.get('message') or event.get('message') or event.get('result') or ''
        if (error.get('code') in {'ECONNRESET', 'ETIMEDOUT'}
                or error.get('status') in {502, 503, 504}
                or (isinstance(text, str) and text.startswith('stream disconnected before completion:'))):
            return 'transport'
        return 'native_error'
    return None


def run(argv, *, evidence, output_path, runner=subprocess.run, **options):
    """Retry only failed judgements; never interpret PASS/REFUSE or launch a worker."""
    evidence = Path(evidence)
    output_path = Path(output_path)
    attempts = []

    def unavailable(reason):
        error = AdmissionUnavailable(reason, len(attempts))
        (evidence / 'admission-failure.json').write_text(json.dumps({
            'reason': reason, 'message': str(error), 'attempts': attempts}, indent=2), encoding='utf-8')
        raise error

    for index in range(len(RETRY_DELAYS) + 1):
        if deadline.remaining() == 0:
            unavailable('task_deadline')
        folder = evidence / f'attempt-{index + 1}'
        folder.mkdir()  # Each native attempt retains its own output.
        output_path.unlink(missing_ok=True)
        kwargs = dict(options)
        remaining = deadline.remaining()
        if remaining is not None:
            kwargs['timeout'] = min(kwargs.get('timeout', remaining), remaining)
        reason = None
        try:
            process = runner(argv, **kwargs)
            stdout, stderr = process.stdout or '', process.stderr or ''
            code = process.returncode
        except subprocess.TimeoutExpired as exc:
            stdout, stderr, code = exc.stdout or '', exc.stderr or '', None
            reason = 'timeout'
        except OSError as exc:
            stdout, stderr, code = '', str(exc), None
            reason = 'native_start'
        stdout = stdout.decode('utf-8', 'replace') if isinstance(stdout, bytes) else stdout
        stderr = stderr.decode('utf-8', 'replace') if isinstance(stderr, bytes) else stderr
        reason = reason or _failure(stdout) or ('native_exit' if code else None)
        for name, text in (('native.jsonl', stdout), ('native.stderr.txt', stderr)):
            (folder / name).write_text(text, encoding='utf-8')
            (evidence / name).write_text(text, encoding='utf-8')
        if output_path.exists():
            (folder / 'result.json').write_bytes(output_path.read_bytes())
        attempts.append({'attempt': index + 1, 'returncode': code, 'reason': reason,
                         'stdout': str(folder / 'native.jsonl'), 'stderr': str(folder / 'native.stderr.txt')})
        (evidence / 'admission-attempts.json').write_text(json.dumps(attempts, indent=2), encoding='utf-8')
        if reason is None:
            return process
        if reason not in {'provider_capacity', 'timeout', 'transport'} or index == len(RETRY_DELAYS):
            unavailable(reason)
        delay = RETRY_DELAYS[index]
        remaining = deadline.remaining()
        if remaining is not None and delay >= remaining:
            unavailable('task_deadline')
        print(f'admission judgement unavailable: {reason}; retry {index + 1}/{len(RETRY_DELAYS)} in {delay}s', flush=True)
        sleep(delay)
