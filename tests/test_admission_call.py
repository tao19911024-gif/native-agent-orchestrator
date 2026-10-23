"""Admission transport failures preserve evidence without deciding project verdicts."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from foundry_runtime import _admission_call as admission, _task_deadline as deadline
from foundry_runtime._provider_stop import reported_stop

CAPACITY = {'type': 'turn.failed', 'error': {
    'message': 'Selected model is at capacity. Please try a different model.'}}


@pytest.fixture
def run(tmp_path, monkeypatch):
    waits = []
    monkeypatch.setattr(admission, 'sleep', waits.append)
    def invoke(runner):
        return admission.run(['native', '--model', 'selected'], evidence=tmp_path,
                             output_path=tmp_path / 'result.json', runner=runner,
                             input='unchanged instructions', timeout=900)
    return invoke, waits


@pytest.mark.parametrize('failure', [CAPACITY, {'type': 'result', 'is_error': True,
    'result': 'overloaded', 'api_error_status': 529},
    {'type': 'turn.failed', 'error': {'message': 'stream disconnected before completion: connection reset'}}])
def test_transient_failure_then_verdict_keeps_inputs_and_attempts(run, tmp_path, failure):
    invoke, waits = run
    calls = []
    def native(argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        if len(calls) == 1:
            (tmp_path / 'result.json').write_text('stale partial output')
            return SimpleNamespace(returncode=1, stdout=json.dumps(failure), stderr='first error')
        assert not (tmp_path / 'result.json').exists()
        return SimpleNamespace(returncode=0, stdout='completed', stderr='')
    assert invoke(native).returncode == 0
    assert len(calls) == 2 and calls[0] == calls[1]
    assert waits == [60]
    assert json.loads((tmp_path / 'attempt-1/native.jsonl').read_text()) == failure
    assert (tmp_path / 'attempt-1/result.json').read_text() == 'stale partial output'
    assert (tmp_path / 'native.jsonl').read_text() == 'completed'
    assert not (tmp_path / 'admission-failure.json').exists()


def test_timeout_exhausts_bound_and_keeps_partial_bytes(run, tmp_path):
    invoke, waits = run
    calls = []
    def native(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, 900, output=b'partial\xff', stderr=b'timeout trace')
    with pytest.raises(admission.AdmissionUnavailable, match='admission judgement unavailable: timeout'):
        invoke(native)
    assert len(calls) == 3 and waits == [60, 300]
    failure = json.loads((tmp_path / 'admission-failure.json').read_bytes())
    assert failure['reason'] == 'timeout' and len(failure['attempts']) == 3
    for i in range(1, 4):
        assert (tmp_path / f'attempt-{i}/native.jsonl').read_text(encoding='utf-8') == 'partial\ufffd'
        assert (tmp_path / f'attempt-{i}/native.stderr.txt').read_text() == 'timeout trace'


@pytest.mark.parametrize('verdict', ['PASS', 'REFUSE'])
def test_completed_verdict_returns_once(run, verdict):
    invoke, waits = run
    calls = []
    def native(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps({'verdict': verdict}), stderr='')
    assert json.loads(invoke(native).stdout)['verdict'] == verdict
    assert len(calls) == 1 and waits == []


@pytest.mark.parametrize('event,reason', [
    ({'type': 'turn.failed', 'error': {'message': 'invalid credentials'}}, 'native_error'),
    ({'type': 'turn.failed', 'error': {'codexErrorInfo': 'usageLimitExceeded'}}, 'provider_limit'),
    ({'type': 'tool', 'message': CAPACITY['error']['message']}, 'native_exit')])
def test_other_failures_are_not_retried(run, event, reason):
    invoke, waits = run
    calls = []
    def native(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1, stdout=json.dumps(event), stderr='')
    with pytest.raises(admission.AdmissionUnavailable, match=reason):
        invoke(native)
    assert len(calls) == 1 and not waits


def test_missing_executable_is_not_retried(run):
    invoke, waits = run
    def native(*args, **kwargs):
        raise FileNotFoundError('missing native executable')
    with pytest.raises(admission.AdmissionUnavailable, match='native_start'):
        invoke(native)
    assert not waits


def test_deadline_caps_call_and_prevents_retry(run, monkeypatch):
    invoke, waits = run
    monkeypatch.setattr(deadline.time, 'time', lambda: 100)
    def native(argv, **kwargs):
        assert kwargs['timeout'] == 5
        return SimpleNamespace(returncode=1, stdout=json.dumps(CAPACITY), stderr='')
    with deadline.scope(105), pytest.raises(admission.AdmissionUnavailable, match='task_deadline'):
        invoke(native)
    assert not waits


def test_capacity_phrase_requires_error_event():
    assert reported_stop(CAPACITY)['category'] == 'provider_capacity'
    assert reported_stop({'message': CAPACITY['error']['message']}) is None
    assert reported_stop({'type': 'turn.failed', 'error': {
        **CAPACITY['error'], 'codexErrorInfo': 'badRequest'}}) is None


@pytest.mark.parametrize('terminal', [{'type': 'turn.completed'},
    {'type': 'result', 'is_error': False, 'subtype': 'success'}])
def test_native_recovery_before_success_does_not_repeat_judgement(run, terminal):
    invoke, waits = run
    transcript = json.dumps(CAPACITY) + '\n' + json.dumps(terminal)
    result = invoke(lambda *a, **kw: SimpleNamespace(returncode=0, stdout=transcript, stderr=''))
    assert result.stdout == transcript and not waits


def test_real_timeout_preserves_output_after_process_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(admission, 'RETRY_DELAYS', ())
    with pytest.raises(admission.AdmissionUnavailable, match='timeout'):
        admission.run([sys.executable, '-u', '-c', 'import time; print("partial", flush=True); time.sleep(30)'],
                      evidence=tmp_path, output_path=tmp_path / 'result.json',
                      timeout=1, capture_output=True, text=True)
    assert (tmp_path / 'native.jsonl').read_text().strip() == 'partial'
