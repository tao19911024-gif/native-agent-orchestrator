import json
import os
from pathlib import Path
import shlex
import sys
import subprocess

import pytest

from foundry_runtime import _launch_context, foundry_native as native
from foundry_runtime._foundry_native_transport import same_argv
from foundry_runtime.foundry_native_support import native_policy_matches, continuation_error


@pytest.mark.skipif(os.name != 'nt', reason='Windows path spelling')
def test_hook_interpreter_spelling_only_is_normalised():
    left = _launch_context.claude_args(launch={'settings': _launch_context.settings(), 'server': None})
    config = json.loads(left[1])
    hook = config['hooks']['PreToolUse'][0]['hooks'][0]
    parts = shlex.split(hook['command'])
    parts[0] = parts[0].upper().replace('/', '\\')
    hook['command'] = shlex.join(parts)
    right = ['--settings', json.dumps(config)]
    assert same_argv(left, right)
    parts[0] = 'D:/different/python.exe'
    hook['command'] = shlex.join(parts)
    assert not same_argv(left, ['--settings', json.dumps(config)])
    del config['hooks']
    assert not same_argv(left, ['--settings', json.dumps(config)])
    assert native_policy_matches({'executable': 'D:/Tool.exe'}, {'executable': 'd:\\tool.exe'})


def test_recovery_refusal_names_recorded_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    folder = tmp_path / 'native'
    native.NativeAnalyst(Path(sys.executable), folder, backend='claude', workspace=tmp_path)
    manifest = folder / 'native-manifest.json'
    row = json.loads(manifest.read_bytes())
    row['adapter_sha256'] = 'old-version'
    row['runtime_provenance']['python'] = 'D:/retained/python.exe'
    manifest.write_text(json.dumps(row))
    with pytest.raises(native.NativeError, match='D:/retained/python.exe'):
        native.NativeAnalyst(Path(sys.executable), folder, backend='claude', workspace=tmp_path, resume=True)
    # Legacy native manifests obtain the interpreter from their owning goal.
    del row['runtime_provenance']
    manifest.write_text(json.dumps(row))
    (tmp_path / 'manifest.json').write_text(json.dumps({'runtime_provenance': {'python': 'D:/legacy/python.exe'}}))
    with pytest.raises(native.NativeError, match='D:/legacy/python.exe'):
        native.NativeAnalyst(Path(sys.executable), folder, backend='claude', workspace=tmp_path, resume=True)
    assert 'provenance is unavailable' in continuation_error('changed', tmp_path / 'absent' / 'child' / 'deep')
    direct = tmp_path / 'direct' / 'old-a7' / 'call-one'
    direct.mkdir(parents=True)
    args = _launch_context.claude_args(launch={'settings': _launch_context.settings(), 'server': None})
    (direct / 'invocation.json').write_text(json.dumps({'argv': args}))
    assert sys.executable.replace('\\', '/') in continuation_error('changed', direct.parent)


@pytest.mark.skipif(not os.environ.get('FOUNDRY_TEST_OLD_PYTHON'), reason='retained older installation required')
def test_real_old_runtime_manifest_is_refused_with_its_interpreter(tmp_path, monkeypatch):
    old = str(Path(os.environ['FOUNDRY_TEST_OLD_PYTHON']).resolve())
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    # Only construct the native owner; no model or native tool process is launched.
    script = ('from pathlib import Path; import sys; '
              'from foundry_runtime.foundry_native import NativeAnalyst; '
              'NativeAnalyst(Path(sys.executable), Path("native"), backend="claude", workspace=Path.cwd())')
    created = subprocess.run([old, '-I', '-c', script], cwd=tmp_path, capture_output=True, timeout=20)
    assert created.returncode == 0, created.stderr
    (tmp_path / 'manifest.json').write_text(json.dumps({'runtime_provenance': {'python': old}}))
    with pytest.raises(native.NativeError) as error:
        native.NativeAnalyst(Path(old), tmp_path/'native', backend='claude', workspace=tmp_path, resume=True)
    assert old in str(error.value) and 'cross-version continuation is unsupported' in str(error.value)
