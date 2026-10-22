"""Small native-adapter helpers kept outside the lifecycle owner."""
from __future__ import annotations

import hashlib
import re
import json
import os
import sys
import shutil
import tempfile
from pathlib import Path


def resolve_executable(backend="codex"):
    if backend == "codex":
        from ._codex_command import resolve_codex
        return Path(resolve_codex())
    if backend != "claude":
        raise ValueError("unknown native backend")
    candidates = list((Path.home() / ".vscode/extensions").glob(
        "anthropic.claude-code-*-win32-x64/resources/native-binary/claude.exe"))
    retained = Path(sys.prefix) / 'native/claude'
    def version(path):
        match = re.search(r"(?:claude-code-|[/\\])(\d+)\.(\d+)\.(\d+)(?:-|[/\\])", str(path))
        return tuple(map(int, match.groups())) if match else (0, 0, 0)
    if not candidates:
        copies = list(retained.glob('*/*/claude.exe'))
        if not copies:
            raise FileNotFoundError("native Claude is unavailable; install the Claude Code extension once to seed this runtime")
        selected = max(copies, key=version)
        if executable_sha256(selected) != selected.parent.name:
            raise OSError(f'retained Claude executable bytes changed: {selected}')
        return selected.resolve(strict=True)
    source = max(candidates, key=version).resolve(strict=True)
    digest = executable_sha256(source)
    destination = retained / '.'.join(map(str, version(source))) / digest / 'claude.exe'
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        # Publish a complete independent copy. A hard link only publishes the
        # temporary copy, never the extension's updatable source file.
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
        try:
            shutil.copyfile(source, temporary)
            if executable_sha256(temporary) != digest:
                raise OSError('Claude extension changed during capture; retry resolution')
            try:
                os.link(temporary, destination)
            except FileExistsError:
                pass  # Another resolver published the same content.
        finally:
            temporary.unlink(missing_ok=True)
    if executable_sha256(destination) != digest:
        raise OSError(f'retained Claude executable bytes changed: {destination}')
    return destination.resolve(strict=True)


def executable_sha256(path: Path) -> str:
    """Hash a chosen executable without running an unowned helper process."""
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def native_manifest_policy(*, executable: Path, executable_sha256: str,
                           adapter_sha256: str, timeout_seconds, kill_seconds,
                           max_input_bytes, max_output_bytes, max_concurrency,
                           max_total_input_bytes, max_total_output_bytes,
                           backend: str, backend_identity, requested_compute,
                           shadow_identity, workspace, role_compute=None):
    """Build the immutable native-recovery identity from validated inputs."""
    return {"schema_version": 7, "executable": str(executable.resolve()), "runtime_provenance": {"python": sys.executable},
            "executable_sha256": executable_sha256,
            "recovery_requires_stable_call_id": True, "ordinal_calls_resumable": False,
            "adapter_sha256": adapter_sha256,
            "timeout_seconds": timeout_seconds, "kill_seconds": kill_seconds,
            "max_input_bytes": max_input_bytes, "max_output_bytes": max_output_bytes,
            "max_concurrency": max_concurrency, "allocation_profile": "native-lifetime-v2-optional-capture",
            "max_total_input_bytes": max_total_input_bytes, "max_total_output_bytes": max_total_output_bytes,
            "backend": backend, "backend_identity": backend_identity,
            "requested_compute": requested_compute or None,
            **({"role_compute": role_compute} if role_compute is not None else {}),
            "shadow_identity": shadow_identity,
            "workspace": str(Path(workspace).resolve()) if workspace is not None else None}


def native_policy_matches(recorded, current):
    # Interpreter provenance is an observation, not another admission predicate.
    def policy(value):
        value = {k: v for k, v in value.items() if k != 'runtime_provenance'}
        if os.name == 'nt':
            for key in ('executable', 'workspace'):
                if value.get(key):
                    value[key] = os.path.normcase(os.path.normpath(value[key]))
        return value
    return policy(recorded) == policy(current)


def continuation_error(reason, folder):
    """Name the retained interpreter when the existing identity check refuses."""
    hint = '; same-session cross-version continuation is unsupported; eligible settled goals can use --foundry-goal --recover-goal <goal> --migrate-runtime to start a new generation (see EXPLICIT_RECOVERY_TRANSITIONS.md)'
    folder = Path(folder)
    candidates = [folder / 'native-manifest.json', folder.parent / 'native-manifest.json']
    for path in candidates:
        try:
            row = json.loads(path.read_bytes())
            executable = row.get('executable')
            if executable and not Path(executable).is_file():
                reason += ('; recorded native executable is missing: ' + executable
                           + '; required sha256=' + str(row.get('executable_sha256', 'unavailable'))
                           + '; restore the exact recorded binary at that path, or migrate an eligible settled goal'
                           + ' using the selected runtime and --migrate-runtime (select --handoff-role when needed)')
            python = row.get('runtime_provenance', {}).get('python')
            if python:
                return reason + '; use the recorded interpreter ' + python + ' with the original native CLI/configuration' + hint
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    # a7 direct analyst calls predate manifest provenance but record this hook.
    import shlex
    for path in [folder / 'invocation.json', *sorted(folder.glob('call-*/invocation.json'))]:
        try:
            args = json.loads(path.read_bytes())['argv']
            config = json.loads(args[args.index('--settings') + 1])
            for group in config.get('hooks', {}).get('PreToolUse', []):
                for hook in group.get('hooks', []):
                    parts = shlex.split(hook.get('command', ''))
                    if parts[1:] == ['-I', '-m', 'foundry_runtime._claude_tool_input']:
                        return reason + '; use the recorded interpreter ' + parts[0] + ' with the original native CLI/configuration' + hint
        except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
            continue
    for path in [folder / 'manifest.json', folder.parent / 'manifest.json', folder.parent.parent / 'manifest.json']:
        try:
            python = json.loads(path.read_bytes()).get('runtime_provenance', {}).get('python')
            if python:
                return reason + '; use the recorded interpreter ' + python + ' with the original native CLI/configuration' + hint
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return reason + '; use <recorded-python> from the original launch and its native CLI/configuration; interpreter provenance is unavailable' + hint
def accounting_observations(result, backend):
    """Usage describes cost coverage; it cannot invalidate a completed answer."""
    import math
    notes = []
    usage = result.get('usage')
    if not isinstance(usage, dict):
        notes.append('usage missing or not an object; token coverage unavailable')
    else:
        required = {'input_tokens', 'output_tokens'}
        if backend == 'codex':
            required.add('cached_input_tokens')
        for key in sorted(required | {k for k in usage if k.endswith('_tokens')}):
            value = usage.get(key)
            if type(value) is not int or value < 0:
                notes.append(key + ' missing or invalid; not a measured token count')
        if (backend == 'codex' and type(usage.get('cached_input_tokens')) is int
                and type(usage.get('input_tokens')) is int
                and usage['cached_input_tokens'] > usage['input_tokens']):
            notes.append('cached_input_tokens exceeds input_tokens; counters are inconsistent')
    cost = result.get('total_cost_usd')
    if cost is not None and (type(cost) not in (int, float)
            or (type(cost) is float and not math.isfinite(cost)) or cost < 0):
        notes.append('total_cost_usd invalid; not a measured cost')
    if backend == 'claude' and (type(result.get('num_turns')) is not int or result['num_turns'] < 1):
        notes.append('num_turns missing or invalid; turn coverage unavailable')
    return notes
