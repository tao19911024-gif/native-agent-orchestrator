"""Review exclusions follow observed coding participants, not configured roles."""
import hashlib
import json

from foundry_runtime import _compute_selection as compute, _context_signals as signals
from foundry_runtime import _foundry_goal_usage as usage


def test_coding_stream_records_acknowledged_team_models(tmp_path):
    path = tmp_path / 'coding.jsonl'
    rows = [
        {'message': {'model': 'claude-opus-5-5', 'content': [
            {'type': 'tool_use', 'id': 'one', 'name': 'Agent', 'input': {'model': 'sonnet'}},
            {'type': 'tool_use', 'id': 'two', 'name': 'Agent', 'input': {'model': 'haiku'}}]}},
        {'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'one', 'content': 'agent started'},
            {'type': 'tool_result', 'tool_use_id': 'two', 'is_error': True, 'content': 'not started'}]}},
    ]
    path.write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
    result = signals.observe_work(path)
    assert result['models'] == []
    assert set(result['participant_models']) == {'claude-opus-5-5', 'sonnet'}


def test_review_excludes_observed_lead_and_members_without_claiming_edits():
    result = compute.execution_evidence({'managed_turns': [{
        'models': [{'model': 'claude-opus-5-5'}],
        'work_observation': {'models': ['claude-sonnet-5-5'], 'participant_models': ['sonnet']},
        'child_agents': {'observations': [{'model_observation': {'declarations': [{'model': 'gpt-6-astra'}]},
                                          'reading_signals': {'implementation_calls': 0}}]},
        'native_team': {'member_roles': {'expert': {'model': 'unused'}}}}]})
    assert result['implementation_models'] == ['claude-sonnet-5-5']
    assert set(result['review_exclusion_models']) == {'claude-opus-5-5', 'claude-sonnet-5-5', 'sonnet', 'gpt-6-astra'}


def test_legacy_participation_reconciles_only_hash_bound_native_stream(tmp_path):
    path = tmp_path / 'coding.jsonl'
    path.write_text(json.dumps({'message': {'model': 'claude-sonnet-5-5', 'content': []}}))
    ref = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    coding = {'backend': 'claude', 'work_observation': {'models': []}, 'source': {'stdout': ref}}
    result = usage.review_participation(coding, {'review_exclusion_models': ['retained-author']})
    assert set(result['review_exclusion_models']) == {'retained-author', 'claude-sonnet-5-5'}
    path.write_text(json.dumps({'message': {'model': 'unbound-model', 'content': []}}))
    result = usage.review_participation(coding, {})
    assert 'unbound-model' not in result['review_exclusion_models']
    assert result['gaps']
