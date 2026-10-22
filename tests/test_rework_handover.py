import json
import pytest
from foundry_runtime import foundry_goal as goal
from foundry_runtime import _foundry_goal_auto as auto


@pytest.mark.parametrize('quota_unavailable,returns', [(False, False), (True, False), (True, True)])
def test_modern_two_blocks_handover_uses_measured_manager_availability(tmp_path, monkeypatch, quota_unavailable, returns):
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    monkeypatch.setattr(goal, '_git', lambda *args: b'head')
    order = tmp_path/'order.md'
    order.write_text('Finish the task')
    state = {'backend':'codex' if quota_unavailable else 'claude', 'model':'fixture', 'live_coordination':1}
    if quota_unavailable:
        state.update(quota_fallback={'path':'measured-receipt'}, quota_return_at=123)
    calls = []
    def run(order, folder, executable, **options):
        calls.append(options)
        folder.mkdir()
        if returns and len(calls) == 3:
            state.pop('quota_fallback')
            state['backend'] = 'claude'  # Existing manager-return path confirmed recovery.
        goal._write(folder/'manifest.json', {'worktree':str(tmp_path), 'base_commit':'base',
            'tag':'fixture', 'manager_state':state})
        result = {'status':'review_blocked' if len(calls)<(4 if returns else 3) else 'candidate_verified',
            'coding_session':'session', 'candidate_commit':str(len(calls)),
            'reviews':[{'verdict':'BLOCK', 'findings':[{'file':'a.py','line':1,'severity':'P1',
                'summary':'different finding '+str(len(calls))}]}]}
        goal._write(folder/'outcome.json', result)
        return result
    monkeypatch.setattr(goal, '_run', run)
    options = {'coding_launcher':'app-server', 'coding_model':'gpt-6-sol',
        'task_staffing':{'kind':'small'}, 'manager_state':state,
        'compute_assignment':{'profile_version':5, 'roles':{}}}
    result = auto.run(goal, order, tmp_path/'goal', order,
        {'model':'gpt-6-sol','effort':'high'}, options)
    assert result['status']=='candidate_verified'
    manifest = json.loads((tmp_path/'goal/manifest.json').read_bytes())
    if quota_unavailable and not returns:
        assert calls[2]['coding_launcher']=='app-server'
        assert manifest['handover_deferred']['quota_evidence']==state['quota_fallback']
    else:
        assert calls[-1]['coding_launcher']=='claude'
        assert calls[-1]['manager_state']==state
        assert calls[-1]['native_team']['lead_role']=='executor'
        assert manifest['handover']['after_rounds']==([1,2] if returns else [0,1])
        assert 'tier_escalation' not in calls[-1]['execution_decision']
        assert 'handover_deferred' not in manifest
