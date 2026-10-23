"""Long review inputs use the existing checkpoint without losing context identity."""
import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest

from foundry_runtime import foundry_goal as goal
from foundry_runtime._foundry_native_request import build_prompt
from test_installed_runtime import external, offline, git

LEGACY_REVIEW_HASH = '9f46fd345eea63cd6d71c570079cf8ea90b3ba0d6e5e2ceedb2cc1072a1af29a'


@pytest.mark.parametrize('backend', ['codex', 'claude'])
def test_read_policy_preserves_legacy_requests_and_complete_context(tmp_path, backend):
    context = {'original_order': 'Review the response.',
               'candidate_commit': 'b' * 40, 'base_commit': 'a' * 40}
    legacy = goal._role_request('run', 'review-0', context)
    old_prompt = build_prompt(legacy, backend, ValueError)
    # Captured from the installed runtime before read-policy support.
    assert hashlib.sha256(old_prompt).hexdigest() == LEGACY_REVIEW_HASH
    selected = goal._role_request('run', 'review-0', context, read_policy=1)
    raw = build_prompt(selected, backend, ValueError)
    envelope = json.loads(raw.rsplit(b'\n\n', 1)[-1])
    assert envelope['packet'] == context
    assert envelope['packet_sha256'] == legacy['packet_sha256']
    assert envelope['response_contract'] == legacy['response_contract']
    assert envelope['review_read_policy'] == 1
    assert b'Plan evidence reads' in raw and b'Plan evidence reads' not in old_prompt
    assert b'in full' in raw and b'uncovered criteria' in raw


@pytest.mark.parametrize('backend', ['codex', 'claude'])
@pytest.mark.parametrize('read_policy', [None, 1])
def test_long_fields_are_readable_from_the_retained_checkpoint(tmp_path, backend, read_policy):
    context = {'original_order': '完整要求和验收条件\n' * 1000,
        'changed_files': [f'src/module_{n}/implementation.py' for n in range(500)],
        'project_record': {'context': {'rules': 'project rule\n' * 600}},
        'execution_decision': {'manager': {'instruction': 'retained decision\n' * 500}},
        'candidate_commit': 'b' * 40, 'base_commit': 'a' * 40,
        'project_review': {'status': 'ready', 'checks': [{'status': 'passed'}]},
        'prior_review': {'findings': [{'summary': 'Preserve the response contract'}]}}
    path = tmp_path / 'review-checkpoint.json'
    goal._write(path, {'context': context})
    reference = goal._fact(path)
    before = path.read_bytes()
    request = goal._role_request('run', 'review-0', context, context_reference=reference, read_policy=read_policy)
    original = copy.deepcopy(request)
    raw = build_prompt(request, backend, ValueError)
    envelope = json.loads(raw.rsplit(b'\n\n', 1)[-1])
    assert request == original
    assert envelope['packet_sha256'] == goal._sha(goal._json_bytes(context))
    assert envelope['context_reference'] == reference
    assert set(envelope['context_fields']) == {
        'original_order', 'changed_files', 'project_record', 'execution_decision'}
    retained = json.loads(goal.goal_usage._reference(reference))
    reconstructed = dict(envelope['packet'])
    for field, pointer in envelope['context_fields'].items():
        assert pointer == '/context/' + field
        reconstructed[field] = retained['context'][field]
    assert reconstructed == context
    verdict = {'candidate_commit': context['candidate_commit'], 'packet_sha256': envelope['packet_sha256'],
               'verdict': 'PASS', 'findings': []}
    goal._validate_review(verdict, context, tmp_path, retained_only=True)
    with pytest.raises(goal.InvalidReview, match='identity differs'):
        goal._validate_review({**verdict, 'packet_sha256': goal._sha(goal._json_bytes(envelope['packet']))},
                              context, tmp_path, retained_only=True)
    assert envelope['packet']['project_review'] == context['project_review']
    assert envelope['packet']['prior_review'] == context['prior_review']
    legacy = build_prompt(goal._role_request('run', 'review-0', context), backend, ValueError)
    assert len(raw) < len(legacy) / 2
    assert b'Read the complete original_order' in raw
    assert path.read_bytes() == before


@pytest.mark.parametrize('backend', ['codex', 'claude'])
def test_short_context_and_legacy_requests_keep_inline_rendering(tmp_path, backend):
    context = {'original_order': 'Implement and check the response.', 'changed_files': ['response.py'],
               'candidate_commit': 'candidate', 'base_commit': 'base'}
    path = tmp_path / 'review-checkpoint.json'
    goal._write(path, {'context': context})
    legacy = goal._role_request('run', 'review-0', context)
    selected = goal._role_request('run', 'review-0', context, context_reference=goal._fact(path))
    assert build_prompt(selected, backend, ValueError) == build_prompt(legacy, backend, ValueError)
    context['original_order'] *= 1000
    old = json.loads(build_prompt(goal._role_request('run', 'review-0', context), backend, ValueError).rsplit(b'\n\n', 1)[-1])
    assert old['packet'] == context and 'context_reference' not in old


