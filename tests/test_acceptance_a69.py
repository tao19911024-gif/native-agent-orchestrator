"""Legacy missing coordination uses the existing kind and manager lifecycle."""
import json
from pathlib import Path
import sys

import pytest

from test_installed_runtime import external, offline, git
from test_settled_failure_recovery import failed_goal
from foundry_runtime import cli, foundry_entry, foundry_goal as goal
from foundry_runtime import _foundry_manager as manager, _model_catalogue as catalogue


@pytest.mark.parametrize('kind', [None, 'small', 'cross-module', 'ui-live'])
@pytest.mark.parametrize('launcher', ['exec', 'claude'])
def test_managerless_amendment_retains_worker_choices(external, tmp_path, monkeypatch, capsys, kind, launcher):
    tree, scope, order, _ = external
    prior = tmp_path/'prior'; prior.mkdir()
    head = git(tree, 'rev-parse', 'HEAD')
    team = {'profile': 'adaptive', 'backend': 'claude' if launcher == 'claude' else 'codex', 'member_roles': {}}
    manifest = {'order': goal._fact(order), 'worktree': str(tree), 'base_commit': head,
        'delivery_context': {'mode': 'goal'}, 'project_record': scope.record(), 'manager_state': None,
        'coding_launcher': launcher, 'native_team': team,
        'requested_compute': {'coding': {'model': 'retained-worker', 'effort': 'high'},
                              'review': {'backend': 'codex', 'model': 'retained-reviewer', 'effort': 'high'}}}
    if kind:
        manifest['task_staffing'] = {'kind': kind, 'sources': {k: 'recorded' for k in
            ('manager', 'coding', 'correction', 'reviewers', 'team')}, 'manager': {'profile': None}}
    goal._write(prior/'manifest.json', manifest)
    goal._write(prior/'outcome.json', {'candidate_commit': head, 'coding_session': 'retained-session'})
    before = (prior/'manifest.json').read_bytes()
    observed = []
    monkeypatch.setattr(foundry_entry, '_analyst_executable', lambda *a: Path(sys.executable))
    def run(order, folder, executable, **kwargs):
        prepared = goal.prepare_goal(order, folder, executable, **kwargs)
        observed.append(prepared['options'])
        return {'status': 'project_handoff'}
    monkeypatch.setattr(goal, 'run_goal', run)
    assert cli.main(['--project-config', str(scope.path), '--order', str(order), '--foundry-goal',
        '--amend-from', str(prior/'outcome.json'), '--coding-session', 'retained-session']) == 0
    options = observed[0]
    expected = None if kind == 'ui-live' else 'opus'
    assert options.get('manager_profile') == expected
    assert options['coding_model'] == 'retained-worker'
    assert options['analyst_model'] == 'retained-reviewer'
    assert options['native_team'] == team
    assert options['coding_session'] == 'retained-session'
    assert (prior/'manifest.json').read_bytes() == before
    row = next(json.loads(line.split(': ', 1)[1]) for line in capsys.readouterr().err.splitlines()
               if line.startswith('FOUNDRY COMPUTE: '))
    assert row['manager_profile'] == expected
    if expected:
        assert row['manager']['model'] == catalogue.model('opus')
        state = manager.configuration_for(options, tmp_path/'next')
        assert state['model'] == catalogue.model('opus')
        assert state.get('quota_fallback_policy') == ('codex' if launcher == 'exec' else None)
        if kind:
            assert options['task_staffing']['manager']['profile'] == 'opus'


@pytest.mark.parametrize('recorded', [False, True])
@pytest.mark.parametrize('parent', [False, True])
@pytest.mark.parametrize('kind', ['small', 'ui-live'])
def test_cli_recovery_supplements_only_new_missing_manager(failed_goal, tmp_path, monkeypatch, recorded, parent, kind):
    tree, scope, order, folder, calls = failed_goal
    manifest = json.loads((folder/'manifest.json').read_bytes())
    manifest['task_staffing'] = {'kind': kind}
    state = {'profile': 'astra', 'backend': 'codex', 'model': 'retained-astra',
             'native_dir': str(tmp_path/'original-manager'), 'quota_fallback': {'receipt': 'retained'}}
    if recorded:
        manifest['manager_state'] = state
        checkpoint = json.loads((folder/'coding-checkpoint.json').read_bytes())
        checkpoint['options']['manager_state'] = state
        goal._write(folder/'coding-checkpoint.json', checkpoint)
        manifest['coding_checkpoint'] = goal._fact(folder/'coding-checkpoint.json')
    goal._write(folder/'manifest.json', manifest)
    before = {p:p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    observed = []
    monkeypatch.setattr(goal.goal_continuation, 'fresh_executables', lambda options: (sys.executable, options))
    def run(order, target, executable, **options):
        observed.append(manager.configuration_for(options, target))
        target.mkdir(exist_ok=True)
        outcome = {'status': 'project_handoff'}
        goal._write(target/'outcome.json', outcome)
        return outcome
    monkeypatch.setattr(goal, '_run', run)
    selected = folder
    if parent:
        selected = folder.parent
        goal._write(selected/'manifest.json', {'mode': 'auto-amend', 'state': 'completed',
            'active_round': str(folder), 'order': manifest['order'], 'project_record': scope.record()})
    answer = tmp_path/'answer.txt'; answer.write_text('Continue retained work.', encoding='utf-8')
    assert cli.main(['--project-config', str(scope.path), '--order', str(order), '--foundry-goal',
        '--recover-goal', str(selected), '--prompt-file', str(answer)]) == 0
    if recorded:
        assert observed[0] == state
    elif kind == 'ui-live':
        assert observed[0] is None
    else:
        assert observed[0]['profile'] == 'opus'
        assert observed[0]['quota_fallback_policy'] == 'codex'
    assert all(p.read_bytes() == raw for p,raw in before.items())
    assert len(calls) == 1


def test_interrupted_continuation_admission_also_restores_manager(failed_goal, tmp_path, monkeypatch):
    _, scope, order, folder, _ = failed_goal
    observed = []
    monkeypatch.setattr(goal.goal_continuation, 'fresh_executables', lambda options: (sys.executable, options))
    def run(order, target, executable, **options):
        observed.append(manager.configuration_for(options, target))
        target.mkdir(exist_ok=True)
        # Legacy continuation admission failed before manager/worker startup.
        frozen = {k:v for k,v in options.items() if k not in
                  ('target_worktree', 'coding_session', 'continuation', 'manager_profile', 'retained')}
        goal._write(target/'manifest.json', {'continuation': options['continuation'],
            'run_id': 'admission-fixture', 'base_commit': options['continuation']['base_commit'],
            'continuation_start': {'executable': sys.executable, 'options': frozen}})
        return {'status': 'coding_incomplete'}
    monkeypatch.setattr(goal, '_run', run)
    answer = tmp_path/'answer.txt'; answer.write_text('Continue retained work.')
    argv = ['--project-config', str(scope.path), '--order', str(order), '--foundry-goal',
        '--recover-goal', str(folder), '--prompt-file', str(answer)]
    assert cli.main(argv) == 2
    assert cli.main(argv) == 2
    assert [s['profile'] for s in observed] == ['opus', 'opus']


def test_manager_choice_remains_dynamic(monkeypatch, tmp_path):
    from foundry_runtime._compute_selection import missing_manager_profile
    original = manager.model
    monkeypatch.setattr(manager, 'model', lambda tier: 'future-opus' if tier == 'opus' else original(tier))
    profile = missing_manager_profile({})
    assert manager.configuration_for({'manager_profile': profile}, tmp_path)['model'] == 'future-opus'
