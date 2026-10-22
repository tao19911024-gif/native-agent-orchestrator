import hashlib
import json

import pytest

from foundry_runtime import _expert_consultation as experts, _foundry_manager as manager
from foundry_runtime._foundry_native_request import build_prompt
from test_expert_consultation import consultation


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
