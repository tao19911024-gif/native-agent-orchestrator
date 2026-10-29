"""New staffing keeps implementation, coordination and review in their owners."""
import json
from pathlib import Path
import sys

import pytest

from foundry_runtime import cli, foundry_entry, foundry_goal as goal
from foundry_runtime import _compute_selection as compute, _foundry_goal_transitions as transitions
from foundry_runtime._model_catalogue import model
from test_installed_runtime import external, offline, git, Analyst
from test_project_default_provider import adapter_for
from test_manager_input_wait import manager_io


@pytest.mark.parametrize('adapter_mode', [False, True])
@pytest.mark.parametrize('flags', [[], ['--provider', 'claude'],
    ['--coding-model', 'claude-sonnet-5-5', '--coding-effort', 'high']])
def test_project_staffing_resolves_sonnet_and_cross_provider_review(external, tmp_path, capsys,
                                                                  adapter_mode, flags):
    _, scope, order, _ = external
    defaults = {'default_provider': 'claude', 'default_staffing': {
        'compute_profile': 'claude', 'manager_profile': 'opus',
        'team_profile': 'adaptive', 'review_profile': 'opus-sol'}}
    adapter = adapter_for(scope, tmp_path, **defaults) if adapter_mode else None
    if not adapter_mode:
        scope.path.write_text(json.dumps({**scope.project, **defaults}))
    args = [] if adapter_mode else ['--project-config', str(scope.path)]
    assert cli.main([*args, '--order', str(order), '--foundry-goal', '--task-kind', 'cross-module',
                     '--print-staffing', *flags], project_adapter=adapter) == 0
    row = next(line for line in capsys.readouterr().err.splitlines() if line.startswith('FOUNDRY COMPUTE: '))
    selected = json.loads(row.removeprefix('FOUNDRY COMPUTE: '))
    assert selected['roles']['coding']['backend'] == 'claude'
    assert selected['roles']['coding']['model'] == model('sonnet')
    assert selected['roles']['correction']['model'] == model('sonnet')
    assert selected['manager_profile'] == 'opus'
    assert [(r['backend'], r['model']) for r in selected['reviewers']] == [
        ('claude', model('opus')), ('codex', model('sol'))]
    assert selected['review_source_detail']['model'] == 'project'
    assert selected['team']['member_roles']['worker']['model'] == model('sonnet')


def test_explicit_claude_model_selects_claude_execution(external, capsys):
    _, scope, order, _ = external
    assert cli.main(['--project-config', str(scope.path), '--order', str(order), '--foundry-goal',
        '--coding-model', 'claude-sonnet-5-5', '--coding-effort', 'high',
        '--review-profile', 'opus-astra', '--print-staffing']) == 0
    row = next(line for line in capsys.readouterr().err.splitlines() if line.startswith('FOUNDRY COMPUTE: '))
    selected = json.loads(row.removeprefix('FOUNDRY COMPUTE: '))
    assert selected['profile'] == 'claude'
    assert selected['roles']['coding']['backend'] == 'claude'
    assert selected['team']['backend'] == 'claude'


def test_automatic_reselection_preserves_independent_opus_slot():
    settings = [{'backend': backend, 'model': model(tier), 'effort': 'high', 'executable': sys.executable}
                for backend, tier in [('claude', 'opus'), ('codex', 'sol')]]
    chosen = transitions.select_reviewers(settings, [model('sol')], {}, None,
                                          resolve=lambda _: Path(sys.executable))
    assert chosen[0] == settings[0]
    assert chosen[1]['model'] == model('astra')


def test_new_adaptive_claude_profile_uses_sonnet():
    options, assignment = compute.resolve_goal_profile('claude', coding_backend='claude',
        backend='claude', team_profile='adaptive', task_kind='cross-module')
    assert options['coding_model'] == model('sonnet')
    assert options['auto_amend_model'] == model('sonnet')
    assert assignment['profile_version'] >= 6


@pytest.mark.parametrize('adapter_mode', [False, True])
def test_project_compute_profile_precedes_ui_kind(external, tmp_path, capsys, adapter_mode):
    _, scope, order, _ = external
    defaults = {'default_staffing': {'compute_profile': 'sol-astra'}}
    adapter = adapter_for(scope, tmp_path, **defaults) if adapter_mode else None
    if not adapter_mode:
        scope.path.write_text(json.dumps({**scope.project, **defaults}))
    args = [] if adapter_mode else ['--project-config', str(scope.path)]
    assert cli.main([*args, '--order', str(order), '--foundry-goal', '--task-kind', 'ui-live',
                     '--print-staffing'], project_adapter=adapter) == 0
    row = next(line for line in capsys.readouterr().err.splitlines() if line.startswith('FOUNDRY COMPUTE: '))
    selected = json.loads(row.removeprefix('FOUNDRY COMPUTE: '))
    assert selected['profile'] == 'sol-astra'
    assert selected['roles']['coding']['backend'] == 'codex'
    assert selected['roles']['coding']['source'] == 'project'


