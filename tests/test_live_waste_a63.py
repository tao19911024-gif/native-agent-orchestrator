import json
import sqlite3
import sys
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import pytest
from foundry_runtime import foundry_goal as goal, _foundry_dispatch as dispatch
from foundry_runtime import _foundry_manager as manager
from foundry_runtime._goal_intervention import LiveEvents
from foundry_runtime._tool_discipline import check
from test_installed_runtime import external, offline, git
from test_manager_input_wait import manager_io, wait
from test_goal_preflight_recovery import retained_goal


@pytest.mark.parametrize('tool', ['Bash', 'PowerShell', 'exec_command', 'Grep'])
@pytest.mark.parametrize('target,allowed', [('handoff.md', True), ('receipt.json', True), ('outside', True), ('src/', False), ('', False)])
def test_index_scope_uses_database_not_extension_list(tmp_path, tool, target, allowed):
    tree = tmp_path / 'tree'; (tree / '.codegraph').mkdir(parents=True)
    (tree / 'src').mkdir(); (tree / 'src/code.py').write_text('code')
    (tree / 'handoff.md').write_text('handoff'); (tree / 'receipt.json').write_text('{}')
    with sqlite3.connect(tree / '.codegraph/codegraph.db') as db:
        db.execute('create table files(path text)'); db.execute("insert into files values ('src/code.py')")
    if target == 'outside': target = str(tmp_path / 'evidence/handoff.md')
    value = {'path': target, 'pattern': 'foo'} if tool == 'Grep' else {'command': 'rg -n foo ' + target}
    result = check({'tool_name': tool, 'tool_input': value, 'cwd': str(tree)},
                   {'index_available': True, 'index_workspace': str(tree)})
    assert (result.get('hookSpecificOutput', {}).get('permissionDecision') != 'deny') == allowed
    # An indexed JSON file must still follow index-first; no hardcoded extension bypass.
    if target == 'receipt.json':
        with sqlite3.connect(tree / '.codegraph/codegraph.db') as db:
            db.execute("insert into files values ('receipt.json')")
        assert check({'tool_name': tool, 'tool_input': value, 'cwd': str(tree)},
                     {'index_available': True, 'index_workspace': str(tree)})['hookSpecificOutput']['permissionDecision'] == 'deny'


