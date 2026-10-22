import json
import os
from pathlib import Path
import sqlite3
import subprocess

import pytest

from foundry_runtime import _code_index as index, recovery
from foundry_runtime._goal_intervention import LiveEvents, answer_events, refusal_signature
from foundry_runtime._dispatch_claude import coding_report
from foundry_runtime._project_execution import AdmissionRefused
from test_installed_runtime import external, offline
from test_owned_goal_recovery import interrupted
from test_manager_input_wait import manager_io, wait
from foundry_runtime import foundry_goal as goal
from foundry_runtime._project_execution import ProjectExecution
import sys
from foundry_runtime._execution_intent import ExecutionIntent


@pytest.mark.skipif(not os.environ.get('FOUNDRY_TEST_CODEGRAPH_ARGV'), reason='explicit local CodeGraph installation required')
def test_new_module_syncs_but_markdown_reuses(external, tmp_path, monkeypatch):
    tree, *_ = external
    monkeypatch.setenv('FOUNDRY_CODEGRAPH_ARGV', os.environ['FOUNDRY_TEST_CODEGRAPH_ARGV'])
    monkeypatch.setenv('FOUNDRY_CODEGRAPH_STATE_DIR', str(tmp_path / 'state'))
    source = tree / 'old.py'; source.write_text('def old(): pass\n')
    state = {}
    assert index.prepare(tree, observation=state)
    (tree / 'notes.md').write_text('new notes')
    assert index.prepare(tree, observation=state)
    assert state['preparation'] == 'verified_reuse'
    new = tree / 'new_module.py'; new.write_text('def new_module(): pass\n')
    assert index.prepare(tree, observation=state)
    assert state['preparation'] == 'sync'
    with sqlite3.connect(tree / '.codegraph/codegraph.db') as db:
        assert db.execute("select count(*) from files where path='new_module.py'").fetchone()[0] == 1
    (tree / '.git/info/exclude').write_text('# changed ignore rules\n')
    assert index.prepare(tree, observation=state)
    assert state['preparation'] == 'sync'
    global_ignore = tmp_path / 'global-ignore'
    global_ignore.write_text('# first global rule\n')
    subprocess.run(['git', '-C', str(tree), 'config', 'core.excludesFile', str(global_ignore)], check=True)
    assert index.prepare(tree, observation=state)
    assert state['preparation'] == 'sync'
    global_ignore.write_text('# changed global rule\n')
    assert index.prepare(tree, observation=state)
    assert state['preparation'] == 'sync'
    source.unlink()
    assert index.prepare(tree, observation=state)
    assert state['preparation'] == 'sync'


@pytest.mark.parametrize('text', ['Explaining FOUNDRY_MANAGER_QUESTION here',
    'FOUNDRY_MANAGER_QUESTION_suffix', '```\nFOUNDRY_MANAGER_QUESTION code\n```'])
def test_quoted_marker_does_not_call_manager(text):
    events = LiveEvents(lambda rows: 'reply')
    def emit(text, identity):
        events.observe({'method': 'item/completed', 'params': {'threadId': 'root',
            'item': {'type': 'agentMessage', 'id': identity, 'text': text}}}, 'root')
    emit(text, 'mention'); assert events.events.empty()
    emit('FOUNDRY_MANAGER_QUESTION: real question', 'real')
    assert events.events.qsize() == 1


def test_deliverable_report_survives_ack_turn(tmp_path):
    stream = tmp_path / 'stream.jsonl'
    rows = [{'type': 'result', 'result': 'FOUNDRY_DELIVERABLE_DONE\nfirst handoff'},
            {'type': 'result', 'result': 'acknowledged'}]
    stream.write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
    assert coding_report(stream, rows[-1]) == rows[0]['result']
    rows.insert(1, {'type': 'result', 'result': 'FOUNDRY_DELIVERABLE_DONE\ncorrected handoff'})
    stream.write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
    assert coding_report(stream, rows[-1]) == rows[1]['result']


def test_new_manager_question_replaces_refusal_question():
    manifest = {'assignment_refusal_question': {'question': 'old'}, 'unanswered_live_events': []}
    answer_events(manifest, {}, {'action': 'await_input', 'instruction': 'new question'})
    assert 'assignment_refusal_question' not in manifest


def test_objection_identity_tracks_evidence_not_assignment_words(external, tmp_path):
    tree, *_ = external
    source = tree / 'new.py'; source.write_text('old evidence')
    receipt = tmp_path / 'refusal.json'
    receipt.write_text(json.dumps({'result': {'goal_premise': {'status': 'REFUSE',
        'statement': 'same false premise', 'evidence_refs': [str(source)]}}}))
    first = AdmissionRefused('refused', receipt=receipt, instructions='first assignment').feedback
    revised = AdmissionRefused('refused', receipt=receipt, instructions='reworded assignment').feedback
    before = refusal_signature(first, tree)
    assert before and before == refusal_signature(revised, tree)
    source.write_text('new evidence')
    assert before != refusal_signature(revised, tree)


def test_existing_recovery_advice_names_successor(interrupted):
    tree, scope, order, folder, *_ = interrupted
    row, _, _ = ExecutionIntent.read_history(folder, task_key='TASK1')
    row['recovery_authorization'] = {'successor_intent_id': 'successor-fixture'}
    report = recovery.blocker_advice(folder, row)
    assert report['status'] == 'recovery_already_recorded'
    assert report['recovery_authorization']['successor_intent_id'] == 'successor-fixture'
    assert 'diagnostic_error' not in report and 'reconcile_argv' not in report


@pytest.mark.parametrize('backend', ['claude', 'codex'])
@pytest.mark.parametrize('changed', [False, True])
def test_reworded_refusal_returns_to_manager_only_with_new_evidence(external, manager_io, tmp_path, monkeypatch, backend, changed):
    tree, scope, order, workers = external
    requests, decisions = manager_io
    decisions.extend([{'action': 'delegate', 'instruction': text} for text in
                      ('initial', 'bad assignment', 'bad reworded assignment')])
    if changed:
        decisions.extend([{'action': 'delegate', 'instruction': 'corrected'}, wait('done')])
    original, rejected = ProjectExecution.preflight, []
    def preflight(self, tag, text, **kw):
        if 'bad ' in text:
            if rejected and changed:
                (tree / 'new-evidence.txt').write_text('new observed source')
            path = tmp_path / ('refusal-%s.json' % len(rejected))
            path.write_text(json.dumps({'result': {'goal_premise': {
                'status': 'REFUSE', 'statement': ('旧哈希 abc 不匹配' if not rejected else '必须更新为另一个哈希 def'), 'evidence': ['same observation']}}}))
            rejected.append(path)
            raise AdmissionRefused('same false premise', receipt=path, instructions=text)
        return original(self, tag, text, **kw)
    monkeypatch.setattr(ProjectExecution, 'preflight', preflight)
    state = {'backend': backend, 'model': None, 'effort': None, 'last_call_id': None,
             'native_dir': str(tmp_path / 'manager')}
    result = goal.run_goal(order, tmp_path / 'goal', Path(sys.executable), manager_state=state,
                          coding_launcher='exec', project_record=scope.record())
    assert not decisions and len(rejected) == 2
    assert len(workers) == (2 if changed else 1)
    assert result['status'] == 'awaiting_input'
    assert ('same objection without new evidence' in result['input_request']['question']) != changed
