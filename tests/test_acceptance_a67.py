import json
from pathlib import Path
import sys

import pytest

from test_installed_runtime import external, offline, Analyst, git
from foundry_runtime import foundry_goal as goal, foundry_entry, cli, _foundry_dispatch as dispatch
from foundry_runtime._project_execution import ProjectExecution
from foundry_runtime import _foundry_manager


@pytest.mark.parametrize('managed', [False, True, 'review_only', 'auto_parent'])
def test_failed_closure_recovery_retries_only_review(external, tmp_path, monkeypatch, managed):
    tree, scope, order, launches = external
    original = dispatch.coding_argv
    def coding(*args, **kwargs):
        argv = original(*args, **kwargs)
        argv[-1] += "; import subprocess; Path('candidate.txt').write_text('done'); subprocess.run(['git','add','candidate.txt'],check=True); subprocess.run(['git','commit','-qm','candidate'],check=True); print('session id: fixture-session')"
        return argv
    monkeypatch.setattr(dispatch, 'coding_argv', coding)
    monkeypatch.setattr(goal, 'NativeAnalyst', Analyst)
    monkeypatch.setattr(goal, 'resolve_executable', lambda backend: Path(sys.executable))
    monkeypatch.setattr(_foundry_manager, 'turn', lambda *a, **k: pytest.fail('closure recovery must not call the manager'))
    ready = tmp_path/'environment-fixed'
    monkeypatch.setattr(ProjectExecution, 'candidate_closure_command', lambda *a: [sys.executable, '-c',
        f'from pathlib import Path; raise SystemExit(0 if Path({str(ready)!r}).exists() else 1)'], raising=False)
    root = tmp_path/'goal'
    folder = root/'round-0' if managed == 'auto_parent' else root
    if managed == 'review_only':
        base = git(tree, 'rev-parse', 'HEAD')
        (tree/'candidate.txt').write_text('done')
        git(tree, 'add', '.'); git(tree, 'commit', '-qm', 'candidate')
        first = goal.run_review(order, folder, Path(sys.executable), target_worktree=tree,
            base_commit=base, candidate_commit=git(tree, 'rev-parse', 'HEAD'), project_record=scope.record())
    else:
        first = goal.run_goal(order, folder, Path(sys.executable), coding_launcher='exec', project_record=scope.record())
    assert first['status'] == 'coding_incomplete'
    assert 'without --prompt-file' in first['next_action']
    manifest = json.loads((folder/'manifest.json').read_bytes())
    if managed is True:
        manifest['manager_state'] = {'profile': 'opus', 'model': 'claude-opus-5-5', 'native_dir': str(tmp_path/'manager')}
        manifest['manager_phase'] = 'review_pending'
        goal._write(folder/'manifest.json', manifest)
    first_receipt = first['candidate_closure']['receipt']
    if managed == 'auto_parent':
        parent = {**{k: manifest[k] for k in ('order', 'worktree', 'base_commit')},
                  'mode': 'auto-amend', 'state': 'completed', 'active_round': str(folder),
                  'rounds': [goal.goal_auto.round_record(goal, folder)]}
        goal._write(root/'manifest.json', parent)
        goal.goal_auto.finish(goal, root, parent, first)
    second = goal.recover_goal(order, root)
    assert second['status'] == 'coding_incomplete'
    assert second['candidate_closure']['receipt'] != first_receipt
    ready.touch()
    result = goal.recover_goal(order, root)
    assert result['status'] == 'candidate_verified', result
    assert result['candidate_closure']['receipt'] != second['candidate_closure']['receipt']
    assert len(launches) == (0 if managed == 'review_only' else 1)
    assert len(result['reviews']) == 2
    assert goal._fact(first_receipt['path']) == first_receipt
    saved = (root/'manifest.json').read_bytes()
    assert goal.recover_goal(order, root) == result
    assert (root/'manifest.json').read_bytes() == saved


@pytest.mark.parametrize('linked', [False, True, 'failed_closure'])
def test_standalone_review_amendment_staffing(external, tmp_path, monkeypatch, capsys, linked):
    tree, scope, order, _ = external
    head = git(tree, 'rev-parse', 'HEAD')
    review = tmp_path/'review-only'; review.mkdir()
    manifest = {'order': goal._fact(order), 'worktree': str(tree), 'base_commit': head,
                'delivery_context': {'mode': 'review-only'}, 'project_record': scope.record()}
    goal._write(review/'manifest.json', manifest)
    goal._write(review/'outcome.json', {'candidate_commit': head})
    state = {'profile': 'opus', 'backend': 'claude', 'model': 'claude-opus-5-5', 'effort': 'high',
             'native_dir': str(tmp_path/'manager')}
    team = {'profile': 'adaptive', 'backend': 'codex', 'member_roles': {}}
    unrelated = tmp_path/'goal-unrelated'; unrelated.mkdir()
    goal._write(unrelated/'manifest.json', {**manifest, 'delivery_context': {'mode': 'goal'},
                'manager_state': {**state, 'model': 'unrelated-manager'}, 'native_team': team})
    goal._write(unrelated/'outcome.json', {'candidate_commit': head, 'coding_session': 'another-session'})
    if linked:
        prior = tmp_path/'goal-original'; prior.mkdir()
        goal._write(prior/'manifest.json', {**manifest, 'delivery_context': {'mode': 'goal'},
                    'manager_state': state, 'native_team': team, 'coding_launcher': 'app-server'})
        result = {'candidate_commit': head, 'coding_session': 'fixture-session'}
        if linked == 'failed_closure':
            result.pop('candidate_commit')
            result['candidate_closure'] = {'candidate_commit': head, 'status': 'failed'}
        goal._write(prior/'outcome.json', result)
    monkeypatch.setattr(foundry_entry, '_analyst_executable', lambda *a, **kw: Path(sys.executable))
    observed = []
    def run(order, folder, executable, **kwargs):
        observed.append(goal.prepare_goal(order, folder, executable, **kwargs))
        return {'status': 'project_handoff'}
    monkeypatch.setattr(goal, 'run_goal', run)
    assert cli.main(['--project-config', str(scope.path), '--order', str(order), '--foundry-goal',
        '--amend-from', str(review/'outcome.json'), '--coding-session', 'fixture-session']) == 0
    options = observed[0]['options']
    if linked:
        assert options['manager_state'] == state
        assert options['native_team'] == team
        assert options['execution_decision']['staffing_goal']['path'].endswith('manifest.json')
    else:
        assert options['manager_profile'] == 'opus'
        assert options['task_staffing']['kind'] == 'small'
    rows = [json.loads(line.split(': ', 1)[1]) for line in capsys.readouterr().err.splitlines()
            if line.startswith('FOUNDRY COMPUTE: ')]
    assert rows[0]['manager']['model'] == 'claude-opus-5-5'
    assert rows[0]['team']['profile'] == ('adaptive' if linked else 'none')
