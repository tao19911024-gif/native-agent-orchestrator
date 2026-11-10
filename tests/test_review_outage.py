"""Long outages and uncertain delivery use only synthetic native processes."""
from datetime import datetime, timezone
import json
import hashlib
import time
import threading

import pytest

from foundry_runtime import _foundry_review_group as group, foundry_native as native, foundry_goal as goal
from test_review_transport_retry import reviews, external, offline


@pytest.fixture
def clock(reviews, monkeypatch):
    monkeypatch.setattr(group, 'TRANSPORT_RETRY_DELAYS', (15, 30, 60, 60, 60, 60))
    now, waits = [time.time()], []
    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds
    monkeypatch.setattr(group, 'timestamp', lambda: now[0])
    monkeypatch.setattr(group, 'sleep', sleep)
    def wait(panel, seconds):
        if panel._cancelled.is_set():
            return True
        group.sleep(seconds)
        return panel._cancelled.is_set()
    monkeypatch.setattr(group.Panel, 'wait_for_cancel', wait)
    monkeypatch.setattr(native, '_now', lambda: datetime.fromtimestamp(now[0], timezone.utc).isoformat())
    return now, waits


def test_recovers_after_several_minutes_without_repeating_completed_review(reviews, clock):
    run, folder, calls, failures = reviews
    failures[1] = ['ENOTFOUND api.example.invalid'] * 4
    assert run()['status'] == 'candidate_verified'
    assert sorted(slot for slot, _ in calls) == [0, 1, 1, 1, 1, 1]
    assert sum(clock[1]) == pytest.approx(165)
    rows = json.loads((folder/'manifest.json').read_bytes())['review_transport_retries']
    assert len(rows) == 4 and len({row['retry_deadline_at'] for row in rows}) == 1


def test_continuous_outage_stops_and_restart_does_not_reset_window(reviews, clock):
    run, folder, calls, failures = reviews
    failures[1] = ['ENOTFOUND api.example.invalid'] * 20
    with pytest.raises(native.NativeError, match='ENOTFOUND'):
        run()
    assert sorted(s for s, _ in calls) == [0] + [1] * 7
    assert sum(clock[1]) == pytest.approx(285)
    assert len(json.loads((folder/'manifest.json').read_bytes())['review_transport_retries']) == 6
    checkpoint = json.loads((folder/'review-checkpoint.json').read_bytes())
    with pytest.raises(native.NativeError, match='ENOTFOUND'):
        goal._finish_review(checkpoint, folder, resume=True)
    assert sorted(s for s, _ in calls) == [0] + [1] * 8  # explicit call only


@pytest.mark.parametrize('error', ['request timed out', 'ECONNRESET',
    'stream disconnected before completion', 'TLS handshake timeout'])
def test_response_loss_without_local_output_is_not_replayed(reviews, clock, error):
    run, folder, calls, failures = reviews
    failures[1] = [error]
    with pytest.raises(native.NativeError):
        run()
    assert sorted(s for s, _ in calls) == [0, 1]
    assert clock[1] == []


@pytest.mark.parametrize('item', [
    {'id': 'cmd', 'type': 'command_execution', 'command': 'fixture check', 'status': 'completed', 'exit_code': 0},
    {'id': 'msg', 'type': 'agent_message', 'text': 'I already inspected the candidate'},
    {'id': 'reason', 'type': 'reasoning', 'text': 'Candidate analysis'},
])
def test_prior_work_prevents_replay_even_when_final_error_is_dns(reviews, clock, item):
    run, _, calls, failures = reviews
    failures['progress'] = [{'type': 'item.completed', 'item': item}]
    with pytest.raises(native.NativeError):
        run()
    assert sorted(s for s, _ in calls) == [0, 1]
    assert clock[1] == []


def test_second_attempt_uncertain_delivery_stops_the_schedule(reviews, clock):
    run, _, calls, failures = reviews
    failures[1] = ['ENOTFOUND', 'request timed out']
    with pytest.raises(native.NativeError, match='request timed out'):
        run()
    assert sorted(s for s, _ in calls) == [0, 1, 1]
    assert sum(clock[1]) == pytest.approx(15)


def test_completed_verdict_survives_failed_exit_without_new_request(reviews, clock):
    run, folder, calls, failures = reviews
    failures[1] = []
    failures['exit_after_result'] = True
    assert run()['status'] == 'candidate_verified'
    assert sorted(s for s, _ in calls) == [0, 1]
    assert not json.loads((folder/'manifest.json').read_bytes()).get('review_transport_retries')


