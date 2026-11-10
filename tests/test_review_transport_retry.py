"""Offline review attempts use synthetic native processes, never a model."""
import json
from pathlib import Path
import sys

import pytest

from foundry_runtime import foundry_goal as goal, foundry_native as native
from foundry_runtime import _code_index, _foundry_review_group as group
from test_installed_runtime import external, offline, git


@pytest.fixture
def reviews(external, tmp_path, monkeypatch):
    tree, scope, order, _ = external
    base = git(tree, 'rev-parse', 'HEAD')
    (tree / 'candidate.txt').write_text('candidate')
    git(tree, 'add', '.'); git(tree, 'commit', '-qm', 'candidate')
    head = git(tree, 'rev-parse', 'HEAD')
    monkeypatch.setattr(_code_index, 'prepare', lambda *a, **k: None)
    monkeypatch.setattr(group, 'TRANSPORT_RETRY_DELAY_SECONDS', 0)
    calls, failures = [], {1: ['Reconnecting... 5/5 (request timed out)']}
    spawn = native.OwnedProcess.spawn
    def child(argv, **kwargs):
        source = kwargs['stdin']
        request = json.loads(source.read().rsplit(b'\n\n', 1)[-1]); source.seek(0)
        assert request['kind'] == 'review'
        slot = int(Path(source.name).parent.parent.parent.name.split('-')[-1])
        calls.append((slot, request))
        error = failures.get(slot, []).pop(0) if failures.get(slot) else None
        verdict = failures.get('verdict', 'PASS')
        value = {'candidate_commit': head, 'packet_sha256': request['packet_sha256'],
                 'verdict': verdict, 'findings': ([] if verdict == 'PASS' else [
                     {'severity': 'P1', 'file': 'candidate.txt', 'line': '1', 'summary': 'Fixture defect'}])}
        sid = '00000000-0000-0000-0000-%012d' % (slot + 1)
        rows = [{'type': 'thread.started', 'thread_id': sid}, {'type': 'turn.started'}]
        rows += ([{'type': 'turn.failed', 'error': {'message': error}}] if error else [
            {'type': 'item.completed', 'item': {'id': 'final', 'type': 'agent_message', 'text': json.dumps(value)}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'cached_input_tokens': 0, 'output_tokens': 5}}])
        script = 'import sys;sys.stdin.buffer.read();sys.stdout.write(' + repr('\n'.join(map(json.dumps, rows))+'\n') + ')'
        return spawn([sys.executable, '-B', '-c', script], **kwargs)
    monkeypatch.setattr(native.OwnedProcess, 'spawn', child)
    folder = tmp_path / 'review'
    def run():
        return goal.run_review(order, folder, Path(sys.executable), target_worktree=tree,
            base_commit=base, candidate_commit=head, project_record=scope.record())
    return run, folder, calls, failures


def test_transport_retry_keeps_completed_slot_and_original_receipts(reviews):
    run, folder, calls, _ = reviews
    result = run()
    assert result['status'] == 'candidate_verified'
    assert [s for s, _ in calls].count(0) == 1
    assert [s for s, _ in calls].count(1) == 2
    assert calls[0][1]['packet_sha256'] == calls[-1][1]['packet_sha256']
    receipts = sorted((folder/'reviews/review-1').glob('attempt-*/call-*/receipt.json'))
    assert [json.loads(p.read_bytes())['outcome'] for p in receipts] == ['provider_failure', 'success']
    manifest = json.loads((folder/'manifest.json').read_bytes())
    assert len(manifest['review_transport_retries']) == 1
    assert manifest['review_transport_retries'][0]['slot'] == 'review-1'
    policy = json.loads((receipts[1].parent.parent/'native-manifest.json').read_bytes())
    assert policy['shadow_identity']['review_predecessors'] == [manifest['review_transport_retries'][0]['receipt']]
    checkpoint = json.loads((folder/'review-checkpoint.json').read_bytes())
    count = len(calls)
    assert goal._finish_review(checkpoint, folder, resume=True)['status'] == 'candidate_verified'
    assert len(calls) == count


def test_second_transport_failure_stops_without_another_automatic_retry(reviews):
    run, folder, calls, failures = reviews
    failures[1] = ['ECONNRESET'] * 3
    with pytest.raises(native.NativeError, match='ECONNRESET'):
        run()
    assert [s for s, _ in calls].count(1) == 2
    checkpoint = json.loads((folder/'review-checkpoint.json').read_bytes())
    with pytest.raises(native.NativeError, match='ECONNRESET'):
        goal._finish_review(checkpoint, folder, resume=True)
    assert [s for s, _ in calls].count(1) == 3  # explicit recovery only
    assert [s for s, _ in calls].count(0) == 1


@pytest.mark.parametrize('error', ['model not found', 'quota exceeded: request timed out',
    'authentication failed: connection reset', 'invalid response schema', 'task deadline exceeded',
    'model example does not exist: request timed out', 'credentials expired: ECONNRESET',
    'stream disconnected before completion: invalid_request_error',
    'stream disconnected before completion: content policy violation',
    'connection closed: request body too large', 'Reconnecting... 5/5 (unknown error)'])
