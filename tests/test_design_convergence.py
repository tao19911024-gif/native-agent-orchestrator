import pytest

from foundry_runtime import _foundry_manager as manager
from foundry_runtime._foundry_native_request import build_prompt
from test_expert_consultation import consultation


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_guidance_is_opt_in_and_preserves_response_contract(backend):
    import json
    request = {'kind': 'coordinate', 'packet': {'stage': 'assign'}}
    old = build_prompt(request, backend, ValueError)
    new = build_prompt({**request, 'design_convergence': 1}, backend, ValueError)
    assert b'Design convergence and evidence reuse:' not in old
    assert b'Design convergence and evidence reuse:' in new
    assert (json.loads(old.rsplit(b'\n\n', 1)[-1])['response_contract'] ==
            json.loads(new.rsplit(b'\n\n', 1)[-1])['response_contract'])


@pytest.mark.parametrize('profile', ['opus', 'astra'])
def test_new_manager_freezes_guidance_version(profile, tmp_path, monkeypatch):
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    assert manager.configuration(profile, tmp_path)['design_convergence'] == 1


@pytest.mark.parametrize('enabled', [False, True])
def test_native_manager_expert_and_replay_keep_same_guidance(consultation, tmp_path, enabled):
    state, calls, _ = consultation
    if enabled:
        state['design_convergence'] = 1
    packet = {'stage': 'assign', 'operator_answer': 'original'}
    decision, saved, _, _ = manager.turn(state, tmp_path, packet)
    assert len(calls) == 3
    assert any(c['packet'].get('stage') == 'expert_consultation' for c in calls)
    assert all(c.get('design_convergence') == (1 if enabled else None) for c in calls)
    assert saved.get('design_convergence') == (1 if enabled else None)
    # Simulate loss of the goal write: replay retained native receipts, no extra call.
    assert manager.turn(state, tmp_path, packet)[0] == decision
    assert len(calls) == 3


def test_interrupted_new_manager_preserves_updated_evidence(consultation, tmp_path):
    from foundry_runtime import foundry_native as native
    state, calls, failures = consultation
    state['design_convergence'] = 1
    failures.add(2)  # Expert answered; manager has not consumed it successfully.
    packet = {'stage': 'assign', 'operator_answer': 'source revision A'}
    with pytest.raises(native.NativeError):
        manager.turn(state, tmp_path, packet)
    updated = {**packet, 'operator_answer': 'source revision B contradicts A'}
    decision, saved, _, accepted = manager.turn(state, tmp_path, updated, operator_update=True)
    assert accepted['operator_answer'] == updated['operator_answer']
    assert saved['design_convergence'] == 1
    assert decision['expert_consultation']['answer']
    assert len([c for c in calls if c['packet'].get('stage') == 'expert_consultation']) == 1
    assert all(c.get('design_convergence') == 1 for c in calls)
