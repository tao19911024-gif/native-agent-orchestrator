"""Standalone review recovery retains its declared design authors."""
from pathlib import Path
import json
import sys

import pytest

from test_installed_runtime import external, offline, git, Analyst
from foundry_runtime import foundry_goal as goal, _foundry_goal_transitions as transitions


@pytest.mark.parametrize('explicit,completed,prior', [
    (False, False, None), (False, False, {}), (False, False, 'partial'),
    (False, False, 'new-transfer'),
    (False, True, None), (True, False, None)])
def test_frozen_review_defaults_reselect_through_existing_transfer(external, tmp_path, monkeypatch, explicit, completed, prior):
    from foundry_runtime._model_catalogue import model
    tree, scope, order, _ = external
    base = git(tree, 'rev-parse', 'HEAD')
    (tree / 'candidate.txt').write_text('candidate')
    git(tree, 'add', '.'); git(tree, 'commit', '-qm', 'candidate')
    head = git(tree, 'rev-parse', 'HEAD')
    calls = []
    class Reader(Analyst):
        interrupted = True
        def __init__(self, *args, **kwargs):
            super().__init__(); self.model = kwargs['model']
        def __call__(self, request):
            calls.append(self.model)
            if self.interrupted:
                raise RuntimeError('fixture review interrupted')
            return super().__call__(request)
        @staticmethod
        def inspect_call(source, request, workspace, settings):
            return {'value': {'candidate_commit': head, 'packet_sha256': request['packet_sha256'],
                             'verdict': 'PASS', 'findings': []}, 'schema_error': None, 'persistent': True,
                    'session': 'completed-review-session', 'snapshot': {'calls': []},
                    'receipt': {'path': str(source / 'receipt.json'), 'sha256': 'a'*64}}
    monkeypatch.setattr(goal, 'NativeAnalyst', Reader)
    monkeypatch.setattr(goal, 'resolve_executable', lambda _: Path(sys.executable))
    monkeypatch.setattr(transitions, 'resolve_executable', lambda _: Path(sys.executable))
    reviewers = [{'backend': 'codex', 'model': model('astra'), 'effort': 'high'},
                 {'backend': 'claude', 'model': model('sonnet'), 'effort': 'high'}]
    folder = tmp_path / 'review'
    with pytest.raises(RuntimeError, match='fixture review interrupted'):
        goal.run_review(order, folder, Path(sys.executable), target_worktree=tree,
            base_commit=base, candidate_commit=head, project_record=scope.record(), reviewers=reviewers)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    checkpoint = json.loads((folder / 'review-checkpoint.json').read_bytes())
    checkpoint['implementation_evidence'] = {'implementation_models': [model('sonnet')] if completed or prior == 'new-transfer' else
        [model(tier) for tier in ('opus', 'sonnet', 'fable', 'sol', 'astra')]}
    checkpoint['model_independence'] = 'observed_editing_participants_excluded'
    checkpoint['compute_assignment'] = {'selection': {'source': 'explicit' if explicit else 'kind'}}
    goal._write(folder / 'review-checkpoint.json', checkpoint)
    manifest['review_checkpoint'] = goal._fact(folder / 'review-checkpoint.json')
    manifest['task_staffing'] = {'review_source_detail': {'model': 'explicit' if explicit else 'kind', 'backend': 'kind'}}
    if prior is not None and prior != 'new-transfer':
        manifest['review_transfers'] = {} if prior == {} else {'review-0': [
            {'backend': 'codex', 'model': model('terra'), 'effort': 'high', 'executable': sys.executable}]}
    goal._write(folder / 'manifest.json', manifest)
    if completed:
        slot = folder / 'reviews/review-0'; slot.mkdir(parents=True, exist_ok=True)
        goal._write(slot / 'native-manifest.json', {'backend': 'codex'})
        request = goal._role_request(checkpoint['run_id'], 'review-0', checkpoint['context'])
        call = slot / ('call-' + request['call_id'][:16]); call.mkdir()
        (call / 'receipt.json').write_text('{}')
    before = (folder / 'review-checkpoint.json').read_bytes()
    Reader.interrupted = False
    calls.clear()
    if explicit:
        with pytest.raises(goal.GoalBlocked, match='Reviewer model matches'):
            goal.recover_goal(order, folder)
        assert not calls
    else:
        transfer = {'handoff_role': 'review-0', 'target_provider': 'claude'} if prior == 'new-transfer' else {}
        assert goal.recover_goal(order, folder, **transfer)['status'] == 'candidate_verified'
        assert len(calls) == (1 if completed else 2)
        if prior == 'new-transfer':
            assert set(calls) == {model('opus'), model('sol')}
        elif not completed:
            assert set(calls) == {model('terra'), model('luna')}
        current = json.loads((folder / 'manifest.json').read_bytes())
        assert current['review_transfers']
        assert 'review-1' in current['review_transfers']
        if prior == 'partial':
            assert len(current['review_transfers']['review-0']) == 1
    assert (folder / 'review-checkpoint.json').read_bytes() == before


def test_quota_fallback_filters_existing_backend_executables(monkeypatch):
    from foundry_runtime._model_catalogue import model
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    settings = [{'backend': backend, 'model': model(tier), 'effort': 'high', 'executable': sys.executable}
                for backend, tier in [('codex', 'astra'), ('claude', 'sonnet')]]
    manifest = {'manager_state': {'quota_fallback': {'path': 'retained-receipt'}}}
    selected = transitions.select_reviewers(settings, [model('astra')], manifest, None,
                                            resolve=lambda _: pytest.fail('existing executable was not reused'))
    assert all(row['backend'] == 'codex' for row in selected)


def test_partial_transfer_retains_frozen_explicit_backend(monkeypatch):
    from foundry_runtime._model_catalogue import model
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    frozen = [{'backend': 'codex', 'model': model(tier), 'effort': 'high', 'executable': sys.executable}
              for tier in ('sol', 'astra')]
    transferred = [{**frozen[0], 'backend': 'claude', 'model': model('opus')}, frozen[1]]
    manifest = {'reviewers': frozen,
                'task_staffing': {'review_source_detail': {'model': 'kind', 'backend': 'explicit'}}}
    selected = transitions.select_reviewers(transferred, [model('astra')], manifest, None,
                                            resolve=lambda _: pytest.fail('existing executable was not reused'))
    assert all(row['backend'] == 'codex' for row in selected)


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
