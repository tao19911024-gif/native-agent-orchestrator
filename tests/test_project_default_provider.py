"""Project coding defaults resolve only for new goals, before task-kind defaults."""
import json
from pathlib import Path
import sys

import pytest

from foundry_runtime import cli, foundry_entry, foundry_goal as goal
from foundry_runtime._project_execution import open_project
from test_installed_runtime import external, offline


def adapter_for(scope, tmp_path, **defaults):
    from types import SimpleNamespace
    return SimpleNamespace(catalogue_cache=scope.catalogue_cache,
        configure_cli=lambda *a: None, validate_cli=lambda *a: None, announce=lambda *a: None,
        record_foundry_location=lambda *a: None, goal_workspace=lambda *a: scope.workspace,
        goal_evidence=lambda *a: tmp_path / 'adapter-goal', goal_admission=lambda *a: True,
        **defaults)


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


@pytest.mark.parametrize('defaults,flags,backend,source', [
    ({'default_provider': 'claude'}, [], 'claude', 'project'),
    ({'default_provider': 'claude'}, ['--provider', 'codex'], 'codex', 'explicit'),
    ({'default_provider': 'claude'}, ['--launcher', 'app-server'], 'codex', 'explicit'),
    ({'default_provider': 'claude'}, ['--compute-profile', 'sol-astra'], 'codex', 'explicit'),
    ({}, [], 'codex', 'kind'),
])
def test_adapter_goal_staffing_uses_project_default(external, tmp_path, capsys, defaults, flags, backend, source):
    _, scope, order, _ = external
    adapter = adapter_for(scope, tmp_path, **defaults)
    assert cli.main(['--order', str(order), '--foundry-goal', '--task-kind', 'cross-module',
                     '--print-staffing', *flags], project_adapter=adapter) == 0
    row = next(line for line in capsys.readouterr().err.splitlines() if line.startswith('FOUNDRY COMPUTE: '))
    selected = json.loads(row.removeprefix('FOUNDRY COMPUTE: '))
    assert selected['roles']['coding']['backend'] == backend
    assert selected['roles']['coding']['source'] == source


@pytest.mark.parametrize('adapter_mode', [False, True])
@pytest.mark.parametrize('default,flags,enabled', [(True, [], True), (False, [], False),
                                                 (False, ['--disposable-scratch'], True)])
def test_new_goal_inherits_project_disposable_scratch(external, tmp_path, monkeypatch,
                                                     adapter_mode, default, flags, enabled):
    _, scope, order, _ = external
    monkeypatch.setattr(foundry_entry, '_analyst_executable', lambda _: Path(sys.executable))
    calls = []
    monkeypatch.setattr(goal, 'run_goal', lambda *a, **kw: calls.append(kw) or {'status': 'project_handoff'})
    adapter = adapter_for(scope, tmp_path, default_disposable_scratch=default) if adapter_mode else None
    if not adapter_mode:
        scope.path.write_text(json.dumps({**scope.project, 'default_disposable_scratch': default}))
    args = ([] if adapter_mode else ['--project-config', str(scope.path)])
    assert cli.main([*args, '--order', str(order), '--foundry-goal', *flags], project_adapter=adapter) == 0
    assert bool(calls[0].get('disposable_scratch')) is enabled


def test_project_scratch_default_does_not_change_recorded_recovery(external, tmp_path, monkeypatch):
    from test_goal_preflight_recovery import retained_goal
    _, scope, order, _ = external
    scope.path.write_text(json.dumps({**scope.project, 'default_provider': 'claude',
                                      'default_disposable_scratch': True}))
    selected = open_project(scope.path)
    root, folder, manifest = retained_goal(external, selected, tmp_path)
    calls = []
    monkeypatch.setattr(goal, 'recover_goal', lambda *a, **kw: calls.append(kw) or {'status': 'project_handoff'})
    assert cli.main(['--project-config', str(scope.path), '--order', str(order), '--foundry-goal',
                     '--recover-goal', str(root)]) == 0
    assert 'disposable_scratch' not in calls[0]
    assert 'disposable_scratch' not in json.loads((folder / 'manifest.json').read_bytes())


def test_descriptor_scratch_default_is_boolean(external):
    _, scope, _, _ = external
    scope.path.write_text(json.dumps({**scope.project, 'default_disposable_scratch': 'false'}))
    with pytest.raises(ValueError, match='default_disposable_scratch must be a boolean'):
        open_project(scope.path)


def test_adapter_defaults_do_not_enter_runtime_boundary_continuation(external, tmp_path, monkeypatch):
    _, scope, order, _ = external
    adapter = adapter_for(scope, tmp_path, default_provider='claude', default_disposable_scratch=True)
    adapter.configure_cli = lambda parser, argv: parser.set_defaults(_boundary_continuation={'retained': True})
    monkeypatch.setattr(foundry_entry, '_analyst_executable', lambda _: Path(sys.executable))
    calls = []
    monkeypatch.setattr(goal, 'run_goal', lambda *a, **kw: calls.append(kw) or {'status': 'project_handoff'})
    assert cli.main(['--order', str(order), '--foundry-goal'], project_adapter=adapter) == 0
    assert calls[0]['coding_launcher'] == 'app-server'
    assert 'disposable_scratch' not in calls[0]
