"""Archived reviews retain their read-only reader across native generations."""
import json
from pathlib import Path
import sys

import pytest

from foundry_runtime import foundry_native as native


@pytest.mark.parametrize('schema', [6, 7])
@pytest.mark.parametrize('failed_receipt', [False, True])
def test_archived_completed_review_is_read_without_new_admission(tmp_path, monkeypatch, schema, failed_receipt):
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    result = {'candidate_commit': 'a'*40, 'packet_sha256': 'b'*64, 'verdict': 'PASS', 'findings': []}
    rows = [
        {'type': 'system', 'subtype': 'init', 'tools': ['StructuredOutput'], 'mcp_servers': [],
         'skills': [], 'plugins': [], 'model': 'fixture', 'session_id': 'review-session'},
        {'type': 'assistant', 'session_id': 'review-session', 'message': {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'name': 'StructuredOutput', 'id': 'f1', 'input': result}]}},
        {'type': 'user', 'session_id': 'review-session', 'message': {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'f1', 'content': 'Structured output provided successfully'}]}},
        {'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 2,
         'result': json.dumps(result), 'structured_output': result,
         'usage': {'input_tokens': 10, 'output_tokens': 5, 'iterations': []}, 'total_cost_usd': 0.0},
    ]
    captured = json.loads((Path(__file__).parent / 'fixtures/claude-2.1.296-result.json').read_bytes())['event']
    rows[-1] = {**captured, 'session_id': 'review-session'}
    spawn = native.OwnedProcess.spawn
    def local_child(argv, **kwargs):
        script = 'import sys;sys.stdin.buffer.read();sys.stdout.write(' + repr('\n'.join(map(json.dumps, rows))+'\n') + ')'
        return spawn([sys.executable, '-B', '-c', script], **kwargs)
    monkeypatch.setattr(native.OwnedProcess, 'spawn', local_child)
    folder = tmp_path/'review'
    analyst = native.NativeAnalyst(Path(sys.executable), folder, backend='claude', workspace=tmp_path)
    request = {'kind': 'review', 'call_id': 'c'*64, 'packet': {'candidate_commit': 'a'*40}}
    assert analyst(request) == result
    # The archived layout is common to schema 6 and 7; schema 6 is a historical
    # reader fixture, not an authorization to run new calls under old identity.
    policy_path = folder/'native-manifest.json'
    policy = json.loads(policy_path.read_bytes()); policy['schema_version'] = schema
    policy_path.write_text(json.dumps(policy), encoding='utf-8')
    if failed_receipt:
        receipt_path = native._call_folder(folder, request['call_id']) / 'receipt.json'
        receipt = json.loads(receipt_path.read_bytes())
        receipt.update(outcome='protocol_failure', error='historical accounting rejection',
                       returncode=None, usage=None, session_id=None, model=None, list_price_usd=None)
        receipt_path.write_text(json.dumps(receipt), encoding='utf-8')
    before = {str(p): p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    monkeypatch.setattr(native.OwnedProcess, 'spawn', lambda *a, **k: pytest.fail('archived review spawned a process'))
    archived = native.NativeAnalyst.inspect_call(folder, request, tmp_path, {'backend': 'claude'})
    assert archived['value'] == result and archived['session'] == 'review-session'
    if failed_receipt:
        recovered = archived['snapshot']['calls'][0]
        assert recovered['outcome'] == 'protocol_failure'
        assert recovered['recovered_verdict']['usage']['iterations'] == []
    assert before == {str(p): p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    if failed_receipt:
        receipt['session_id'] = 'different-session'
        receipt_path.write_text(json.dumps(receipt), encoding='utf-8')
        assert native.NativeAnalyst.inspect_call(folder, request, tmp_path, {'backend': 'claude'})['value'] is None


@pytest.mark.parametrize('usage', [None, {}, {'input_tokens': 'unknown', 'output_tokens': -1}])
def test_codex_valid_verdict_survives_accounting_changes(usage):
    result = {'verdict': 'PASS'}
    rows = json.loads((Path(__file__).parent / 'fixtures/codex-review-events.json').read_bytes())['events']
    rows[-1]['usage'] = usage
    raw = '\n'.join(map(json.dumps, rows)).encode()
    assert native.parse_native_output(raw, max_bytes=10000, backend='codex') == result
    with pytest.raises(native.NativeError):
        native.parse_native_output('\n'.join(map(json.dumps, rows[:-1])).encode(), max_bytes=10000, backend='codex')


@pytest.mark.parametrize('cost', [10**400, 'unknown', -1, None])
def test_accounting_observer_never_discards_completed_claude_result(cost):
    from foundry_runtime.foundry_native_support import accounting_observations
    from foundry_runtime.foundry_claude import validate_result_header
    row = {'subtype': 'success', 'is_error': False, 'result': '{}', 'total_cost_usd': cost,
           'num_turns': None, 'usage': {'input_tokens': 1, 'output_tokens': 2}}
    validate_result_header(row, native.NativeError, native.NativeError)
    assert any('num_turns' in note for note in accounting_observations(row, 'claude'))
