"""Retrieval is a cheap native role; analysis and implementation stay separate."""
import json

import pytest

from foundry_runtime import _compute_selection as compute
from foundry_runtime._dispatch_claude import argv
from foundry_runtime._launch_context import settings


@pytest.fixture
def dynamic_models(monkeypatch):
    monkeypatch.setattr(compute, 'model', lambda tier: 'current-' + tier)


@pytest.mark.parametrize('backend,tier,worker', [('claude', 'haiku', 'sonnet'),
                                                ('codex', 'luna', 'sol')])
def test_retrieval_and_implementation_have_distinct_dynamic_assignments(dynamic_models, backend, tier, worker):
    team = compute.resolve_native_team('adaptive', backend=backend)
    assert team['member_roles']['investigator']['model'] == 'current-' + tier
    assert team['member_roles']['worker']['model'] == 'current-' + worker
    prompt = compute.team_prompt('task', team)
    assert 'Code fact gathering' in prompt
    assert 'not a target utilization percentage' in prompt
    if backend == 'codex':
        assert team['config']['agents.default_subagent_model'] == 'current-sol'
    else:
        assert 'Code fact gathering' in team['member_instructions']


def test_claude_native_agent_receives_frozen_retrieval_contract(dynamic_models):
    team = compute.resolve_native_team('adaptive', backend='claude')
    args = argv('claude', 'fixture', native_team=team,
                launch_context={'settings': settings(), 'server': None})
    agents = json.loads(args[args.index('--agents') + 1])
    reader = agents['investigator']
    assert reader['model'] == 'current-haiku'
    assert 'read-only' in reader['description'].lower()
    assert 'path:line' in reader['prompt']
    assert 'Do not implement' in reader['prompt']
    assert 'Implement edits' in agents['worker']['prompt']
    assert 'member_role_briefs' not in reader


def test_recorded_role_and_prompt_survive_new_defaults(dynamic_models):
    team = {'profile': 'adaptive', 'version': 10, 'instructions': 'old guidance',
            'member_roles': {'investigator': {'model': 'retained-sonnet', 'effort': 'high'}}}
    args = argv('claude', 'fixture', resume=True, native_team=team,
                launch_context={'settings': settings(), 'server': None})
    reader = json.loads(args[args.index('--agents') + 1])['investigator']
    assert reader['model'] == 'retained-sonnet'
    assert reader['description'] == 'Foundry investigator role'
    assert 'Implement edits' in reader['prompt']
    assert compute.team_prompt('continued', team) == 'old guidance\nTask:\ncontinued'
