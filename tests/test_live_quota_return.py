from pathlib import Path
from foundry_runtime import _foundry_goal_transitions as transitions
from foundry_runtime import _compute_selection as compute


def test_reset_returns_to_opus_only_at_next_decision(monkeypatch,tmp_path):
    calls=[]
    def transfer(owner,folder,manifest,**kwargs):
        calls.append(kwargs)
        return {**manifest,'manager_state':{'backend':'claude','expert':{'model':'fable'}}}
    monkeypatch.setattr(transitions,'manager',transfer)
    monkeypatch.setattr('time.time',lambda:1000)
    class Owner:
        def _write(self,*args): pass
    manifest={'manager_state':{'quota_fallback':{'path':'receipt'},'quota_return_at':1001}}
    assert transitions.restore_opus(Owner(),tmp_path,manifest) is manifest
    assert not calls
    manifest['manager_state']['quota_return_at']=999
    result=transitions.restore_opus(Owner(),tmp_path,manifest,live=True)
    assert calls==[{'provider':'claude','migrate':False,'answer':None,'source_locked':True,'live':True}]
    assert result['coordination']['status']=='opus_recovery_pending'


def test_incomplete_historical_findings_do_not_stop_escalation(monkeypatch):
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE','snapshot')
    rows=[{'severity':'P1','summary':'missing old file field'}]
    assert compute.escalation(compute.model('sol'),{},prior_findings=rows,findings=rows) is None


def test_pure_codex_review_excludes_coder_and_design_manager(monkeypatch):
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE','snapshot')
    authors=[compute.model('sol'),compute.model('astra')]
    selected=compute.independent_reviewers([{'backend':'codex','model':authors[1],'effort':'high'}]*2,
                                           authors,available_backends={'codex'})
    assert all(r['backend']=='codex' and r['model'] not in authors for r in selected)
