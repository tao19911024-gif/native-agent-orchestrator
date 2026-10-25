"""Codex repository-agent protocol, with process custody owned by NativeAnalyst.

The agent runs with the ordinary Codex tool surface in the target repository.
This module records visible tool events and validates the final structured
result; it does not choose what the agent reads or how it performs the work.

Protocol source: https://developers.openai.com/codex/noninteractive
Installed CLI/schema observations: evidence/FOUNDRYG1/codex-backend-research.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import tomllib

from . import _codex_command
from .foundry_collaboration import SchemaResponseError, validate_response_shape


_SKILL_CONTEXT_NOTICE = (
    'Skill descriptions were shortened to fit the skills context budget. '
    'Codex can still see every skill, but some descriptions are shorter. '
    'Disable unused skills or plugins to leave more room for the rest.')


def decode_event(raw):
    """Decode exec events, retaining both IDs of flattened web-search items.

    rust-v0.156.0 exec_events.rs flattens WebSearchItem.id into ThreadItem.id.
    The first ID belongs to the event lifecycle, the second to the search.
    This normalization is only for that event field, never the final verdict.
    """
    class Pairs(list):
        pass

    def constant(value):
        raise _error(f'nonfinite JSON number {value}')

    try:
        pairs = json.loads(raw, object_pairs_hook=Pairs, parse_constant=constant)
        event_type = dict(pairs).get('type') if isinstance(pairs, Pairs) else None

        def convert(value, path=()):
            if isinstance(value, Pairs):
                search = (path == ('item',) and event_type in
                          {'item.started', 'item.updated', 'item.completed'}
                          and dict(value).get('type') == 'web_search')
                ids = [v for k, v in value if k == 'id']
                flattened = search and len(ids) == 2 and all(isinstance(v, str) and v for v in ids)
                result = {}
                for key, child in value:
                    if key in result:
                        if key == 'id' and flattened and 'search_id' not in dict(value):
                            result['search_id'] = child
                            continue
                        raise _error(f'duplicate JSON key {key}')
                    result[key] = convert(child, path + (key,))
                return result
            if isinstance(value, list):
                return [convert(child, path + (i,)) for i, child in enumerate(value)]
            return value

        return convert(pairs)
    except (ValueError, UnicodeError, TypeError, RecursionError) as exc:
        raise _error(f'invalid native JSON: {exc}') from exc


def _error(message):
    # NativeAnalyst imports its backend; retain its single exception authority.
    from .foundry_native import NativeError
    return NativeError(message)


def codex_metadata() -> dict:
    return {
        'protocol_profile': 'codex-exec-repository-agent-v2',
        'final_response_selection': 'last_completed_agent_message_at_turn_completion',
        'schema_projection': 'codex-structured-output-v1',
        'capability_profile': 'repository-agent',
        'native_cost_threshold_usd': None,
        'cost_reporting': 'unavailable',
        'model_identity': 'not_reported_by_exec_json',
        'tool_boundary': 'record_visible_tool_events',
        'configuration_limit': 'user_config_digest_only; other_managed_project_plugin_context_not_frozen',
    }


def codex_schema(host_schema: dict) -> dict:
    """Derive transport schema; the unchanged host contract governs admission.

    Installed probe-4 rejects uniqueItems. OpenAI Structured Outputs documents
    string enums and array maxItems; maxItems:0 is equivalent to the host's
    sole constant, []. String bounds/patterns remain for the non-fine-tuned
    native model. No fallback removes additional constraints after failures.
    https://developers.openai.com/api/docs/guides/structured-outputs
    """
    projected = copy.deepcopy(host_schema)
    if projected.get('uniqueItems'):
        hint = 'All array items must be unique; the host rejects duplicate items.'
        projected['description'] = ' '.join(filter(None, (projected.get('description'), hint)))
    projected.pop('uniqueItems', None)
    if ('enum' in projected and 'type' not in projected and projected['enum']
            and all(isinstance(value, str) for value in projected['enum'])):
        projected['type'] = 'string'
    if projected.get('const') == []:
        del projected['const']
        projected.update(type='array', items={'type': 'string'}, maxItems=0)
    if 'properties' in projected:
        projected['properties'] = {key: codex_schema(value) for key, value in projected['properties'].items()}
    if 'items' in projected:
        projected['items'] = codex_schema(projected['items'])
    if 'anyOf' in projected:
        projected['anyOf'] = [codex_schema(branch) for branch in projected['anyOf']]
    return projected


def codex_argv(executable: Path, schema_path: Path, *, workspace: Path, model=None, effort=None,
               persistent=False, resume_session=None, execution_policy=None, launch_context=None) -> list[str]:
    """Run the native role as a normal repository-aware Codex agent."""
    argv = _codex_command.coding_argv(executable, 'exec', workspace=workspace, launch_context=launch_context, **_codex_command.compute_kwargs(model, effort),
                                   **({"execution_policy": execution_policy} if execution_policy is not None else {}))
    # Managers retain the native conversation. Independent one-shot roles keep
    # their existing ephemeral execution; a resume names exactly its predecessor.
    return [*argv, '-C', str(Path(workspace).resolve()), '--json',
            *([] if persistent or resume_session else ['--ephemeral']),
            '--skip-git-repo-check', '--color', 'never',
            '--output-schema', str(schema_path),
            *(['resume', resume_session] if resume_session else []), '-']


def codex_backend_env(executable: Path) -> dict[str, str]:
    """Reuse environment custody; callers record identity, never credential values."""
    return _codex_command.codex_env(executable)


def usage_observation(call):
    """Read a retained exec session total; resumed exec totals are cumulative."""
    folder = Path(call['evidence'])
    path = folder / 'receipt.json'
    raw = path.read_bytes()
    receipt = json.loads(raw)
    if (receipt.get('backend') != 'codex' or receipt.get('call_id') != call['call_id']
            or receipt.get('attempt_id') != call.get('attempt_id')
            or receipt.get('usage') != call.get('usage')):
        raise ValueError('Codex usage receipt differs from selected execution')
    protocol, digest, size = CodexStreamProtocol(), hashlib.sha256(), 0
    with (folder / 'stdout.jsonl').open('rb') as stream:
        for line in stream:
            digest.update(line); size += len(line)
            if line.strip():
                protocol.accept(decode_event(line))
    _, init, result = protocol.finish()
    if (size != receipt.get('stdout_bytes') or digest.hexdigest() != receipt.get('stdout_sha256')
            or init['session_id'] != receipt.get('session_id') or result['usage'] != receipt.get('usage')):
        raise ValueError('Codex session usage stream differs from receipt')
    return {**call, 'session_id': init['session_id'], 'ended_utc': receipt.get('ended_utc'),
            'source': {'path': str(path.resolve()), 'sha256': hashlib.sha256(raw).hexdigest()}}


def _user_config_path(executable: Path) -> Path:
    environment = codex_backend_env(executable)
    return Path(environment.get('CODEX_HOME') or Path.home() / '.codex') / 'config.toml'


def _file_identity(path: Path) -> dict:
    path = path.resolve()
    try:
        with path.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
    except FileNotFoundError:
        return {'path': str(path), 'exists': False, 'sha256': None}
    return {'path': str(path), 'exists': True, 'sha256': digest}


def _config_identity(path: Path, scope_root=None) -> dict:
    path = path.resolve()
    try:
        with path.open('rb') as stream:
            config = tomllib.load(stream)
    except FileNotFoundError:
        return {'path': str(path), 'exists': False, 'sha256': None, 'projection': 1}
    except tomllib.TOMLDecodeError:
        raise _error('Codex user configuration is not valid TOML') from None
    # Official config-reference describes these keys as presentation settings.
    # Unknown keys remain bound. Never persist config text or credential values.
    tui = config.get('tui')
    if isinstance(tui, dict):
        for key in ('theme', 'status_line', 'notifications', 'notification_method', 'animations', 'keymap'):
            tui.pop(key, None)
        if not tui:
            config.pop('tui')
    projects = config.get('projects')
    if scope_root is not None and isinstance(projects, dict):
        root = Path(scope_root).resolve()
        for key in list(projects):
            project = Path(key)
            # Relative or unfamiliar paths remain bound rather than guessed away.
            if project.is_absolute():
                project = project.resolve()
                if not (root.is_relative_to(project) or project.is_relative_to(root)):
                    del projects[key]
        if not projects:
            config.pop('projects')
    raw = json.dumps(config, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
                     default=lambda value: {'toml_type': type(value).__name__, 'value': value.isoformat()})
    return {'path': str(path), 'exists': True, 'sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest(),
            'projection': 1}


def backend_identity(executable: Path, *, scope_root=None) -> dict:
    """Freeze adapter/config digests without capturing auth or config contents.

    NativeAnalyst separately binds the executable bytes. This intentionally does
    not claim to freeze every managed, project, plugin or environment input.
    """
    return {'metadata': codex_metadata(),
            'adapter': _file_identity(Path(__file__)),
            'command_policy': _file_identity(Path(_codex_command.__file__)),
            'user_config': _config_identity(_user_config_path(executable), scope_root)}


class CodexStreamProtocol:
    """One admission validator for incremental capture and receipt recovery.

    Successful visible streams contain one thread, one turn, progress/tool items,
    a last completed agent message containing the JSON result, and terminal usage.
    Select that message at turn completion, as Codex SDK Thread.run does; message
    JSON shape is not a completion event.
    Additional progress/tool variants remain telemetry; they cannot supply a verdict.
    """

    def __init__(self, expected_schema=None):
        self.schema = expected_schema
        self.init = self.result = self.decoded = None
        self.schema_failure = None
        self.tool_events = []
        self._turn_started = False
        self._item_ids = set()
        self._last_message = None

    def accept(self, row):
        if not isinstance(row, dict):
            raise _error('Codex event must be an object')
        kind = row.get('type')
        if kind not in {'thread.started', 'turn.started', 'turn.completed', 'turn.failed',
                        'error', 'item.started', 'item.updated', 'item.completed'}:
            # Provider extensions are telemetry, not a verdict or a failed turn.
            # The original event remains in stdout, including after completion.
            return
        if self.result is not None:
            raise _error('Codex event after terminal turn')
        if self.init is not None and 'thread_id' in row and row['thread_id'] != self.init['session_id']:
            raise _error('Codex stream changed thread identity')
        if kind in ('error', 'turn.failed'):
            error = row.get('error') if kind == 'turn.failed' else row
            message = error.get('message') if isinstance(error, dict) else None
            if isinstance(message, str) and message:
                # Exec reports stream retry progress as error events, including
                # before thread.started. Completion still requires the identity.
                retry = re.match(r'^Reconnecting\.\.\.\s+(\d+)/(\d+)(?:\s|$)', message) if kind == 'error' else None
                if retry and 0 < int(retry[1]) < int(retry[2]):
                    return
                from .foundry_native import NativeProviderError
                raise NativeProviderError('native Codex failure: ' + message, event=row)
        if self.init is None:
            thread_id = row.get('thread_id')
            if (kind != 'thread.started' or set(row) != {'type', 'thread_id'}
                    or not isinstance(thread_id, str)
                    or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', thread_id)):
                raise _error('Codex stream must start with one valid thread identity')
            self.init = {'session_id': thread_id, 'model': None}
            return
        # Completion and session identity remain authoritative; extra telemetry
        # fields on these events do not change their meaning.
        if kind == 'turn.started':
            if self._turn_started:
                raise _error('Codex stream has unexpected or duplicate turn identity')
            self._turn_started = True
            return
        if not self._turn_started:
            raise _error('Codex event before turn start')
        if kind in {'item.started', 'item.updated'}:
            # In-flight tools and child progress never supply the final answer.
            return
        if kind == 'item.completed':
            item = row.get('item')
            if (not isinstance(item, dict)
                    or not isinstance(item.get('id'), str) or not item['id']
                    or item['id'] in self._item_ids):
                raise _error('Codex item is malformed or duplicated')
            # Captured from installed 0.153.4 in codex-adapter-probe-3. This is
            # one client diagnostic, not permission to swallow error items.
            if (set(item) == {'id', 'type', 'message'} and item['type'] == 'error'
                    and item['message'] == _SKILL_CONTEXT_NOTICE):
                self._item_ids.add(item['id'])
                return
            if item.get('type') == 'error':
                raise _error('Codex error item')
            if item.get('type') not in {'reasoning', 'agent_message'}:
                self._item_ids.add(item['id'])
                self.tool_events.append(copy.deepcopy(item))
                return
            if not isinstance(item.get('text'), str):
                raise _error('Codex content item is malformed')
            self._item_ids.add(item['id'])
            if item['type'] == 'reasoning':
                return
            # Native exec has no final/commentary field. Keep every message in
            # order until the terminal event identifies the last one as final.
            self._last_message = copy.deepcopy(item)
            self.tool_events.append(self._last_message)
            return
        if kind == 'turn.completed':
            if row.get('error'):
                raise _error('Codex terminal turn reports an error')
            usage = row.get('usage')
            if self._last_message is None:
                raise _error('Codex terminal turn lacks a final response')
            from .foundry_native import _json
            decoded = _json(self._last_message['text'])
            if not isinstance(decoded, dict):
                raise _error('Codex final agent message must be a JSON object')
            if self.schema is not None:
                try:
                    validate_response_shape(decoded, self.schema)
                except SchemaResponseError as exc:
                    if not exc.repairable:
                        raise _error(f'Codex structured response schema failed: {exc}') from exc
                    self.schema_failure = exc
            self.decoded = decoded
            self.tool_events.remove(self._last_message)
            from .foundry_native_support import accounting_observations
            self.result = {'usage': usage, 'total_cost_usd': None,
                           'accounting_observations': accounting_observations(row, 'codex')}
            return
        raise _error('Codex failure, extra turn or unknown event')

    def finish(self):
        if self.init is None or not self._turn_started or self.decoded is None or self.result is None:
            raise _error('Codex stream lacks a complete unique thread/turn/result')
        return self.decoded, self.init, self.result