def test_failure_duration_counts_against_retry_window(reviews, clock):
    run, folder, calls, failures = reviews
    failures[1] = ['ENOTFOUND'] * 20
    def advance(slot):
        if slot == 1:
            clock[0][0] += 120
    failures['advance'] = advance
    with pytest.raises(native.NativeError, match='ENOTFOUND'):
        run()
    assert sorted(s for s, _ in calls) == [0, 1, 1, 1]
    assert len(json.loads((folder/'manifest.json').read_bytes())['review_transport_retries']) == 2


def test_overslept_window_does_not_start_a_request(reviews, clock, monkeypatch):
    run, folder, calls, _ = reviews
    monkeypatch.setattr(group, 'sleep', lambda _: clock[0].__setitem__(0, clock[0][0] + 301))
    with pytest.raises(native.NativeError, match='ENOTFOUND'):
        run()
    assert sorted(s for s, _ in calls) == [0, 1]
    assert len(json.loads((folder/'manifest.json').read_bytes())['review_transport_retries']) == 1


@pytest.mark.parametrize('legacy', [False, True])
def test_interrupted_retry_keeps_deadline_and_legacy_allowance(reviews, clock, monkeypatch, legacy):
    run, folder, calls, failures = reviews
    failures[1] = ['ENOTFOUND'] * 20
    write = goal._write
    def interrupt(path, value):
        write(path, value)
        if path.name == 'manifest.json' and value.get('review_transport_retries'):
            raise KeyboardInterrupt('fixture stop after retry decision')
    monkeypatch.setattr(goal, '_write', interrupt)
    with pytest.raises(KeyboardInterrupt):
        run()
    monkeypatch.setattr(goal, '_write', write)
    path = folder/'manifest.json'
    saved = json.loads(path.read_bytes())
    if legacy:
        for row in saved['review_transport_retries']:
            row.pop('retry_deadline_at'); row.pop('retry_at')
        write(path, saved)
    else:
        clock[0][0] += 301
    checkpoint = json.loads((folder/'review-checkpoint.json').read_bytes())
    with pytest.raises(native.NativeError, match='ENOTFOUND'):
        goal._finish_review(checkpoint, folder, resume=True)
    assert sorted(s for s, _ in calls) == [0, 1, 1]
    assert json.loads(path.read_bytes())['review_transport_retries'] == saved['review_transport_retries']


@pytest.mark.parametrize('backend', ['codex', 'claude'])
@pytest.mark.parametrize('earlier', [None, 'request timed out', 'quota exhausted: ENOTFOUND',
    'ENOTFOUND; connection reset', 'stream disconnected before completion: dns error',
    'connection refused; broken pipe', 'DNS failure after response stream closed'])
def test_complete_stream_must_contain_only_connection_failures(tmp_path, backend, earlier):
    rows = ([{'type': 'thread.started', 'thread_id': 'fixture'}, {'type': 'turn.started'}]
            if backend == 'codex' else [{'type': 'system', 'subtype': 'init', 'session_id': 'fixture'}])
    if earlier:
        rows.append({'type': 'error', 'message': 'Reconnecting... 1/5 (' + earlier + ')'})
    rows.append({'type': 'error', 'message': 'ENOTFOUND api.example.invalid'})
    raw = ('\n'.join(map(json.dumps, rows))+'\n').encode()
    (tmp_path/'stdout.jsonl').write_bytes(raw)
    receipt = tmp_path/'receipt.json'
    receipt.write_text(json.dumps({'outcome': 'provider_failure', 'error': 'ENOTFOUND',
        'cleanup': {'cleanup_verified': True}, 'truncated': False, 'backend': backend,
        'stdout_bytes': len(raw), 'stdout_sha256': hashlib.sha256(raw).hexdigest()}))
    error = native.NativeError('ENOTFOUND', evidence=tmp_path)
    assert group.transport_failure_receipt(error) == (receipt if earlier is None else None)
    (tmp_path/'stdout.jsonl').write_bytes(raw+b'\n')
    assert group.transport_failure_receipt(error) is None


def test_successful_review_is_not_cut_short_by_retry_admission_window(reviews, clock):
    run, _, calls, failures = reviews
    def advance(slot):
        if slot == 1 and sum(s == 1 for s, _ in calls) == 2:
            clock[0][0] += 600  # a successful review can take longer than the retry window
    failures['advance'] = advance
    assert run()['status'] == 'candidate_verified'
    assert sorted(s for s, _ in calls) == [0, 1, 1]


def test_restart_during_first_delay_keeps_original_window(reviews, clock, monkeypatch):
    run, folder, calls, failures = reviews
    failures[1] = ['ENOTFOUND'] * 20
    sleep = group.sleep
    def cancel(_):
        raise KeyboardInterrupt('fixture interrupted first delay')
    monkeypatch.setattr(group, 'sleep', cancel)
    with pytest.raises(KeyboardInterrupt):
        run()
    monkeypatch.setattr(group, 'sleep', sleep)
    clock[0][0] += 301
    checkpoint = json.loads((folder/'review-checkpoint.json').read_bytes())
    with pytest.raises(native.NativeError, match='ENOTFOUND'):
        goal._finish_review(checkpoint, folder, resume=True)
    assert sorted(s for s, _ in calls) == [0, 1, 1]