def test_non_transport_failure_is_not_retried(reviews, error):
    run, folder, calls, failures = reviews
    failures[1] = [error]
    with pytest.raises(native.NativeError):
        run()
    assert [s for s, _ in calls].count(1) == 1
    assert not json.loads((folder/'manifest.json').read_bytes()).get('review_transport_retries')


def test_both_failed_slots_retry_but_a_real_block_remains_a_block(reviews):
    run, folder, calls, failures = reviews
    failures[0] = ['ENOTFOUND api.example.invalid']
    failures['verdict'] = 'BLOCK'
    assert run()['status'] == 'review_blocked'
    assert sorted(s for s, _ in calls) == [0, 0, 1, 1]
    assert len(json.loads((folder/'manifest.json').read_bytes())['review_transport_retries']) == 2


def test_mixed_failure_retries_only_the_transport_slot(reviews):
    run, folder, calls, failures = reviews
    failures[0] = ['model not found']
    with pytest.raises(native.NativeError, match='model not found'):
        run()
    assert sorted(s for s, _ in calls) == [0, 1, 1]
    assert len(list((folder/'reviews/review-0').glob('attempt-*'))) == 1
    assert [r['slot'] for r in json.loads((folder/'manifest.json').read_bytes())['review_transport_retries']] == ['review-1']


def test_interrupted_retry_decision_does_not_reset_allowance(reviews, monkeypatch):
    run, folder, calls, failures = reviews
    failures[1] = ['ECONNRESET'] * 3
    write = goal._write
    def interrupt(path, value):
        write(path, value)
        if Path(path).name == 'manifest.json' and value.get('review_transport_retries'):
            raise KeyboardInterrupt('fixture controller stop after durable decision')
    monkeypatch.setattr(goal, '_write', interrupt)
    with pytest.raises(KeyboardInterrupt):
        run()
    assert [s for s, _ in calls].count(1) == 1
    monkeypatch.setattr(goal, '_write', write)
    checkpoint = json.loads((folder/'review-checkpoint.json').read_bytes())
    with pytest.raises(native.NativeError, match='ECONNRESET'):
        goal._finish_review(checkpoint, folder, resume=True)
    assert [s for s, _ in calls].count(1) == 2
    assert [s for s, _ in calls].count(0) == 1


@pytest.mark.parametrize('message', ['ENOTFOUND api.example.invalid', 'EAI_AGAIN', 'ECONNRESET',
    'ETIMEDOUT', 'request timeout', 'connection reset by peer', 'DNS resolution failed',
    'tls handshake eof', 'stream disconnected before completion: request timed out'])
def test_transport_receipt_classification(tmp_path, message):
    path = tmp_path/'receipt.json'
    record = {'outcome': 'provider_failure', 'error': message, 'cleanup': {'cleanup_verified': True}}
    path.write_text(json.dumps(record))
    error = native.NativeError(message, evidence=tmp_path)
    assert group.transport_failure_receipt(error) == path
    for outcome, cleaned in [('timeout', True), ('provider_limit', True), ('provider_capacity', True),
                             ('schema_failure', True), ('cleanup_unknown', False), ('provider_failure', False)]:
        path.write_text(json.dumps({**record, 'outcome': outcome, 'cleanup': {'cleanup_verified': cleaned}}))
        assert group.transport_failure_receipt(error) is None


def test_retry_does_not_extend_task_deadline(monkeypatch):
    import time
    from foundry_runtime import _task_deadline
    monkeypatch.setattr(group.time, 'sleep', lambda _: pytest.fail('deadline cannot fit retry delay'))
    with _task_deadline.scope(time.time() + 1):
        assert group.wait_for_transport_retry() is False


@pytest.mark.parametrize('raw', ['{', '[]', '{"outcome":"provider_failure","cleanup":true}',
    '{"outcome":"provider_failure","cleanup":{"cleanup_verified":true},"error":{}}'])
def test_unreadable_receipt_keeps_original_failure(tmp_path, raw):
    (tmp_path/'receipt.json').write_text(raw)
    assert group.transport_failure_receipt(native.NativeError('original', evidence=tmp_path)) is None


def test_retry_uses_existing_deadline_after_five_second_delay(monkeypatch):
    from foundry_runtime import _task_deadline
    sleeps = []
    monkeypatch.setattr(group.time, 'sleep', sleeps.append)
    with _task_deadline.scope(None):
        assert group.wait_for_transport_retry()
    assert sleeps == [5]


def test_expired_deadline_after_delay_does_not_admit_retry(reviews, monkeypatch):
    from foundry_runtime import _task_deadline
    run, folder, calls, _ = reviews
    wait = group.wait_for_transport_retry
    def expire_during_wait():
        readings = iter([100, 0])
        with monkeypatch.context() as context:
            context.setattr(group.time, 'sleep', lambda _: None)
            context.setattr(_task_deadline, 'remaining', lambda: next(readings))
            return wait()
    monkeypatch.setattr(group, 'wait_for_transport_retry', expire_during_wait)
    with pytest.raises(native.NativeError, match='request timed out'):
        run()
    assert sorted(s for s, _ in calls) == [0, 1]
    assert not json.loads((folder/'manifest.json').read_bytes()).get('review_transport_retries')
