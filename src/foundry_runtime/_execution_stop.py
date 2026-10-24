"""Explicit abort of one recorded executor, followed by existing reconciliation.

No success or native terminal result is fabricated. The controller retains Job
cleanup ownership; reconciliation closes the abandoned intent only after exit.
"""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import time

from ._execution_intent import ExecutionIntent, IntentConflict, IntentBusy
from ._executor_lifecycle import _api, _creation, process_identity_exited


def _terminate(record):
    api = _api()
    api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.TerminateProcess.restype = wintypes.BOOL
    handle = api.OpenProcess(0x100000 | 0x1000 | 0x0001, False, record['root_pid'])
    if not handle:
        if process_identity_exited(record['root_pid'], record['root_creation_time']):
            return
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if _creation(api, handle) != record['root_creation_time']:
            raise IntentConflict('recorded executor PID has been reused; nothing stopped')
        if api.WaitForSingleObject(handle, 0) == 0:
            return
        if not api.TerminateProcess(handle, 130):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        api.CloseHandle(handle)


def _stop_cleaned_controller(owner, original):
    """Explicit escalation only after the original owner released its intent lock.

    Keep that same lock through identity verification and termination so a
    recovery successor cannot acquire this predecessor in the meantime.
    """
    try:
        owner._acquire()
        latest, _, _ = owner._read(owner.path)
        keys = ('attempt_id', 'root_pid', 'root_creation_time', 'owner_pid', 'owner_creation_time')
        cleanup = latest.get('cleanup', {})
        if (latest['state'] != 'unknown' or 'recovery_authorization' in latest
                or any(latest.get(k) != original.get(k) for k in keys)
                or cleanup.get('cleanup_verified') is not True
                or cleanup.get('root_exited') is not True
                or type(cleanup.get('job_active_processes')) is not int
                or cleanup['job_active_processes'] != 0
                or any(cleanup.get(k) != latest.get(k) for k in keys[:3])
                or not process_identity_exited(latest['root_pid'], latest['root_creation_time'])):
            raise IntentConflict('controller stop requires matching durable unknown/verified cleanup with Job count zero')
        _terminate({'root_pid': latest['owner_pid'], 'root_creation_time': latest['owner_creation_time']})
    finally:
        owner.__exit__(None, None, None)


def stop(common, task, frozen, *, intent_id, output, reason, wait_seconds=20, keep_recoverable=False,
         stop_controller_after_cleanup=False):
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('stopping an executor needs an operator reason')
    owner = ExecutionIntent(common, task, frozen, intent_id=intent_id)
    record, _, _ = owner._read(owner.path)
    if record['state'] in ('completed', 'closed'):
        return record
    if record['state'] not in ('active', 'unknown') or not record.get('root_pid'):
        raise IntentConflict('stop requires a recorded active/unknown executor root')
    if 'recovery_authorization' in record:
        raise IntentConflict('stop the recovery successor rather than its predecessor')
    output = Path(output).resolve()
    request = output.with_suffix(output.suffix + '.stop-request.json')
    binding = {'intent_id': intent_id, 'task': task, 'reason': reason.strip(),
               **{key: record[key] for key in ('attempt_id', 'root_pid', 'root_creation_time')}}
    if keep_recoverable:
        binding['keep_recoverable'] = True
    if stop_controller_after_cleanup:
        binding['stop_controller_after_cleanup'] = True
    output.parent.mkdir(parents=True, exist_ok=True)
    if request.exists():
        if json.loads(request.read_bytes()) != binding:
            raise IntentConflict('stop output already names another request')
    else:
        with request.open('x', encoding='utf-8') as stream:
            json.dump(binding, stream, ensure_ascii=False, indent=2)
    if not stop_controller_after_cleanup:
        _terminate(record)
    deadline = time.monotonic() + wait_seconds
    while True:
        if stop_controller_after_cleanup:
            try:
                _stop_cleaned_controller(owner, record)
                stop_controller_after_cleanup = False
            except IntentBusy:
                # Cleanup is published before the controller releases its intent
                # lock. Wait through that handoff using this stop request's clock.
                pass
        latest, _, _ = owner._read(owner.path)
        if latest['state'] in ('completed', 'closed'):
            return latest  # A native terminal result racing the stop remains authoritative.
        if process_identity_exited(record['owner_pid'], record['owner_creation_time']):
            try:
                return ExecutionIntent.settle_abandoned(common, task, frozen, intent_id=intent_id,
                    output=output, close_reason=None if keep_recoverable else 'Operator stop: ' + reason.strip())
            except IntentBusy:
                pass
        if time.monotonic() >= deadline:
            raise TimeoutError('stop requested; controller cleanup is still pending. '
                               'Repeat this same --stop-reason command; do not launch a replacement yet.')
        time.sleep(.1)
