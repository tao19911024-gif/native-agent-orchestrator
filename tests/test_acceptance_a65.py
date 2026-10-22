import sqlite3
import pytest
from foundry_runtime._tool_discipline import check, SEARCH_VERBS, SEARCH

@pytest.mark.parametrize('tool', ['Bash', 'PowerShell', 'exec_command'])
@pytest.mark.parametrize('command,allowed', [
    ('rg foo ..', False), ('rg foo {parent}', False), ('rg foo H:/x.md', True),
    ('findstr /s foo *.py', False), ('sls foo src/a.py', False),
    ('egrep foo src/', False), ('fgrep foo src/', False), ('findstr foo notes.md', True),
    ("rg 'foo$' notes.md", True), ("rg 'a;b' notes.md", True), ("rg '=>' notes.md", True),
    ('rg -c foo notes.md', True), ('rg --json --stats foo notes.md', True), ('grep -I foo notes.md', True),
    ('Select-String -Pattern foo -Path notes.md -SimpleMatch -CaseSensitive', True),
    ('rg foo "my notes.md"', True), ("rg 'foo$' src/", False), ('rg -c foo src/', False),
    ('rg foo $(ls src)', False), ('rg foo notes.md | grep -r bar', False),
    ('rg foo notes.md | grep -R bar', False), ('rg foo notes.md | grep --recursive bar', False),
    ('rg foo notes.md | grep bar', True), ('rg foo notes.md | grep -rn bar', False),
    ('sls -Pattern foo -Path H:/x.md,src/a.py', False),
    ('rg "foo$" notes.md', True), ('rg "foo$" "src/a.py"', False),
])
def test_natural_search_forms(tmp_path, tool, command, allowed):
    root=tmp_path/'project'; (root/'.codegraph').mkdir(parents=True); (root/'src').mkdir()
    (root/'src/a.py').write_text('source')
    for name in ('notes.md', 'my notes.md'): (root/name).write_text('notes')
    with sqlite3.connect(root/'.codegraph/codegraph.db') as db:
        db.execute('create table files(path text)'); db.execute("insert into files values ('src/a.py')")
    command=command.format(parent=tmp_path.as_posix())
    result=check({'tool_name':tool,'cwd':str(root),'tool_input':{'command':command}},
                 {'index_available':True,'index_workspace':str(root)})
    assert (result.get('hookSpecificOutput',{}).get('permissionDecision') != 'deny') == allowed


def test_detection_and_operand_parsing_share_verbs():
    for name in SEARCH_VERBS:
        assert SEARCH.search(name+' foo src/')
