"""Vendor upgrades refresh new expert staffing, never a recorded assignment."""
import json

import pytest

from foundry_runtime import _model_catalogue as catalogue, _compute_selection as compute
from foundry_runtime import _tool_discipline as discipline, foundry_native_support
from foundry_runtime._dispatch_claude import argv
from foundry_runtime._launch_context import settings


def test_fable_is_dynamic_expert_but_not_manager_or_worker(monkeypatch):
    monkeypatch.setattr(compute, 'model', lambda tier: 'current-' + tier)
    team = compute.resolve_native_team('adaptive', backend='claude')
    assert team['member_roles']['expert'] == {'model': 'current-fable', 'effort': 'high'}
    assert team['member_roles']['worker']['model'] == 'current-sonnet'
    assert compute.GOAL_PROFILES['claude']['coding'][0] == 'sonnet'
    assert compute.profile_pair('claude', 'coding', team_profile='adaptive')[0] == 'current-opus'
    args = argv('claude', 'fixture', native_team=team,
                launch_context={'settings': settings(), 'server': None})
    experts = json.loads(args[args.index('--agents') + 1])
    assert experts['expert']['model'] == 'current-fable'
    assert 'decision' in experts['expert']['prompt']


@pytest.mark.parametrize('tool,field', [('Agent','prompt'), ('spawn_agent','message')])
def test_fable_keeps_expert_assignment_rule(tool, field):
    for model in ('fable', 'claude-fable-5-1', 'claude-fable-99', 'best', 'best[1m]'):
        event = {'tool_name': tool, 'tool_input': {'model': model, field: 'Read code'}}
        assert discipline.check(event, {})
        event['tool_input'][field] = '[decision: resolve conflicting ownership] Inspect the evidence.'
        assert discipline.check(event, {}) == {}


@pytest.fixture
def vendor(monkeypatch, tmp_path):
    executable = tmp_path / 'claude.exe'; executable.write_bytes(b'fixture')
    monkeypatch.setattr(foundry_native_support, 'resolve_executable', lambda _: executable)
    clock = [1800000000.0]
    monkeypatch.setattr(catalogue.time, 'time', lambda: clock[0])
    calls = []
    def alias(exe, tier):
        calls.append(tier)
        return 'claude-' + tier + ('-5' if clock[0] == 1800000000.0 else '-5-1')
    monkeypatch.setattr(catalogue, '_claude_alias', alias)
    return tmp_path / 'catalogue.json', clock, calls, alias


def test_unchanged_cli_refreshes_aliases_after_one_day(vendor):
    cache, clock, calls, _ = vendor
    with catalogue.catalogue_scope(cache):
        first, _ = catalogue._claude()
        assert first['fable'] == 'claude-fable-5'
        assert catalogue._claude()[0] == first and len(calls) == 4
        clock[0] += 24 * 60 * 60
        fresh, _ = catalogue._claude()
        assert fresh['fable'] == 'claude-fable-5-1' and len(calls) == 8


def test_unavailable_fable_does_not_downgrade_other_tiers_or_hide_fallback(vendor, monkeypatch):
    cache, _, calls, original = vendor
    def alias(exe, tier):
        if tier == 'fable':
            calls.append(tier)
            raise RuntimeError('fixture model unavailable')
        return original(exe, tier)
    monkeypatch.setattr(catalogue, '_claude_alias', alias)
    with catalogue.catalogue_scope(cache):
        models, notes = catalogue._claude()
        assert models['sonnet'] == 'claude-sonnet-5'
        assert models['fable'] == catalogue.SNAPSHOT['claude']['fable']
        assert any('fable' in n and 'unavailable' in n for n in notes)
        assert catalogue._claude() == (models, notes) and len(calls) == 4


@pytest.mark.parametrize('timestamp', [None, 'invalid', '2000-01-01T00:00:00Z'])
def test_legacy_or_expired_cache_is_refreshed(vendor, timestamp):
    cache, _, calls, _ = vendor
    with catalogue.catalogue_scope(cache):
        catalogue._claude()
        saved = json.loads(cache.read_text()); saved['claude']['observed_utc'] = timestamp
        cache.write_text(json.dumps(saved))
        catalogue._claude()
        assert len(calls) == 8


def test_unverified_probe_cleanup_stops_discovery(vendor, monkeypatch):
    cache, _, calls, _ = vendor
    def unsettled(exe, tier):
        calls.append(tier)
        raise catalogue._ProbeCleanupError('fixture unsettled probe')
    monkeypatch.setattr(catalogue, '_claude_alias', unsettled)
    with catalogue.catalogue_scope(cache), pytest.raises(RuntimeError, match='unsettled'):
        catalogue._claude()
    assert len(calls) == 1 and not cache.exists()


def test_fable_alias_cannot_silently_become_opus(monkeypatch):
    monkeypatch.setattr(catalogue, '_spawn', lambda *a, **k: type('Probe', (), {'stdout': None})())
    monkeypatch.setattr(catalogue, '_stop', lambda p: None)
    monkeypatch.setattr(catalogue, '_json_lines', lambda *a: iter([
        {'type': 'system', 'subtype': 'init', 'model': 'claude-opus-5-5'}]))
    with pytest.raises(RuntimeError, match='another family'):
        catalogue._claude_alias('claude', 'fable')


def test_new_and_retained_native_experts_are_distinct(monkeypatch):
    monkeypatch.setattr(compute, 'model', lambda tier: 'current-' + tier)
    current = compute.resolve_native_team('adaptive', backend='claude')
    retained = {**current, 'version': 11,
                'member_roles': {**current['member_roles'], 'expert': {'model': 'claude-opus-5-5', 'effort': 'high'}}}
    args = argv('claude', 'fixture', resume=True, native_team=retained,
                launch_context={'settings': settings(), 'server': None})
    assert json.loads(args[args.index('--agents') + 1])['expert']['model'] == 'claude-opus-5-5'
    assert current['member_roles']['expert']['model'] == 'current-fable'
