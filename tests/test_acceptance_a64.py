import sqlite3
import pytest
from foundry_runtime._goal_intervention import protocol_markers
from foundry_runtime._tool_discipline import check

DONE = 'FOUNDRY_DELIVERABLE_DONE'
QUESTION = 'FOUNDRY_MANAGER_QUESTION'

@pytest.mark.parametrize('fence', ['```python', '~~~text', '````json'])
def test_tagged_fences_keep_examples_out_and_real_markers_in(fence):
    close = fence[:len(fence)-len(fence.lstrip('`~'))]
    assert protocol_markers(f'{fence}\n{DONE}\n{close}') == set()
    assert protocol_markers(f'{fence}\ncode\n{close}\n{DONE} handoff') == {DONE}
    assert protocol_markers(f'{fence}\ncode\n{close}\n{DONE}\n{fence}\ncode\n{close}\n{QUESTION}') == {DONE, QUESTION}
    assert protocol_markers(f'{fence}\n{DONE}') == set()

@pytest.mark.parametrize('text,expected', [
    ('```x```\n'+DONE, {DONE}),
    ('````python\n```\n'+DONE+'\n````\n'+QUESTION, {QUESTION}),
    ('```python\n~~~\n'+DONE+'\n```\n'+QUESTION, {QUESTION}),
    ('```python\n```suffix\n'+DONE+'\n```\n'+QUESTION, {QUESTION}),
    ('- **'+DONE+'**', {DONE}), ('> > '+QUESTION, {QUESTION}), ('1. '+DONE, {DONE}),
    ('- ```python\n  '+DONE+'\n  ```\n'+QUESTION, {QUESTION}),
])
def test_marker_wrappers_and_exact_fence_closure(text, expected):
    assert protocol_markers(text) == expected

@pytest.mark.parametrize('command,allowed', [
    ('rg -C2 foo notes.md', True), ('rg -A 2 -g "*.md" foo notes.md', True),
    ('rg -t markdown --context=2 foo notes.md', True), ('grep -rn foo build.log', True),
    ('Get-Content build.log | Select-String foo', True),
    ('Get-Content -Path build.log -Tail 20 | Select-String -Pattern foo | rg bar', True),
    ('rg -C2 foo src/', False), ('grep -rn foo src', False),
    ('rg foo notes.md src/', False), ('rg -C2 foo', False),
    ('Get-Content build.log | rg foo src/', False),
    ('Get-Content build.log | Get-Content src/code.py | Select-String foo', False),
    ('rg --unknown foo notes.md', True), ('rg foo notes.md; rg foo src/', False),
    ('rg "foo|bar" notes.md | grep -i foo', True),
    ('Get-Content src/code.py | Select-String foo', False),
])
@pytest.mark.parametrize('tool', ['Bash', 'PowerShell', 'exec_command'])
def test_search_forms_keep_source_scope(tmp_path, command, allowed, tool):
    (tmp_path / '.codegraph').mkdir(); (tmp_path / 'src').mkdir()
    (tmp_path / 'src/code.py').write_text('code')
    (tmp_path / 'notes.md').write_text('notes'); (tmp_path / 'build.log').write_text('log')
    with sqlite3.connect(tmp_path / '.codegraph/codegraph.db') as db:
        db.execute('create table files(path text)'); db.execute("insert into files values ('src/code.py')")
    result = check({'tool_name': tool, 'cwd': str(tmp_path), 'tool_input': {'command': command}},
                   {'index_available': True, 'index_workspace': str(tmp_path)})
    assert (result.get('hookSpecificOutput', {}).get('permissionDecision') != 'deny') == allowed
