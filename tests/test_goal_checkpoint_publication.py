"""Normal worker publication retains the prior checkpoint across interruption."""
import json
from pathlib import Path
import sys

import pytest

from test_installed_runtime import external, offline
from test_manager_input_wait import manager_io
from test_owned_goal_recovery import SERVER, SID
from foundry_runtime import foundry_goal as goal, app_server_transport as transport


def test_second_worker_checkpoint_publication_recovers_without_replaying_worker(
        external, manager_io, tmp_path, monkeypatch):
    _, scope, order, _ = external
    requests, decisions = manager_io
    decisions.extend([{'action': 'delegate', 'instruction': 'First work', 'artifacts': []},
                      {'action': 'delegate', 'instruction': 'Second work', 'artifacts': []},
                      {'action': 'deliver_artifacts', 'instruction': 'Deliver recovered work',
                       'artifacts': ['candidate.txt']}])
    monkeypatch.setattr(goal, '_session_from_log', lambda _: SID)
    run = transport.run_executor
    launches = []
    def execute(*args, **kwargs):
        launches.append(1)
        def spawn(argv, **opts):
            return transport.OwnedProcess.spawn(
                [sys.executable, '-B', '-c', 'HANG=False\n' + SERVER], **opts)
        return run(*args, process_factory=spawn, **kwargs)
    monkeypatch.setattr(transport, 'run_executor', execute)
    folder = tmp_path / 'goal'
    write = goal._write
    checkpoints = []
    def interrupt(path, value):
        write(path, value)
        if Path(path) == folder / 'coding-checkpoint.json':
            checkpoints.append(goal._fact(path))
            if len(checkpoints) == 2:
                raise RuntimeError('fixture interrupted checkpoint publication')
    with monkeypatch.context() as crash:
        crash.setattr(goal, '_write', interrupt)
        with pytest.raises(RuntimeError, match='fixture interrupted checkpoint publication'):
            goal.run_goal(order, folder, Path(sys.executable), coding_launcher='app-server',
                project_record=scope.record(), manager_state={'backend': 'codex', 'model': None,
                    'effort': None, 'native_dir': str(tmp_path / 'manager'), 'artifact_delivery': True})
    saved = json.loads((folder / 'manifest.json').read_bytes())
    assert saved['manager_phase'] == 'dispatch_pending'
    assert saved['coding_checkpoint'] == checkpoints[0]
    assert saved['coding_checkpoint'] != goal._fact(folder / 'coding-checkpoint.json')
    assert len(launches) == 2
    monkeypatch.setattr(transport, 'run_executor', lambda *a, **k: pytest.fail('worker replayed'))
    result = goal.recover_goal(order, folder, answer='Return both completed worker turns to the manager')
    assert result['status'] == 'artifacts_delivered'
    assert len(requests) == 3
    assert requests[-1]['packet']['stage'] == 'worker_returned'
    checkpoint = json.loads((folder / 'coding-checkpoint.json').read_bytes())
    assert len(checkpoint['coding_usage']['managed_turns']) == 2
    assert goal.recover_goal(order, folder) == result
