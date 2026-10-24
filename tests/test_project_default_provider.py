"""Project coding defaults resolve only for new goals, before task-kind defaults."""
import json
from pathlib import Path
import sys

import pytest

from foundry_runtime import cli, foundry_entry, foundry_goal as goal
from foundry_runtime._project_execution import open_project
from test_installed_runtime import external, offline


@pytest.mark.parametrize('default,flags,kind,backend,source', [
    ('claude', [], 'cross-module', 'claude', 'project'),
    ('claude', ['--provider', 'codex'], 'cross-module', 'codex', 'explicit'),
    ('codex', [], 'ui-live', 'codex', 'project'),
    (None, [], 'cross-module', 'codex', 'kind'),
    (None, [], 'ui-live', 'claude', 'kind'),
    ('claude', ['--compute-profile', 'sol-astra'], 'cross-module', 'codex', 'explicit'),
])
def test_new_goal_staffing_precedence(external, capsys, default, flags, kind, backend, source):
    _, scope, order, _ = external
    descriptor = dict(scope.project)
    if default:
        descriptor['default_provider'] = default
    scope.path.write_text(json.dumps(descriptor))
    assert cli.main(['--project-config', str(scope.path), '--order', str(order), '--foundry-goal',
                     '--task-kind', kind, '--print-staffing', *flags]) == 0
    row = next(line for line in capsys.readouterr().err.splitlines() if line.startswith('FOUNDRY COMPUTE: '))
    selected = json.loads(row.removeprefix('FOUNDRY COMPUTE: '))
    for role in ('coding', 'correction'):
        assert selected['roles'][role]['backend'] == backend
        assert selected['roles'][role]['source'] == source
    assert selected['team']['backend'] == backend
    assert selected['team']['profile'] == 'adaptive'
    if source == 'project':
        assert selected['team_source_detail']['backend'] == 'project'


def test_recorded_codex_recovery_ignores_claude_default(external, tmp_path, monkeypatch):
    from test_goal_preflight_recovery import retained_goal
    _, scope, order, _ = external
    descriptor = {**scope.project, 'default_provider': 'claude'}
    scope.path.write_text(json.dumps(descriptor))
    scope = open_project(scope.path)
    root, folder, manifest = retained_goal(external, scope, tmp_path)
    answer = tmp_path / 'answer.txt'; answer.write_text('Keep the original worker')
    calls = []
    def run(*args, **options):
        calls.append(options)
        outcome = {'status': 'project_handoff'}
        goal._write(Path(args[1]) / 'outcome.json', outcome)
        return outcome
    monkeypatch.setattr(goal.goal_execution, 'run_owned', run)
    assert cli.main(['--project-config', str(scope.path), '--order', str(order), '--foundry-goal',
                     '--recover-goal', str(root), '--prompt-file', str(answer)]) == 0
    assert len(calls) == 1
    assert calls[0]['coding_launcher'] == 'exec'
    assert calls[0]['coding_model'] == 'recorded-codex'
    assert calls[0]['coding_session'] == 'original-session'
    assert calls[0]['manager_answer'] == answer.read_text()


def test_project_source_is_retained_in_prepared_goal(external, monkeypatch):
    _, scope, order, _ = external
    scope.path.write_text(json.dumps({**scope.project, 'default_provider': 'claude'}))
    monkeypatch.setattr(foundry_entry, '_analyst_executable', lambda _: Path(sys.executable))
    prepared = []
    monkeypatch.setattr(goal, 'run_prepared', lambda order, folder, value:
                        prepared.append(value) or {'status': 'project_handoff'})
    assert cli.main(['--project-config', str(scope.path), '--order', str(order), '--foundry-goal',
                     '--task-kind', 'cross-module']) == 0
    value, = prepared
    options = value['options']
    assert options['coding_launcher'] == 'claude'
    assert options['compute_assignment']['selection']['source'] == 'project'
    for role in ('coding', 'correction'):
        assert options['compute_assignment']['roles'][role]['source'] == 'project'
        assert options['compute_assignment']['roles'][role]['backend'] == 'claude'
    assert value['correction']['model'].startswith('claude-')
    assert options['native_team']['backend'] == 'claude'
