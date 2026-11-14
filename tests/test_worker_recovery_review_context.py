"""Explicit worker recovery retains the review lineage of its owning goal."""
import json
import sys

import pytest

from foundry_runtime import foundry_goal as goal, app_server_transport as transport
from test_installed_runtime import external, offline, git
from test_owned_goal_recovery import interrupted, native_manager, prepare, SERVER


def exercise_review_recovery(case, tmp_path, monkeypatch, prior_review, container='continuation'):
    tree, scope, order, predecessor, cleanup, header, ruling, root, active = case
    calls, real_spawn = native_manager(case, monkeypatch, review=True)
    manifest = json.loads((active / 'manifest.json').read_bytes())
    if prior_review is not None:
        if container == 'amendment':
            manifest['amendment'] = prior_review
        else:
            manifest['continuation'] = {'prior_review': prior_review,
                'prior_record': prior_review['prior_outcome'], 'answer': 'Previous correction',
                'base_commit': manifest['base_commit'], 'checkpoint': manifest['coding_checkpoint']}
    goal._write(active / 'manifest.json', manifest)
    recovery_file = prepare(case, tmp_path)
    before = {p: p.read_bytes() for p in predecessor.iterdir()}
    launches, reviews = [], []
    original = transport.run_executor
    server = SERVER.replace("  send({'method':'item/completed'", "  import subprocess\n"
        "  subprocess.check_call(['git','add','candidate.txt'])\n"
        "  subprocess.check_call(['git','commit','-qm','Recovered candidate'])\n"
        "  send({'method':'item/completed'")

    def execute(*args, **kwargs):
        def spawn(argv, **options):
            launches.append(1)
            return real_spawn([sys.executable, '-u', '-c', 'HANG=False\n' + server], **options)
        return original(*args, process_factory=spawn, **kwargs)

    def finish(checkpoint, folder, **kwargs):
        reviews.append(checkpoint)
        result = {'status': 'candidate_verified', 'candidate_commit': checkpoint['context']['candidate_commit']}
        goal._write(folder / 'outcome.json', result)
        return result

    monkeypatch.setattr(transport, 'run_executor', execute)
    monkeypatch.setattr(goal, '_finish_review', finish)
    result = goal.recover_goal(order, root, answer=ruling.read_text(), recovery_file=recovery_file)
    assert result['status'] == 'candidate_verified'
    assert len(launches) == 1 and len(calls) == 2 and len(reviews) == 1
    context = reviews[0]['context']
    if prior_review is None:
        assert 'prior_review' not in context and 'review_delta' not in context
    else:
        full = {'prior_outcome', 'prior_manifest', 'candidate_commit', 'base_commit', 'reviews'} <= prior_review.keys()
        assert context.get('prior_review') == (goal._review_focus(prior_review) if full else prior_review)
        if prior_review.get('candidate_commit'):
            assert context['review_delta']['base_commit'] == prior_review['candidate_commit']
            assert context['review_delta']['candidate_commit'] == git(tree, 'rev-parse', 'HEAD')
            assert context['review_delta']['changed_files'] == ['candidate.txt']
        else:
            assert 'review_delta' not in context
    assert context['base_commit'] == manifest['base_commit']  # Full acceptance scope is unchanged.
    retained = json.loads((active / 'manifest.json').read_bytes())
    assert retained.get(container) == manifest.get(container)
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert goal.recover_goal(order, root, answer=ruling.read_text(), recovery_file=recovery_file) == result
    assert len(launches) == 1 and len(calls) == 2 and len(reviews) == 1
    return context


@pytest.mark.parametrize('kind', ['continuation', 'partial', 'amendment', 'none'])
def test_worker_recovery_preserves_review_context(interrupted, tmp_path, monkeypatch, kind):
    tree = interrupted[0]
    base = git(tree, 'rev-parse', 'HEAD')
    outcome = tmp_path / 'prior-outcome.json'
    goal._write(outcome, {'status': 'review_blocked', 'candidate_commit': base, 'reviews': []})
    prior = {'prior_outcome': goal._fact(outcome), 'findings': [{'summary': 'Retain object identity'}]}
    if kind in ('continuation', 'amendment'):
        old_manifest = tmp_path / 'prior-manifest.json'
        goal._write(old_manifest, {'base_commit': base})
        prior = {**prior, 'prior_manifest': goal._fact(old_manifest), 'base_commit': base,
                 'candidate_commit': base, 'reviews': [{'findings': prior['findings']}]}
    exercise_review_recovery(interrupted, tmp_path, monkeypatch,
        None if kind == 'none' else prior, 'amendment' if kind == 'amendment' else 'continuation')
