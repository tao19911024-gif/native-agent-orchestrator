"""Extension updates must not remove an admitted manager's executable."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys

import pytest

from foundry_runtime import foundry_native_support as support
from foundry_runtime import _foundry_manager as manager, foundry_native as native
from test_manager_recovery import conversation
from test_installed_runtime import external, offline


@pytest.fixture
def installation(tmp_path, monkeypatch):
    home = tmp_path/'用户'
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    monkeypatch.setattr(sys, 'prefix', str(tmp_path/'runtime'))
    def install(version, content):
        path = home/f'.vscode/extensions/anthropic.claude-code-{version}-win32-x64/resources/native-binary/claude.exe'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path
    return install


def test_manager_resumes_after_extension_removal(conversation, installation, tmp_path, monkeypatch):
    source = installation('2.1.9', b'old native CLI')
    monkeypatch.setattr(manager, 'resolve_executable', support.resolve_executable)
    state, requests, _ = conversation
    with pytest.raises(native.NativeError):
        manager.turn(state, tmp_path, {'stage': 'assign'})
    manifest = Path(state['native_dir'])/'native-manifest.json'
    saved = manifest.read_bytes()
    executable = Path(json.loads(saved)['executable'])
    assert executable != source
    source.unlink()
    newer = installation('2.1.10', b'new native CLI')
    selected = support.resolve_executable('claude')
    assert selected != executable and selected.read_bytes() == newer.read_bytes()
    decision, _, _, _ = manager.turn(state, tmp_path, {'stage': 'assign', 'operator_answer': '继续'})
    assert decision['session_id'] == 'original-manager'
    assert all(Path(argv[0]) == executable for _, argv in requests)
    assert manifest.read_bytes() == saved


def test_normal_manager_next_turn_survives_extension_upgrade(conversation, installation, tmp_path, monkeypatch):
    source = installation('2.1.9', b'old native CLI')
    monkeypatch.setattr(manager, 'resolve_executable', support.resolve_executable)
    state, requests, failures = conversation
    failures.clear()
    first, state, _, _ = manager.turn(state, tmp_path, {'stage': 'assign'})
    executable = Path(requests[0][1][0])
    source.unlink()
    installation('2.1.10', b'new native CLI')
    second, _, _, _ = manager.turn(state, tmp_path, {'stage': 'worker_returned'})
    assert second['session_id'] == first['session_id']
    assert len(requests) == 2 and all(Path(argv[0]) == executable for _, argv in requests)
    assert executable.read_bytes() == b'old native CLI'
    assert executable.read_bytes() == b'old native CLI'


def test_concurrent_resolution_and_offline_reuse(installation):
    source = installation('2.1.10', b'CLI bytes')
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(lambda _: support.resolve_executable('claude'), range(8)))
    assert len(set(paths)) == 1
    assert paths[0] != source
    source.unlink()
    assert support.resolve_executable('claude') == paths[0]
    assert paths[0].read_bytes() == b'CLI bytes'
    assert not list(paths[0].parent.glob('*.tmp'))


def test_missing_recorded_cli_explains_identity_without_writes(tmp_path):
    source = tmp_path/'anthropic.claude-code-2.1.289-win32-x64/resources/native-binary/claude.exe'
    folder = tmp_path/'manager'
    folder.mkdir()
    manifest = folder/'native-manifest.json'
    manifest.write_text(json.dumps({'executable': str(source), 'executable_sha256': 'b'*64,
        'runtime_provenance': {'python': sys.executable}}), encoding='utf-8')
    saved = manifest.read_bytes()
    with pytest.raises(native.NativeError) as error:
        native.NativeAnalyst(source, folder, backend='claude', resume=True)
    message = str(error.value)
    assert '2.1.289' in message and 'b'*64 in message
    assert str(source) in message and '--migrate-runtime' in message
    assert manifest.read_bytes() == saved
    assert list(folder.iterdir()) == [manifest]


def test_missing_legacy_extension_can_migrate(conversation, installation, external, tmp_path, monkeypatch):
    from foundry_runtime import foundry_goal as goal, _foundry_goal_transitions as transitions
    from test_goal_preflight_recovery import retained_goal
    source = installation('2.1.9', b'legacy CLI')
    monkeypatch.setattr(manager, 'resolve_executable', lambda _: source)
    state, _, _ = conversation
    tree, scope, _, _ = external
    with pytest.raises(native.NativeError):
        manager.turn(state, tree, {'stage': 'assign'})
    source.unlink()
    installation('2.1.10', b'replacement CLI')
    root, folder, manifest = retained_goal(external, scope, tmp_path)
    manifest['manager_state'] = state
    goal._write(folder/'manifest.json', manifest)
    frozen = {p: p.read_bytes() for p in Path(state['native_dir']).rglob('*') if p.is_file()}
    migrated = transitions.manager(goal, folder, manifest, provider=None, migrate=True, answer=None)
    assert Path(migrated['manager_state']['executable']).read_bytes() == b'replacement CLI'
    assert migrated['manager_state']['native_dir'] != state['native_dir']
    assert all(p.read_bytes() == raw for p, raw in frozen.items())


def test_same_extension_version_new_bytes_preserve_old_copy(installation):
    source = installation('2.1.10', b'first')
    old = support.resolve_executable('claude')
    source.write_bytes(b'second')
    new = support.resolve_executable('claude')
    assert new != old
    assert old.read_bytes() == b'first' and new.read_bytes() == b'second'


def test_retained_cli_does_not_self_update(monkeypatch):
    from foundry_runtime.foundry_claude import environment
    monkeypatch.setenv('DISABLE_AUTOUPDATER', '0')
    assert environment(execution_environment={})['DISABLE_AUTOUPDATER'] == '1'
