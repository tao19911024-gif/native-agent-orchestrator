import hashlib
import json
from pathlib import Path

import pytest

from foundry_runtime import _expert_consultation as experts, _foundry_manager as manager
from foundry_runtime._foundry_native_request import build_prompt
from test_expert_consultation import consultation
from test_installed_runtime import external, offline


def reference(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')
    return {'path': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def test_history_uses_immutable_existing_amendment_sources(tmp_path):
    first = reference(tmp_path/'first.json', {'reviews': [{'findings': []}]})
    manifest = reference(tmp_path/'manifest.json', {'amendment': {'prior_outcome': first}})
    second = reference(tmp_path/'second.json', {'reviews': [{'findings': []}]})
    amendment = {'prior_manifest': manifest, 'prior_outcome': second}
    assert [r['evidence'] for r in experts.review_history(amendment)] == [first, second]
    (tmp_path/'first.json').write_text('{}')
    assert experts.review_history(amendment) == []  # no new task-blocking gate


@pytest.mark.parametrize('container', ['continuation', 'automatic', 'automatic_continuation'])
def test_history_follows_existing_continuation_and_round_ownership(tmp_path, container):
    first = reference(tmp_path/'first.json', {'candidate_commit': '1'*40, 'reviews': [{'findings': []}]})
    latest = {'status': 'review_blocked', 'candidate_commit': '2'*40, 'coding_session': 'worker',
              'reviews': [{'findings': []}]}
    second = reference(tmp_path/'outcome.json', latest)
    identity = {'order': {'path': 'order.md', 'sha256': 'a'*64}, 'worktree': 'project', 'base_commit': '0'*40}
    if container == 'continuation':
        body = {**identity, 'continuation': {'prior_review': {'prior_outcome': first}}}
    else:
        child = tmp_path/'round-1'; child.mkdir()
        final = reference(child/'outcome.json', latest)
        link = ({'continuation': {'prior_review': {'prior_outcome': first}}}
                if container == 'automatic_continuation' else {'amendment': {'prior_outcome': first}})
        reference(child/'manifest.json', {**identity, **link})
        body = {**identity, 'mode': 'auto-amend', 'active_round': str(child),
                'rounds': [{'outcome': first}, {'outcome': final}]}
    manifest = reference(tmp_path/'manifest.json', body)
    amendment = {'prior_manifest': manifest, 'prior_outcome': second}
    history = experts.review_history(amendment)
    assert [row['evidence'] for row in history] == [first, second]
    assert [row['candidate_commit'] for row in history] == ['1'*40, '2'*40]
    if container.startswith('automatic'):
        reference(tmp_path/'round-1/outcome.json', {**latest, 'candidate_commit': '3'*40})
        assert experts.review_history(amendment) == []


from test_settled_failure_recovery import failed_goal
from test_manager_input_wait import manager_io, wait


def test_coding_recovery_keeps_prior_findings_for_manager(failed_goal, manager_io, tmp_path):
    from foundry_runtime import foundry_goal as goal
    _, _, order, folder, worker_calls = failed_goal
    requests, decisions = manager_io
    first = reference(tmp_path/'first-review.json', {'reviews': [{'findings': []}]})
    prior_manifest = reference(tmp_path/'review-manifest.json', {'amendment': {'prior_outcome': first}})
    prior_outcome = reference(tmp_path/'second-review.json', {'reviews': [{'findings': []}]})
    amendment = {'prior_manifest': prior_manifest, 'prior_outcome': prior_outcome}
    manifest = json.loads((folder/'manifest.json').read_bytes())
    checkpoint = json.loads((folder/'coding-checkpoint.json').read_bytes())
    state = {'backend': 'claude', 'model': None, 'effort': None, 'review_convergence': 1,
             'native_dir': str(tmp_path/'manager')}
    checkpoint['options']['manager_state'] = state
    reference(folder/'coding-checkpoint.json', checkpoint)
    manifest.update(amendment=amendment, manager_state=state, manager_phase='feedback_pending',
                    coding_checkpoint=goal._fact(folder/'coding-checkpoint.json'))
    reference(folder/'manifest.json', manifest)
    decisions.append(wait('Retained feedback received; operator decision needed'))
    result = goal.recover_goal(order, folder, answer='Continue the interrupted correction')
    assert result['status'] == 'awaiting_input' and len(worker_calls) == 1
    packet = requests[0]['packet']
    assert packet['prior_review'] == prior_outcome
    assert [row['evidence'] for row in packet['review_history']] == [first, prior_outcome]
    assert goal.recover_goal(order, folder, answer='Continue the interrupted correction') == result
    assert len(requests) == 1  # Retained manager result, no second paid decision.


@pytest.mark.parametrize('same_question', [True, False])
def test_unmanaged_continuation_reuses_answer_for_same_review(failed_goal, tmp_path, monkeypatch, same_question):
    from foundry_runtime import foundry_goal as goal
    from foundry_runtime._compute_selection import resolve_native_team
    _, _, order, folder, worker_calls = failed_goal
    prior = reference(tmp_path/'review.json', {'reviews': [{
        'correction_guidance': {'model': 'expert', 'reason': 'ownership'}, 'findings': []}]})
    amendment = {'prior_manifest': reference(tmp_path/'review-manifest.json', {}), 'prior_outcome': prior}
    question = experts.review_request({'prior_review': prior})
    answer = {'question': question if same_question else {'trigger': 'reviewer', 'question': 'older issue'},
              'answer': 'Preserve the existing ledger owner.', 'evidence': prior}
    checkpoint = json.loads((folder/'coding-checkpoint.json').read_bytes())
    checkpoint['options'].update(native_team=resolve_native_team('adaptive'),
                                 execution_decision={'expert_consultation': answer})
    reference(folder/'coding-checkpoint.json', checkpoint)
    manifest = json.loads((folder/'manifest.json').read_bytes())
    manifest.update(amendment=amendment, coding_checkpoint=goal._fact(folder/'coding-checkpoint.json'))
    reference(folder/'manifest.json', manifest)
    consultations = []
    def consult(*args):
        consultations.append(args[3])
        return {**answer, 'question': args[3], 'answer': 'Updated ownership answer.'}
    monkeypatch.setattr(experts, 'consult', consult)
    outcome = goal.recover_goal(order, folder, answer='Continue correction with retained expert guidance')
    assert outcome['status'] == 'project_handoff' and len(worker_calls) == 2
    saved = json.loads((Path(outcome['continuation_evidence'])/'manifest.json').read_bytes())
    assert len(consultations) == (0 if same_question else 1)
    assert saved['execution_decision']['expert_consultation'] == (answer if same_question else
        {**answer, 'question': question, 'answer': 'Updated ownership answer.'})


def test_repeated_blocking_review_consults_before_manager_on_both_backends(consultation, tmp_path):
    state, calls, _ = consultation
    state['review_convergence'] = 1
    finding = {'severity': 'P1', 'file': 'writer.py', 'summary': 'Two writers own the same ledger'}
    history = [{'evidence': {'path': str(tmp_path/f'{n}.json'), 'sha256': str(n)*64},
                'reviews': [{'findings': [finding]}]} for n in (1, 2)]
    packet = {'stage': 'assign', 'review_history': history}
    decision, _, _, _ = manager.turn(state, tmp_path, packet)
    assert len(calls) == 2 and calls[0]['packet']['stage'] == 'expert_consultation'
    assert decision['expert_consultation']['trigger'] == 'recurring_reasoning'
    assert len(decision['expert_consultation']['question']['evidence_refs']) == 2
    assert manager.turn(state, tmp_path, packet)[0] == decision and len(calls) == 2


@pytest.mark.parametrize('backend', ['claude', 'codex'])
def test_semantic_trigger_uses_existing_expert_contract_and_is_opt_in(backend):
    request = {'kind': 'coordinate', 'packet': {}, 'expert_consultation': True}
    old = build_prompt(request, backend, ValueError)
    new = build_prompt({**request, 'review_convergence': 1}, backend, ValueError)
    assert b'Review non-convergence trigger' not in old
    assert b'not identical wording' in new and b'recurring_reasoning' in new
    assert json.loads(old.rsplit(b'\n\n', 1)[-1])['response_contract'] == json.loads(new.rsplit(b'\n\n', 1)[-1])['response_contract']
