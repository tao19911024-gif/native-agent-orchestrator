import json
import subprocess
from pathlib import Path

import pytest

from foundry_runtime import release_selection as release


def test_switch_and_rollback_leave_a_resolved_run_unchanged(tmp_path, monkeypatch):
    def probe(argv, **kwargs):
        assert argv[1:3] == ["-I", "-c"]
        return json.dumps({"python": argv[0], "version": "a" if Path(argv[0]).parent.name == "old" else "b"})
    monkeypatch.setattr(subprocess, "check_output", probe)
    selection = tmp_path / "shared" / "current.json"
    old = tmp_path / "old" / "python.exe"
    new = tmp_path / "new" / "python.exe"
    release.select(selection, old, receipt=receipt(tmp_path, 'a'))
    retained = json.loads(selection.read_text())
    release.select(selection, new, receipt=receipt(tmp_path, 'b'))
    assert json.loads(selection.read_text())["python"] == str(new)
    assert retained["python"] == str(old)
    release.select(selection, old, receipt=receipt(tmp_path, 'a'))
    assert json.loads(selection.read_text()) == retained
    assert list(selection.parent.iterdir()) == [selection]


def test_failed_probe_preserves_current_selection(tmp_path, monkeypatch):
    selection = tmp_path / "current.json"
    selection.write_bytes(b'{"python":"original"}\n')
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])
    monkeypatch.setattr(subprocess, "check_output", fail)
    with pytest.raises(subprocess.CalledProcessError):
        release.select(selection, 'missing-runtime', receipt=receipt(tmp_path, 'b'))
    assert selection.read_bytes() == b'{"python":"original"}\n'


def test_replace_failure_cleans_temporary_file(tmp_path, monkeypatch):
    selection = tmp_path / "current.json"
    selection.write_text("original")
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: '{"python":"new","version":"b"}')
    def fail(*args):
        raise OSError("replace interrupted")
    monkeypatch.setattr(release.os, "replace", fail)
    with pytest.raises(OSError):
        release.select(selection, 'new', receipt=receipt(tmp_path, 'b'))
    assert selection.read_text() == "original"
    assert sorted(p.name for p in tmp_path.iterdir()) == ['accept-b.json', 'current.json']


def receipt(folder, version):
    path = folder / ('accept-' + version + '.json')
    path.write_text(json.dumps({'version': version, 'decision': 'accepted',
        'reviewer': 'fixture reviewer', 'source_tag': 'v' + version, 'source_commit': 'a'*40}))
    return path


def test_selection_requires_receipt_and_preserves_pointer(tmp_path, monkeypatch):
    selection = tmp_path / 'current.json'
    selection.write_text('original')
    with pytest.raises(SystemExit):
        release.main(['--selection', str(selection), '--python', 'new'])
    monkeypatch.setattr(subprocess, 'check_output', lambda *a, **k: '{"python":"new","version":"b"}')
    with pytest.raises(ValueError, match='acceptance receipt'):
        release.select(selection, 'new', receipt=receipt(tmp_path, 'a'))
    assert selection.read_text() == 'original'
