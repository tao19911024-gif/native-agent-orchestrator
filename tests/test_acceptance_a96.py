"""Follow-up regression cases for review launches and Git observations."""
import subprocess
import sys
import time
from pathlib import Path

import pytest

from foundry_runtime import foundry_goal as goal
from foundry_runtime import _launch_context as launch
from foundry_runtime import _foundry_native_transport as transport
from foundry_runtime import _task_deadline as deadline
from foundry_runtime._foundry_review_group import Panel
from foundry_runtime._foundry_response_contract import SchemaResponseError


def test_review_group_captures_reviewer_settings(tmp_path, monkeypatch):
    monkeypatch.delenv('FOUNDRY_TOOL_TOKENS', raising=False)
    seen = []
    class Member:
        def __init__(self, *args, **kwargs):
            seen.append(kwargs['launch_context'])
        def __call__(self, request):
            return {'verdict': 'PASS'}
    request = {'kind': 'review', 'call_id': 'review-0', 'packet': {}}
    panel = Panel([{'executable': sys.executable}], {'review-0': request}, Member,
                  tmp_path / 'reviews', tmp_path)
    assert panel(request) == {'verdict': 'PASS'}
    assert seen[0]['settings']['tool_tokens'] == 0
    assert seen[0]['tool_discipline']['root_role'] == 'reviewer'
    assert launch.capture(enable_index=False)['settings']['tool_tokens'] == 3000
    assert transport.bind_profile({}, None, launch_context=seen[0])['launch_context']['tool_tokens'] == 0


@pytest.mark.parametrize('value', [None, [], 'PASS', 1])
def test_invalid_review_shape_uses_schema_error(value, tmp_path):
    with pytest.raises(SchemaResponseError):
        goal._validate_review(value, {'review_field_reads': True}, tmp_path, retained_only=True)


@pytest.mark.parametrize('progress', [False, True])
def test_deadlineless_git_observation_stall(tmp_path, monkeypatch, progress):
    # Real pipes and process cleanup; shorten only the fixture's inactivity bound.
    popen = subprocess.Popen
    children = []
    script = ('import sys,time\nfor i in range(12):\n print(i,flush=True); '
              'print(i,file=sys.stderr,flush=True); time.sleep(.1)') if progress else 'import time; time.sleep(2)'
    def process(*args, **kwargs):
        child = popen([sys.executable, '-u', '-c', script], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(subprocess, 'Popen', process)
    monkeypatch.setattr(goal, '_GIT_STALL_SECONDS', .5, raising=False)
    started = time.monotonic()
    with deadline.scope(None):
        if progress:
            expected = b''.join(str(n).encode() + (b'\r\n' if sys.platform == 'win32' else b'\n') for n in range(12))
            assert goal._git(tmp_path, 'status') == expected
            assert time.monotonic() - started > .5
        else:
            with pytest.raises(goal.GoalBlocked, match='git observation stalled.*status'):
                goal._git(tmp_path, 'status')
            assert time.monotonic() - started < 5
    assert children[0].poll() is not None


def test_git_observation_cancellation_reaps_child(tmp_path, monkeypatch):
    import queue
    popen = subprocess.Popen
    children = []
    def process(*args, **kwargs):
        child = popen([sys.executable, '-c', 'import time; time.sleep(10)'], **kwargs)
        children.append(child)
        return child
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(subprocess, 'Popen', process)
    monkeypatch.setattr(queue.Queue, 'get', interrupt)
    with deadline.scope(None), pytest.raises(KeyboardInterrupt):
        goal._git(tmp_path, 'status')
    assert children[0].poll() is not None
