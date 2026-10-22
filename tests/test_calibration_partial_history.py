"""Partial predecessor evidence must not discard usable completed task history."""
import copy
from pathlib import Path

import pytest

from foundry_runtime import _foundry_team_calibration as calibration
from foundry_runtime import _compute_selection as compute


@pytest.fixture
def mixed_report(monkeypatch):
    def settled(name, status, verdict, findings):
        return {'reference': {'path': name, 'sha256': 'a'*64}, 'kind': 'invocation',
                'delivery': {'status': status, 'reviews': [{'verdict': verdict, 'finding_count': findings}],
                             'context': {'mode': 'amendment'}, 'host_findings': []}}
    first = settled('first/outcome.json', 'review_blocked', 'BLOCK', 2)
    last = settled('last/outcome.json', 'candidate_verified', 'PASS', 0)
    unfinished = {'reference': {'path': 'original/manifest.json', 'sha256': 'b'*64},
                  'kind': 'unfinished_invocation', 'delivery': {'status': 'no_outcome', 'error': None}}
    report = {'selected_outcomes': [last['reference']], 'records': [last, first, unfinished],
              'coding_model_usage': [], 'coverage': 'partial', 'gaps': [{'kind': 'missing_outcome'}],
              'reviews_by_backend': {}, 'coding': {}, 'links': []}
    monkeypatch.setattr(calibration, 'task_report', lambda _: report)
    return report


def sample(report):
    return calibration._sample(Path('last/outcome.json'), {'order': {'path': 'TASK.md'}},
                               {'path': 'last/manifest.json'}, 'Original task')


@pytest.mark.parametrize('unfinished_kind', ['unfinished_invocation', 'unfinished_automatic_root'])
def test_partial_chain_keeps_settled_findings_and_unknown_predecessor(mixed_report, unfinished_kind):
    mixed_report['records'][-1]['kind'] = unfinished_kind
    original = copy.deepcopy(mixed_report)
    result = sample(mixed_report)
    assert result['round_count'] == 2
    assert result['findings_recorded'] == 2
    assert result['blocked_rounds'] == 1
    assert result['final_review_verdicts'] == ['PASS']
    assert result['coverage'] == 'partial' and result['gap_count'] == 1
    assert result['unfinished_records'] == [{'reference': mixed_report['records'][-1]['reference'],
                                            'kind': unfinished_kind, 'status': 'no_outcome'}]
    assert mixed_report == original


def test_invalid_settled_review_is_not_silently_treated_as_empty(mixed_report):
    del mixed_report['records'][0]['delivery']['reviews']
    with pytest.raises(KeyError, match='reviews'):
        sample(mixed_report)


def test_compact_team_feedback_keeps_partial_evidence(mixed_report, monkeypatch):
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    result = sample(mixed_report)
    team = compute.resolve_native_team('adaptive')
    team['calibration'] = {'selection': 'fixture', 'sample_size': 6, 'available_orders': 1,
        'samples': [result], 'scope': 'observational', 'evidence_root': 'retained', 'gaps': []}
    rendered = compute.team_prompt('task', team)
    assert 'original/manifest.json' in rendered
    assert 'unfinished_records' in rendered and 'no_outcome' in rendered
    assert 'partial' in rendered
