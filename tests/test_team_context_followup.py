import json
from pathlib import Path
import pytest

from foundry_runtime import _launch_context as launch, _tool_discipline as hook
from foundry_runtime import _compute_selection as compute, foundry_goal as goal
from foundry_runtime._execution_usage import _model_observation, _time
from test_installed_runtime import external, offline


@pytest.mark.parametrize('role,child', [('expert', False), ('executor', True)])
def test_expert_uses_existing_meter_and_direct_evidence_escape(tmp_path, role, child):
    source = tmp_path/'source.py'
    source.write_text('line\n'*200)
    event = {'tool_name': 'Read', 'tool_input': {'file_path': str(source)}, 'cwd': str(tmp_path)}
    if child:
        event.update(agent_id='member', agent_type='expert')
    policy = {'version': 6, 'root_role': role, 'reading_budget': launch.READING_DEFAULTS['claude']}
    assert 'Expert reading budget' in hook.check(event, policy)['hookSpecificOutput']['permissionDecisionReason']
    event['tool_input']['description'] = '#decide: verify the conflicting ownership branch'
    assert hook.check(event, policy) == {}
    event['tool_input'].pop('description')
    assert hook.check(event, {**policy, 'version': 5}) == {}
    event.update(agent_id='member', agent_type='reviewer')
    assert hook.check(event, policy) == {}


def test_frozen_compaction_and_operator_tuning(monkeypatch):
    old = {'settings': {'context_tokens': 300000, 'compact_percent': 90, 'bash_chars': 12000, 'tool_tokens': 3000}, 'server': None}
    assert launch.claude_settings(old)['env']['CLAUDE_CODE_AUTO_COMPACT_WINDOW'] == '300000'
    assert not any('model_auto_compact_token_limit' in arg for arg in launch.codex_settings(launch=old))
    monkeypatch.setenv('FOUNDRY_COMPACT_TOKENS', '100000')
    new = {**old, 'settings': launch.settings()}
    assert 'model_auto_compact_token_limit=100000' in launch.codex_settings(launch=new)
    assert int(launch.claude_settings(new)['env']['CLAUDE_CODE_AUTO_COMPACT_WINDOW'])*90//100 == 100000


def test_dynamic_claude_aliases_follow_frozen_team(monkeypatch):
    from foundry_runtime._dispatch_claude import argv
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    team = compute.resolve_native_team('adaptive', backend='claude')
    team['member_roles']['investigator']['model'] = 'claude-haiku-99'
    team['member_roles']['mechanical']['model'] = 'claude-haiku-99'
    frozen = {'settings': launch.settings(), 'server': None}
    args = argv('claude', 'test', native_team=team, launch_context=frozen)
    config = json.loads(args[args.index('--settings')+1])
    assert config['env']['ANTHROPIC_DEFAULT_HAIKU_MODEL'] == 'claude-haiku-99'
    team['version'] = 14
    args = argv('claude', 'test', native_team=team, launch_context=frozen)
    assert 'ANTHROPIC_DEFAULT_HAIKU_MODEL' not in json.loads(args[args.index('--settings')+1])['env']


def test_codex_expert_has_no_claude_dependency(monkeypatch):
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    team = compute.resolve_native_team('adaptive', backend='codex')
    assert team['member_roles']['expert']['model'].endswith('-astra')
    assert all(row['model'].startswith('gpt-') for row in team['member_roles'].values())
    assert 'expert_route' not in team


@pytest.mark.parametrize('kind', ['small', 'cross-module', 'ui-live'])
def test_codex_execution_starts_with_opus_until_quota_is_measured_unavailable(external, monkeypatch, capsys, kind):
    import sys
    from foundry_runtime import cli, foundry_entry
    _, scope, order, _ = external
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    monkeypatch.setattr(foundry_entry, '_analyst_executable', lambda *a: Path(sys.executable))
    assert cli.main(['--project-config', str(scope.path), '--order', str(order), '--foundry-goal',
                     '--task-kind', kind, '--provider', 'codex', '--print-staffing']) == 0
    row = next(line for line in capsys.readouterr().err.splitlines() if line.startswith('FOUNDRY COMPUTE: '))
    config = json.loads(row.removeprefix('FOUNDRY COMPUTE: '))
    assert config['manager_profile'] == 'opus'
    assert config['roles']['coding']['backend'] == 'codex'
    assert config['admission']['provider'] == 'codex'
    assert 'claude' in config['model_catalogue']
    assert all(row['backend'] == 'codex' for row in config.get('reviewers', []))
    assert all(row['backend'] == 'codex' for row in config['roles'].values() if isinstance(row, dict) and 'backend' in row)


def test_member_native_context_without_turn_id_is_bound_by_started_event():
    stamp = '2026-10-14T01:00:00Z'
    rows = [{'timestamp': stamp, 'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'a'}},
            {'timestamp': stamp, 'type': 'turn_context', 'payload': {'model': 'gpt-99-sol'}}]
    assert _model_observation(rows, 'a', _time(stamp), _time(stamp))['declarations'] == [{'model': 'gpt-99-sol', 'effort': None}]
    assert not _model_observation(rows, 'b', _time(stamp), _time(stamp))['declarations']
    assert not _model_observation(rows[1:], 'a', _time(stamp), _time(stamp))['declarations']


def test_standalone_design_author_uses_existing_exclusion(monkeypatch, tmp_path):
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    monkeypatch.setattr(goal, '_run', lambda *a, **kw: kw)
    args = (tmp_path/'order', tmp_path/'evidence', Path('python'))
    options = dict(target_worktree=tmp_path, base_commit='a'*40, candidate_commit='b'*40, backend='claude', review_author_models=['opus'])
    result = goal.run_review(*args, **options)
    assert all(r['model'] != compute.model('opus') for r in result['reviewers'])
    assert result['review_author_models'] == ['opus']
    with pytest.raises(ValueError, match='Reviewer model matches'):
        goal.run_review(*args, **options, analyst_model=compute.model('opus'))
