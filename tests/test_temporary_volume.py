import pytest
from foundry_runtime import _tool_discipline as hook


@pytest.mark.parametrize('path,base,denied', [
    ('H:/PDCL-archive/command-evidence/TAG/HANDOFF.md', None, False),
    ('/root/x', 'G:/WSL/Ubuntu', False),
    ('/var/tmp/x', 'G:/WSL/Ubuntu', False),
    ('C:/Users/test/AppData/Local/Temp/x', None, True),
    ('/tmp/x', 'C:/WSL/Ubuntu', True),
    ('/mnt/c/Users/test/AppData/Local/Temp/x', 'G:/WSL/Ubuntu', True),
    ('/tmp/x', None, False),
])
def test_storage_not_linux_path_spelling(path, base, denied, monkeypatch):
    monkeypatch.setattr(hook, '_distro_base', lambda name: base)
    command = ('wsl -d Ubuntu -- sh -c ' if path.startswith('/') else '') + 'echo text > ' + path
    reason = hook.temporary_verdict(command)
    assert bool(reason) == denied
    if denied:
        assert 'No temporary files on C:' in reason
        assert 'H:/PDCL-archive/command-evidence/' in reason


def test_explicit_non_c_mktemp_and_powershell():
    assert hook.temporary_verdict('mktemp -p H:/PDCL-archive/scratch') is None
    assert hook.check({'tool_name': 'PowerShell', 'tool_input': {
        'command': 'Set-Content C:/Users/test/AppData/Local/Temp/x hello'}}, {})


def test_reading_temp_and_handoff_mentioning_temp_are_not_temp_writes():
    assert hook.temporary_verdict('cat /tmp/old') is None
    assert hook.temporary_verdict('echo "read /tmp/old" > H:/PDCL-archive/command-evidence/T/HANDOFF.md') is None
    assert hook.temporary_verdict('echo "example > /tmp/old; Set-Content /tmp/x text" > H:/evidence/HANDOFF.md') is None
    assert hook.temporary_verdict('Path("H:/evidence/HANDOFF.md").write_text("example > /tmp/old")') is None


@pytest.mark.parametrize('command', ['cp existing /tmp/new', 'touch /tmp/new',
    'mkdir /tmp/new', 'echo text | tee /tmp/new', 'Path("/tmp/new").write_text("text")'])
def test_explicit_temporary_output_remains_refused(command, monkeypatch):
    monkeypatch.setenv('TEMP', 'C:/Users/test/AppData/Local/Temp')
    assert hook.temporary_verdict(command)


def test_patch_body_is_not_an_output_target():
    assert hook.check({'tool_name': 'apply_patch', 'tool_input': {'command':
        '*** Add File: H:/evidence/HANDOFF.md\n+Do not use /tmp/\n+example > /tmp/file'}}, {}) == {}
