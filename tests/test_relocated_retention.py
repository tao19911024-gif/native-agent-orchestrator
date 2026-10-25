"""Registered release aliases share dependency and deletion ownership."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from foundry_runtime import _release_retention as retention
from test_release_retention import estate, write


def link(alias, physical):
    if os.name == 'nt':
        quote = lambda p: "'" + str(p).replace("'", "''") + "'"
        command = 'New-Item -ItemType Junction -Path ' + quote(alias) + ' -Target ' + quote(physical) + ' | Out-Null'
        subprocess.run([str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'),
                        '-NoProfile', '-NonInteractive', '-Command', command], check=True, capture_output=True)
    else:
        alias.symlink_to(physical, target_is_directory=True)


@pytest.fixture
def relocated(estate):
    home, selection, versions, evidence = estate
    physical = home / 'relocated/0.1.0a5'
    write(physical / 'Scripts/python.exe', {})
    alias = home / 'releases/0.1.0a5'
    link(alias, physical)
    return alias, physical


@pytest.mark.parametrize('edge', ['process', 'scheduler', 'pth', 'venv'])
def test_registered_alias_protects_physical_installation(estate, relocated, monkeypatch, edge):
    home, selection, versions, evidence = estate
    alias, physical = relocated
    host = {'processes': [], 'registrations': []}
    if edge == 'process':
        host['processes'] = [{'ProcessId': 42, 'CommandLine': 'runner --python "'+str(alias / 'Scripts/python.exe')+'"'}]
    elif edge == 'scheduler':
        host['registrations'] = [{'name': 'Foundry-pending', 'actions': [
            {'Execute': 'runner.exe', 'Arguments': '--python "'+str(alias / 'Scripts/python.exe')+'"'}]}]
    else:
        executable = write(home / 'consumer/Scripts/python.exe', {})
        if edge == 'pth':
            (home / 'consumer/Lib/site-packages').mkdir(parents=True)
            (home / 'consumer/Lib/site-packages/release.pth').write_text('import sys; sys.path.insert(0, '+repr(str(alias / 'Lib/site-packages'))+')')
        else:
            (home / 'consumer/pyvenv.cfg').write_text('home = '+str(alias))
        host['processes'] = [{'ProcessId': 42, 'ExecutablePath': str(executable)}]
    monkeypatch.setattr(retention, 'host_snapshot', lambda: host)
    report = retention.inventory(selection, versions[2] / 'Scripts/python.exe', roots=[evidence])
    assert not report['gaps']
    assert str(physical) not in report['candidates']
    assert any(row['release'] == str(physical) for row in report['keep'])


@pytest.mark.skipif(os.name != 'nt', reason='Native release removal uses Windows')
@pytest.mark.parametrize('change', ['none', 'retarget', 'process'])
def test_native_alias_prune_rechecks_owner_and_live_references(estate, relocated, monkeypatch, change):
    home, selection, versions, evidence = estate
    alias, physical = relocated
    original = retention._remove_release
    child = None
    other = home / 'other/0.1.0a5'
    def remove(path, root, current, **kwargs):
        nonlocal child
        if change == 'retarget':
            other.mkdir(parents=True)
            alias.rmdir(); link(alias, other)  # Only this synthetic junction.
        elif change == 'process':
            script = 'import time; time.sleep(30) # '+str(alias / 'Scripts/python.exe')
            child = subprocess.Popen([sys.executable, '-B', '-c', script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return original(path, root, current, **kwargs)
    monkeypatch.setattr(retention, '_remove_release', remove)
    try:
        report = retention.prune(selection, versions[2] / 'Scripts/python.exe', [alias], roots=[evidence])
    finally:
        if child is not None:
            child.terminate(); child.wait()
    if change == 'none':
        assert report['deleted'] == [str(alias)] and not report['errors']
        assert not physical.exists() and not alias.exists() and not alias.is_junction()
    else:
        assert report['errors'] and not report['deleted']
        assert physical.exists() and alias.exists()
        if change == 'retarget':
            assert other.exists()
    assert all(path.exists() for path in versions)


def test_alias_removal_rejection_is_not_retried(estate, relocated, monkeypatch):
    _, selection, versions, evidence = estate
    alias, physical = relocated
    calls = []
    def rejected(*args):
        calls.append(1)
        raise OSError('blocked by policy')
    monkeypatch.setattr(retention, '_powershell', rejected)
    report = retention.prune(selection, versions[2] / 'Scripts/python.exe', [alias], roots=[evidence])
    assert len(calls) == 1 and report['errors'] and not report['deleted']
    assert physical.exists() and alias.exists()
