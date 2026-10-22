import json
from pathlib import Path
import sys

import pytest

from foundry_runtime import _foundry_manager as manager, foundry_goal as goal
from foundry_runtime import _foundry_goal_transitions as transitions
from foundry_runtime._compute_selection import resolve_native_team
from foundry_runtime._foundry_native_request import build_prompt
from foundry_runtime._execution_plan_prompt import execution_plan_prompt
from test_unified_quota_deadline import quota_manager
from test_installed_runtime import external, offline
from test_goal_preflight_recovery import retained_goal


@pytest.mark.parametrize('backend', ['codex', 'claude'])
def test_managed_worker_has_execution_not_design_authority(backend, monkeypatch):
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    team = resolve_native_team('adaptive', backend=backend, managed=True)
    assert team['lead_role'] == 'executor'
    assert 'retain requirements, architectural decisions' not in team['instructions']
    assert 'final acceptance in your own context' not in team['instructions']
    if backend == 'codex':
        from foundry_runtime._compute_selection import _EXPERT_HANDOFF
        assert _EXPERT_HANDOFF not in team['instructions']
        assert 'expert' not in team['member_roles']
        assert 'Use Astra/high for expert decisions' not in team['instructions']
        assert 'You retain responsibility for the final delivery' not in team['instructions']
    prompt = execution_plan_prompt('task', {'manager': {'live_coordination': 1}})
    assert 'final answer' in prompt and 'existing Foundry manager loop' in prompt
    native = build_prompt({'kind': 'coordinate', 'packet': {'stage': 'assign'},
                          'live_coordination': 1}, backend, ValueError)
    assert b'not by fixed turn, time or token limits' in native


@pytest.mark.parametrize('quota_manager', ['claude'], indirect=True)
def test_measured_manager_quota_handoff_without_wait_or_replay(quota_manager, external, tmp_path, monkeypatch):
    state, calls, waits, _ = quota_manager
    state.update(live_coordination=1, quota_fallback_policy='codex', quota_short_wait_seconds=0, profile='opus',
                 expert={'backend': 'claude', 'model': 'fixture', 'effort': 'high'})
    tree, scope, order, _ = external
    # No expert question in this packet: the refusal belongs to the manager.
    with pytest.raises(manager.ManagerQuotaUnavailable) as caught:
        manager.turn(state, tree, {'stage': 'assign'})
    assert len(calls) == 1 and not waits
    with pytest.raises(manager.ManagerQuotaUnavailable):
        manager.turn(state, tree, {'stage': 'assign'})
    assert len(calls) == 1 and not waits
    root, folder, manifest = retained_goal(external, scope, tmp_path)
    manifest.update(manager_state=state, manager_phase='feedback_pending')
    goal._write(folder/'manifest.json', manifest)
    frozen = {p: p.read_bytes() for p in Path(state['native_dir']).rglob('*') if p.is_file()}
    monkeypatch.setattr(transitions, 'resolve_executable', lambda _: Path(sys.executable))
    moved = transitions.manager(goal, folder, manifest, provider='codex', migrate=False,
                                answer=None, quota_receipt=caught.value.receipt)
    assert moved['manager_state']['expert']['backend'] == 'codex'
    assert moved['manager_state']['handoff']['operator_instructions'] is None
    assert moved['coordination']['reason'] == 'measured_claude_provider_limit'
    assert all(row['executable'] == str(Path(sys.executable)) for row in moved['reviewers'])
    assert all(p.read_bytes() == raw for p, raw in frozen.items())


def test_coordination_receipt_survives_optional_usage_failure(tmp_path, monkeypatch):
    saved = {'coordination': {'status': 'codex_quota_fallback', 'evidence': {'path': 'retained'}},
             'candidate_closure': {'status': 'passed', 'candidate_commit': 'candidate'}}
    goal._write(tmp_path/'manifest.json', saved)
    def unavailable(*args, **kwargs):
        raise ValueError('usage snapshot unavailable')
    monkeypatch.setattr(goal.goal_usage, 'task_usage', unavailable)
    outcome = {'status': 'candidate_verified'}
    goal._record_usage(tmp_path, {}, None, outcome)
    assert outcome['coordination'] == saved['coordination']
    assert outcome['candidate_closure'] == saved['candidate_closure']


@pytest.mark.parametrize('outcome,due', [('provider_limit', 60), ('provider_limit', None), ('provider_capacity', 3600)])
def test_short_rate_limit_or_other_failure_does_not_transfer(tmp_path, monkeypatch, outcome, due):
    monkeypatch.setattr(manager, 'time', lambda: 1000)
    manager._quota_handoff({'backend':'claude','quota_fallback_policy':'codex'},
        {'outcome':outcome,'provider_stop':{'automatic_resume_at':1000+due if due else None}},
        {'folder':tmp_path})


def test_missing_manager_executable_is_not_quota_evidence(tmp_path, monkeypatch):
    def missing(*args):
        raise FileNotFoundError('fixture executable absent')
    monkeypatch.setattr(manager, 'resolve_executable', missing)
    with pytest.raises(FileNotFoundError):
        manager.turn({'backend':'claude','model':None,'effort':None,'last_call_id':None,
            'native_dir':str(tmp_path/'manager'),'quota_fallback_policy':'codex'},tmp_path,{'stage':'assign'})
