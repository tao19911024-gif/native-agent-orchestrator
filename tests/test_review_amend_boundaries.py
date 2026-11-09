"""Shared review workspace and retained amendment inputs use their owning boundary."""
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from test_installed_runtime import external, offline, git, Analyst
from foundry_runtime import cli, foundry_entry, foundry_goal as goal
from foundry_runtime._project_execution import open_project


@pytest.fixture(autouse=True)
def no_native_models(monkeypatch):
    from foundry_runtime import _foundry_manager as manager
    def forbidden(*args, **kwargs):
        pytest.fail('offline boundary regression attempted a native model call')
    monkeypatch.setattr(manager, 'NativeAnalyst', forbidden)
    monkeypatch.setattr(goal, 'NativeAnalyst', forbidden)


def candidate(external):
    tree, scope, order, _ = external
    base = git(tree, 'rev-parse', 'HEAD')
    (tree / 'candidate.txt').write_text('candidate')
    git(tree, 'add', '.'); git(tree, 'commit', '-qm', 'candidate')
    return tree, scope, order, base, git(tree, 'rev-parse', 'HEAD')


@pytest.mark.parametrize('residue', [None, 'untracked', 'tracked', 'staged', 'head'])
def test_review_pair_owns_final_workspace_validation(external, tmp_path, monkeypatch, residue):
    tree, scope, order, base, head = candidate(external)
    probe = tree / 'review-probe.txt'
    created, validated = threading.Event(), threading.Event()
    calls, cancellations = [], []
    original = goal._validate_review
    def validate(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            validated.set()
    monkeypatch.setattr(goal, '_validate_review', validate)
    class Reader(Analyst):
        def __init__(self, executable, evidence, **kwargs):
            super().__init__()
            self.slot = evidence.parent.name
        def __call__(self, request):
            calls.append(self.slot)
            if self.slot == 'review-0':
                probe.write_text('temporary independent reproduction')
                created.set()
                assert validated.wait(15), 'other reviewer did not return'
                if residue != 'untracked':
                    probe.unlink()
                if residue in ('tracked', 'staged', 'head'):
                    (tree / 'candidate.txt').write_text('changed')
                if residue in ('staged', 'head'):
                    git(tree, 'add', 'candidate.txt')
                if residue == 'head':
                    git(tree, 'commit', '-qm', 'changed head')
            else:
                assert created.wait(15), 'probe was not created'
                assert probe.exists()
            return super().__call__(request)
        def cancel(self, reason):
            cancellations.append(reason)
    monkeypatch.setattr(goal, 'NativeAnalyst', Reader)
    folder = tmp_path / 'review'
    kwargs = dict(target_worktree=tree, base_commit=base, candidate_commit=head,
                  project_record=scope.record())
    if residue:
        with pytest.raises(goal.GoalBlocked, match='candidate.*(dirty|changed)'):
            goal.run_review(order, folder, Path(sys.executable), **kwargs)
        outcome = json.loads((folder / 'outcome.json').read_bytes())
        assert outcome['status'] == 'review_blocked'
        assert len(outcome['reviews']) == 2
        if residue != 'head':
            assert outcome['workspace']['status_porcelain']
    else:
        result = goal.run_review(order, folder, Path(sys.executable), **kwargs)
        assert result['status'] == 'candidate_verified'
        assert len(result['reviews']) == 2
        assert not cancellations
    assert sorted(calls) == ['review-0', 'review-1']


@pytest.mark.parametrize('resume_input', [None, {}])
@pytest.mark.parametrize('supplied', ['omitted', 'same', 'different'])
def test_amendment_inherits_omitted_project_inputs(external, tmp_path, monkeypatch, capsys, resume_input, supplied):
    tree, scope, order, base, head = candidate(external)
    scope = open_project(scope.path, preflight_input={'expectHeadSha': base},
                         resume_preflight_input=resume_input)
    prior = tmp_path / 'prior'; prior.mkdir()
    manifest = {'order': goal._fact(order), 'worktree': str(tree), 'base_commit': base,
                'coding_launcher': 'exec', 'project_record': scope.record(),
                'runtime_provenance': {'python': sys.executable}}
    goal._write(prior / 'manifest.json', manifest)
    review = {'candidate_commit': head, 'verdict': 'PASS', 'findings': [], 'packet_sha256': 'b'*64}
    goal._write(prior / 'outcome.json', {'status': 'candidate_verified', 'candidate_commit': head,
        'coding_session': 'fixture-session', 'reviews': [review, review]})
    before = {p: p.read_bytes() for p in prior.iterdir()}
    # Neither rule-file content nor an advancing integration ref is a descriptor/input change.
    (tree / 'AGENTS.md').write_text('Updated project instructions.')
    git(tree, 'add', '.'); git(tree, 'commit', '-qm', 'upstream rules')
    git(tree, 'update-ref', 'refs/remotes/origin/main', git(tree, 'rev-parse', 'HEAD'))
    git(tree, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')
    prompt = tmp_path / 'additions.md'; prompt.write_text('Apply the operator additions.')
    from foundry_runtime._foundry_cli_help import amendment_invocation
    argv = amendment_invocation(order, prior / 'outcome.json', 'fixture-session', manifest,
                                prompt_file=str(prompt))['argv'][3:]
    if supplied != 'omitted':
        inputs = tmp_path / 'preflight.json'
        inputs.write_text(json.dumps({'expectHeadSha': base if supplied == 'same' else head}))
        argv.extend(['--project-preflight-input', str(inputs)])
    monkeypatch.setattr(foundry_entry, '_analyst_executable', lambda _: Path(sys.executable))
    received = []
    def run(order, evidence, executable, **kwargs):
        received.append(kwargs)
        return {'status': 'candidate_verified'}
    monkeypatch.setattr(goal, '_run', run)
    if supplied == 'different':
        assert cli.main(argv) == 2
        assert 'amendment project input differs from original run: preflight_input' in capsys.readouterr().err
        assert not received
    else:
        assert cli.main(argv) == 0
        assert len(received) == 1
        assert received[0]['project_record'] == scope.record()
    assert {p: p.read_bytes() for p in prior.iterdir()} == before


def test_retained_context_mismatch_names_the_field(external):
    from foundry_runtime._project_execution import from_record
    _, scope, _, _ = external
    saved = scope.record()
    saved['context']['descriptor_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='project context differs from retained invocation: descriptor_sha256'):
        from_record(saved, selected=scope)


@pytest.mark.parametrize('root', [False, True])
def test_completed_goal_answer_exits_nonzero_without_execution(external, tmp_path, root):
    tree, scope, order, base, head = candidate(external)
    folder = tmp_path / 'goal'; active = folder / 'round-0' if root else folder
    active.mkdir(parents=True)
    manifest = {'order': goal._fact(order), 'worktree': str(tree), 'base_commit': base,
                'run_id': 'completed-round', 'project_record': scope.record(),
                'runtime_provenance': {'python': sys.executable}, 'state': 'completed'}
    goal._write(active / 'manifest.json', manifest)
    goal._write(active / 'outcome.json', {'status': 'candidate_verified',
        'candidate_commit': head, 'coding_session': 'fixture-session', 'reviews': []})
    if root:
        goal._write(folder / 'manifest.json', {**manifest, 'mode': 'auto-amend',
                    'active_round': str(active)})
    before = {p: p.read_bytes() for p in folder.rglob('*.json')}
    prompt = tmp_path / 'answer.md'; prompt.write_text('Apply an additional requirement.')
    result = subprocess.run([sys.executable, '-m', 'foundry_runtime', '--project-config',
        str(scope.path), '--order', str(order), '--foundry-goal', '--recover-goal', str(folder),
        '--prompt-file', str(prompt)], capture_output=True, text=True)
    assert result.returncode == 2, result.stdout + result.stderr
    assert 'REFUSE Foundry recovery' in result.stderr
    assert '--amend-from' in result.stderr and 'fixture-session' in result.stderr
    assert 'recovery_invocation' not in result.stderr
    assert {p: p.read_bytes() for p in folder.rglob('*.json')} == before
