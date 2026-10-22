"""Historical verdict recovery cannot start paid work or accept today's tree."""
import json
import sys
from pathlib import Path

import pytest

from foundry_runtime import foundry_goal as goal
from test_installed_runtime import external, offline, git


@pytest.mark.parametrize('incomplete', [False, True])
def test_historical_dirty_tree_recovery_never_launches(external, tmp_path, monkeypatch, incomplete):
    tree, scope, order, _ = external
    base = git(tree, 'rev-parse', 'HEAD')
    (tree / 'candidate.txt').write_text('candidate')
    git(tree, 'add', '.'); git(tree, 'commit', '-qm', 'candidate')
    head = git(tree, 'rev-parse', 'HEAD')
    context = {'base_commit': base, 'candidate_commit': head}
    folder = tmp_path / 'review'; folder.mkdir()
    specs = [{'backend': 'claude', 'executable': sys.executable}] * 2
    checkpoint = {'schema_version': 1, 'context': context, 'worktree': str(tree),
        'order': goal._fact(order), 'run_id': 'fixture', 'reviewers': specs,
        'coding': {'evidence': []}, 'coding_usage': {}}
    goal._write(folder / 'review-checkpoint.json', checkpoint)
    goal._write(folder / 'manifest.json', {'order': goal._fact(order), 'run_id': 'fixture',
        'worktree': str(tree), 'base_commit': base,
        'review_checkpoint': goal._fact(folder / 'review-checkpoint.json')})
    (folder / 'reviews').mkdir()
    # A read-only fake native reader supplies canonical retained values; all
    # other review/group/finalization code runs normally, including slot checks.
    class Reader:
        @staticmethod
        def inspect_call(source, request, workspace, settings):
            value = {'candidate_commit': head, 'packet_sha256': goal._sha(goal._json_bytes(context)),
                     'verdict': 'PASS', 'findings': []}
            return {'value': value, 'schema_error': None, 'persistent': True,
                    'session': request['call_id'], 'snapshot': {'calls': []},
                    'receipt': {'path': str(source / 'receipt.json'), 'sha256': 'a'*64}}
        def __init__(self, *args, **kwargs):
            pytest.fail('historical recovery attempted native launch')
    for index in range(1 if incomplete else 2):
        slot = folder / 'reviews' / ('review-' + str(index)); slot.mkdir()
        goal._write(slot / 'native-manifest.json', {'backend': 'claude'})
        request = goal._role_request('fixture', 'review-' + str(index), context)
        call = slot / ('call-' + request['call_id']); call.mkdir()
        (call / 'receipt.json').write_text('{}')
    monkeypatch.setattr(goal, 'NativeAnalyst', Reader)
    from foundry_runtime import _followup
    monkeypatch.setattr(_followup, 'announce', lambda *a: pytest.fail('historical recovery announced live execution'))
    (tree / 'later.txt').write_text('later'); git(tree, 'add', '.'); git(tree, 'commit', '-qm', 'later')
    (tree / 'dirty.txt').write_text('preserve unfinished business work')
    if incomplete:
        with pytest.raises(goal.GoalBlocked, match='no model calls'):
            goal.recover_goal(order, folder, retained_only=True)
        assert not (folder / 'outcome.json').exists()
    else:
        outcome = goal.recover_goal(order, folder, retained_only=True)
        assert outcome['status'] == 'review_recorded' and not outcome['integration_pending']
        assert outcome['candidate_commit'] == head
        assert outcome['retained_recovery']['current_head'] != head
        assert outcome['retained_recovery']['acceptance_evaluated'] is False
        assert goal.recover_goal(order, folder, retained_only=True) == outcome
    assert (tree / 'dirty.txt').read_text() == 'preserve unfinished business work'
