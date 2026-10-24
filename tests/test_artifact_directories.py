import hashlib
from pathlib import Path

import pytest

from foundry_runtime import foundry_goal as owner
from foundry_runtime._foundry_goal_delivery import artifacts


def deliver(tree, paths):
    return artifacts(owner, {'artifacts': paths, 'instruction': 'inspect checker evidence'},
                     tree, 0, 'fixture-session', [])


def test_nested_unicode_directory_and_file_handoff(tmp_path):
    folder = tmp_path / '检查结果'
    (folder / 'nested').mkdir(parents=True)
    (folder / 'nested' / '报告.txt').write_bytes(b'proof')
    (tmp_path / 'summary.txt').write_bytes(b'summary')
    result = deliver(tmp_path, ['检查结果', 'summary.txt', '检查结果'])
    assert result['status'] == 'artifacts_delivered' and result['acceptance_pending']
    directory, file = result['artifacts']
    assert directory['entries'] == [{'path': 'nested/报告.txt', 'sha256': hashlib.sha256(b'proof').hexdigest()}]
    assert file == {'path': str(tmp_path / 'summary.txt'), 'sha256': hashlib.sha256(b'summary').hexdigest()}
    assert deliver(tmp_path, ['检查结果'])['artifacts'][0] == directory
    (folder / 'nested' / '报告.txt').write_bytes(b'changed')
    assert deliver(tmp_path, ['检查结果'])['artifacts'][0]['sha256'] != directory['sha256']


def test_empty_directory_and_missing_path(tmp_path):
    (tmp_path / 'empty').mkdir()
    assert deliver(tmp_path, ['empty'])['artifacts'][0]['entries'] == []
    with pytest.raises(owner.GoalBlocked, match='unreadable'):
        deliver(tmp_path, ['missing'])


@pytest.mark.parametrize('paths', [None, [], [None], [{}]])
def test_invalid_declaration_stops_at_delivery_boundary(tmp_path, paths):
    with pytest.raises(owner.GoalBlocked):
        deliver(tmp_path, paths)


@pytest.mark.parametrize('inside', [False, True])
def test_delivery_resolves_paths_before_scratch_reclamation(tmp_path, monkeypatch, inside):
    from foundry_runtime import _goal_scratch
    tree = tmp_path / 'project'; tree.mkdir()
    folder = tree / 'goal'; folder.mkdir()
    manifest = {'run_id': 'fixture'}
    _goal_scratch.register(folder, manifest)
    scratch = folder / 'disposable'
    artifact = (scratch / 'report.txt') if inside else (tree / 'report.txt')
    artifact.write_text('retained report')
    elsewhere = tmp_path / 'other'; elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    outcome = deliver(tree, [str(artifact.relative_to(tree))])
    assert outcome['artifacts'][0]['path'] == str(artifact)
    result = _goal_scratch.reclaim(folder, manifest, outcome)
    assert result['status'] == ('retained' if inside else 'reclaimed')
    assert artifact.read_text() == 'retained report'
    assert scratch.exists() is inside