def test_two_slots_share_delays_and_stop_retrying_a_completed_slot(reviews, clock):
    run, _, calls, failures = reviews
    failures[0] = ['ENOTFOUND'] * 2
    failures[1] = ['ENOTFOUND'] * 4
    assert run()['status'] == 'candidate_verified'
    assert sum(s == 0 for s, _ in calls) == 3
    assert sum(s == 1 for s, _ in calls) == 5


def test_long_peer_review_does_not_hold_the_other_retry_schedule(reviews, clock):
    run, _, calls, failures = reviews
    failures[0] = ['ENOTFOUND']
    failures[1] = ['ENOTFOUND'] * 3
    other_admitted = threading.Event()
    def advance(slot):
        if slot == 0 and sum(s == 0 for s, _ in calls) == 2:
            assert other_admitted.wait(5), 'peer schedule waited behind the running review'
        if slot == 1 and sum(s == 1 for s, _ in calls) == 4:
            other_admitted.set()
    failures['advance'] = advance
    assert run()['status'] == 'candidate_verified'
    assert sum(s == 0 for s, _ in calls) == 2
    assert sum(s == 1 for s, _ in calls) == 4


def test_cancel_during_delay_prevents_attempt_admission(reviews, clock, monkeypatch):
    run, _, calls, _ = reviews
    def cancel(panel, seconds):
        panel.cancel('fixture operator stop')
        return True
    monkeypatch.setattr(group.Panel, 'wait_for_cancel', cancel)
    with pytest.raises(native.NativeError, match='ENOTFOUND'):
        run()
    assert sorted(s for s, _ in calls) == [0, 1]


def test_controller_interrupt_cancels_and_joins_retry_threads(monkeypatch):
    class Analyst:
        cancelled = threading.Event()
        def cancel(self, _):
            self.cancelled.set()
    analyst = Analyst()
    joined, calls = [], []
    join = threading.Thread.join
    def interrupted_join(thread, *args, **kwargs):
        if thread.name.startswith('foundry-review-') and not joined:
            joined.append(thread)
            raise KeyboardInterrupt('fixture controller exit')
        return join(thread, *args, **kwargs)
    def waiting(name, request):
        assert analyst.cancelled.wait(5)
        calls.append(name)
    monkeypatch.setattr(threading.Thread, 'join', interrupted_join)
    with pytest.raises(KeyboardInterrupt):
        group.prewarm(['review-0', 'review-1'], {'review-0': {}, 'review-1': {}}, analyst, invoke=waiting)
    assert sorted(calls) == ['review-0', 'review-1']
    assert all(not thread.is_alive() for thread in joined)


@pytest.mark.parametrize('ended', [None, 'invalid', '2026-11-10T00:00:00'])
def test_invalid_failure_timestamp_preserves_original_error(tmp_path, monkeypatch, ended):
    receipt = tmp_path/'receipt.json'
    receipt.write_text(json.dumps({'ended_utc': ended}))
    monkeypatch.setattr(group, 'transport_failure_receipt', lambda _: receipt)
    assert group.transport_retry_plan(native.NativeError('original'), []) is None


def test_clock_adjustment_cannot_extend_a_single_delay(monkeypatch):
    waits = []
    monkeypatch.setattr(group, 'timestamp', lambda: 10)
    assert group.wait_for_transport_retry(1000, 1300, wait=waits.append, max_delay=15)
    assert waits == [15]


def test_restore_failure_keeps_original_native_error(reviews, monkeypatch):
    run, _, calls, _ = reviews
    def unreadable(panel, name):
        raise OSError('fixture receipt unavailable')
    monkeypatch.setattr(group.Panel, 'reopen_slot', unreadable)
    with pytest.raises(native.NativeError, match='ENOTFOUND') as error:
        run()
    assert isinstance(error.value.__cause__, OSError)
    assert sorted(s for s, _ in calls) == [0, 1]


def test_cancel_between_wait_and_admission_blocks_the_new_call(reviews, clock, monkeypatch):
    run, _, calls, _ = reviews
    def cancel(panel, seconds):
        panel.cancel('fixture cancellation after delay')
        return False  # emulate a cancellation racing the wait's return
    monkeypatch.setattr(group.Panel, 'wait_for_cancel', cancel)
    with pytest.raises(native.NativeError, match='panel cancelled'):
        run()
    assert sorted(s for s, _ in calls) == [0, 1]
