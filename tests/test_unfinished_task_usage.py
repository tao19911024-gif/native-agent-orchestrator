"""Unfinished goals expose retained completed turns without claiming completion."""
import hashlib
import json

import pytest

from foundry_runtime._foundry_goal_usage import coding_usage, task_report


def save(path, value):
    raw = json.dumps(value).encode()
    path.write_bytes(raw)
    return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}


def turn(identity, tokens=100):
    return {'coverage': 'observed_native_turn', 'session_id': 'session', 'turn_id': identity,
            'response_ids': ['response-' + identity], 'response_count': 1,
            'usage': {'input_tokens': tokens, 'cached_input_tokens': 20, 'output_tokens': 10,
                      'reasoning_output_tokens': 5, 'total_tokens': tokens + 10},
            'execution': {'process_outcome': 'completed'},
            'child_agents': {'coverage': 'observed_linked_descendants', 'observations': [],
                             'missing_observations': []}}


@pytest.fixture
def pending(tmp_path):
    manifest = {'schema_version': 1, 'run_id': 'run', 'order': {'path': 'order.md'},
                'worktree': 'project', 'manager_phase': 'dispatch_pending'}
    checkpoint = {**manifest, 'coding_usage': {'managed_turns': [turn('one'), turn('two'), turn('one')]}}
    manifest['coding_checkpoint'] = save(tmp_path/'coding-checkpoint.json', checkpoint)
    save(tmp_path/'manifest.json', manifest)
    return tmp_path, manifest, checkpoint


def test_completed_turns_visible_before_final_outcome_and_deduplicated(pending):
    folder, manifest, _ = pending
    before = {p: p.read_bytes() for p in folder.iterdir()}
    report = task_report([folder, folder/'manifest.json'])
    assert report['coding']['observed_turn_count'] == 2
    assert report['coding']['provider_usage_metrics']['input_tokens']['observed_sum'] == 200
    assert report['coverage'] == 'partial'
    row = report['records'][0]
    assert row['kind'] == 'unfinished_invocation'
    assert row['usage_reference'] == manifest['coding_checkpoint']
    assert row['checkpoint_usage_observation']['manager_phase'] == 'dispatch_pending'
    assert any(g['kind'] == 'missing_outcome' for g in report['gaps'])
    assert report['actual_account_charge'] == 'unknown'
    efficiency = report['execution_efficiency']
    assert len(efficiency['member_usage']) == 2
    assert efficiency['recorded_states'][0]['manager_phase'] == 'dispatch_pending'
    assert efficiency['recorded_states'][0]['waiting_reason'] == 'not_established'
    assert all(row['cost_row_index'] < len(report['api_price_equivalent']['rows'])
               for row in efficiency['stage_costs'])
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert set(folder.iterdir()) == set(before)


@pytest.mark.parametrize('problem', ['changed_hash', 'different_run', 'missing_usage'])
def test_invalid_checkpoint_is_unknown_not_zero_or_current_progress(pending, problem):
    folder, manifest, checkpoint = pending
    if problem == 'different_run': checkpoint['run_id'] = 'another-run'
    if problem == 'missing_usage': checkpoint.pop('coding_usage')
    fact = save(folder/'coding-checkpoint.json', checkpoint)
    if problem != 'changed_hash':
        manifest['coding_checkpoint'] = fact
    else:
        (folder/'coding-checkpoint.json').write_bytes(b'{}')
    save(folder/'manifest.json', manifest)
    report = task_report([folder])
    assert report['coding']['observed_turn_count'] == 0
    assert report['coverage'] == 'partial'
    assert any(g['kind'] == 'usage' for g in report['gaps'])


def test_final_usage_precedes_checkpoint_and_corruption_is_not_hidden(pending):
    folder, _, _ = pending
    usage = {'coding': coding_usage({'managed_turns': [turn('final', 300)]})}
    save(folder/'usage.json', usage)
    report = task_report([folder])
    assert report['coding']['provider_usage_metrics']['input_tokens']['observed_sum'] == 300
    assert 'checkpoint_usage_observation' not in report['records'][0]
    (folder/'usage.json').write_bytes(b'{invalid')
    report = task_report([folder])
    assert report['coding']['observed_turn_count'] == 0
    assert any(g['kind'] == 'usage' for g in report['gaps'])


def test_conflicting_checkpoint_turns_use_existing_conflict_rules(pending):
    folder, manifest, checkpoint = pending
    checkpoint['coding_usage']['managed_turns'].append(turn('one', 500))
    manifest['coding_checkpoint'] = save(folder/'coding-checkpoint.json', checkpoint)
    save(folder/'manifest.json', manifest)
    report = task_report([folder])
    assert report['coding']['observed_turn_count'] == 1
    assert report['coding']['provider_usage_metrics']['input_tokens']['observed_sum'] == 100
    assert report['coding']['missing_observations']


def test_manifest_without_managed_checkpoint_retains_explicit_gap(tmp_path):
    save(tmp_path/'manifest.json', {'schema_version': 1, 'order': {'path': 'order.md'}})
    report = task_report([tmp_path])
    assert report['coverage'] == 'partial'
    assert report['coding']['observed_turn_count'] == 0
    assert any(g['kind'] == 'usage' for g in report['gaps'])


def test_completed_outcome_never_falls_back_to_unfinished_checkpoint(pending):
    folder, _, _ = pending
    save(folder/'outcome.json', {'status': 'candidate_verified', 'reviews': [],
                               'task_usage': {'path': str(folder/'missing-usage.json'), 'sha256': '0'*64}})
    report = task_report([folder])
    assert report['records'][0]['kind'] == 'invocation'
    assert report['coding']['observed_turn_count'] == 0
    assert report['coverage'] == 'partial'
