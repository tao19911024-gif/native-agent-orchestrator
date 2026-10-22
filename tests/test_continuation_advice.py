import json

import pytest

from foundry_runtime.foundry_entry import _print_report


@pytest.mark.parametrize('status', ['review_blocked', 'candidate_verified', 'awaiting_input'])
def test_report_separates_current_result_from_historical_source(capsys, status):
    path = "D:/任务/O'Brien/continue-123/outcome.json"
    result = {'status': status, 'continuation_evidence': "D:/任务/O'Brien/continue-123",
              'result_outcome': {'path': path, 'sha256': 'fixture'},
              'delivery': {'context': {'continues': {'path': 'old/round-0/outcome.json'}}}}
    _print_report(result)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == result
    assert path in captured.err
    assert 'historical provenance' in captured.err
    assert 'old/round-0/outcome.json' not in captured.err
    assert ('--amend-from' in captured.err) == (status != 'awaiting_input')
    if status != 'awaiting_input':
        assert "--amend-from 'D:/任务/O''Brien/continue-123/outcome.json'" in captured.err
        assert '--coding-session <existing-coding-session>' in captured.err
        assert 'not a full command' in captured.err


def test_old_report_is_not_reinterpreted(capsys):
    _print_report({'status': 'coding_incomplete', 'continuation_evidence': 'historical'})
    assert capsys.readouterr().err == ''