def test_review_refusal_publishes_reason_without_native_calls(external, tmp_path, monkeypatch):
    tree, scope, order, _ = external
    base = git(tree, 'rev-parse', 'HEAD')
    (tree / 'candidate.txt').write_text('candidate')
    git(tree, 'add', '.'); git(tree, 'commit', '-qm', 'candidate')
    head = git(tree, 'rev-parse', 'HEAD')
    folder = tmp_path / 'review'
    monkeypatch.setattr(goal, 'NativeAnalyst', lambda *a, **k: pytest.fail('must not call a model'))
    # Admit the frozen packet, then encounter an unavailable independent slot.
    # A direct packet isolates the review boundary from standalone admission.
    checkpoint = {'run_id': 'fixture', 'worktree': str(tree), 'order': goal._fact(order),
        'context': {'candidate_commit': head, 'base_commit': base, 'project_record': scope.record()}, 'coding': {'returncode': 0},
        'coding_usage': {}, 'implementation_evidence': {'implementation_models': [model('sonnet')]},
        'model_independence': 'observed_editing_participants_excluded',
        'reviewers': [{'backend': 'claude', 'model': model('sonnet'), 'effort': 'high',
                       'executable': sys.executable}] * 2}
    folder.mkdir()
    goal._write(folder / 'manifest.json', {'task_staffing': {'review_source_detail': {'model': 'explicit'}}})
    with pytest.raises(goal.GoalBlocked, match='Reviewer model matches'):
        goal._finish_review(checkpoint, folder)
    outcome = json.loads((folder / 'outcome.json').read_bytes())
    assert outcome['status'] == 'review_blocked'
    assert 'Reviewer model matches' in outcome['reason']
    assert outcome['reviews'] == []


def test_sol_profile_follows_catalogue_upgrade(monkeypatch):
    monkeypatch.setattr(compute, 'model', lambda tier: 'gpt-6.2-sol' if tier == 'sol' else model(tier))
    assert compute.review_assignments('opus-sol')[1]['model'] == 'gpt-6.2-sol'


@pytest.mark.parametrize('explicit,quota', [(False, True), (True, True), (False, False), (False, 'both')])
def test_quota_upgrade_keeps_completed_review_and_original_refusal(external, tmp_path, monkeypatch,
                                                                  explicit, quota):
    from foundry_runtime.foundry_native import NativeError
    tree, scope, order, _ = external
    base = git(tree, 'rev-parse', 'HEAD')
    (tree / 'candidate.txt').write_text('candidate')
    git(tree, 'add', '.'); git(tree, 'commit', '-qm', 'candidate')
    head = git(tree, 'rev-parse', 'HEAD')
    calls = []
    class Reviewer(Analyst):
        def __init__(self, executable, folder, **options):
            super().__init__(); self.folder, self.model = folder, options['model']
        def __call__(self, request):
            calls.append(self.model)
            if self.model == model('astra') and quota == 'both':
                raise NativeError('fixture upgraded refusal')
            if self.model == model('sol'):
                evidence = self.folder / 'failed-call'; evidence.mkdir(parents=True)
                goal._write(evidence / 'receipt.json', {'outcome': 'provider_limit' if quota else 'protocol_failure',
                    'cleanup': {'cleanup_verified': True}})
                raise NativeError('fixture measured refusal', evidence=evidence)
            return super().__call__(request)
    monkeypatch.setattr(goal, 'NativeAnalyst', Reviewer)
    monkeypatch.setattr(goal, 'resolve_executable', lambda _: Path(sys.executable))
    monkeypatch.setattr(transitions, 'resolve_executable', lambda _: Path(sys.executable))
    folder = tmp_path / 'review'
    folder.mkdir()
    goal._write(folder / 'manifest.json', {'task_staffing': {
        'review_source_detail': {'model': 'explicit' if explicit else 'project'}}})
    checkpoint = {'run_id': 'fixture', 'worktree': str(tree), 'order': goal._fact(order),
        'context': {'candidate_commit': head, 'base_commit': base, 'project_record': scope.record()}, 'coding': {'returncode': 0},
        'coding_usage': {}, 'compute_assignment': {'profile_version': 6},
        'reviewers': [{**row, 'executable': sys.executable} for row in compute.review_assignments('opus-sol')]}
    if explicit or not quota or quota == 'both':
        with pytest.raises(NativeError, match='fixture .*refusal'):
            goal._finish_review(checkpoint, folder)
        if quota == 'both':
            assert calls.count(model('astra')) == 1
            assert len(json.loads((folder / 'outcome.json').read_bytes())['review_tier_decisions']) == 1
        else:
            assert model('astra') not in calls
    else:
        result = goal._finish_review(checkpoint, folder)
        assert result['status'] == 'candidate_verified'
        assert calls.count(model('opus')) == calls.count(model('sol')) == calls.count(model('astra')) == 1
        decision, = result['review_tier_decisions']
        assert decision['signals'] == ['provider_limit']
        assert json.loads(goal.goal_usage._reference(decision['receipt']))['outcome'] == 'provider_limit'


