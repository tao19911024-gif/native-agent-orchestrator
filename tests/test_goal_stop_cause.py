import json

import pytest

from foundry_runtime._foundry_goal_delivery import STOP_OUTCOMES, retained_stop_cause, stop_cause


def app_server_terminal(status='failed', info='serverOverloaded', expiration=None):
    # Shape of AXBFG1 dispatch_resumed4.terminal.json (2026-10-05).
    error = None if status != 'failed' else {
        'message': 'Selected model is at capacity. Please try a different model.',
        'codexErrorInfo': info, 'additionalDetails': None, 'misalignment': None}
    return {'schema_version': 'pdcl-app-server-terminal-1',
            'terminal': {'method': 'turn/completed', 'params': {
                'threadId': 'thread', 'turn': {'id': 'turn', 'items': [], 'status': status, 'error': error}}},
            'returncode': 1 if status == 'failed' else 0, 'returncode_source': 'turn_status',
            'native_returncode': None, 'expiration_cause': expiration}


@pytest.mark.parametrize('info, cause', [('serverOverloaded', 'provider_capacity'),
                                          ('usageLimitExceeded', 'provider_limit'),
                                          ('rateLimitExceeded', 'provider_limit')])
def test_failed_app_server_turn_with_provider_cause_is_continuable(info, cause):
    assert stop_cause(app_server_terminal(info=info)) == cause
    assert cause in STOP_OUTCOMES


@pytest.mark.parametrize('info', ['badRequest', 'internalServerError', 'contextWindowExceeded', 'other', None,
                                  {'httpConnectionFailed': {'httpStatusCode': 503}}])
def test_other_failed_app_server_turns_retain_generic_failure_without_guessing_quota(info):
    assert stop_cause(app_server_terminal(info=info)) == 'provider_error'


def test_completed_app_server_turn_has_no_stop_cause():
    assert stop_cause(app_server_terminal(status='completed')) is None


def test_recorded_app_server_expiration_wins():
    assert stop_cause(app_server_terminal(expiration='input_required')) == 'input_required'


def test_retained_cause_reads_the_last_terminal_record(tmp_path):
    first, last = tmp_path / 'dispatch.terminal.json', tmp_path / 'dispatch_resumed1.terminal.json'
    first.write_text(json.dumps(app_server_terminal(status='completed')), encoding='utf-8')
    last.write_text(json.dumps(app_server_terminal()), encoding='utf-8')
    evidence = [{'path': str(first)}, {'path': str(tmp_path / 'dispatch.log')}, {'path': str(last)}]
    assert retained_stop_cause(evidence) == 'provider_capacity'
