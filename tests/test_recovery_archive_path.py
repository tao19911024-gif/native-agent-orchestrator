import json
import os
from pathlib import Path
import subprocess

import pytest

from foundry_runtime._execution_intent import IntentConflict
from foundry_runtime._execution_intent import ExecutionIntent
from foundry_runtime._recovery_authorization import RecoveryAuthorization


def test_historical_archive_reference_survives_relocation(tmp_path, monkeypatch):
    target = tmp_path / 'archive'
    target.mkdir()
    alias = tmp_path / 'original'
    if os.name == 'nt':
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(target)],
                       check=True, capture_output=True)
    else:
        alias.symlink_to(target, target_is_directory=True)
    decision = alias / 'decision.json'
    decision.write_text(json.dumps({'successor': {'candidate_head': 'a' * 40}}))
    # Isolate path custody from envelope validation (covered by recovery suites).
    monkeypatch.setattr(RecoveryAuthorization, '_validate', lambda *args: None)
    kwargs = dict(task_key='T1', worktree=tmp_path, session_id='s', candidate_head='a' * 40)
    historical = RecoveryAuthorization.load_historical(decision, **kwargs)
    assert historical.path == decision
    assert historical.path != decision.resolve()
    historical.revalidate()
    with pytest.raises(IntentConflict):
        historical.require_live_execution()
    live = RecoveryAuthorization.load(decision, amendment=b'x', **kwargs)
    assert live.path == decision.resolve()
    decision.write_text('{}')
    with pytest.raises(IntentConflict, match='bytes changed'):
        historical.revalidate()
    # Remove only the junction, leaving pytest ownership of the target contents.
    if os.name == 'nt':
        alias.rmdir()
    else:
        alias.unlink()


@pytest.mark.parametrize('exited', [False, True])
def test_consumption_before_start_requires_exact_controller_exit(tmp_path, monkeypatch, exited):
    from foundry_runtime import _executor_lifecycle
    calls = []
    monkeypatch.setattr(ExecutionIntent, 'read_history', staticmethod(
        lambda *args, **kwargs: ({'state': 'not_started'}, 1, 'digest')))
    def observed(pid, created):
        calls.append((pid, created))
        return exited
    monkeypatch.setattr(_executor_lifecycle, 'process_identity_exited', observed)
    successor = tmp_path / ('b' * 64)
    successor.mkdir()
    (successor / '00000001.json').write_text(json.dumps({'kind': 'prepared'}))
    consumption = dict(successor_intent_id='b' * 64, attempt_id='old', owner_pid=123, owner_creation_time=456)
    assert ExecutionIntent.recovery_spent_without_start(tmp_path, consumption, task_key='T') is exited
    assert calls == [(123, 456)]
    (successor / '00000002.json').write_text(json.dumps({'kind': 'start', 'data': {'attempt_id': 'new'}}))
    assert ExecutionIntent.recovery_spent_without_start(tmp_path, consumption, task_key='T') is exited
    consumption['owner_creation_time'] = None
    assert not ExecutionIntent.recovery_spent_without_start(tmp_path, consumption, task_key='T')


def test_started_successor_is_not_unused_consumption(tmp_path, monkeypatch):
    monkeypatch.setattr(ExecutionIntent, 'read_history', staticmethod(
        lambda *args, **kwargs: ({'state': 'starting'}, 2, 'digest')))
    successor = tmp_path / ('b' * 64)
    successor.mkdir()
    (successor / '00000001.json').write_text(json.dumps({'kind': 'prepared'}))
    (successor / '00000002.json').write_text(json.dumps({'kind': 'start', 'data': {'attempt_id': 'old'}}))
    assert not ExecutionIntent.recovery_spent_without_start(
        tmp_path, dict(successor_intent_id='b' * 64, attempt_id='old', owner_pid=123, owner_creation_time=456), task_key='T')
