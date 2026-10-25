"""Native reconnect progress retains its turn and healthy review sibling."""
import json
from pathlib import Path
import sys

import pytest

from foundry_runtime import foundry_native as native, _foundry_review_group as group
from test_installed_runtime import Analyst


def events(notice):
    rows = json.loads((Path(__file__).parent / 'fixtures/codex-review-events.json').read_bytes())['events']
    rows.insert(2, {'type': 'error', 'message': notice})
    return rows


@pytest.mark.parametrize('attempt', [1, 2, 4])
def test_retry_progress_does_not_end_codex_turn(attempt):
    rows = events(f'Reconnecting... {attempt}/5 (request timed out)')
    assert native.parse_native_output('\n'.join(map(json.dumps, rows)).encode(),
                                      max_bytes=10000, backend='codex') == {'verdict': 'PASS'}


@pytest.mark.parametrize('kind,message', [
    ('error', 'Reconnecting... 5/5 (request timed out)'),
    ('turn.failed', 'Reconnecting... 2/5 (request timed out)'),
    ('error', 'authentication failed'),
])
def test_terminal_errors_remain_terminal(kind, message):
    rows = events(message)
    rows[2] = ({'type': kind, 'error': {'message': message}} if kind == 'turn.failed'
               else {'type': kind, 'message': message})
    with pytest.raises(native.NativeProviderError):
        native.parse_native_output('\n'.join(map(json.dumps, rows)).encode(), max_bytes=10000, backend='codex')


@pytest.mark.parametrize('returncode', [0, 1])
def test_native_retry_completion_and_receipt_replay(tmp_path, monkeypatch, returncode):
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    rows = events('Reconnecting... 2/5 (stream disconnected before completion: tls handshake eof)')
    result = {'candidate_commit': 'a'*40, 'packet_sha256': 'b'*64, 'verdict': 'PASS', 'findings': []}
    rows[-2]['item']['text'] = json.dumps(result)
    spawn = native.OwnedProcess.spawn
    calls = []
    def local_child(argv, **kwargs):
        calls.append(argv)
        script = ('import sys;sys.stdin.buffer.read();sys.stderr.write("ERROR: Reconnecting... 2/5\\n");'
                  'sys.stdout.write(' + repr('\n'.join(map(json.dumps, rows))+'\n') + ');sys.exit(' + str(returncode) + ')')
        return spawn([sys.executable, '-B', '-c', script], **kwargs)
    monkeypatch.setattr(native.OwnedProcess, 'spawn', local_child)
    folder = tmp_path / 'native'
    analyst = native.NativeAnalyst(Path(sys.executable), folder, backend='codex', workspace=tmp_path)
    request = {'kind': 'review', 'call_id': 'c'*64, 'packet': {'candidate_commit': 'a'*40}}
    if returncode:
        with pytest.raises(native.NativeError):
            analyst(request)
    else:
        assert analyst(request) == result
        assert analyst(request) == result and len(calls) == 1
        receipt = json.loads((native._call_folder(folder, request['call_id']) / 'receipt.json').read_bytes())
        assert receipt['outcome'] == 'success' and receipt['returncode'] == 0
        before = {str(p): p.read_bytes() for p in folder.rglob('*') if p.is_file()}
        archived = native.NativeAnalyst.inspect_call(folder, request, tmp_path, {'backend': 'codex'})
        assert archived['value'] == result
        assert before == {str(p): p.read_bytes() for p in folder.rglob('*') if p.is_file()}


def test_failed_reviewer_does_not_cancel_completed_sibling(tmp_path):
    cancelled = []
    class Member(Analyst):
        def __init__(self, executable, folder, **kwargs):
            super().__init__(); self.name = folder.parent.name
        def __call__(self, request):
            if self.name == 'review-0':
                raise native.NativeProviderError('terminal fixture failure')
            return super().__call__(request)
        def cancel(self, reason):
            cancelled.append(self.name)
    requests = {name: {'kind': 'review', 'call_id': name, 'packet': {'candidate_commit': 'a'*40},
                       'packet_sha256': 'b'*64} for name in ('review-0', 'review-1')}
    panel = group.Panel([{'backend': 'codex', 'executable': sys.executable}]*2,
                        requests, Member, tmp_path / 'reviews', tmp_path)
    outcomes = group.prewarm(list(requests), requests, panel)
    assert outcomes['review-0'][1] is not None and outcomes['review-1'][1] is None
    panel.cancel('review-0 failed')
    assert cancelled == ['review-0']


@pytest.mark.parametrize('changed', [False, True])
def test_same_backend_model_transfer_restarts_original_review_after_schema_failure(tmp_path, monkeypatch, changed):
    from foundry_runtime._model_catalogue import model
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    requests = {name: {'kind': 'review', 'call_id': str(n + 1)*64, 'packet': {}}
                for n, name in enumerate(('review-0', 'review-1'))}
    folder = tmp_path / 'reviews'
    slot = folder / 'review-0'; slot.mkdir(parents=True)
    (slot / 'native-manifest.json').write_text(json.dumps({'backend': 'codex',
        'requested_compute': {'model': model('astra'), 'effort': 'high'}}))
    call = native._call_folder(slot, requests['review-0']['call_id']); call.mkdir()
    (call / 'receipt.json').write_text('{}')
    class Reader:
        @staticmethod
        def inspect_call(*args):
            return {'value': None, 'schema_error': 'missing verdict', 'persistent': True,
                    'receipt': {'path': str(call / 'receipt.json'), 'sha256': 'a'*64}, 'snapshot': {'calls': []}}
        @staticmethod
        def review_repair_request(request, error, *, source=None):
            return {**request, 'call_id': 'd'*64, **({'continue_from': str(source)} if source else {})}
    specs = [{'backend': 'codex', 'model': model('astra'), 'effort': 'high', 'executable': sys.executable}]*2
    transfers = {'review-0': [{**specs[0], 'model': model('luna')}]} if changed else {}
    panel = group.Panel(specs, requests, Reader, folder, tmp_path, resume=True, transfers=transfers)
    assert len(panel.history['review-0']) == 1
    if changed:
        assert panel.requests['review-0'] == requests['review-0']
    else:
        assert panel.requests['review-0']['continue_from'] == str(slot)
