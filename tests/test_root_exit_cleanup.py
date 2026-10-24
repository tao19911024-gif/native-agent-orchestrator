"""A native root exiting must release descendants before waiting for pipe EOF."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from foundry_runtime import _code_index, _foundry_manager as manager
from foundry_runtime import foundry_native as native, app_server_transport as transport


CHILD = "import time;time.sleep(30)"
SPAWN_CHILD = ('import subprocess;subprocess.Popen([sys.executable,"-B","-c",'
               + repr(CHILD) + '],close_fds=False)\n')


@pytest.mark.parametrize('backend', ['codex', 'claude'])
def test_native_root_exit_drains_output_and_cleans_descendants(tmp_path, monkeypatch, backend):
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    monkeypatch.setattr(manager, 'resolve_executable', lambda _: Path(sys.executable))
    monkeypatch.setattr(_code_index, 'prepare', lambda _, **kw: None)
    real_spawn, factory = native.OwnedProcess.spawn, manager.NativeAnalyst
    processes = []
    answer = {'action': 'delegate', 'instruction': 'buffered answer ' + 'x' * 40000}
    if backend == 'codex':
        rows = [{'type': 'thread.started', 'thread_id': '00000000-0000-0000-0000-000000000001'},
                {'type': 'turn.started'},
                {'type': 'item.completed', 'item': {'id': 'final', 'type': 'agent_message', 'text': json.dumps(answer)}},
                {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'cached_input_tokens': 0, 'output_tokens': 5}}]
    else:
        rows = [{'type': 'system', 'subtype': 'init', 'tools': ['StructuredOutput'],
                 'mcp_servers': [], 'skills': [], 'plugins': [], 'model': 'fixture', 'session_id': 'fixture-manager'},
                {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
                    {'type': 'tool_use', 'name': 'StructuredOutput', 'id': 'format-1', 'input': answer}]}},
                {'type': 'user', 'message': {'role': 'user', 'content': [
                    {'type': 'tool_result', 'tool_use_id': 'format-1', 'content': 'Structured output provided successfully'}]}},
                {'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 2,
                 'result': json.dumps(answer), 'structured_output': answer,
                 'usage': {'input_tokens': 10, 'output_tokens': 20, 'iterations': [{'type': 'message'}]}, 'total_cost_usd': 0.0}]
    output = '\n'.join(map(json.dumps, rows)) + '\n'
    def spawn(argv, **kwargs):
        script = 'import sys\n' + SPAWN_CHILD + 'sys.stdin.buffer.read();sys.stdout.buffer.write(' + repr(output.encode()) + ');sys.stdout.buffer.flush()'
        script_path = tmp_path / 'fixture.py'
        script_path.write_text(script, encoding='utf-8')
        process = real_spawn([sys.executable, '-B', str(script_path)], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(native.OwnedProcess, 'spawn', spawn)
    # Bound the failing baseline without altering any production wait policy.
    monkeypatch.setattr(manager, 'NativeAnalyst', lambda *a, **kw: factory(*a, **{**kw, 'timeout_seconds': 3}))
    state = {'backend': backend, 'model': None, 'effort': None, 'last_call_id': None,
             'native_dir': str(tmp_path / 'manager')}
    try:
        decision, _, _, _ = manager.turn(state, tmp_path, {'stage': 'assign'})
        assert decision['instruction'] == answer['instruction']
        receipt = next((tmp_path / 'manager').glob('call-*/receipt.json'))
        record = json.loads(receipt.read_bytes())
        assert record['outcome'] == 'success' and record['returncode'] == 0
        assert record['cleanup']['cleanup_verified'] and record['cleanup']['job_active_processes'] == 0
        assert receipt.with_name('stdout.jsonl').read_bytes() == output.encode()
    finally:
        for process in processes:
            assert process.stop_and_verify(drain_seconds=0, kill_seconds=3).cleanup_verified


@pytest.mark.parametrize('ending', ['startup', 'crash', 'stopped', 'success'])
def test_app_server_root_exit_settles_without_waiting_for_work_deadline(tmp_path, ending):
    processes, cleanups = [], []
    lease = SimpleNamespace(attempt_id='fixture-root-exit',
        _payload={'owner_pid': 1, 'owner_creation_time': 1},
        require_reserved=lambda *a: None, bind_process=lambda *a: None)
    script = '''import sys,json,time
sid='00000000-0000-0000-0000-000000000001'
for line in sys.stdin:
 r=json.loads(line);m=r.get('method');i=r.get('id')
 if i is None:continue
 if ENDING=='startup':
  exec(CHILD_SCRIPT)
  break
 if m=='turn/start':exec(CHILD_SCRIPT)
 result={'thread':{'id':sid}} if m in ('thread/start','thread/resume') else {'data':[]} if m=='model/list' else {'turn':{'id':'fixture-turn','status':'inProgress'}} if m=='turn/start' else {}
 print(json.dumps({'id':i,'result':result}),flush=True)
 if m=='turn/start':
  if ENDING=='stopped':time.sleep(30)
  if ENDING=='success':
   print(json.dumps({'method':'item/completed','params':{'threadId':sid,'turnId':'fixture-turn','item':{'id':'final','type':'agentMessage','phase':'final_answer','text':'buffered final answer'}}}),flush=True)
   print(json.dumps({'method':'turn/completed','params':{'threadId':sid,'turn':{'id':'fixture-turn','status':'completed','items':[],'error':None}}}),flush=True)
  break
'''.replace('CHILD_SCRIPT', repr(SPAWN_CHILD)).replace('ENDING', repr(ending))
    def spawn(argv, **kwargs):
        process = native.OwnedProcess.spawn([sys.executable, '-B', '-c', script], **kwargs)
        processes.append(process)
        return process
    def on_turn(binding):
        if ending == 'stopped':
            from foundry_runtime._execution_stop import _terminate
            _terminate(binding)
    def run():
        return transport.run_executor(sys.executable, 'fixture', tmp_path, tmp_path/'run.log',
            tmp_path/'result.txt', 'fixture', tmp_path/'active.json', lambda pid: None,
            lease=lease, on_cleanup=cleanups.append, on_turn=on_turn, process_factory=spawn,
            app_server_policy={'overall_seconds': 3, 'interrupt_grace_seconds': .01})
    try:
        # A root exit without a terminal is unknown execution, not a work timeout.
        if ending == 'success':
            assert run()[0] == 0
            assert (tmp_path/'result.txt').read_text(encoding='utf-8') == 'buffered final answer'
        else:
            with pytest.raises(transport.RpcError if ending == 'startup' else transport.UncertainExecution):
                run()
        assert len(cleanups) == 1 and cleanups[0].cleanup_verified
        assert cleanups[0].job_active_processes == 0
        assert not (tmp_path/'active.json').exists()
        assert '"expiration_cause": null' in (tmp_path/'run.log').read_text(encoding='utf-8')
    finally:
        for process in processes:
            assert process.stop_and_verify(drain_seconds=0, kill_seconds=3).cleanup_verified
