import json
import pytest
from foundry_runtime._goal_intervention import protocol_markers, refusal_signature
from foundry_runtime._dispatch_claude import coding_report
from test_installed_runtime import external


@pytest.mark.parametrize('text,expected', [
    ('**FOUNDRY_DELIVERABLE_DONE**', 'FOUNDRY_DELIVERABLE_DONE'),
    ('- FOUNDRY_MANAGER_QUESTION: question', 'FOUNDRY_MANAGER_QUESTION'),
    ('  FOUNDRY_DELIVERABLE_DONE.', 'FOUNDRY_DELIVERABLE_DONE'),
    ('> FOUNDRY_DELIVERABLE_DONE—handoff', 'FOUNDRY_DELIVERABLE_DONE'),
    ('*FOUNDRY_DELIVERABLE_DONE*', 'FOUNDRY_DELIVERABLE_DONE'),
    ('```x```\nFOUNDRY_DELIVERABLE_DONE', 'FOUNDRY_DELIVERABLE_DONE'),
    ('```python\nexample\nFOUNDRY_DELIVERABLE_DONE', None),
    ('see FOUNDRY_DELIVERABLE_DONE in the note', None),
    ('FOUNDRY_DELIVERABLE_DONE_suffix', None),
    ('```\nFOUNDRY_DELIVERABLE_DONE\n```', None),
    ('~~~\nFOUNDRY_MANAGER_QUESTION\n~~~', None),
])
def test_marker_contract(text, expected):
    assert protocol_markers(text) == ({expected} if expected else set())


def test_latest_marked_message_survives_plain_result_and_child(tmp_path):
    rows = [
        {'type': 'result', 'result': 'FOUNDRY_DELIVERABLE_DONE\nold'},
        {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': '**FOUNDRY_DELIVERABLE_DONE**\nhandoff'}]}},
        {'type': 'assistant', 'parent_tool_use_id': 'child', 'message': {'content': [{'type': 'text', 'text': 'FOUNDRY_DELIVERABLE_DONE\nchild'}]}},
        {'type': 'result', 'result': 'plain final'},
    ]
    stream = tmp_path / 'stream.jsonl'
    stream.write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
    assert coding_report(stream, rows[-1]) == '**FOUNDRY_DELIVERABLE_DONE**\nhandoff'


def test_refusal_ignores_prose_but_tracks_cited_bytes_and_gate(external, tmp_path):
    tree, *_ = external
    evidence = tmp_path / 'evidence.txt'; evidence.write_text('original')
    def event(statement, gate='premise'):
        return {'refused_judgements': {gate: {'status': 'REFUSE', 'statement': statement,
            'evidence': [statement], 'evidence_refs': [str(evidence)]}}}
    first = refusal_signature(event('hash abc is stale'), tree)
    assert first == refusal_signature(event('use another hash def'), tree)
    assert first != refusal_signature(event('hash abc is stale', 'other'), tree)
    evidence.write_text('changed')
    assert first != refusal_signature(event('use another hash def'), tree)
