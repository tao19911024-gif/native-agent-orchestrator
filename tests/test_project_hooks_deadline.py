import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

from foundry_runtime import _project_hooks as hooks, _task_deadline


@pytest.mark.parametrize('operation', ['review_context', 'accept_result'])
@pytest.mark.parametrize('deadline_case', ['expired', 'during', 'none'])
def test_project_hook_uses_task_deadline(tmp_path, monkeypatch, operation, deadline_case):
    script = tmp_path / 'observe.py'
    script.write_text(
        "import json,sys,time\n"
        "request=json.load(sys.stdin)\n"
        "time.sleep(float(sys.argv[1]))\n"
        "review=request['operation']=='review_context'\n"
        "result={'schema_version':request['schema_version'], 'subject':request['subject'], "
        "'status':'ready' if review else 'accepted'}\n"
        "result.update({key:[] for key in "
        "(('criteria','findings','checks') if review else ('reasons','evidence'))})\n"
        "print(json.dumps(result))\n", encoding='utf-8')
    selected = SimpleNamespace(
        project={'execution': {operation + '_argv': [sys.executable, str(script),
                                                    '2' if deadline_case == 'during' else '0']}},
        context=SimpleNamespace(record=lambda: {'project_id': 'synthetic'}), workspace=tmp_path)
    original_spawn = hooks.OwnedProcess.spawn
    processes = []

    def spawn(*args, **kwargs):
        process = original_spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(hooks.OwnedProcess, 'spawn', spawn)
    deadline = (time.time() - 1 if deadline_case == 'expired' else
                time.time() + .5 if deadline_case == 'during' else None)
    with _task_deadline.scope(deadline):
        result = hooks.observe(selected, operation, tmp_path / 'evidence',
                               task={'order': 'synthetic'}, subject={'candidate_commit': 'candidate'}, env=None)
    folder = Path(result['observation']['path']).parent
    if deadline_case == 'expired':
        assert not processes
        assert not (folder / 'cleanup.json').exists()
    else:
        assert len(processes) == 1
        assert processes[0].poll() is not None
        assert json.loads((folder / 'cleanup.json').read_bytes())['cleanup_verified']
    if deadline_case == 'none':
        assert result['status'] == ('ready' if operation == 'review_context' else 'accepted')
    else:
        assert result['status'] == 'not_evaluated'
        assert 'task deadline' in result['error']
