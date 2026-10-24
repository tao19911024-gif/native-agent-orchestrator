"""Retained goal recovery; real project checks, no provider or business writes."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from foundry_runtime import foundry_goal as goal, _foundry_dispatch as dispatch
from foundry_runtime import _foundry_manager as manager
from foundry_runtime._project_execution import open_project, from_record
from test_installed_runtime import external, git, offline


def checked_project(external, tmp_path):
    tree, scope, order, calls = external
    helper = tmp_path / 'preflight.py'
    helper.write_text('''import json, subprocess, sys
r = json.load(sys.stdin); p = r['preflight_input']; w = r['workspace']
def git(*args):
    return subprocess.check_output(['git', '-C', w, *args], text=True).strip()
sys.exit(0 if git('rev-parse', 'HEAD') == p['expectHeadSha'] and
    (not p['requireCleanWorktree'] or not git('status', '--porcelain')) else 1)
''')
    config = scope.project
    config['execution']['preflight_argv'] = [sys.executable, '-B', str(helper)]
    scope.path.write_text(json.dumps(config))
    inputs = {'expectHeadSha': git(tree, 'rev-parse', 'HEAD'), 'requireCleanWorktree': True}
    return open_project(scope.path, preflight_input=inputs)


def retained_goal(external, scope, tmp_path):
    tree, _, order, _ = external
    root = tmp_path / 'goal'; folder = root / 'round-0'; folder.mkdir(parents=True)
    state = {'profile': 'opus', 'backend': 'claude', 'model': 'recorded-opus',
             'effort': 'high', 'native_dir': str(root / 'manager'), 'last_call_id': 'prior-call'}
    options = {'project_record': scope.record(), 'coding_launcher': 'exec',
               'coding_model': 'recorded-codex', 'coding_effort': 'high'}
    manifest = {'schema_version': 1, 'run_id': 'original-goal', 'order': goal._fact(order),
        'worktree': str(tree), 'base_commit': git(tree, 'rev-parse', 'HEAD'),
        'tag': 'TASK1', 'project_record': scope.record(), 'manager_state': state,
        'manager_phase': 'delegation_ready', 'execution_decision': {'manager': {'instruction': 'old'}},
        'manager_packet': {'stage': 'worker_returned', 'returncode': 0, 'coding_session': 'original-session'},
        'delivery_context': {}}
    checkpoint = {**{key: manifest[key] for key in ('order', 'run_id', 'worktree', 'base_commit')},
        'schema_version': 1, 'returncode': 0, 'coding_session': 'original-session', 'evidence': [],
        'coding_usage': {}, 'options': options, 'executable': sys.executable}
    goal._write(folder / 'coding-checkpoint.json', checkpoint)
    manifest['coding_checkpoint'] = goal._fact(folder / 'coding-checkpoint.json')
    goal._write(folder / 'manifest.json', manifest)
    goal._write(root / 'manifest.json', {'mode': 'auto-amend', 'state': 'interrupted',
        'error': 'project preflight failed', 'order': manifest['order'], 'active_round': str(folder),
        'project_record': scope.record(), 'rounds': []})
    return root, folder, manifest


def test_distinct_admission_and_resume_inputs(external, tmp_path):
    scope = checked_project(external, tmp_path)
    tree = external[0]
    scope.preflight('TASK1', 'initial', operation='dispatch', env=os.environ.copy())
    (tree / 'own.txt').write_text('task change')
    git(tree, 'add', 'own.txt'); git(tree, 'commit', '-qm', 'reviewed own change')
    new = {'expectHeadSha': git(tree, 'rev-parse', 'HEAD'), 'requireCleanWorktree': True}
    resumed = from_record({**scope.record(), 'resume_preflight_input': new})
    resumed.preflight('TASK1', 'resume', operation='resume', env=os.environ.copy())
    with pytest.raises(ValueError, match='preflight did not pass'):
        resumed.preflight('TASK1', 'initial', operation='dispatch', env=os.environ.copy())
    (tree / 'foreign.txt').write_text('unreviewed')
    with pytest.raises(ValueError, match='preflight did not pass'):
        resumed.preflight('TASK1', 'resume', operation='resume', env=os.environ.copy())
    assert resumed.record()['preflight_input'] == scope.preflight_input


def test_real_dispatcher_retries_refused_resume_after_owner_commit(external, tmp_path, monkeypatch):
    from foundry_runtime import _native_session
    scope = checked_project(external, tmp_path)
    tree, _, order, launches = external
    assert dispatch.dispatch_order(order, project=scope)[0] == 0
    monkeypatch.setattr(_native_session, 'read_session_worktree', lambda sid: tree)
    (tree / 'handoff.md').write_text('reviewed')
    with pytest.raises(ValueError, match='preflight did not pass'):
        dispatch.resume_order('original-session', tree, 'TASK1', 'continue', project=scope)
    assert len(launches) == 1
    git(tree, 'add', 'handoff.md'); git(tree, 'commit', '-qm', 'owner accepts handoff')
    current = from_record({**scope.record(), 'resume_preflight_input': {
        'expectHeadSha': git(tree, 'rev-parse', 'HEAD'), 'requireCleanWorktree': True}})
    result = dispatch.resume_order('original-session', tree, 'TASK1', 'continue', project=current)
    assert result[0] == 0
    assert dispatch.resume_order('original-session', tree, 'TASK1', 'continue', project=current) == result
    assert len(launches) == 2


def test_cli_forwards_explicit_recovery_input_only(external, tmp_path, monkeypatch):
    from foundry_runtime import cli
    scope = checked_project(external, tmp_path)
    root, _, _ = retained_goal(external, scope, tmp_path)
    received = []
    def recover(*args, **kwargs):
        received.append(kwargs)
        return {'status': 'candidate_verified'}
    monkeypatch.setattr(goal, 'recover_goal', recover)
    base = ['--project-config', str(scope.path), '--order', str(external[2]),
            '--foundry-goal', '--recover-goal', str(root)]
    assert cli.main(base) == 0
    assert 'project_preflight_input' not in received[-1]
    inputs = tmp_path / 'input.json'; inputs.write_text(json.dumps({'expectHeadSha': 'reviewed', 'requireCleanWorktree': True}))
    for option in ('--project-preflight-input', '--project-resume-preflight-input'):
        assert cli.main([*base, option, str(inputs)]) == 0
        assert received[-1]['project_preflight_input']['expectHeadSha'] == 'reviewed'


@pytest.mark.parametrize('with_outcome', [True, False])
def test_existing_awaiting_input_answer_keeps_coding_continuation(external, tmp_path, monkeypatch, with_outcome):
    scope = checked_project(external, tmp_path)
    root, folder, manifest = retained_goal(external, scope, tmp_path)
    checkpoint = json.loads((folder / 'coding-checkpoint.json').read_bytes())
    checkpoint['returncode'] = 1
    goal._write(folder / 'coding-checkpoint.json', checkpoint)
    manifest.update(manager_phase='awaiting_input', coding_checkpoint=goal._fact(folder / 'coding-checkpoint.json'))
    goal._write(folder / 'manifest.json', manifest)
    if with_outcome:
        goal._write(folder / 'outcome.json', {'status': 'awaiting_input'})
    called = []
    def continue_coding(owner, order, evidence, retained, answer, **kwargs):
        called.append(answer)
        return {'status': 'awaiting_input', 'continuation_evidence': str(tmp_path / 'continue-original')}
    monkeypatch.setattr(goal.goal_continuation, 'continue_coding', continue_coding)
    monkeypatch.setattr(goal, '_recover_manager_feedback', lambda *a, **k: pytest.fail('answer swallowed by old outcome'))
    result = goal.recover_goal(external[2], root, answer='operator response')
    assert called == ['operator response']
    assert result['continuation_evidence'].endswith('continue-original')


@pytest.mark.parametrize('foreign', [False, True])
def test_interrupted_goal_rc0_accepts_correction_and_new_head(external, tmp_path, monkeypatch, foreign):
    scope = checked_project(external, tmp_path)
    tree, _, order, _ = external
    root, folder, original = retained_goal(external, scope, tmp_path)
    old_checkpoint = (folder / 'coding-checkpoint.json').read_bytes()
    (tree / 'handoff.md').write_text('reviewed task handoff')
    git(tree, 'add', 'handoff.md'); git(tree, 'commit', '-qm', 'reviewed handoff')
    inputs = {'expectHeadSha': git(tree, 'rev-parse', 'HEAD'), 'requireCleanWorktree': True}
    if foreign:
        (tree / 'foreign.txt').write_text('unreviewed')
    packets, launches = [], []
    def turn(state, workspace, packet, **kwargs):
        if kwargs.get('pending_only'):
            return None
        assert state['native_dir'] == original['manager_state']['native_dir']
        assert state['model'] == 'recorded-opus'
        packets.append(packet)
        action = 'continue' if len(packets) == 1 else 'deliver_artifacts'
        return {'action': action, 'instruction': 'corrected premise'}, state, {}, packet
    monkeypatch.setattr(manager, 'turn', turn)
    def resume(session, workspace, tag, text, **kwargs):
        assert session == 'original-session'
        assert kwargs['model'] == 'recorded-codex'
        assert kwargs['project'].record()['preflight_input'] == scope.preflight_input
        try:
            kwargs['project'].preflight(tag, text, operation='resume', env=os.environ.copy())
        except ValueError as exc:
            from foundry_runtime._dispatch_package import DispatchNotStarted
            exc.dispatch_stop = DispatchNotStarted('fixture preflight refusal',
                SimpleNamespace(path=tmp_path, frozen={'session_id': session}))
            raise
        launches.append(session)
        log = tmp_path / 'worker.log'; log.write_text('completed')
        log.with_suffix('.cleanup.json').write_text('{}')
        out = tmp_path / 'worker.txt'; out.write_text('handoff')
        return 0, log, out
    monkeypatch.setattr(dispatch, 'resume_order', resume)
    monkeypatch.setattr(dispatch, 'dispatch_order', lambda *a, **k: pytest.fail('duplicate dispatch'))
    monkeypatch.setattr(goal, 'artifacts', lambda *a: {'status': 'artifacts_delivered', 'coding_session': 'original-session'})
    monkeypatch.setattr(goal, '_record_usage', lambda *a, **k: None)
    if foreign:
        with pytest.raises(ValueError, match='preflight did not pass'):
            goal.recover_goal(order, root, answer='new business ruling', project_preflight_input=inputs)
        assert not launches
        assert (folder / 'coding-checkpoint.json').read_bytes() == old_checkpoint
        current = json.loads((folder / 'manifest.json').read_bytes())
        assert current['manager_phase'] == 'delegation_ready'
        assert current['project_record']['resume_preflight_input'] == inputs
        # The next invocation uses the retained correction and inputs; it must
        # not ask the manager again before retrying the not-started delegation.
        (tree / 'foreign.txt').unlink()
        assert goal.recover_goal(order, root)['status'] == 'artifacts_delivered'
        assert launches == ['original-session']
    else:
        result = goal.recover_goal(order, root, answer='new business ruling', project_preflight_input=inputs)
        assert result['status'] == 'artifacts_delivered'
        assert launches == ['original-session']
        assert json.loads((root / 'manifest.json').read_bytes())['state'] == 'completed'
        saved = json.loads((folder / 'coding-checkpoint.json').read_bytes())
        assert saved['options']['project_record']['resume_preflight_input'] == inputs
        assert saved['run_id'] == 'original-goal'
    assert packets[0]['operator_answer'] == 'new business ruling'
    assert original['project_record'] == scope.record()
