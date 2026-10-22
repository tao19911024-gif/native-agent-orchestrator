"""Execution completion and host registration are independent observable facts."""
import json
from pathlib import Path

import pytest

from foundry_runtime import cli, _followup
from foundry_runtime._followup_host import MARKER, binding
from test_installed_runtime import external, offline


@pytest.fixture
def host(tmp_path, monkeypatch):
    home = tmp_path / 'host'
    home.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(home))
    monkeypatch.setenv('CODEX_THREAD_ID', 'chat')
    return home


@pytest.fixture
def goal(host, tmp_path):
    folder = tmp_path / '中文任务'; folder.mkdir()
    (folder / 'manifest.json').write_text(json.dumps({
        'run_id': 'run', 'originating_session': _followup.origin()}), encoding='utf-8')
    return folder


def save(host, request, *, ident='fixture', prompt=None, thread=None, state='ACTIVE'):
    folder = host / 'automations' / ident; folder.mkdir(parents=True, exist_ok=True)
    data = {'id': ident, 'kind': 'heartbeat', 'name': request['name'],
            'prompt': prompt if prompt is not None else request['prompt'],
            'target_thread_id': thread or request['targetThreadId'],
            'status': state, 'rrule': request['rrule']}
    (folder / 'automation.toml').write_text('\n'.join(
        key + ' = ' + json.dumps(value, ensure_ascii=False) for key, value in data.items()), encoding='utf-8')


def test_completed_goal_without_registration_is_visible_and_has_ready_request(goal, host):
    (goal / 'outcome.json').write_text('{"status":"project_handoff"}')
    report = _followup.status(goal, plan=True)
    assert report['status'] == 'outcome_available'
    assert report['host_follow_up']['status'] == 'unregistered'
    request = report['registration_request']
    assert request['targetThreadId'] == 'chat'
    assert request['kind'] == 'heartbeat' and request['status'] == 'ACTIVE'
    assert MARKER + json.dumps(binding(report), ensure_ascii=True, sort_keys=True) in request['prompt']
    save(host, request)
    observed = _followup.status(goal)
    assert observed['host_follow_up']['status'] == 'registered'
    assert observed['host_follow_up']['delivery'] == 'not_observed'
    assert 'registration_request' not in observed  # Compact repeated checks.


def test_paused_registration_is_preserved_and_is_not_delivery(goal, host):
    report = _followup.status(goal, plan=True)
    save(host, report['registration_request'], state='PAUSED')
    observed = _followup.status(goal, plan=True)
    assert observed['host_follow_up']['status'] == 'paused'
    assert observed['registration_action'] == 'preserve_paused'
    assert observed['host_follow_up']['delivery'] == 'not_observed'


@pytest.mark.parametrize('wrong', ['thread', 'run', 'path'])
def test_other_task_or_chat_registration_does_not_cover_this_goal(goal, host, wrong):
    request = _followup.status(goal, plan=True)['registration_request']
    bound = binding(_followup.status(goal))
    if wrong == 'run': bound['run_id'] = 'other'
    if wrong == 'path': bound['goal'] += '-other'
    save(host, request, prompt=MARKER + json.dumps(bound), thread='other' if wrong == 'thread' else None)
    assert _followup.status(goal)['host_follow_up']['status'] == 'unregistered'


def test_legacy_registration_is_adopted_without_duplicate_or_cadence_change(goal, host):
    request = _followup.status(goal, plan=True)['registration_request']
    request['rrule'] = 'FREQ=MINUTELY;INTERVAL=30'
    save(host, request, prompt=f'Observe run at {goal.as_posix()}')
    report = _followup.status(goal, plan=True)
    assert report['host_follow_up']['status'] == 'legacy_candidate'
    assert report['registration_action'] == 'inspect_and_update_existing'
    assert report['host_follow_up']['automations'][0]['rrule'].endswith('30')


def test_duplicate_and_unreadable_registry_are_not_missing_registration(goal, host):
    request = _followup.status(goal, plan=True)['registration_request']
    save(host, request)
    save(host, request, ident='second')
    assert _followup.status(goal)['host_follow_up']['status'] == 'ambiguous'
    for path in (host / 'automations').glob('*/automation.toml'):
        path.write_text('bad toml')
    report = _followup.status(goal, plan=True)
    assert report['host_follow_up']['status'] == 'unavailable'
    assert report['registration_action'] == 'inspect_registry_error'