@pytest.mark.parametrize('backend', ['claude', 'codex'])
@pytest.mark.parametrize('new_event', [False, 'tool', 'question', 'commit', 'tier', 'escalation'])
def test_undelivered_delegate_starts_next_worker_without_second_ruling(external, manager_io, tmp_path, monkeypatch, backend, new_event):
    tree, scope, order, workers = external
    requests, decisions = manager_io
    decisions.extend([{'action': 'delegate', 'instruction': 'initial'},
                      {'action': 'delegate', 'instruction': 'retained ruling'}])
    if new_event: decisions.append({'action': 'delegate', 'instruction': 'updated ruling'})
    decisions.append(wait('finished fixture'))
    if new_event == 'tier':
        for decision in decisions[2:]:
            decision['member_tier_decisions'] = {'fixture-tier': {'action': 'continue', 'reason': 'fixture ruling'}}
    original_dispatch, original_turn, original_resume = dispatch.dispatch_order, manager.turn, dispatch.resume_order
    started, release = Event(), Event()
    def turn(state, workspace, packet, **kwargs):
        if packet.get('stage') == 'worker_running':
            started.set(); assert release.wait(10)
        return original_turn(state, workspace, packet, **kwargs)
    monkeypatch.setattr(manager, 'turn', turn)
    from foundry_runtime import _goal_intervention as intervention
    carry = intervention.carry_events
    def carry_packet(manifest, packet):
        packet = carry(manifest, packet)
        if new_event == 'tier' and packet.get('stage') == 'worker_returned':
            packet['member_tier_decisions'] = [{'decision_id': 'fixture-tier', 'status': 'manager_answer_required', 'reason': 'fixture final tier evidence'}]
            packet['execution_observation'] = {'fixture_final_result': True}
        if new_event == 'escalation' and packet.get('stage') == 'worker_returned':
            packet['tier_escalation'] = {'action': 'expert_consultation_required', 'from_model': 'fixture', 'signals': ['fixture final evidence']}
        return packet
    monkeypatch.setattr(intervention, 'carry_events', carry_packet)
    prompts, returned_packets = [], []
    def resume(session, workspace, tag, text, **kwargs):
        prompts.append(text)
        returned_packets.append(json.loads((tmp_path / 'goal/manifest.json').read_bytes())['manager_packet'])
        return original_resume(session, workspace, tag, text, **kwargs)
    monkeypatch.setattr(dispatch, 'resume_order', resume)
    def launch(*args, **kwargs):
        result = original_dispatch(*args, **kwargs)
        events = LiveEvents()
        app = SimpleNamespace(outcome_ready=Event(), turn_id='turn', _log=lambda x: None)
        events.events.put({'kind': 'worker_message', 'text': 'handoff'})
        events.drain(app); assert started.wait(5)
        app.outcome_ready.set()
        if new_event == 'commit':
            (tree / 'new.txt').write_text('new committed evidence')
            git(tree, 'add', '.'); git(tree, 'commit', '-qm', 'worker final commit')
        if new_event == 'question':
            events.events.put({'kind': 'worker_message', 'text': 'new question'})
        if new_event == 'tool':
            events.observe({'method': 'item/completed', 'params': {'threadId': 'root', 'turnId': 'turn',
                'item': {'id': 'new-tool', 'type': 'commandExecution'}}}, 'root')
        release.set(); events.close(app)
        return result
    monkeypatch.setattr(dispatch, 'dispatch_order', launch)
    state = {'backend': backend, 'model': None, 'effort': None, 'last_call_id': None,
             'native_dir': str(tmp_path / 'manager'), 'live_coordination': 1}
    result = goal.run_goal(order, tmp_path / 'goal', Path(sys.executable), manager_state=state,
                          coding_launcher='exec', project_record=scope.record())
    assert result['status'] == 'awaiting_input' and len(workers) == 2
    assert len(requests) == (4 if new_event else 3) and not decisions
    assert returned_packets[0]['stage'] == 'worker_returned'
    assert returned_packets[0]['worker_evidence'] and returned_packets[0]['observed_head']
    if new_event == 'tier':
        assert returned_packets[0]['execution_observation']['fixture_final_result']
        assert returned_packets[0]['member_tier_decisions'][0]['status'] == 'manager_answer_required'
    assert len(prompts) == 1 and ('updated ruling' if new_event else 'retained ruling') in prompts[0]


def test_recover_parent_running_during_child_then_interrupted_on_failure(external, tmp_path, monkeypatch):
    tree, scope, order, _ = external
    root, folder, manifest = retained_goal(external, scope, tmp_path)
    def observe(*args, **kwargs):
        state = json.loads((root / 'manifest.json').read_bytes())
        assert state['state'] == 'running' and 'error' not in state
        raise RuntimeError('fixture native failure')
    monkeypatch.setattr(manager, 'turn', observe)
    with pytest.raises(RuntimeError, match='fixture native failure'):
        goal.recover_goal(order, root)
    state = json.loads((root / 'manifest.json').read_bytes())
    assert state['state'] == 'interrupted' and 'fixture native failure' in state['error']


def test_recover_parent_finishes_after_admitted_child(external, tmp_path, monkeypatch):
    tree, scope, order, _ = external
    root, folder, manifest = retained_goal(external, scope, tmp_path)
    receipt = tmp_path / 'decision.json'; receipt.write_text('{}')
    def decide(state, workspace, packet, **kwargs):
        assert json.loads((root / 'manifest.json').read_bytes())['state'] == 'running'
        return {'action': 'await_input', 'instruction': 'commander decision', 'call_id': 'fixture-call', 'evidence': goal._fact(receipt)}, state, {}, packet
    monkeypatch.setattr(manager, 'turn', decide)
    result = goal.recover_goal(order, root)
    assert result['status'] == 'awaiting_input'
    assert json.loads((root / 'manifest.json').read_bytes())['state'] == 'completed'
