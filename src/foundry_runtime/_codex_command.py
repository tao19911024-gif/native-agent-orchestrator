"""Native Codex resolution and the trusted policy selected for both coding transports.

This preserves command's existing trusted executor permissions, not OS write
isolation. Custody, budgets and authority remain the dispatcher's responsibility.
User model selection remains inherited unless a complete task override is supplied.
Authentication and rules remain inherited.
"""
from . import _execution_profile as profiles
import json
import os
from pathlib import Path
import shutil
from ._compute_selection import compute_kwargs


def resolve_codex() -> str:
    found = shutil.which('codex') or shutil.which('codex.cmd')
    if not found:
        raise FileNotFoundError('Codex is not on PATH')
    path = Path(found).resolve(strict=True)
    if path.suffix.lower() == '.exe':
        return str(path)
    if path.suffix.lower() != '.cmd':
        raise ValueError('Unsupported Codex launcher; an installed native executable is required')
    package = path.parent / 'node_modules/@openai/codex'
    try:
        metadata = json.loads((package/'package.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('Unknown Codex batch wrapper; refusing shell fallback') from exc
    if metadata.get('name') != '@openai/codex':
        raise ValueError('Codex package identity is not @openai/codex')
    # Match the installed npm wrapper/package structure; do not copy the binary
    # away from its resources or execute a string-built shell command.
    native = package/'node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe'
    if not native.is_file():
        raise FileNotFoundError('Installed Windows x64 Codex native package is missing: ' + str(native))
    return str(native.resolve())


def codex_env(executable) -> dict[str, str]:
    child = {key: value for key, value in os.environ.items() if not key.upper().startswith('GIT_')}
    # A new executor owns its own identity and must not inherit its launching
    # desktop session's tool pipe. Keep native authentication/config in place.
    for name in ('CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CODEX_PERMISSION_PROFILE',
                 'CODEX_APP_TOOLS_PIPE_PATH'):
        child.pop(name, None)
    for parent in Path(executable).resolve().parents:
        if parent.name != 'codex':
            continue
        try:
            metadata = json.loads((parent/'package.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if metadata.get('name') != '@openai/codex':
            continue
        child['CODEX_MANAGED_PACKAGE_ROOT'] = str(parent)
        for manager in ('NPM', 'BUN', 'PNPM', 'VITE_PLUS'):
            child.pop('CODEX_MANAGED_BY_' + manager, None)
        child['CODEX_MANAGED_BY_NPM'] = '1'
        break
    return child


def coding_argv(executable, mode: str, *args, model=None, effort=None, native_team=None, execution_policy=None, workspace=None, launch_context=None) -> list[str]:
    if mode not in ('exec', 'app-server'):
        raise ValueError('Only existing exec and app-server transports are supported')
    # Codex 0.153.4 loses root -c values when more -c values occur after a
    # subcommand. Keep all generated options in the entrypoint's scope; the
    # installed config/read regression verifies policy, not only CLI acceptance.
    # Callers must also keep any additional -c before nested exec resume.
    # The operator selected the existing trusted executor contract (A). Keep it
    # explicit on every entry; never obtain this permission by a config fallback.
    policy = execution_policy if execution_policy is not None else profiles.coding_policy()
    settings = tuple(f'{key}={json.dumps(policy[key])}'
                     for key in ('default_permissions', 'approval_policy'))
    selection = compute_kwargs(model, effort)
    if selection:
        settings += (f'model={json.dumps(selection["model"])}',
                     f'model_reasoning_effort={json.dumps(selection["effort"])}')
    if native_team is not None:
        def toml(value):
            if isinstance(value, dict):
                return '{' + ', '.join(k + '=' + toml(v) for k, v in value.items()) + '}'
            return json.dumps(value)
        settings += tuple(key + '=' + toml(value) for key, value in native_team['config'].items())
    from ._launch_context import codex_settings
    if workspace is None and "-C" in args:
        workspace = args[args.index("-C") + 1]
    settings += tuple(codex_settings(workspace, launch=launch_context))
    return [str(executable), mode,
            *[part for setting in settings for part in ('-c', setting)], *map(str, args)]
