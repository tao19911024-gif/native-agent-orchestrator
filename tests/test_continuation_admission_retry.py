"""A refused handoff can retry without losing its frozen dispatch inputs."""
import json
from pathlib import Path
import sys

import pytest

from test_installed_runtime import external, offline
from foundry_runtime import foundry_goal as goal, _foundry_dispatch as dispatch


@pytest.mark.parametrize('legacy', [False, True])
def test_refused_handoff_retries_same_answer_and_run(external, tmp_path, monkeypatch, legacy):
    tree, scope, order, launches = external
    original = dispatch.coding_argv

    def coding(*args, **kwargs):
        argv = original(*args, **kwargs)
        argv[-1] = argv[-1].replace("print('fixture child completed')", "print('OpenAI Codex vfixture\\n--------\\nsession id: 00000000-0000-0000-0000-000000000001\\n--------')")
        argv[-1] += "; Path('candidate.txt').write_text('unfinished')"
        return argv

    monkeypatch.setattr(dispatch, 'coding_argv', coding)
    folder = tmp_path / 'original'
    assert goal.run_goal(order, folder, Path(sys.executable), coding_launcher='exec',
        coding_model='recorded-model', coding_effort='high',
        project_record=scope.record())['status'] == 'project_handoff'
    before = {p: p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    handoff = goal.goal_continuation.dispatch_handoff
    calls = []

    def refuse(*args, **kwargs):
        calls.append(args)
        raise goal.GoalBlocked('unresolved intent fixture')

    monkeypatch.setattr(goal.goal_continuation, 'dispatch_handoff', refuse)
    options = dict(answer='保留工作并继续', migrate_runtime=True, handoff_role='coding')
    with pytest.raises(goal.GoalBlocked, match='unresolved intent'):
        goal.recover_goal(order, folder, **options)
    target, = tmp_path.glob('continue-*')
    manifest_path = target / 'manifest.json'
    saved = json.loads(manifest_path.read_bytes())
    if legacy:
        saved.pop('continuation_start')
        manifest_path.write_text(json.dumps(saved), encoding='utf-8')
    # Still refused until dispatch admission actually permits the same retry.
    with pytest.raises(goal.GoalBlocked, match='unresolved intent'):
        goal.recover_goal(order, folder, **options)
    assert len(calls) == 2 and len(launches) == 1
    monkeypatch.setattr(goal.goal_continuation, 'dispatch_handoff', handoff)
    result = goal.recover_goal(order, folder, **options)
    assert result['status'] == 'project_handoff' and len(launches) == 2
    completed = json.loads(manifest_path.read_bytes())
    assert completed['run_id'] == saved['run_id']
    assert completed['requested_compute'] == saved['requested_compute']
    assert completed['continuation']['answer'] == options['answer']
    assert all(p.read_bytes() == content for p, content in before.items())
    assert goal.recover_goal(order, folder, **options) == result
    assert len(launches) == 2
