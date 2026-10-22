import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from foundry_runtime._member_activity import CodexMemberActivity
from foundry_runtime import _followup
from foundry_runtime.app_server_transport import AppServer
from test_installed_runtime import external, offline


def event(ident='spawn', status='running', *, child='member', tool='spawnAgent', parent='parent'):
    return {'method': 'item/completed', 'params': {'threadId': parent, 'turnId': 'turn',
        'item': {'type': 'collabAgentToolCall', 'id': ident, 'tool': tool,
                 'agentsStates': {child: {'status': status}}}}}


def observe(monitor, row, now):
    monitor.observe(row, now, thread_id='parent', turn_id='turn')


def test_repeated_wait_is_not_progress_and_completion_clears_member():
    m = CodexMemberActivity(10)
    observe(m, event(), 0)
    observe(m, event('wait1', tool='wait'), 9)
    assert m.warnings(9) == []
    assert m.warnings(11)[0]['member'] == 'member'
    observe(m, event('wait2', tool='wait'), 12)
    assert m.warnings(25) == []
    observe(m, event('finish', 'completed', tool='wait'), 26)
    assert m.warnings(100) == [] and m.members == {}


def test_child_progress_and_followup_reset_warning_without_duplicate_event_reset():
    m = CodexMemberActivity(10)
    observe(m, event(), 0)
    assert m.warnings(11)
    observe(m, {'method': 'item/agentMessage/delta', 'params': {'threadId': 'member', 'delta': 'working'}}, 12)
    assert m.warnings(15) == []
    assert m.warnings(23)
    observe(m, event('follow', tool='followupTask'), 24)
    observe(m, event('follow', tool='followupTask'), 33)
    assert m.warnings(35)


@pytest.mark.parametrize('row', [None, {'threadId': []}, {'threadId': 'unrelated'},
    {'threadId': 'parent', 'turnId': 'old', 'item': event()['params']['item']}])
def test_unknown_or_foreign_events_do_not_create_members(row):
    m = CodexMemberActivity(1)
    observe(m, {'method': 'item/completed', 'params': row}, 0)
    assert m.warnings(100) == []


def test_actual_app_server_observation_uses_member_projection_without_changing_terminal():
    app = AppServer(SimpleNamespace(stdout=io.StringIO()), io.StringIO(), deadline=None)
    app.thread_id, app.turn_id = 'parent', 'turn'
    app._notification(event(), observed_at=0)
    assert app.member_activity.warnings(301)
    app._notification(event('finish', 'completed'), observed_at=302)
    assert app.member_activity.warnings(700) == []
    assert not app.completed.is_set()  # Member completion is not the parent terminal.


def test_followup_event_identity_origin_and_pending_are_read_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('CODEX_THREAD_ID', 'origin-chat')
    manifest = {'run_id': 'run1', 'tag': 'TASK1', 'originating_session': _followup.origin()}
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    _followup.announce(tmp_path, manifest)
    announcement = json.loads(capsys.readouterr().out.split(' ', 1)[1])
    assert announcement['originating_session']['thread_id'] == 'origin-chat'
    assert announcement['delivery_status'] == 'automatic_delivery_unavailable'
    assert _followup.status(tmp_path)['status'] == 'pending'
    outcome = {'status': 'coding_incomplete', 'reason': '额度等待', 'next_action': 'resume original task'}
    (tmp_path / 'outcome.json').write_text(json.dumps(outcome), encoding='utf-8')
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    first = _followup.status(tmp_path)
    assert first['outcome_status'] == 'coding_incomplete' and first['reason'] == '额度等待'
    assert first['event_id'] == _followup.status(tmp_path)['event_id']
    assert first['delivery_status'] == 'automatic_delivery_unavailable'
    assert before == {p: p.read_bytes() for p in tmp_path.iterdir()}
    outcome['status'] = 'project_handoff'
    (tmp_path / 'outcome.json').write_text(json.dumps(outcome))
    assert first['event_id'] != _followup.status(tmp_path)['event_id']


def test_followup_rejects_malformed_outcome_instead_of_notifying_success(tmp_path):
    (tmp_path / 'manifest.json').write_text('{"run_id":"run1"}')
    (tmp_path / 'outcome.json').write_text('{"partial":')
    with pytest.raises(ValueError):
        _followup.status(tmp_path)


def test_member_warning_reaches_existing_log_and_stderr_once(capsys):
    log = io.StringIO()
    app = AppServer(SimpleNamespace(stdout=io.StringIO()), log, deadline=None)
    app.thread_id, app.turn_id = 'parent', 'turn'
    app._notification(event(), observed_at=0)
    app.member_activity.publish(app, 301)
    app.member_activity.publish(app, 302)
    assert log.getvalue().count('member_no_transcript_progress') == 1
    assert capsys.readouterr().err.count('member_no_transcript_progress') == 1
    app._terminal = {'status': 'completed'}
    app.member_activity.members['member']['reported'] = False
    app.member_activity.publish(app, 999)
    assert log.getvalue().count('member_no_transcript_progress') == 1


def test_auto_amend_announces_outer_goal_and_cli_reads_that_result(external, tmp_path, monkeypatch, capsys):
    from foundry_runtime import foundry_goal as goal, cli
    monkeypatch.setenv('CODEX_THREAD_ID', 'origin-chat')
    tree, scope, order, launches = external
    folder = tmp_path / 'auto-goal'
    result = goal.run_goal(order, folder, Path(sys.executable), coding_launcher='exec',
        project_record=scope.record(), auto_amend_model='fixture-model', auto_amend_effort='high')
    announcements = [json.loads(line.split(' ', 1)[1]) for line in capsys.readouterr().out.splitlines()
                     if line.startswith('FOUNDRY_FOLLOW_UP ')]
    assert len(announcements) == 1
    assert announcements[0]['goal'] == str(folder.resolve())
    parent = json.loads((folder / 'manifest.json').read_bytes())
    child = json.loads((folder / 'round-0' / 'manifest.json').read_bytes())
    assert child['follow_up_goal'] == str(folder.resolve())
    assert announcements[0]['run_id'] == parent['run_id'] != child['run_id']
    assert cli.main(['--foundry-project', 'status', '--status-goal', str(folder)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['run_id'] == parent['run_id'] and report['outcome_status'] == result['status']
    assert len(launches) == 1
    _followup.announce(folder / 'round-0', child)
    assert json.loads(capsys.readouterr().out.split(' ', 1)[1])['run_id'] == parent['run_id']


def test_historical_auto_goal_observation_does_not_rewrite_manifest(tmp_path):
    manifest = {'mode': 'auto-amend', 'order': {'path': 'old-order', 'sha256': 'hash'},
                'state': 'interrupted', 'error': 'historical error'}
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    first = _followup.status(tmp_path)
    assert first['status'] == 'pending' and first['run_id'].startswith('legacy-')
    assert json.loads(path.read_bytes()) == manifest
    manifest.update(state='completed', rounds=[{'outcome': 'finished'}])
    path.write_text(json.dumps(manifest))
    assert first['run_id'] == _followup.status(tmp_path)['run_id']


@pytest.mark.parametrize('manifest', ['null', '[]', '"text"'])
def test_cli_malformed_manifest_returns_inspection_failure(tmp_path, capsys, manifest):
    from foundry_runtime import cli
    (tmp_path / 'manifest.json').write_text(manifest)
    assert cli.main(['--foundry-project', 'status', '--status-goal', str(tmp_path)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report['status'] == 'inspection_failed' and report['read_only']
