import pytest

from foundry_runtime._goal_intervention import LiveEvents
from test_claude_live_events import bridge, assistant


def compact(live, thread, identity, tokens, root='root'):
    live.observe({'method': 'item/completed', 'params': {'threadId': thread,
        'item': {'type': 'contextCompaction', 'id': identity}}}, root)
    return live.observe({'method': 'thread/tokenUsage/updated', 'params': {'threadId': thread,
        'tokenUsage': {'last': {'inputTokens': tokens}}}}, root)


def test_root_pressure_never_recommends_replacing_fresh_members():
    live = LiveEvents(lambda rows: None, threshold=120000)
    for i, tokens in enumerate([96401, 106322, 105560, 105208, 129814, 106469,
                               107854, 107614, 107427, 107173, 107330]):
        sample = compact(live, 'root', str(i), tokens)
        assert sample['thread_role'] == 'root'
        assert sample['measured_thread_id'] == 'root'
        assert not sample['member_renewal_recommended']
        assert not sample['renewal_recommended'] and sample['context_pressure_high']
        assert sample['renewal_target'] is None
    assert live.events.qsize() == 1
    event = live.events.get_nowait()
    assert event['kind'] == 'context_renewal' and event['thread_role'] == 'root'
    sample = compact(live, 'actual-member', 'own-pressure', 106000)
    assert sample['member_renewal_recommended']
    assert sample['renewal_target'] == 'actual-member'
    assert live.events.qsize() == 1


@pytest.mark.parametrize('child', [None, 'native-child'])
def test_claude_adapter_keeps_measured_identity(child):
    live = bridge()
    extra = {'parent_tool_use_id': child} if child else {}
    live.observe({'type': 'system', 'subtype': 'compact_boundary', 'uuid': 'c', **extra})
    live.observe({**assistant('u', 'ordinary', usage={'input_tokens': 10000,
        'cache_read_input_tokens': 75000, 'cache_creation_input_tokens': 0}), **extra})
    event = live.live_events.events.get_nowait()
    assert event['thread_role'] == ('member' if child else 'root')
    assert event['measured_thread_id'] == (child or 'session')
    assert event['renewal_target'] == child
    assert event['member_renewal_recommended'] == bool(child)


def test_missing_root_identity_does_not_guess_member_remedy():
    live = LiveEvents(lambda rows: None, threshold=100000)
    sample = compact(live, 'unbound', 'c', 90000, root=None)
    assert sample['thread_role'] == 'unknown'
    assert sample['renewal_target'] is None
    assert not sample['member_renewal_recommended']


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_native_manager_uses_shared_scoped_guidance(backend):
    from foundry_runtime._foundry_native_request import build_prompt
    from foundry_runtime._context_signals import CONTEXT_RENEWAL_GUIDANCE
    prompt = build_prompt({'kind': 'coordinate', 'live_coordination': 1,
        'packet': {'stage': 'worker_running'}}, backend, ValueError).decode()
    assert CONTEXT_RENEWAL_GUIDANCE in prompt
    assert 'root residual is root context pressure' in prompt
    assert 'Only member_renewal_recommended with a matching renewal_target' in prompt
