import json
import pytest
from foundry_runtime import foundry_goal as goal
from foundry_runtime import _foundry_goal_auto as auto


@pytest.mark.parametrize('quota_unavailable,returns', [(False, False), (True, False), (True, True)])
def test_two_blocks_keep_the_assigned_correction_role_across_manager_changes(tmp_path, monkeypatch, quota_unavailable, returns):
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    monkeypatch.setattr(goal, '_git', lambda *args: b'head')
    order = tmp_path/'order.md'
    order.write_text('Finish the task')
    state = {'backend':'codex' if quota_unavailable else 'claude', 'model':'fixture', 'live_coordination':1,
             'review_convergence':2}
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
                'summary':'Different finding '+str(len(calls)) if returns else 'Same receipt error'}]}]}
        goal._write(folder/'outcome.json', result)
        return result
    monkeypatch.setattr(goal, '_run', run)
    options = {'coding_launcher':'app-server', 'coding_model':'gpt-6-sol',
        'task_staffing':{'kind':'small'}, 'manager_state':state,
        'compute_assignment':{'profile_version':6, 'correction_policy':'fixed',
                              'roles':{'correction':{'backend':'codex','model':'gpt-6-sol','effort':'high'}}}}
    result = auto.run(goal, order, tmp_path/'goal', order,
        {'model':'gpt-6-sol','effort':'high'}, options)
    assert result['status']=='candidate_verified'
    manifest = json.loads((tmp_path/'goal/manifest.json').read_bytes())
    assert {(c['coding_launcher'], c['coding_model']) for c in calls} == {('app-server', 'gpt-6-sol')}
    assert calls[-1]['manager_state'] == state
    assert calls[-1]['compute_assignment']['coding_role'] == 'correction'
    assert calls[-1]['execution_decision']['correction']['model_action'] == 'retain'
    assert 'handover' not in manifest
    assert not manifest.get('tier_escalations')


def test_coding_tier_escalation_preserves_explicit_reviewer(monkeypatch):
    from foundry_runtime._compute_selection import escalation
    from foundry_runtime._model_catalogue import model
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    findings = [{'severity':'P1','file':'a.py','summary':'Repeated failure'}]
    result = escalation(model('sol'), {}, prior_findings=findings, findings=findings, reserved_models=['astra'])
    assert result['action'] == 'expert_consultation_required'
    assert result['model'] == model('sol')