@pytest.mark.parametrize('source', ['project', 'explicit'])
@pytest.mark.parametrize('signal', ['finding', 'assertion'])
def test_repeated_review_finding_upgrades_only_automatic_codex_slot(tmp_path, monkeypatch, source, signal):
    from foundry_runtime import _foundry_goal_auto as auto
    order = tmp_path / 'TASK_fixture.md'; order.write_text('fixture')
    calls = []
    finding = {'severity': 'P1', 'file': 'writer.py', 'summary': 'same ownership failure'}
    def run(order, folder, executable, **options):
        calls.append(options)
        folder.mkdir()
        result = {'status': 'review_blocked' if len(calls) <= 2 else 'candidate_verified',
            'candidate_commit': str(len(calls)), 'coding_session': 'fixture',
            'reviews': [{'verdict': 'BLOCK', 'findings': [finding] if signal == 'finding' else []}]}
        if signal == 'assertion':
            result['review_execution_evidence'] = {'review-1': {'test_failures': [{
                'session_id': str(len(calls)), 'call_id': 'focused-test', 'test_id': 'test_writer.py::test_balance',
                'model': options['reviewers'][1]['model']}]}}
        goal._write(folder / 'manifest.json', {'requested_compute': {'coding': {'model': 'custom-worker'}},
                                              'reviewers': options['reviewers']})
        goal._write(folder / 'outcome.json', result)
        return result
    monkeypatch.setattr(goal, '_run', run)
    result = auto.run_owned(goal, order, tmp_path / 'goal', sys.executable,
        {'model': 'custom-worker', 'effort': 'high'},
        {'coding_launcher': 'claude', 'coding_model': 'custom-worker',
         'compute_assignment': {'profile_version': 6}, 'reviewers': compute.review_assignments('opus-sol'),
         'task_staffing': {'review_source_detail': {'model': source}}})
    assert len(calls) == 3
    assert calls[0]['reviewers'][1]['model'] == calls[1]['reviewers'][1]['model'] == model('sol')
    assert calls[2]['reviewers'][0]['model'] == model('opus')
    assert calls[2]['reviewers'][1]['model'] == model('astra' if source == 'project' else 'sol')
    if source == 'project':
        assert result['review_tier_decisions'][0]['signals'] == ([
            'finding:writer.py:same ownership failure'] if signal == 'finding' else ['test:test_writer.py::test_balance'])
    else:
        assert 'review_tier_decisions' not in result


def test_review_assertion_observation_uses_existing_receipt_and_request_identity(tmp_path):
    from foundry_runtime import _foundry_goal_usage as usage
    folder = tmp_path / 'call'; folder.mkdir()
    goal._write(folder / 'receipt.json', {'call_id': 'request', 'attempt_id': 'native-attempt',
        'tool_events': [{'id': 'command-1', 'type': 'command_execution',
                         'aggregated_output': 'FAILED test_writer.py::test_balance - AssertionError: balance'}]})
    snapshot = {'calls': [{'backend': 'codex', 'reviewer_slot': 'review-1', 'call_id': 'request',
                          'evidence': str(folder), 'requested_compute': {'model': model('sol')}}]}
    evidence = usage.review_execution_evidence(snapshot)['review-1']
    assert evidence['test_failures'] == [{'call_id': 'command-1', 'test_id': 'test_writer.py::test_balance',
                                        'model': model('sol'), 'session_id': 'native-attempt'}]
    assert json.loads(usage._reference(evidence['receipts'][0]))['attempt_id'] == 'native-attempt'
    snapshot['calls'][0]['call_id'] = 'different-request'
    assert usage.review_execution_evidence(snapshot) == {}


