"""Project handoff is successful execution, not review or project acceptance."""
import json
from pathlib import Path
import sys

import pytest

from test_installed_runtime import external, offline
from test_goal_preflight_recovery import retained_goal
from foundry_runtime import cli, foundry_goal as goal, _foundry_dispatch as dispatch
from foundry_runtime._foundry_cli_help import recovery_advice


@pytest.mark.parametrize('dirty', [True, False])
def test_goal_distinguishes_project_handoff_from_missing_candidate(external, tmp_path, monkeypatch, dirty):
    tree, scope, order, launches = external
    original = dispatch.coding_argv
    def coding(*args, **kwargs):
        argv = original(*args, **kwargs)
        if dirty:
            argv[-1] += "; Path('candidate.txt').write_text('handoff'); print('session id: fixture-session')"
        return argv
    monkeypatch.setattr(dispatch, 'coding_argv', coding)
    folder = tmp_path / 'goal'
    result = goal.run_goal(order, folder, Path(sys.executable), coding_launcher='exec', project_record=scope.record())
    assert result['status'] == ('project_handoff' if dirty else 'coding_incomplete')
    assert result['project_acceptance']['status'] == 'not_evaluated'
    assert result['reviews'] == []
    if dirty:
        assert goal.recover_goal(order, folder)['status'] == result['status']
    else:
        with pytest.raises(goal.GoalBlocked):
            goal.recover_goal(order, folder)
    assert len(launches) == 1


def test_cli_handoff_success_and_refusal_diagnostics(external, tmp_path, monkeypatch, capsys):
    scope = external[1]
    root, folder, manifest = retained_goal(external, scope, tmp_path)
    native = Path(manifest['manager_state']['native_dir']); native.mkdir()
    (native / 'native-manifest.json').write_text(json.dumps({'runtime_provenance': {'python': 'D:/原运行时/python.exe'}}))
    (native / 'cancellation.json').write_text(json.dumps({'observed_utc': '2026-10-06T06:09:30Z', 'reason': 'historical limit'}))
    call = native / 'call-old'; call.mkdir()
    (call / 'receipt.json').write_text(json.dumps({'call_id': 'old', 'outcome': 'provider_failure', 'ended_utc': '2026-10-06T06:09:31Z'}))
    args = ['--project-config', str(scope.path), '--order', str(external[2]), '--foundry-goal', '--recover-goal', str(root)]
    # The fake missing interpreter now fails before the goal owner is called.
    assert cli.main(args) == 2
    assert 'recorded native interpreter is missing' in capsys.readouterr().err
    # The remainder tests downstream outcome/diagnostic rendering in isolation;
    # complete native preflight has real receipt coverage in its own tests.
    from foundry_runtime import _foundry_recovery_preflight
    monkeypatch.setattr(_foundry_recovery_preflight, 'prepare', lambda _, **kw: None)
    monkeypatch.setattr(goal, 'recover_goal', lambda *a, **kw: {'status': 'project_handoff', 'project_acceptance': {'status': 'not_evaluated'}})
    assert cli.main(args) == 0
    def refused(*args, **kwargs):
        raise ValueError('persisted historical limit')
    monkeypatch.setattr(goal, 'recover_goal', refused)
    before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert cli.main(args) == 2
    error = capsys.readouterr().err
    assert 'historical, not a live quota' in error and '06:09:30Z' in error
    advice = recovery_advice(external[2], root)
    assert advice['recovery_argv'][0] == 'D:/原运行时/python.exe'
    assert '--project-config' in advice['recovery_argv']
    assert advice['historical_failed_calls'][0]['call_id'] == 'old'
    assert str(folder) == advice['evidence']
    assert before == {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    (folder / 'manager-usage.json').write_text(json.dumps({'calls': [{'state': 'unknown', 'call_id': 'old'}]}))
    from foundry_runtime._followup import _goal_status
    observed = _goal_status(root)['native_reconciliation']['invocation']['argv']
    assert observed[0] == 'D:/原运行时/python.exe'
    assert observed[observed.index('--order') + 1] == str(external[2])


def test_missing_legacy_entry_keeps_historical_recovery_advice(tmp_path):
    folder = tmp_path / 'goal'; folder.mkdir()
    (folder / 'manifest.json').write_text(json.dumps({'runtime_provenance': {}}))
    result = recovery_advice(tmp_path / 'order.md', folder)
    assert result['recovery_argv'] == []
    assert result['recovery_invocation']['status'] == 'unavailable'
    assert 'runtime_provenance.python' in result['recovery_invocation']['reason']
    assert result['historical_cancellations'] == [] and result['historical_failed_calls'] == []
