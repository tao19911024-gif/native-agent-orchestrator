"""Service custody and execution evidence remain separate from host notifications."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from foundry_runtime import _hosted as hosted, _windows_host as windows, _followup
from test_installed_runtime import external, offline


@pytest.fixture
def service(monkeypatch):
    calls = []
    def call(operation, request):
        calls.append((operation, request))
        return {'exists': True, 'state': 4}
    monkeypatch.setattr(windows, 'call', call)
    monkeypatch.setenv('CODEX_THREAD_ID', 'origin-chat')
    return calls


def test_submission_is_once_even_after_repeated_identical_request(tmp_path, service):
    first = hosted.submit(tmp_path, ['--prompt', '中文 "命令"'])
    second = hosted.submit(tmp_path, ['--prompt', '中文 "命令"'])
    assert [op for op, _ in service].count('register') == 1
    assert first['run_id'] == second['run_id']
    assert 'registration_request' not in first
    assert first['originating_session']['thread_id'] == 'origin-chat'
    assert '--hosted-status' in first['status_argv']
    with pytest.raises(ValueError, match='different invocation'):
        hosted.submit(tmp_path, ['--prompt', 'different'])


def test_single_call_compaction_setting_survives_service_handoff(tmp_path, service, monkeypatch):
    monkeypatch.setenv('FOUNDRY_COMPACT_TOKENS', '240000')
    hosted.submit(tmp_path, ['--help'])
    request = hosted.read(tmp_path / 'request.json')
    assert request['environment']['FOUNDRY_COMPACT_TOKENS'] == '240000'
    monkeypatch.setenv('FOUNDRY_COMPACT_TOKENS', '120000')
    assert hosted.read(tmp_path / 'request.json') == request
    assert [op for op, _ in service].count('register') == 1


def test_uncertain_service_response_never_resubmits(tmp_path, service, monkeypatch):
    def failed(*args):
        raise subprocess.TimeoutExpired('service', 30)
    monkeypatch.setattr(windows, 'call', failed)
    report = hosted.submit(tmp_path, ['--help'])
    assert report['status'] == 'reconciliation_required'
    assert hosted.read(tmp_path / 'submission.json')['outcome'] == 'unconfirmed'
    monkeypatch.setattr(windows, 'call', lambda op, r: service.append(op) or {'exists': False})
    assert hosted.submit(tmp_path, ['--help'])['status'] == 'reconciliation_required'
    assert 'register' not in service


def test_early_cli_failure_has_terminal_receipt_and_stable_event(tmp_path, service):
    hosted.submit(tmp_path, ['--bad'])
    hosted.write(tmp_path / 'finished.json', {'returncode': 2})
    report = hosted.status(tmp_path)
    assert report['status'] == 'launch_finished'
    assert report['completion']['returncode'] == 2
    assert report['event_id'] == hosted.status(tmp_path)['event_id']


def test_goal_receipt_is_projection_not_another_notification(tmp_path, service, capsys):
    directory = tmp_path / 'launch'
    hosted.submit(directory, ['--help'])
    goal = tmp_path / 'goal'; goal.mkdir()
    hosted.write(goal / 'manifest.json', {'run_id': 'goal-run'})
    hosted.write(goal / 'outcome.json', {'status': 'project_handoff'})
    token = hosted.active_directory.set(directory)
    try:
        _followup._announce({'goal': str(goal), 'run_id': 'goal-run'})
    finally:
        hosted.active_directory.reset(token)
    assert capsys.readouterr().out == ''
    report = hosted.status(directory, plan=True)
    assert report['status'] == 'outcome_available'
    assert report['execution']['run_id'] == 'goal-run'
    assert report['feedback'] == report['execution']['feedback']
    assert report['feedback']['artifacts']['outcome']['path'] == str(goal / 'outcome.json')
    assert report['run_id'] != 'goal-run'
    assert '"hosted":' in report['registration_request']['prompt']


def test_status_projects_dead_controller_without_claiming_cleanup(tmp_path, service, monkeypatch):
    from foundry_runtime import _executor_lifecycle
    hosted.submit(tmp_path, ['--help'])
    hosted.write(tmp_path / 'started.json', {'pid': 123, 'creation_time': 456})
    monkeypatch.setattr(_executor_lifecycle, 'process_identity_exited', lambda *a: True)
    report = hosted.status(tmp_path)
    assert report['status'] == 'execution_stopped'
    assert 'cleanup is unconfirmed' in report['reason']


def test_environment_does_not_copy_host_pipe_or_api_key(tmp_path, service, monkeypatch):
    monkeypatch.setenv('CODEX_APP_TOOLS_PIPE_PATH', 'desktop-private-pipe')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'fixture-not-real')
    hosted.submit(tmp_path, ['--help'])
    env = hosted.read(tmp_path / 'request.json')['environment']
    assert 'CODEX_APP_TOOLS_PIPE_PATH' not in env and 'ANTHROPIC_API_KEY' not in env
    assert env['CODEX_THREAD_ID'] == 'origin-chat'


def test_hosted_status_cli_does_not_require_a_project(tmp_path, service, capsys):
    from foundry_runtime.cli import main
    hosted.submit(tmp_path, ['--help'])
    assert main(['--hosted-status', str(tmp_path), '--follow-up-plan']) == 0
    assert json.loads(capsys.readouterr().out)['hosted'] == str(tmp_path)


def test_wrapper_temporary_preflight_values_are_frozen(tmp_path, service):
    source = tmp_path / 'temporary.json'
    source.write_text('{"expectHeadSha":"fixture", "中文":true}', encoding='utf-8')
    directory = tmp_path / 'launch'
    hosted.submit(directory, ['--project-preflight-input', str(source)])
    source.unlink()
    assert hosted.read(directory / 'request.json')['inputs']['--project-preflight-input']['中文'] is True


@pytest.mark.parametrize('mode', ['argument', 'wrapper_environment'])
def test_descriptor_cli_uses_same_hosting_before_executor(external, tmp_path, service, monkeypatch, mode):
    from foundry_runtime.cli import main
    _, scope, order, launches = external
    directory = tmp_path / 'launch'
    argv = ['--project-config', str(scope.path), '--order', str(order)]
    if mode == 'argument':
        argv += ['--hosted=' + str(directory)]
    else:
        monkeypatch.setenv('FOUNDRY_HOSTED_DIRECTORY', str(directory))
    assert main(argv) == 0
    request = hosted.read(directory / 'request.json')
    assert request['entry'] is None
    assert '--order' in request['argv'] and not any(a.startswith('--hosted') for a in request['argv'])
    assert 'FOUNDRY_HOSTED_DIRECTORY' not in request['environment']
    assert launches == []


def test_terminal_receipt_readable_when_scheduler_is_unavailable(tmp_path, service, monkeypatch):
    hosted.submit(tmp_path, ['--help'])
    hosted.write(tmp_path / 'finished.json', {'returncode': 0})
    def failed(*args):
        raise OSError('service unavailable')
    monkeypatch.setattr(windows, 'call', failed)
    report = hosted.status(tmp_path)
    assert report['status'] == 'launch_finished'
    assert report['scheduler']['error'] == 'service unavailable'


def test_hosted_goal_capture_does_not_require_codex_origin(tmp_path, service):
    token = hosted.active_directory.set(tmp_path)
    try:
        _followup.announce(tmp_path / 'goal', {'run_id': 'fixture'})
    finally:
        hosted.active_directory.reset(token)
    assert hosted.read(tmp_path / 'execution.json')['run_id'] == 'fixture'
    assert 'delivery_status' not in hosted.read(tmp_path / 'execution.json')


def test_hosted_event_binds_launch_and_distinguishes_abnormal_exit(tmp_path, service, monkeypatch):
    from foundry_runtime import _executor_lifecycle
    goal = tmp_path / 'goal'; goal.mkdir()
    hosted.write(goal / 'manifest.json', {'run_id': 'inner'})
    hosted.write(goal / 'outcome.json', {'status': 'awaiting_input'})
    events = []
    for name in ('first', 'second'):
        directory = tmp_path / name
        hosted.submit(directory, ['--help'])
        hosted.write(directory / 'execution.json', {'goal': str(goal), 'run_id': 'inner'})
        report = hosted.status(directory)
        assert report['status'] == 'outcome_available'
        events.append(report['event_id'])
        assert hosted.status(directory)['event_id'] == report['event_id']
    assert events[0] != events[1]
    hosted.write(directory / 'started.json', {'pid': 123, 'creation_time': 456})
    monkeypatch.setattr(_executor_lifecycle, 'process_identity_exited', lambda *a: True)
    stopped = hosted.status(directory)
    assert stopped['status'] == 'execution_stopped' and stopped['event_id'] != events[1]
    assert hosted.status(directory)['event_id'] == stopped['event_id']
    assert stopped['host_follow_up']['delivery'] == 'not_observed'
    hosted.write(directory / 'finished.json', {'returncode': 0})
    finished = hosted.status(directory)
    assert finished['status'] == 'outcome_available'
    assert finished['event_id'] == events[1]  # Publishing controller exit does not duplicate the outcome.