def test_status_does_not_mutate_host_or_execution_files(goal, host):
    save(host, _followup.status(goal, plan=True)['registration_request'])
    before = {p: p.read_bytes() for p in goal.parent.rglob('*') if p.is_file()}
    _followup.status(goal, plan=True)
    assert before == {p: p.read_bytes() for p in goal.parent.rglob('*') if p.is_file()}


def test_bad_registry_field_cannot_abort_launch_announcement(goal, host, capsys):
    request = _followup.status(goal, plan=True)['registration_request']
    save(host, request)
    path = host / 'automations' / 'fixture' / 'automation.toml'
    path.write_text(path.read_text(encoding='utf-8').replace('status = "ACTIVE"', 'status = 1'), encoding='utf-8')
    _followup.announce(goal, json.loads((goal / 'manifest.json').read_bytes()))
    notice = json.loads(capsys.readouterr().out.split(' ', 1)[1])
    assert notice['host_follow_up']['status'] == 'unavailable'
    assert 'registration_request' not in notice and 'registration_action' not in notice


def test_cli_plan_and_dispatch_announcement_use_same_registration_contract(external, host, capsys):
    from foundry_runtime import _foundry_dispatch as dispatch
    _, project, order, _ = external
    rc, log, _ = dispatch.dispatch_order(order, project=project)
    notice = next(json.loads(line.split(' ', 1)[1]) for line in capsys.readouterr().out.splitlines()
                  if line.startswith('FOUNDRY_FOLLOW_UP '))
    assert rc == 0 and 'registration_request' not in notice
    save(host, _followup.dispatch_status(log.with_suffix('.package.json'), plan=True)['registration_request'])
    assert cli.main(['--foundry-project', 'status', '--status-dispatch',
                     str(log.with_suffix('.package.json')), '--follow-up-plan']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['registration_action'] == 'reuse_existing'
    assert report['host_follow_up']['status'] == 'registered'
    assert binding(notice) == binding(report)


def test_missing_origin_is_not_inferred_from_observing_chat(tmp_path, host):
    (tmp_path / 'manifest.json').write_text('{"run_id":"old"}')
    report = _followup.status(tmp_path, plan=True)
    assert report['host_follow_up']['status'] == 'origin_unavailable'
    assert report['registration_request'] is None


def test_deleted_subscription_is_not_recreated_by_announcement_or_status(goal, host, capsys):
    request = _followup.status(goal, plan=True)['registration_request']
    save(host, request)
    path = host / 'automations/fixture/automation.toml'
    path.unlink()
    (goal / 'outcome.json').write_text('{"status":"awaiting_input"}')
    for _ in range(2):
        _followup.announce(goal, json.loads((goal / 'manifest.json').read_bytes()))
        notice = json.loads(capsys.readouterr().out.split(' ', 1)[1])
        assert 'registration_request' not in notice
        assert notice['host_follow_up']['automatic_registration'] is False
        assert notice['host_follow_up']['push_status'] == 'receiver_not_available'
        assert notice['delivery_status'] == 'automatic_delivery_unavailable'
    first, second = _followup.status(goal), _followup.status(goal)
    assert first['event_id'] == second['event_id']
    assert first['host_follow_up']['status'] == 'unregistered'
    assert first['host_follow_up']['delivery'] == 'not_observed'
    assert not path.exists()


@pytest.mark.parametrize('outcome', ['candidate_verified', 'coding_incomplete', 'awaiting_input'])
def test_offline_host_preserves_terminal_event_without_claiming_delivery(goal, host, outcome):
    (goal / 'outcome.json').write_text(json.dumps({'status': outcome}))
    before = {p: p.read_bytes() for p in goal.rglob('*') if p.is_file()}
    report = _followup.status(goal)
    event = report['event_id']
    request = _followup.status(goal, plan=True)['registration_request']
    save(host, request)  # Fixture-only host return; this is not actual chat delivery.
    registered = _followup.status(goal)
    assert registered['event_id'] == event
    assert registered['host_follow_up']['mechanism'] == 'heartbeat_polling'
    assert registered['host_follow_up']['push_status'] == 'receiver_not_available'
    assert registered['host_follow_up']['delivery'] == 'not_observed'
    assert before == {p: p.read_bytes() for p in goal.rglob('*') if p.is_file()}
