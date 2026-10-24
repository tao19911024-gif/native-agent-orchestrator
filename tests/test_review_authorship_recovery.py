"""Standalone review recovery retains its declared design authors."""
from pathlib import Path
import sys

import pytest

from test_installed_runtime import external, offline, git, Analyst
from foundry_runtime import foundry_goal as goal, _foundry_goal_transitions as transitions


def test_standalone_review_transfer_preserves_declared_author_exclusion(external, tmp_path, monkeypatch):
    tree, scope, order, _ = external
    base = git(tree, 'rev-parse', 'HEAD')
    (tree / 'candidate.txt').write_text('candidate')
    git(tree, 'add', '.'); git(tree, 'commit', '-qm', 'candidate')
    head = git(tree, 'rev-parse', 'HEAD')
    calls = []
    class InterruptedAnalyst(Analyst):
        interrupted = True
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.selected = kwargs['model']
        def __call__(self, request):
            calls.append(self.selected)
            if self.interrupted:
                raise RuntimeError('fixture interrupted review')
            return super().__call__(request)
    monkeypatch.setattr(goal, 'NativeAnalyst', InterruptedAnalyst)
    monkeypatch.setattr(goal, 'resolve_executable', lambda _: Path(sys.executable))
    monkeypatch.setattr(transitions, 'resolve_executable', lambda _: Path(sys.executable))
    folder = tmp_path / 'review'
    with pytest.raises(RuntimeError, match='fixture interrupted review'):
        goal.run_review(order, folder, Path(sys.executable), target_worktree=tree,
            base_commit=base, candidate_commit=head, project_record=scope.record(),
            review_author_models=['opus'])
    InterruptedAnalyst.interrupted = False
    before = list(calls)
    checkpoint = (folder / 'review-checkpoint.json').read_bytes()
    with pytest.raises(goal.GoalBlocked, match='Reviewer model matches'):
        goal.recover_goal(order, folder, handoff_role='review-0', target_provider='claude')
    assert calls == before
    assert not (folder / 'outcome.json').exists()
    result = goal.recover_goal(order, folder, handoff_role='review-0', target_provider='codex')
    assert result['status'] == 'candidate_verified'
    assert len(calls) == len(before) + 2
    assert (folder / 'review-checkpoint.json').read_bytes() == checkpoint
