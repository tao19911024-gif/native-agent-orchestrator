"""Status joins exact current execution identities without modifying task records."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from foundry_runtime import _dispatch_package as package, _followup as followup
from test_unfinished_task_usage import save


@pytest.fixture
def observed(tmp_path, monkeypatch):
    folder = tmp_path / 'goal'
    folder.mkdir()
    dispatches = tmp_path / 'dispatches'
    dispatches.mkdir()
    manifest = {'schema_version': 1, 'run_id': 'goal-run', 'tag': 'TASK1',
        'order': {'path': 'order.md'}, 'manager_phase': 'dispatch_pending',
        'dispatch_evidence_root': str(dispatches), 'runtime_provenance': {'version': 'controller-version'}}
    save(folder / 'manifest.json', manifest)
    records = {}
    monkeypatch.setattr(package, 'read_intent', lambda row: (tmp_path / 'journal', records[row['intent_id']]))
    from foundry_runtime import _project_status, _provider_activity, _executor_lifecycle, _followup_host
    monkeypatch.setattr(_project_status, '_liveness', lambda record: {'owner': 'alive', 'root': 'alive'})
    monkeypatch.setattr(_provider_activity, 'execution_activity', lambda *a, **k: {'state': 'working'})
    monkeypatch.setattr(_provider_activity, 'observe', lambda *a, **k: {'status': 'available'})
    monkeypatch.setattr(_executor_lifecycle, 'observe_locked_byte', lambda *a: {'state': 'held'})
    monkeypatch.setattr(_followup_host, 'enrich', lambda report, *a, **k: report)

    def add(name='dispatch', *, run='goal-run', goal=None, attempt='one', current='one', state='active'):
        record = {'attempt_id': current, 'state': state, 'frozen': {'launcher': 'exec'}}
        records[name] = record
        path = dispatches / (name + '.package.json')
        save(path, {'intent_id': name, 'attempt_id': attempt, 'tag': 'TASK1',
            'goal': {'directory': str(goal or folder), 'run_id': run},
            'runtime_provenance': {'version': 'worker-version'}})
        return path
    return folder, manifest, add


def test_current_worker_visible_during_dispatch_pending_and_status_is_read_only(observed):
    folder, _, add = observed
    path = add()
    before = {p: p.read_bytes() for p in folder.parent.rglob('*') if p.is_file()}
    report = followup.status(folder)
    assert report['phase'] == 'dispatch_pending'
    assert report['activity_observation'] == {'state': 'working'}
    summary = report['summary']
    assert summary['waiting_for'] == 'worker'
    assert summary['next_action'] == 'wait'
    assert summary['runtime']['controller']['version'] == 'controller-version'
    assert summary['runtime']['worker']['version'] == 'worker-version'
    assert summary['evidence']['dispatch'] == str(path)
    assert report['coding_recovery'] is None
    assert {p: p.read_bytes() for p in folder.parent.rglob('*') if p.is_file()} == before


def test_summary_reports_questions_decision_and_overrun_while_worker_continues(observed, monkeypatch):
    from foundry_runtime import cli
    folder, manifest, add = observed
    path = add('dispatch_resumed3')
    messages = path.with_name('dispatch_resumed3.messages.jsonl')
    question = {'item_id': 'q1', 'text': 'FOUNDRY_MANAGER_QUESTION Which contract applies?'}
    update = {'item_id': 'm2', 'text': 'Focused check is running.'}
    messages.write_text('\n'.join(map(json.dumps, [question, update])) + '\n{"item_id":"partial"}')
    save(folder / 'manifest.json', {**manifest,
        'time_budget': {'started_at': 100, 'minutes': 1},
        'live_manager_call': {'state': 'decision_available', 'stage': 'coordinate'},
        'live_manager_decision': {'action': 'delegate', 'instruction': 'Use the original contract.',
                                  'evidence': {'path': 'manager-receipt.json'}}})
    monkeypatch.setattr(followup.time, 'time', lambda: 161)
    before = {p: p.read_bytes() for p in folder.parent.rglob('*') if p.is_file()}
    summary = followup.status(folder)['summary']
    progress = summary['progress']
    assert summary['next_action'] == 'wait'
    assert summary['time_budget']['report_due'] is True
    assert summary['time_budget']['elapsed_seconds'] == 61
    assert progress['active_worker']['launcher'] == 'exec'
    assert progress['worker_messages']['latest_question']['item_id'] == 'q1'
    assert progress['worker_messages']['latest']['item_id'] == 'm2'
    assert progress['last_manager_decision']['instruction'] == 'Use the original contract.'
    assert progress['last_manager_decision']['receipt']['path'] == 'manager-receipt.json'
    assert {p: p.read_bytes() for p in folder.parent.rglob('*') if p.is_file()} == before
    with pytest.raises(SystemExit) as error:
        cli.main(['--foundry-project', 'status', '--status-goal', str(folder), '--time-budget-minutes', '1'])
    assert error.value.code == 2


def test_claude_progress_reads_root_messages_for_the_current_session(observed):
    folder, _, add = observed
    path = add()
    record = package.read_intent(json.loads(path.read_bytes()))[1]
    record['frozen']['launcher'] = 'claude'
    record['session_id'] = 'selected'
    rows = [{'type': 'assistant', 'uuid': item_id, 'session_id': session, 'parent_tool_use_id': parent,
             'message': {'content': [{'type': 'text', 'text': text}]}}
            for item_id, session, parent, text in (
                ('root', 'selected', None, 'Focused check finished.'),
                ('member', 'selected', 'subtask', 'Member update'),
                ('other', 'other', None, 'Unrelated conversation'))]
    stream = path.with_name('dispatch.claude.jsonl')
    stream.write_text('\n'.join(map(json.dumps, rows)) + '\n')
    progress = followup.status(folder)['summary']['progress']
    assert progress['worker_messages']['path'] == str(stream)
    assert progress['worker_messages']['latest']['item_id'] == 'root'
    assert progress['last_worker_evidence_write_utc']


@pytest.mark.parametrize('mismatch', ['run', 'directory', 'attempt', 'completed'])
def test_other_goal_or_superseded_attempt_never_appears_current(observed, mismatch):
    folder, _, add = observed
    options = {'run': {'run': 'other-run'}, 'directory': {'goal': folder.parent / 'other'},
               'attempt': {'current': 'two'}, 'completed': {'state': 'completed'}}
    add(**options[mismatch])
    report = followup.status(folder)
    assert report['dispatch_observation']['executions'] == []
    assert report['summary']['runtime']['worker'] is None


@pytest.mark.parametrize('launcher,suffix', [('claude', '.claude.jsonl'), ('app-server', '.log'), ('exec', '.log')])
def test_watcher_reads_exact_current_resumed_log(observed, monkeypatch, launcher, suffix):
    folder, _, add = observed
    add('dispatch_resumed2', state='completed')
    path = add('dispatch_resumed3')
    record = package.read_intent(json.loads(path.read_bytes()))[1]
    record['frozen']['launcher'] = launcher
    log = folder.parent/'dispatches/dispatch_resumed3.log'
    expected = str(log.with_suffix(suffix).resolve())
    report = followup.status(folder)
    assert report['summary']['evidence']['worker_live_event_log'] == expected
    assert report['dispatch_observation']['executions'][0]['live_event_log'] == expected
    # The current package determines the path before its writer creates it.
    assert not Path(expected).exists()
    record['evidence'] = {'log': {'path': str(folder.parent/'recorded-worker.log')}}
    assert followup.status(folder)['summary']['evidence']['worker_live_event_log'] == str(
        (folder.parent/'recorded-worker.log').with_suffix(suffix).resolve())


def test_multiple_current_bindings_are_reported_without_selecting_latest(observed):
    folder, _, add = observed
    add('dispatch1'); add('dispatch2')
    report = followup.status(folder)
    assert report['dispatch_observation']['status'] == 'ambiguous'
    assert report['summary']['runtime']['worker'] is None
    assert report['summary']['next_action'] == 'inspect_dispatch_bindings'


def test_capacity_wait_queued_input_and_explicit_selection_are_distinct(observed, monkeypatch):
    folder, _, add = observed
    add()
    from foundry_runtime import _provider_activity, _goal_intervention
    monkeypatch.setattr(_provider_activity, 'observe', lambda *a: {'status': 'quota_blocked'})
    monkeypatch.setattr(_goal_intervention, 'observe', lambda *a: [{'status': 'queued'}, {'status': 'delivered'}])
    selection = folder.parent / 'current.json'
    save(selection, {'version': 'selected-version', 'python': 'selected-python'})
    summary = followup.status(folder, runtime_selection=selection)['summary']
    assert summary['waiting_for'] == 'provider_capacity'
    assert summary['pending_operator_inputs'] == 1
    assert summary['runtime']['selected']['version'] == 'selected-version'
    assert summary['runtime']['worker']['version'] == 'worker-version'
    assert summary['next_command_argv'][-2:] == ['--runtime-selection', str(selection)]


def test_package_publication_records_existing_goal_identity(tmp_path, monkeypatch):
    folder = tmp_path / 'goal'
    folder.mkdir()
    save(folder / 'manifest.json', {'run_id': 'run'})
    intent = SimpleNamespace(intent_id='intent', path=tmp_path / 'intent', frozen={},
        record={'attempt_id': 'attempt', 'state': 'active', 'root_pid': 123})
    monkeypatch.setattr(package, 'observe', lambda **k: {'version': 'test'})
    token = followup.goal_scope.set(str(folder))
    try:
        path = package.record_package(tmp_path / 'dispatch.log', 'TASK1', 'dispatch', intent, 'work')
    finally:
        followup.goal_scope.reset(token)
    assert json.loads(path.read_bytes())['goal'] == {'directory': str(folder), 'run_id': 'run'}


def test_feedback_continuation_preserves_recorded_environment():
    invocation = {'argv': ['recorded-python', 'dispatch.py', '--amend-from', 'outcome.json'],
                  'environment': {'PROJECT_MODE': 'review'}, 'powershell': 'recorded invocation'}
    summary = followup._summary({'goal': 'goal', 'status': 'outcome_available',
        'feedback': {'continuation': {'invocation': invocation}}})
    assert summary['next_invocation'] == invocation
    assert summary['waiting_for'] == 'operator'
    settled = followup._summary({'goal': 'goal', 'status': 'outcome_available',
        'phase': 'review_pending', 'outcome_status': 'candidate_verified',
        'feedback': {'continuation': {'invocation': invocation}}})
    assert settled['phase'] == 'candidate_verified'
    assert settled['waiting_for'] is None
    assert settled['next_action'] == 'inspect_delivery'


def test_manager_lock_preserves_worker_binding_to_active_round(observed, monkeypatch):
    from foundry_runtime import foundry_goal as goal

    round_dir, manifest, add = observed
    outer = round_dir.parent
    # A controller and its active round both own goal records. The adjacent
    # manager folder owns native receipts and has no goal manifest.
    save(outer / 'manifest.json', {'mode': 'auto-amend', 'run_id': 'outer',
        'active_round': str(round_dir), 'order': manifest['order']})
    path = add('dispatch_resumed2')
    intent = SimpleNamespace(intent_id='dispatch_resumed2', path=round_dir.parent / 'journal',
        record={'attempt_id': 'one', 'state': 'active', 'root_pid': 123}, frozen={})
    _, record = package.read_intent(json.loads(path.read_bytes()))
    record['frozen']['launcher'] = 'claude'
    def run_owned(*args, **kwargs):
        package.record_package(path.with_name('dispatch_resumed2.log'), 'TASK1', 'resume', intent, 'work')
        return followup.status(outer)['summary']

    monkeypatch.setattr(goal.goal_execution, 'run_owned', run_owned)
    with goal._run_lock(outer):
        summary = goal._run(None, round_dir, None,
            manager_state={'native_dir': str(round_dir.with_name('goal-manager'))})
    assert summary['waiting_for'] == 'worker'
    assert summary['evidence']['worker_live_event_log'] == str(path.with_name('dispatch_resumed2.claude.jsonl'))


def test_stopped_controller_with_unsettled_worker_requires_inspection(observed, monkeypatch):
    folder, _, add = observed
    add()
    from foundry_runtime import _executor_lifecycle
    monkeypatch.setattr(_executor_lifecycle, 'observe_locked_byte', lambda *a: {'state': 'released'})
    report = followup.status(folder)
    assert report['status'] == 'execution_stopped'
    assert report['summary']['next_action'] == 'inspect_retained_execution'
    assert '--recover-goal' not in report['summary']['next_command_argv']


def test_input_wait_exposes_existing_recorded_answer_invocation(observed):
    folder, manifest, _ = observed
    manifest['runtime_provenance']['python'] = 'recorded-python'
    manifest['project_record'] = {'context': {'descriptor': 'project.json'}}
    manifest['manager_phase'] = 'awaiting_input'
    save(folder / 'manifest.json', manifest)
    save(folder / 'outcome.json', {'status': 'awaiting_input'})
    summary = followup.status(folder)['summary']
    assert summary['next_action'] == 'answer_input_request'
    assert summary['waiting_for'] == 'operator'
    assert '--recover-goal' in summary['next_invocation']['argv']
    assert summary['next_invocation']['argv'][0] == 'recorded-python'


def test_cli_prints_exact_goal_summary_without_full_observations(observed, capsys):
    from foundry_runtime import cli
    folder, _, add = observed
    add()
    assert cli.main(['--foundry-project', 'status', '--status-goal', str(folder), '--summary']) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary['waiting_for'] == 'worker'
    assert 'dispatch_observation' not in summary