def test_quota_transfer_survives_correction_and_is_reported_once(tmp_path, monkeypatch):
    from foundry_runtime import _foundry_goal_auto as auto
    order = tmp_path / 'TASK_fixture.md'; order.write_text('fixture')
    calls = []
    decision = {'slot': 'review-1', 'from_model': model('sol'), 'model': model('astra'),
                'effort': 'high', 'action': 'escalate', 'signals': ['provider_limit'],
                'receipt': {'path': 'retained-native-refusal', 'sha256': 'a' * 64}}
    def run(order, folder, executable, **options):
        calls.append(options)
        folder.mkdir()
        final = len(calls) == 2
        result = {'status': 'candidate_verified' if final else 'review_blocked',
            'candidate_commit': str(len(calls)), 'coding_session': 'fixture',
            'reviews': [{'verdict': 'PASS' if final else 'BLOCK', 'findings': []}]}
        manifest = {'reviewers': options['reviewers']}
        if not final:
            manifest.update(review_transfers={'review-1': [{**options['reviewers'][1], 'model': model('astra')}]},
                            review_tier_decisions=[decision])
            result['review_tier_decisions'] = [decision]
        goal._write(folder / 'manifest.json', manifest)
        goal._write(folder / 'outcome.json', result)
        return result
    monkeypatch.setattr(goal, '_run', run)
    result = auto.run_owned(goal, order, tmp_path / 'goal', sys.executable,
        {'model': 'custom-worker', 'effort': 'high'},
        {'coding_launcher': 'claude', 'coding_model': 'custom-worker',
         'compute_assignment': {'profile_version': 6}, 'reviewers': compute.review_assignments('opus-sol'),
         'task_staffing': {'review_source_detail': {'model': 'project'}}})
    assert calls[1]['reviewers'][1]['model'] == model('astra')
    assert result['review_tier_decisions'] == [{'after_round': 0, **decision}]
    # Recovery finalization may receive the same active-round decision again.
    root = tmp_path / 'goal'
    manifest = json.loads((root / 'manifest.json').read_bytes())
    recovered = auto.finish(goal, root, manifest, {**result, 'review_tier_decisions': [decision]})
    assert recovered['review_tier_decisions'] == result['review_tier_decisions']
    # The same reasoning can recur after an intervening successful reset.
    repeated = {**decision, 'signals': ['finding:writer.py:recurring ownership']}
    manifest['review_tier_decisions'] = [{'after_round': 1, **repeated}, {'after_round': 4, **repeated}]
    recovered = auto.finish(goal, root, manifest, {**result, 'review_tier_decisions': [repeated]})
    assert [row['after_round'] for row in recovered['review_tier_decisions']] == [1, 4]


@pytest.mark.parametrize('writer', ['sonnet', 'opus'])
def test_manager_worker_review_boundary_excludes_actual_coding_participants(external, manager_io,
                                                                          tmp_path, monkeypatch, writer):
    from foundry_runtime import _foundry_dispatch as dispatch, _foundry_goal_usage as usage
    from foundry_runtime import _foundry_goal_execution as execution
    tree, scope, order, workers = external
    requests, decisions = manager_io
    decisions.extend([{'action': 'delegate', 'instruction': 'Implement and commit the fixture'},
                      {'action': 'review', 'instruction': 'Review the committed candidate'}])
    original = dispatch.coding_argv
    def coding(*args, **kwargs):
        argv = original(*args, **kwargs)
        argv[-1] += "; import subprocess; Path('candidate.txt').write_text('done'); subprocess.run(['git','add','candidate.txt'],check=True); subprocess.run(['git','commit','-qm','candidate'],check=True)"
        return argv
    reviews = []
    class Reviewer(Analyst):
        def __init__(self, *args, **kwargs):
            super().__init__(); self.model = kwargs['model']
        def __call__(self, request):
            reviews.append(self.model)
            return super().__call__(request)
    monkeypatch.setattr(dispatch, 'coding_argv', coding)
    monkeypatch.setattr(goal, 'NativeAnalyst', Reviewer)
    monkeypatch.setattr(goal, 'resolve_executable', lambda _: Path(sys.executable))
    monkeypatch.setattr(usage, 'capture', lambda *a: {'work_observation': {
        'models': [model(writer)], 'participant_models': [model(writer)]}})
    state = {'backend': 'claude', 'model': model('opus'), 'effort': 'high',
             'native_dir': str(tmp_path / 'manager'), 'last_call_id': None}
    folder = tmp_path / 'goal'
    result = execution.run_owned(order, folder, Path(sys.executable), coding_launcher='exec',
        coding_model=model(writer), coding_effort='high', project_record=scope.record(), manager_state=state,
        compute_assignment={'profile_version': 6, 'selection': {'source': 'kind'}},
        task_staffing={'review_source_detail': {'model': 'kind', 'backend': 'kind'}},
        reviewers=[{**row, 'executable': sys.executable} for row in compute.review_assignments('opus-sol')])
    assert result['status'] == 'candidate_verified'
    assert len(workers) == 1 and len(requests) == 2
    assert set(reviews) == {model('opus' if writer == 'sonnet' else 'astra'), model('sol')}
    assert model(writer) in result['implementation_evidence']['review_exclusion_models']
    if writer == 'sonnet':
        assert model('opus') not in result['implementation_evidence']['review_exclusion_models']
    assert json.loads((folder / 'manifest.json').read_bytes())['coordination_models'] == [model('opus')]