def test_goal_recovery_reuses_completed_review_with_the_same_context_reference(
        external, tmp_path, monkeypatch):
    from foundry_runtime import foundry_native as native, _code_index
    tree, scope, order, workers = external
    order.write_text('Implement the complete contract.\n' * 400 + 'FINAL_REQUIREMENT', encoding='utf-8')
    base = git(tree, 'rev-parse', 'HEAD')
    (tree / 'response.py').write_text('result = 42\n')
    git(tree, 'add', '.'); git(tree, 'commit', '-qm', 'candidate')
    head = git(tree, 'rev-parse', 'HEAD')
    monkeypatch.setattr(_code_index, 'prepare', lambda *a, **k: None)
    calls, recovering = [], False
    spawn = native.OwnedProcess.spawn
    def local(argv, **kwargs):
        source = kwargs['stdin']
        request = json.loads(source.read().rsplit(b'\n\n', 1)[-1]); source.seek(0)
        slot = 1 if 'review-1' in str(source.name) else 0
        calls.append((slot, request))
        assert request['review_read_policy'] == 1
        assert request['context_fields']['original_order'] == '/context/original_order'
        assert 'original_order' not in request['packet']
        result = {'candidate_commit': head, 'packet_sha256': request['packet_sha256'],
                  'verdict': 'PASS', 'findings': []}
        sid = f'00000000-0000-4000-8000-{slot + 1:012d}'
        rows = [{'type': 'thread.started', 'thread_id': sid}, {'type': 'turn.started'}]
        if slot == 1 and not recovering:
            rows.append({'type': 'turn.failed', 'error': {'message': 'fixture interrupted review'}})
        else:
            rows.extend([{'type': 'item.completed', 'item': {'id': 'final',
                'type': 'agent_message', 'text': json.dumps(result)}},
                {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'cached_input_tokens': 0, 'output_tokens': 5}}])
        # The real child reads the saved requirement, rather than relying on an
        # in-memory context supplied by the test controller.
        script = ("import sys,json,hashlib; from pathlib import Path; "
            "r=json.loads(sys.stdin.buffer.read().rsplit(b'\\n\\n',1)[-1]); "
            "raw=Path(r['context_reference']['path']).read_bytes(); "
            "assert hashlib.sha256(raw).hexdigest()==r['context_reference']['sha256']; "
            "assert json.loads(raw)['context']['original_order'].split('\\n\\nPROJECT EXECUTION CONTEXT\\n')[0].endswith('FINAL_REQUIREMENT'); "
            "sys.stdout.write(" + repr('\n'.join(map(json.dumps, rows)) + '\n') + ")")
        return spawn([sys.executable, '-B', '-c', script], **kwargs)
    monkeypatch.setattr(native.OwnedProcess, 'spawn', local)
    folder = tmp_path / 'goal'
    with pytest.raises(native.NativeError) as interrupted:
        goal.run_review(order, folder, Path(sys.executable), target_worktree=tree,
            base_commit=base, candidate_commit=head, project_record=scope.record())
    checkpoint = folder / 'review-checkpoint.json'
    before = checkpoint.read_bytes()
    assert json.loads(before)['context_delivery'] == 'reference-v1'
    assert json.loads(before)['review_read_policy'] == 1
    completed = {p: p.read_bytes() for p in (folder / 'reviews/review-0').rglob('*') if p.is_file()}
    assert sorted(slot for slot, _ in calls) == [0, 1], str(interrupted.value)
    from foundry_runtime import _followup, cli
    feedback = _followup.status(folder)['feedback']
    assert feedback['candidate_commit'] == head and feedback['outcome_status'] is None
    assert feedback['artifacts']['outcome'] is None
    assert feedback['artifacts']['review_checkpoint'] == goal._fact(checkpoint)
    assert feedback['continuation']['status'] == 'review_completion_required'
    command = feedback['continuation']['invocation']['argv']
    assert command[0] == sys.executable and command[-2:] == ['--recover-goal', str(folder)]
    assert checkpoint.read_bytes() == before
    with goal._run_lock(folder):
        assert 'feedback' not in _followup.status(folder)
    recovering = True
    assert cli.main(command[3:]) == 0
    result = json.loads((folder / 'outcome.json').read_bytes())
    assert result['status'] == 'candidate_verified'
    assert [slot for slot, _ in calls].count(0) == 1 and calls[-1][0] == 1
    assert len(calls) == 3 and not workers
    assert all(request['context_reference'] == goal._fact(checkpoint) for _, request in calls)
    assert all(p.read_bytes() == raw for p, raw in completed.items())
    assert checkpoint.read_bytes() == before
    assert goal.recover_goal(order, folder) == result and len(calls) == 3
    # A settled manager boundary reads the same native review history during an
    # upgrade. The empty manager directory represents no completed manager call.
    from foundry_runtime import _foundry_goal_upgrade as upgrade
    manager = tmp_path / 'manager'; manager.mkdir()
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    prepared = upgrade.prepare(goal, folder,
        {**manifest, 'manager_state': {'native_dir': str(manager)}},
        {'order': str(order), 'answer': 'Continue from the completed reviews.'})
    history = prepared['manager_packet']['runtime_upgrade']['previous_review_history']
    assert set(history) == {'review-0', 'review-1'}
    assert len(calls) == 3 and checkpoint.read_bytes() == before
