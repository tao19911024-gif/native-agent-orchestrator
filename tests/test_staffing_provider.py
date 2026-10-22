"""Quota observations cannot silently replace the requested execution provider."""
from pathlib import Path
import sys
import pytest

from test_installed_runtime import external, offline
from foundry_runtime import cli, foundry_entry, foundry_goal, _model_catalogue


@pytest.mark.parametrize('kind', ['small', 'cross-module'])
def test_task_kind_keeps_codex_worker_without_quota_routing(external, monkeypatch, kind):
    tree, scope, order, launches = external
    def quota():
        pytest.fail('staffing must not consult quota to select another provider')
    monkeypatch.setattr(_model_catalogue, 'codex_quota', quota)
    monkeypatch.setattr(foundry_entry, '_analyst_executable', lambda *a, **k: Path(sys.executable))
    observed = []
    monkeypatch.setattr(foundry_goal, 'run_goal', lambda *a, **k: observed.append(k) or {'status': 'project_handoff'})
    assert cli.main(['--project-config', str(scope.path), '--order', str(order),
                     '--foundry-goal', '--task-kind', kind]) == 0
    assert observed[0]['coding_launcher'] == 'app-server'
    assert observed[0]['compute_profile'] == 'sol-claude'
    assert observed[0]['manager_profile'] == 'opus'
    assert observed[0]['compute_source'] == 'kind'
    assert not launches
