"""Saved report comparison preserves native identity and cumulative semantics."""
import copy

import pytest

from foundry_runtime._foundry_goal_usage import coding_usage, delivery, task_report
from foundry_runtime._usage_summary import incremental, observations, summarize
from test_unfinished_task_usage import save, turn


def snapshot(units):
    return {'schema_version': 'pdcl-foundry-task-report-1',
            'usage_observations': {'schema_version': 1, 'units': units, 'gaps': []}}


def native_unit(identity='session', tokens=100, *, roles=None):
    return {'identity': ['claude', 'session_model', identity, 'model'], 'backend': 'claude',
        'roles': roles or ['coding'], 'cumulative': True,
        'tokens': {'input_tokens': tokens, 'uncached_input_tokens': tokens, 'cached_input_tokens': 20,
                   'cache_write_input_tokens': 0, 'output_tokens': 10, 'total_tokens': tokens + 30},
        'reading': {}, 'observed_responses': None}


def test_same_turn_is_reused_and_new_turn_counted_once():
    first, second = turn('one'), turn('two', 200)
    first['reading_signals'] = {'observed_read_outputs': 3, 'identical_call_output_repeats': 1, 'truncated_read_outputs': 1}
    old = observations(coding_usage({'managed_turns': [first]}), {}, {})
    current = observations(coding_usage({'managed_turns': [first, second, second]}), {}, {})
    result = incremental(snapshot(current['units']), snapshot(old['units']))
    assert result['reused_units'] == 1
    assert result['added_units'] == 1
    row = result['by_role_and_backend'][0]
    assert row['tokens']['uncached_input_tokens']['observed_sum'] == 180
    assert row['tokens']['total_tokens']['observed_sum'] == 210
    assert row['observed_responses']['observed_sum'] == 1
    assert row['reading']['identical_call_output_repeats']['observed_sum'] is None


def test_cumulative_session_growth_not_whole_resumed_total():
    result = incremental(snapshot([native_unit(tokens=160)]), snapshot([native_unit(tokens=100)]))
    assert result['added_units'] == 0
    assert result['changed_units'] == 1
    row = result['by_role_and_backend'][0]
    assert row['tokens']['input_tokens']['observed_sum'] == 60
    assert row['tokens']['total_tokens']['observed_sum'] == 60
    assert row['tokens']['output_tokens']['observed_sum'] == 0
    assert row['observed_responses']['observed_sum'] is None


def test_shared_claude_session_is_one_shared_role_row():
    session = {'session_id': 'session', 'ended_utc': '2026-11-04T10:00:00Z', 'models': [
        {'model': 'model', 'input_tokens': 100, 'output_tokens': 10,
         'cache_read_input_tokens': 20, 'cache_creation_input_tokens': 0,
         'thinking_tokens': None, 'list_price_usd': None}]}
    coding = {'claude_session_usage': {'sessions': [session]}}
    report = {'claude_manager_session_usage': {'sessions': [session]}}
    view = observations(coding, {}, report)
    assert len(view['units']) == 1
    summary = summarize(view)['by_role_and_backend'][0]
    assert summary['roles'] == ['coding', 'manager']
    assert summary['tokens']['total_tokens']['observed_sum'] == 130


def test_missing_cached_usage_remains_unknown():
    view = observations({}, {'review': [{'attempt_id': 'attempt',
        'usage': {'input_tokens': 100, 'output_tokens': 10, 'cached_input_tokens': None}}]}, {})
    row = summarize(view)['by_role_and_backend'][0]
    assert row['tokens']['uncached_input_tokens']['observed_sum'] is None
    assert row['tokens']['uncached_input_tokens']['unobserved_units'] == 1


@pytest.mark.parametrize('change', ['regression', 'immutable_change', 'missing_baseline_unit'])
def test_incompatible_observations_produce_gaps_instead_of_false_savings(change):
    old = native_unit()
    new = native_unit(tokens=90)
    if change == 'immutable_change':
        old['cumulative'] = new['cumulative'] = False
    if change == 'missing_baseline_unit':
        new['identity'][2] = 'different-session'
    result = incremental(snapshot([new]), snapshot([old]))
    assert result['coverage'] == 'partial'
    assert result['gaps']
    if change != 'missing_baseline_unit':
        assert result['by_role_and_backend'][0]['tokens']['input_tokens']['observed_sum'] is None


def test_saved_report_is_immutable_baseline_even_when_goal_continues(tmp_path):
    folder = tmp_path / 'goal'
    folder.mkdir()
    manifest = {'schema_version': 1, 'run_id': 'run', 'order': {'path': 'order.md'}, 'worktree': 'project'}
    checkpoint = {**manifest, 'coding_usage': {'managed_turns': [turn('one')]}}
    manifest['coding_checkpoint'] = save(folder / 'coding-checkpoint.json', checkpoint)
    save(folder / 'manifest.json', manifest)
    baseline = task_report([folder])
    baseline_file = tmp_path / 'before.json'
    save(baseline_file, baseline)
    saved_bytes = baseline_file.read_bytes()
    checkpoint['coding_usage']['managed_turns'].append(turn('two', 200))
    manifest['coding_checkpoint'] = save(folder / 'coding-checkpoint.json', checkpoint)
    save(folder / 'manifest.json', manifest)
    report = task_report([folder, folder / 'manifest.json'], since=baseline_file)
    assert report['incremental_usage']['reused_units'] == 1
    assert report['incremental_usage']['by_role_and_backend'][0]['tokens']['total_tokens']['observed_sum'] == 210
    assert baseline_file.read_bytes() == saved_bytes
    assert report['summary']['deliveries'][0]['status'] == 'no_outcome'


