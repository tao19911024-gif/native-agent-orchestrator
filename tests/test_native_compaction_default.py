"""Native default, explicit overrides and immutable historical observation."""
import json
import pytest
from foundry_runtime import _launch_context as context

@pytest.mark.parametrize('value', [None, '0', '240000'])
def test_capture_and_native_arguments_freeze_policy(monkeypatch, value):
    monkeypatch.delenv('FOUNDRY_COMPACT_TOKENS', raising=False)
    if value is not None:
        monkeypatch.setenv('FOUNDRY_COMPACT_TOKENS', value)
    codex = context.capture(enable_index=False)
    claude = context.capture(enable_index=False, backend='claude')
    expected = int(value or 0)
    assert codex['settings']['compact_tokens'] == expected
    assert claude['settings']['compact_tokens'] == int(value or 0)
    monkeypatch.setenv('FOUNDRY_COMPACT_TOKENS', '500000')
    argv = context.codex_settings(launch=codex)
    limits = [arg for arg in argv if arg.startswith('model_auto_compact_token_limit=')]
    assert limits == ([f'model_auto_compact_token_limit={expected}'] if expected else [])
    report = context.compaction_observation(codex, 'codex')
    assert report['mode'] == ('explicit_override' if expected else 'native_default')
    assert report['token_limit'] == (expected or None)

@pytest.mark.parametrize('value', [120000, 240000, 0])
def test_goal_status_observes_frozen_policy_read_only(tmp_path, monkeypatch, value):
    from foundry_runtime._followup import status
    launch = {'settings': {'compact_tokens': value}}
    manifest = {'run_id': 'fixture', 'coding_launcher': 'app-server', 'launch_context': launch}
    path = tmp_path / 'manifest.json'
    raw = json.dumps(manifest).encode()
    path.write_bytes(raw)
    monkeypatch.setenv('FOUNDRY_COMPACT_TOKENS', '500000')
    report = status(tmp_path)
    assert report['compaction']['token_limit'] == (value or None)
    assert report['compaction']['mode'] == ('explicit_override' if value else 'native_default')
    assert path.read_bytes() == raw


def test_goal_missing_policy_is_not_current_default(tmp_path):
    from foundry_runtime._followup import status
    (tmp_path/'manifest.json').write_text('{"run_id":"fixture"}')
    assert status(tmp_path)['compaction']['mode'] == 'unrecorded'


def test_usage_bound_prefers_actual_frozen_launch(tmp_path):
    from foundry_runtime._context_signals import recorded_bound
    log = tmp_path/'dispatch.log'
    path = log.with_suffix('.package.json')
    receipt = {'attempt_id': 'a', 'evidence_refs': [{'kind': 'log', 'path': str(log)}]}
    package = {'attempt_id': 'a', 'runtime_provenance': {'launch_context': {
        'context_tokens': 300000, 'compact_tokens': 500000}},
        'launch_context': {'settings': {'context_tokens': 300000, 'compact_tokens': 240000}}}
    path.write_text(json.dumps(package))
    assert recorded_bound(receipt)['tokens'] == 240000
    package['launch_context']['settings']['compact_tokens'] = 0
    path.write_text(json.dumps(package))
    assert recorded_bound(receipt)['tokens'] is None


def test_mixed_review_panel_keeps_provider_defaults(tmp_path, monkeypatch):
    import sys
    from foundry_runtime import _foundry_review_group as group, _code_index
    monkeypatch.delenv('FOUNDRY_COMPACT_TOKENS', raising=False)
    monkeypatch.setattr(_code_index, 'prepare', lambda *a, **kw: None)
    launched = {}
    class Member:
        def __init__(self, executable, folder, **options):
            launched[options['backend']] = options['launch_context']
        def __call__(self, request):
            return request['call_id']
    requests = {f'review-{i}': {'call_id': str(i)} for i in range(2)}
    panel = group.Panel([{'executable': sys.executable, 'backend': backend}
                         for backend in ('codex', 'claude')], requests, Member, tmp_path/'reviews', tmp_path)
    outcomes = group.prewarm(list(requests), requests, panel)
    assert all(error is None for value, error in outcomes.values()), outcomes
    assert launched['codex']['settings']['compact_tokens'] == 0
    assert launched['claude']['settings']['compact_tokens'] == 0
    assert launched['claude']['settings']['context_tokens'] == 500000
    assert launched['codex']['tool_discipline']['reading_budget']['unit'] == 'chars'
    assert launched['claude']['tool_discipline']['reading_budget']['unit'] == 'lines'


def test_claude_window_target_and_live_observation(monkeypatch):
    from foundry_runtime._dispatch_claude import ClaudeLive
    import io
    monkeypatch.delenv('FOUNDRY_CONTEXT_TOKENS', raising=False)
    monkeypatch.delenv('FOUNDRY_COMPACT_TOKENS', raising=False)
    monkeypatch.delenv('FOUNDRY_COMPACT_PERCENT', raising=False)
    launch = context.capture(enable_index=False, backend='claude')
    env = context.claude_settings(launch)['env']
    assert env['CLAUDE_CODE_AUTO_COMPACT_WINDOW'] == '500000'
    assert env['CLAUDE_AUTOCOMPACT_PCT_OVERRIDE'] == '90'
    assert context.compaction_observation(launch, 'claude')['token_limit'] == 450000
    live = ClaudeLive('fixture', io.StringIO(), launch)
    assert live.live_events.residual.threshold == 450000
    old = {'settings': {**launch['settings'], 'context_tokens': 300000, 'compact_tokens': 120000}}
    assert context.claude_settings(old)['env']['CLAUDE_CODE_AUTO_COMPACT_WINDOW'] == '133334'
    assert context.compaction_observation(old, 'claude')['token_limit'] == 120000

def test_goal_status_uses_exact_pending_package(tmp_path):
    from foundry_runtime._followup import status
    import hashlib
    package = tmp_path/'dispatch.package.json'
    package.write_text(json.dumps({'launch_context': {'settings': {'compact_tokens': 0}}}))
    reference = {'path': str(package), 'sha256': hashlib.sha256(package.read_bytes()).hexdigest()}
    manifest = {'run_id': 'fixture', 'coding_launcher': 'app-server', 'pending_dispatch': reference,
                'launch_context': {'settings': {'compact_tokens': 120000}}}
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    before = status(tmp_path)
    assert before['compaction']['mode'] == 'native_default'
    package.write_text('{}')
    report = status(tmp_path)
    assert report['compaction']['mode'] == 'unrecorded'
    assert report['compaction_observation_error']
    assert report['status'] == before['status']
