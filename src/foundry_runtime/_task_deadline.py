"""One absolute task clock; persistence stays with goals and execution intents."""
from contextvars import ContextVar
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
import math
import time

_current = ContextVar('foundry_task_deadline', default=None)


def normalize(value):
    if value is None:
        return None
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('task deadline requires an explicit timezone')
        value = parsed.timestamp()
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError('task deadline must be an absolute timestamp')
    return float(value)


def current():
    return _current.get()


@contextmanager
def scope(value):
    value = normalize(value)
    inherited = current()
    if inherited is not None:
        value = min(value, inherited) if value is not None else inherited
    token = _current.set(value)
    try:
        yield value
    finally:
        _current.reset(token)


def remaining(value=None):
    value = current() if value is None else value
    return None if value is None else max(0, value - time.time())


def check():
    if remaining() == 0:
        raise TimeoutError('task deadline reached; no new execution started')


def goal_scope(invoke):
    @wraps(invoke)
    def run(*args, **kwargs):
        retained = (kwargs.get('retained') or {}).get('manifest') or {}
        recorded = retained.get('task_deadline_at')
        recorded = recorded or (kwargs.get('manager_state') or {}).get('task_deadline_at')
        with scope(recorded):
            with scope(kwargs.get('task_deadline_at')):
                kwargs['task_deadline_at'] = current()
                return invoke(*args, **kwargs)
    return run