def test_older_report_without_identity_projection_has_actionable_error():
    with pytest.raises(ValueError, match='saved task report containing usage_observations'):
        incremental(snapshot([]), {'schema_version': 'pdcl-foundry-task-report-1'})


@pytest.mark.parametrize('acceptance', ['not_evaluated', 'accepted', 'rejected'])
def test_delivery_levels_preserve_project_acceptance(acceptance):
    outcome = {'status': 'candidate_verified', 'candidate_commit': 'head',
        'reviews': [{'verdict': 'PASS', 'candidate_commit': 'head'}],
        'project_acceptance': {'status': acceptance, 'evidence': ['project-check.json']}}
    original = copy.deepcopy(outcome)
    levels = delivery(outcome)['levels']
    assert levels['candidate_review']['status'] == 'passed'
    assert levels['project_acceptance'] == outcome['project_acceptance']
    assert outcome == original


def test_artifact_delivery_and_blocked_review_are_not_candidate_acceptance():
    assert delivery({'status': 'artifacts_delivered', 'artifacts': ['report.md']})['levels']['candidate_review']['status'] == 'not_completed'
    assert delivery({'status': 'review_blocked', 'reviews': [{'verdict': 'BLOCK'}]})['levels']['candidate_review']['status'] == 'blocked'


def test_legacy_saved_delivery_summary_retains_current_levels(tmp_path):
    from test_delivery_baseline import completed
    reference = completed(tmp_path / 'goal')
    report = task_report([reference['path']])
    levels = report['records'][0]['delivery']['levels']
    assert levels['candidate_review']['status'] == 'passed'
    assert levels['project_acceptance']['status'] == 'accepted'


def codex_session(tokens, ended):
    return {'session_id': 'shared', 'ended_utc': ended, 'call_id': ended,
            'usage': {'input_tokens': tokens, 'cached_input_tokens': 20,
                      'output_tokens': 10, 'total_tokens': tokens + 10}}


def test_codex_shared_session_uses_latest_snapshot_and_role_order_is_irrelevant():
    earlier = codex_session(100, '2026-11-04T10:00:00Z')
    later = codex_session(160, '2026-11-04T10:01:00Z')
    roles = {'manager': [earlier], 'expert': [later]}
    view = observations({}, roles, {})
    reversed_view = observations({}, dict(reversed(list(roles.items()))), {})
    assert view['units'] == reversed_view['units']
    assert len(view['units']) == 1
    row = summarize(view)['by_role_and_backend'][0]
    assert row['roles'] == ['expert', 'manager']
    assert row['tokens']['input_tokens']['observed_sum'] == 160
    baseline = observations({}, {'manager': [earlier]}, {})
    result = incremental(snapshot(view['units']), snapshot(baseline['units']))
    assert result['by_role_and_backend'][0]['tokens']['input_tokens']['observed_sum'] == 60


def test_coding_turn_overlapping_cumulative_session_is_not_added_twice():
    coding = turn('one')
    coding['session_id'] = 'shared'
    coding['reading_signals'] = {'observed_read_outputs': 3, 'identical_call_output_repeats': 1, 'truncated_read_outputs': 1}
    view = observations(coding_usage(coding), {'manager': [codex_session(160, '2026-11-04T10:01:00Z')]}, {})
    assert len(view['units']) == 1
    assert view['units'][0]['roles'] == ['coding', 'manager']
    assert summarize(view)['by_role_and_backend'][0]['tokens']['input_tokens']['observed_sum'] is None
    assert view['gaps']
    baseline = observations(coding_usage(coding), {}, {})
    result = incremental(snapshot(view['units']), snapshot(baseline['units']))
    assert result['coverage'] == 'partial'
    assert result['by_role_and_backend'][0]['tokens']['input_tokens']['observed_sum'] is None
    assert result['by_role_and_backend'][0]['reading']['observed_read_outputs']['observed_sum'] is None
    assert result['by_role_and_backend'][0]['observed_responses']['observed_sum'] is None


def test_native_attempt_increment_is_separate_from_session_and_response_counts():
    first = {'backend': 'codex', 'attempt_id': 'attempt-one', 'evidence': 'one'}
    second = {'backend': 'codex', 'attempt_id': 'attempt-two', 'evidence': 'two'}
    old = observations({}, {}, {}, attempts={'manager': {'codex': [first]}})
    new = observations({}, {}, {}, attempts={'manager': {'codex': [first, second]}})
    result = incremental({'usage_observations': new}, {'schema_version': 'pdcl-foundry-task-report-1', 'usage_observations': old})
    assert result['observed_native_invocations'] == 1
    assert result['native_invocations'] == [{'backend': 'codex', 'roles': ['manager'], 'count': 1}]


def test_cli_prints_only_usage_summary_and_uses_saved_baseline(tmp_path, capsys):
    from foundry_runtime import cli
    folder = tmp_path / 'goal'
    folder.mkdir()
    manifest = {'schema_version': 1, 'run_id': 'run', 'order': {'path': 'order.md'}, 'worktree': 'project'}
    checkpoint = {**manifest, 'coding_usage': {'managed_turns': [turn('one')]}}
    manifest['coding_checkpoint'] = save(folder / 'coding-checkpoint.json', checkpoint)
    save(folder / 'manifest.json', manifest)
    baseline = tmp_path / 'before.json'
    save(baseline, task_report([folder]))
    assert cli.main(['--foundry-usage', str(folder), '--summary']) == 0
    import json
    summary = json.loads(capsys.readouterr().out)
    assert 'records' not in summary
    assert summary['observed_native_invocations'] == 1
    assert cli.main(['--foundry-usage', str(folder), '--usage-since', str(baseline), '--summary']) == 0
    delta = json.loads(capsys.readouterr().out)
    assert delta['reused_units'] == 1
    assert delta['observed_native_invocations'] == 0
