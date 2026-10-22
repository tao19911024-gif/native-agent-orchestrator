"""Local provider streams exercise temporary stops and read-only refusals."""
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from test_manager_recovery import conversation
from foundry_runtime import _foundry_manager as manager, foundry_native as native


@pytest.mark.parametrize('conversation,category', [
    ({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
      'result': "You've hit your session limit · resets 12:30am"}, 'provider_limit'),
    ({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
      'api_error_status': 429, 'reset_at': '2026-10-07T07:30:00Z',
      'result': 'rate limited'}, 'provider_limit'),
    ({'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'overloaded'}}, 'provider_capacity'),
], indirect=['conversation'])
def test_temporary_manager_stop_has_no_cancellation_and_accepts_supplement(conversation, tmp_path, category):
    state, requests, _ = conversation
    packet = {'stage': 'assign'}
    with pytest.raises(native.NativeError, match=category):
        manager.turn(state, tmp_path, packet)
    folder = Path(state['native_dir'])
    receipt = next(folder.glob('call-*/receipt.json'))
    row = json.loads(receipt.read_bytes())
    assert row['outcome'] == category
    assert row['provider_stop']['category'] == category
    assert not list(folder.glob('cancellation*.json'))
    frozen = receipt.read_bytes()
    decision, updated, _, accepted = manager.turn(state, tmp_path, {**packet, 'operator_answer': '补充'})
    assert decision['instruction'] == '补充' and accepted['operator_answer'] == '补充'
    assert updated['session_id'] == 'original-manager'
    assert receipt.read_bytes() == frozen and len(requests) == 3
    assert not list(folder.glob('cancellation*.json'))
    manager.turn(state, tmp_path, accepted)
    assert len(requests) == 3


def test_restore_rejection_never_creates_cancellation(conversation, tmp_path, monkeypatch):
    state, _, failures = conversation
    failures.clear()
    packet = {'stage': 'assign'}
    manager.turn(state, tmp_path, packet)
    folder = Path(state['native_dir'])
    before = {str(p): p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    original = native.NativeAnalyst._inspect_receipt
    def refused(self, *args, **kwargs):
        raise native.NativeError('native backend, command or protocol identity changed')
    monkeypatch.setattr(native.NativeAnalyst, '_inspect_receipt', refused)
    with pytest.raises(native.NativeError) as caught:
        manager.turn(state, tmp_path, packet)
    snapshot = caught.value.analyst.evidence_snapshot()
    row = snapshot['calls'][0]
    assert row['call_id'] in snapshot['cancel_reason']
    assert row['evidence'] in snapshot['cancel_reason']
    assert 'kind=coordinate' in snapshot['cancel_reason']
    assert 'native backend, command or protocol identity changed' in snapshot['cancel_reason']
    assert before == {str(p): p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    monkeypatch.setattr(native.NativeAnalyst, '_inspect_receipt', original)
    assert manager.turn(state, tmp_path, packet)[0]['instruction'] == 'original'


@pytest.mark.parametrize('backend', ['claude', 'codex'])
@pytest.mark.parametrize('root_alive', [False, True])
def test_missing_manager_receipt_reconciles_only_a_verified_dead_tree(conversation, tmp_path, monkeypatch, root_alive, backend):
    state, requests, failures = conversation
    state['backend'] = backend
    failures.clear()
    packet = {'stage': 'assign'}
    manager.turn(state, tmp_path, packet)
    folder = Path(state['native_dir'])
    receipt = next(folder.glob('call-*/receipt.json'))
    original = json.loads(receipt.read_bytes())
    receipt.unlink()  # Fixture: controller exited before publishing its receipt.
    if root_alive:
        from foundry_runtime import _executor_lifecycle
        def still_live(*args, **kwargs):
            raise RuntimeError('recorded process is still running: fixture-root')
        monkeypatch.setattr(_executor_lifecycle, 'measure_abandoned', still_live)
        with pytest.raises(native.NativeError, match='call_id=.*kind=coordinate.*fixture-root'):
            manager.turn(state, tmp_path, packet)
        assert len(requests) == 1 and not receipt.exists()
        assert not list(folder.glob('cancellation*.json'))
    else:
        result = manager.turn(state, tmp_path, packet)
        assert result[0]['instruction'] == 'original' and len(requests) == 2
        reconciled = json.loads(receipt.read_bytes())
        assert reconciled['outcome'] == 'failure' and reconciled['returncode'] is None
        assert reconciled['ended_utc'] is None and reconciled['usage'] is None
        assert reconciled['cleanup']['cleanup_verified'] is True
        assert reconciled['cleanup']['root_pid'] == original['cleanup']['root_pid']
        assert receipt.with_name('interrupted.json').is_file()


def test_codex_structured_provider_failure():
    from foundry_runtime.foundry_codex import CodexStreamProtocol
    protocol = CodexStreamProtocol()
    protocol.accept({'type': 'thread.started', 'thread_id': '00000000-0000-0000-0000-000000000001'})
    with pytest.raises(native.NativeProviderError) as caught:
        protocol.accept({'type': 'turn.failed', 'error': {'message': 'capacity', 'codexErrorInfo': 'serverOverloaded'}})
    assert caught.value.outcome == 'provider_capacity'


def test_reconciliation_never_terminates_real_live_fixture_root(conversation, tmp_path):
    state, requests, failures = conversation
    failures.clear()
    manager.turn(state, tmp_path, {'stage': 'assign'})
    folder = Path(state['native_dir'])
    receipt = next(folder.glob('call-*/receipt.json'))
    allocation = json.loads(receipt.with_name('reservation.json').read_bytes())
    receipt.unlink()
    with open('NUL', 'rb') as source, open('NUL', 'wb') as sink:
        child = native.OwnedProcess.spawn([sys.executable, '-B', '-c', 'import time;time.sleep(120)'],
            cwd=str(tmp_path), stdin=source, stdout=sink, stderr=sink, attempt_id=allocation['attempt_id'])
        try:
            receipt.with_name('process.json').write_text(json.dumps({'attempt_id': allocation['attempt_id'],
                'root_pid': child.pid, 'root_creation_time': child.creation_time}))
            with pytest.raises(native.NativeError, match='recorded process is still running'):
                manager.turn(state, tmp_path, {'stage': 'assign'})
            assert child.poll() is None and not receipt.exists() and len(requests) == 1
        finally:
            assert child.stop_and_verify(drain_seconds=0, kill_seconds=2).cleanup_verified


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_reconcile_before_first_complete_session_event(conversation, tmp_path, backend):
    state, requests, failures = conversation
    state['backend'] = backend
    failures.clear()
    packet = {'stage': 'assign'}
    manager.turn(state, tmp_path, packet)
    receipt = next(Path(state['native_dir']).glob('call-*/receipt.json'))
    receipt.unlink()
    receipt.with_name('stdout.jsonl').write_bytes(b'{"type":')
    result = manager.turn(state, tmp_path, packet)
    assert result[0]['instruction'] == 'original' and len(requests) == 2


@pytest.mark.parametrize('conversation', [{'type': 'error', 'error': {
    'type': 'overloaded_error', 'message': 'overloaded'}}], indirect=True)
@pytest.mark.parametrize('primary', ['capture', 'timeout', 'cleanup'])
def test_late_provider_stop_does_not_hide_execution_failure(conversation, tmp_path, monkeypatch, primary):
    state, _, _ = conversation
    spawn = native.OwnedProcess.spawn
    cleanup_started, stdout_read = threading.Event(), threading.Event()
    def wrapped_spawn(argv, **kwargs):
        process = spawn(argv, **kwargs)
        read, cleanup = process.stdout.buffer.read, process.stop_and_verify
        def delayed_read(size):
            assert cleanup_started.wait(3)
            result = read(size)
            stdout_read.set()
            return result
        def bad_read(size):
            raise OSError('injected stderr capture failure')
        def cleanup_with_late_output(**kwargs):
            cleanup_started.set()
            assert stdout_read.wait(3)
            receipt = cleanup(**kwargs)
            if primary == 'cleanup':
                from dataclasses import replace
                receipt = replace(receipt, cleanup_verified=False, reason='injected cleanup uncertainty')
            return receipt
        process.stdout = SimpleNamespace(buffer=SimpleNamespace(read=delayed_read))
        if primary in ('capture', 'cleanup'):
            process.stderr = SimpleNamespace(buffer=SimpleNamespace(read=bad_read))
        process.stop_and_verify = cleanup_with_late_output
        return process
    monkeypatch.setattr(native.OwnedProcess, 'spawn', wrapped_spawn)
    analyst = manager.NativeAnalyst
    def short_timeout(*args, **kwargs):
        # Each branch must establish its declared primary failure before the
        # deliberately late provider stop. A 50 ms process deadline is itself
        # the primary condition only in the timeout branch.
        return analyst(*args, **{**kwargs, 'timeout_seconds': 0.05 if primary == 'timeout' else 5})
    monkeypatch.setattr(manager, 'NativeAnalyst', short_timeout)
    with pytest.raises(native.NativeError):
        manager.turn(state, tmp_path, {'stage': 'assign'})
    folder = Path(state['native_dir'])
    row = json.loads(next(folder.glob('call-*/receipt.json')).read_bytes())
    assert row['outcome'] == {'capture': 'failure', 'timeout': 'timeout', 'cleanup': 'cleanup_unknown'}[primary]
    assert row['provider_stop'] is None
    assert (folder / 'cancellation.json').exists()


@pytest.mark.parametrize('info,category', [
    ('serverOverloaded', 'provider_capacity'),
    ({'httpConnectionFailed': {'httpStatusCode': 503}}, 'provider_failure'),
    ('internalServerError', 'provider_failure'), ('other', 'provider_failure')])
def test_codex_manager_temporary_stop_retries_original_session(tmp_path, monkeypatch, info, category):
    from foundry_runtime import _code_index
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    monkeypatch.setattr(manager, 'resolve_executable', lambda _: Path(sys.executable))
    monkeypatch.setattr(_code_index, 'prepare', lambda _, **kw: None)
    real_spawn, requests = native.OwnedProcess.spawn, []
    sid = '00000000-0000-0000-0000-000000000001'
    def spawn(argv, **kwargs):
        stream = kwargs['stdin']
        envelope = json.loads(stream.read().rsplit(b'\n\n', 1)[-1]); stream.seek(0)
        requests.append((envelope, argv))
        rows = [{'type': 'thread.started', 'thread_id': sid}, {'type': 'turn.started'}]
        if len(requests) == 1:
            rows.append({'type': 'turn.failed', 'error': {'message': 'fixture failure', 'codexErrorInfo': info}})
        else:
            decision = {'action': 'delegate', 'instruction': envelope['packet'].get('operator_answer', 'original')}
            rows.extend([{'type': 'item.completed', 'item': {'id': 'final', 'type': 'agent_message', 'text': json.dumps(decision)}},
                         {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'cached_input_tokens': 0, 'output_tokens': 5}}])
        script = 'import sys;sys.stdin.buffer.read();sys.stdout.write(' + repr('\n'.join(map(json.dumps, rows))+'\n') + ')'
        return real_spawn([sys.executable, '-B', '-c', script], **kwargs)
    monkeypatch.setattr(native.OwnedProcess, 'spawn', spawn)
    state = {'backend': 'codex', 'model': None, 'effort': None, 'last_call_id': None, 'native_dir': str(tmp_path/'manager')}
    with pytest.raises(native.NativeError, match=category):
        manager.turn(state, tmp_path, {'stage': 'assign'})
    result, _, _, _ = manager.turn(state, tmp_path, {'stage': 'assign', 'operator_answer': 'new'})
    assert result['instruction'] == 'new' and len(requests) == 3
    assert all(argv[argv.index('resume') + 1] == sid for _, argv in requests[1:])
    assert bool(list((tmp_path/'manager').glob('cancellation*.json'))) == (category == 'provider_failure')
