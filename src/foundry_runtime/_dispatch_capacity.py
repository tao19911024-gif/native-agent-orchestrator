"""Bounded capacity recovery through the existing dispatcher and intent journal.

Each retry is a normal same-session resume, after verified terminal cleanup.
The ResultReceipt records the recovery observation; it grants no execution authority.
"""
from functools import wraps
import json
import math
from pathlib import Path
from time import sleep, time

from ._provider_stop import reported_stop

from ._app_server_policy import DEFAULT_CAPACITY_DELAYS
from . import _task_deadline as deadline


def recover_capacity(invoke):
    def recover(*args, **kwargs):
        result = invoke(*args, **kwargs)
        if getattr(result, 'returncode_source', None) not in ('turn_status', 'native_exit'):
            return result
        from . import _foundry_dispatch as owner, _dispatch_resume
        from ._dispatch_package import read_intent
        from ._execution_intent import ExecutionIntent
        attempts, executions, origin = [], [result], None
        quota_resets = set()
        while result[0]:
            log = Path(result[1])
            if not log.with_suffix('.terminal.json').exists():
                break
            terminal = json.loads(log.with_suffix('.terminal.json').read_bytes())
            quota_due = None
            launcher = 'app-server'
            if terminal.get('schema_version') == 'pdcl-claude-terminal-1':
                from ._dispatch_claude import read_result
                from ._provider_activity import observe, short_window_reset
                launcher = 'claude'
                session = terminal['session_id']
                native, _ = read_result(terminal['stdout']['path'], session=session)
                stop = reported_stop(native)
                if not native.get('is_error') or not stop or stop['category'] != 'provider_limit':
                    break
                quota_due = short_window_reset(observe(log, launcher), session)
            else:
                turn = terminal.get('terminal', {}).get('params', {}).get('turn', {})
                stop = reported_stop(turn)
                if (terminal.get('expiration_cause') or turn.get('status') != 'failed'
                        or not stop or stop['category'] not in ('provider_capacity', 'provider_limit')):
                    break
                if stop['category'] == 'provider_limit':
                    from ._provider_activity import stream_reset
                    quota_due = stream_reset(log, 'codex', None)
            if stop['category'] == 'provider_limit':
                previous_recovery = {}
                try:
                    previous_recovery = json.loads(log.with_suffix('.receipt.json').read_bytes()).get('provider_recovery') or {}
                except (OSError, ValueError):
                    pass
                retained_reset = any(r.get('reset_at') == quota_due for r in previous_recovery.get('attempts', []))
                if quota_due is None or (quota_due <= time() and not retained_reset) or quota_due in quota_resets:
                    break
            package = json.loads(log.with_suffix('.package.json').read_bytes())
            _, record = read_intent(package)
            ExecutionIntent._verify_completion(record)
            if record['state'] != 'completed':
                raise ValueError('capacity recovery requires a completed intent')
            frozen = record['frozen']
            cutoff = frozen.get('task_deadline_at', deadline.current())
            if deadline.current() is not None:
                cutoff = min(cutoff, deadline.current()) if cutoff is not None else deadline.current()
            if cutoff is not None and (time() >= cutoff or (quota_due is not None and quota_due >= cutoff)):
                if not result.replayed:
                    owner.result_receipt.annotate_capacity(log, {'status': 'deadline_prevents_retry',
                        'reason': stop['category'], 'task_deadline_at': cutoff, 'next_retry_at': quota_due})
                break
            delays = (frozen.get('app_server_policy') or {}).get('capacity_retry_seconds', DEFAULT_CAPACITY_DELAYS)
            origin = origin or record['intent_id']
            session = record['session_id'] if launcher == 'claude' else record['turn_binding']['thread_id']
            capacity_count = sum('reset_at' not in attempt for attempt in attempts)
            if quota_due is None and capacity_count >= len(delays):
                result.outcome = 'provider_capacity_exhausted'
                break
            delay = quota_due - time() if quota_due is not None else delays[capacity_count]
            if cutoff is not None and time() + delay >= cutoff:
                if not result.replayed:
                    owner.result_receipt.annotate_capacity(log, {'status': 'deadline_prevents_retry',
                        'reason': stop['category'], 'task_deadline_at': cutoff, 'next_retry_at': time() + delay})
                break
            if quota_due is not None:
                quota_resets.add(quota_due)
            else:
                capacity_count += 1
            attempts.append({'predecessor': record['intent_id'], 'terminal': record['evidence']['terminal'],
                             'session': session, 'backoff_seconds': delay})
            if quota_due is not None:
                # Store absolute reset, not a relative delay that changes on replay.
                attempts[-1].update(reason='provider_limit', reset_at=quota_due)
                attempts[-1].pop('backoff_seconds')
            def before_attempt():
                # Run under normal intent admission, only for an unfinished successor.
                # Replaying a completed chain must neither wait nor announce new work.
                due = quota_due if quota_due is not None else time() + delay
                try:
                    previous = json.loads(log.with_suffix('.receipt.json').read_bytes()).get('provider_recovery') or {}
                    if (previous.get('status') in ('backoff', 'interrupted')
                            and previous.get('attempts') == attempts
                            and type(previous.get('next_retry_at')) in (int, float)
                            and math.isfinite(previous['next_retry_at'])):
                        due = previous['next_retry_at']
                except (OSError, ValueError, AttributeError, TypeError):
                    pass  # Missing observation uses the bounded delay; journal still owns admission.
                remaining = max(0, due - time())
                deadline.check()
                if cutoff is not None and due >= cutoff:
                    raise TimeoutError('task deadline precedes provider recovery; original session retained')
                observation = {'status': 'backoff', 'attempts': attempts, 'next_retry_at': due,
                               'schedule_seconds': list(delays), 'scheduled_wait_seconds': sum(delays[:capacity_count])}
                if quota_due is not None:
                    observation.update(reason='provider_limit', scheduled_wait_seconds=remaining,
                                       schedule_seconds=[], reset_at=quota_due)
                if quota_due is not None:
                    print('provider quota: wait until %s (%ss remaining); same session %s' %
                          (due, remaining, session), flush=True)
                else:
                    print('provider capacity: retry %d/%d in %ss; same thread %s' %
                          (capacity_count, len(delays), remaining, session), flush=True)
                owner.result_receipt.annotate_capacity(log, observation)
                try:
                    sleep(remaining)
                    deadline.check()
                except KeyboardInterrupt:
                    owner.result_receipt.annotate_capacity(log, {**observation, 'status': 'interrupted'})
                    raise
            # Stable text gives dispatcher replay its existing idempotency key.
            # Never replay the original order or change the recorded compute.
            prompt = ('Foundry provider-capacity continuation %s/%d. '
                      'Continue the same task from retained work after the provider capacity error. '
                      'Preserve completed work and the existing instructions; do not restart the task.'
                      % (origin, len(attempts)))
            if quota_due is not None:
                prompt = ('Foundry provider-limit continuation %s/reset-%s. Continue the same task '
                          'from retained work after the short usage window resets. Preserve the '
                          'existing instructions and completed work; do not restart the task.' % (origin, quota_due))
            with deadline.scope(cutoff):
                result = _dispatch_resume.resume_order(owner, session, Path(frozen['worktree']),
                    record['task_key'], prompt, launcher=launcher,
                    before_attempt=before_attempt,
                    app_server_policy=frozen.get('app_server_policy'),
                    **(frozen.get('requested_compute') or {}),
                    **({'project': kwargs['project']} if kwargs.get('project') is not None else {}))
            executions.append(result)
            if not result.replayed:
                owner.result_receipt.annotate_capacity(log, {'status': 'resumed', 'attempts': attempts,
                    'successor_log': str(result[1])})
        if attempts or result.outcome == 'provider_capacity_exhausted':
            result.capacity_executions = executions
            result.new_execution_count = sum(not item.replayed for item in executions)
            result.replayed = result.new_execution_count == 0
            result.capacity_recovery = {'status': result.outcome, 'attempts': attempts,
                'final_log': str(result[1]), 'automatic_retries': len(attempts),
                'schedule_seconds': list(delays), 'scheduled_wait_seconds': sum(delays[:capacity_count])}
            if quota_resets:
                result.capacity_recovery.update(reason='provider_limit', schedule_seconds=[],
                                                scheduled_wait_seconds=None,
                                                reset_times=sorted(quota_resets))
            from ._project_status import resume_argv
            python = package['runtime_provenance']['python']
            project = kwargs.get('project')
            argv = resume_argv(project.path if project is not None else '', record['task_key'], session, frozen, python)
            if project is None:
                adapter = package['runtime_provenance'].get('adapter') or {}
                if adapter.get('module') == '_pdcl_project' and adapter.get('path'):
                    argv = [python, str(Path(adapter['path']).with_name('dispatch.py')),
                            *argv[5:], '--worktree', frozen['worktree']]
                else:
                    argv = None
            result.capacity_recovery['resume_argv'] = argv
            # Existing derived receipt, not a parallel task state or retry queue.
            if not result.replayed:
                owner.result_receipt.annotate_capacity(result[1], result.capacity_recovery)
        return result
    @wraps(invoke)
    def run(*args, **kwargs):
        value = kwargs.pop('task_deadline_at', None)
        with deadline.scope(value):
            return recover(*args, **kwargs)
    return run
