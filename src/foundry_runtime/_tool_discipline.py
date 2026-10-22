"""Shared native tool-call rules; no scheduler, concurrency limit or model router."""
import base64
import hashlib
import json
import os
import ntpath
from pathlib import Path
import re
import shlex
import subprocess
import sys

from ._code_index import TOOLS
from ._model_catalogue import tier_for_model
from ._compute_selection import MEMBER_TIERS

SEARCH_VERBS = {'rg': 'rg', 'grep': 'grep', 'egrep': 'grep', 'fgrep': 'grep',
                'select-string': 'select-string', 'sls': 'select-string', 'findstr': 'findstr'}
SEARCH = re.compile(r'\b(?:' + '|'.join(map(re.escape, SEARCH_VERBS)) + r')\b', re.I)
INDEX_REASON = re.compile(r'#idx\s*:\s*\S.+', re.I)
DECISION = re.compile(r'\[decision:\s*[^\]\s][^\]]+\]', re.I)
EXPERT = re.compile(r'\[expert:\s*[^\]\s][^\]]+\]', re.I)
ACTIVITY = re.compile(r'^\s*\[activity:\s*(investigate|mechanical|execute|review|decision|expert)\s*\]', re.I | re.M)
LOOKUP = re.compile(r'\b(?:find|locate|grep|search|scan|map|list|read)\b|查|翻', re.I)
EXECUTION_REVIEW = re.compile(r'\b(?:implement|implementation|fix|edit|modify|execute|review|audit)\b|实现|修复|修改|审查|评审', re.I)


def lookup_assignment(text):
    """Positive lookup prohibition, not a classifier for all allowed work."""
    activity = declared_activity(text)
    prose = re.sub(r'\[(?:activity|decision|expert):[^\]]*\]', '', text, flags=re.I)
    return (activity not in ('execute', 'review') and not EXECUTION_REVIEW.search(prose)
            and (activity in ('investigate', 'mechanical') or bool(LOOKUP.search(prose))))


def declared_activity(text):
    """An assignment declaration, never a guess at actual work from prose/model."""
    values = {match.lower() for match in ACTIVITY.findall(text)}
    return next(iter(values)) if len(values) == 1 else None


def spawn_reason(chosen, text, policy):
    """Extend the existing spawn contract; retained v1 launches keep their rule."""
    tier = tier_for_model(chosen)
    if policy.get('version', 1) < 2:
        if re.search(r'opus|fable|astra|^best(?:\[1m\])?$', chosen, re.I) and not DECISION.search(text):
            return 'Use the routine tier, or include [decision: specific decision or difficult final diff].'
        return None
    activity = declared_activity(text)
    if policy.get('version', 1) >= 4:
        lookup = lookup_assignment(text)
        readers = {roles['investigator'] for roles in MEMBER_TIERS.values()}
        if lookup and tier is not None and tier not in readers:
            return ('Pure lookup belongs to the configured Haiku/Luna investigator. Delegate it there, '
                    'or describe the actual execution/review deliverable; [decision:] alone is not an exemption.')
        if lookup and tier in readers and not (
                re.search(r'file\s*:\s*line|文件.{0,6}行号', text, re.I)
                and re.search(r'quotes?|excerpts?|原文|摘录|引用', text, re.I)):
            return 'Reader brief must request file:line references plus quotes/excerpts; add those outputs and retry.'
    if activity is None and policy.get('version', 1) < 3:
        return ('Start the member brief with [activity: investigate|mechanical|execute|review|decision|expert] '
                '(choose one). Reading needed for implementation belongs to execute; do not split every lookup.')
    role = {'investigate': 'investigator', 'mechanical': 'mechanical'}.get(activity)
    justified = (policy.get('version', 1) == 3 and (DECISION.search(text) or EXPERT.search(text))
                 or policy.get('version', 1) >= 4 and not lookup_assignment(text))
    if role and (tier is not None or policy.get('version', 1) < 3) and tier not in {roles[role] for roles in MEMBER_TIERS.values()} and not justified:
        return 'Use the configured investigator/mechanical tier for this separate deliverable; do not inherit the coordinator model.'
    if (tier == 'fable' or chosen.lower() in ('best', 'best[1m]')) and not EXPERT.search(text):
        return 'A Fable consultation needs [expert: specific unresolved question].'
    if tier in ('opus', 'astra') and not DECISION.search(text):
        return 'An Opus/Astra assignment needs [decision: specific critical implementation, review or decision].'
    return None


def _distro_base(name=None):
    """WSL storage authority is the registered distro, never its Linux path spelling."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Lxss') as root:
            default = winreg.QueryValueEx(root, 'DefaultDistribution')[0] if name is None else None
            for index in range(winreg.QueryInfoKey(root)[0]):
                key = winreg.EnumKey(root, index)
                with winreg.OpenKey(root, key) as distro:
                    label = winreg.QueryValueEx(distro, 'DistributionName')[0]
                    if (name is None and key == default) or (name and label.casefold() == name.casefold()):
                        return winreg.QueryValueEx(distro, 'BasePath')[0]
    except (OSError, ImportError):
        pass
    return None


def temporary_volume(path, *, distro_base=None):
    """Resolve observed storage, including Windows mounts inside a WSL command."""
    path = path.replace('\\', '/')
    mount = re.match(r'^/mnt/([a-z])(?:/|$)', path, re.I)
    if mount:
        path = mount[1] + ':' + path[6:]
    elif path.startswith('/') and distro_base:
        path = distro_base
    elif re.match(r'^/[a-z]/', path, re.I):
        path = path[1] + ':' + path[2:]
    elif path.startswith(('/tmp', '/var/tmp')):
        # Git Bash on this host; WSL commands supply registered distro storage.
        path = os.environ.get('TEMP', 'C:/Users/Default/AppData/Local/Temp')
    physical = os.path.realpath(path) if os.name == 'nt' else path
    drive = ntpath.splitdrive(physical)[0].removeprefix('\\\\?\\')
    return drive.casefold()


def temporary_verdict(command, paths=()):
    if not paths and not re.search(r'>|\b(?:tee|cp|mv|touch|mkdir|mktemp|Set-Content|Add-Content|Out-File|New-Item|write_text|write_bytes)\b', command, re.I):
        return None
    wsl = re.search(r'\bwsl(?:\.exe)?\b', command, re.I)
    distro_base = None
    if wsl:
        name = re.search(r'(?:--distribution|-d)\s+[\'\"]?([^\s\'\"]+)', command[wsl.end():])
        distro_base = _distro_base(name[1] if name else None)
    # Quoted document contents are single shell operands, not more commands.
    def operands(text):
        lexer = shlex.shlex(text, posix=True, punctuation_chars=';&|<>')
        lexer.whitespace_split, lexer.escape = True, ''
        try:
            tokens = list(lexer)
        except ValueError:
            return []  # Unparsed text is not proof of a forbidden output.
        result = []
        for index, token in enumerate(tokens):
            if token in ('>', '>>', '>|') and index + 1 < len(tokens):
                result.append(tokens[index + 1])
            if token.lower() in ('-c', '-lc', '-command') and index + 1 < len(tokens):
                result.extend(operands(tokens[index + 1]))
            if token.lower() not in ('cp', 'mv', 'tee', 'touch', 'mkdir', 'mktemp',
                                     'set-content', 'add-content', 'out-file', 'new-item'):
                continue
            args = []
            for arg in tokens[index + 1:]:
                if arg in (';', '&&', '||', '|', '>', '>>'):
                    break
                args.append(arg)
            flags = ('-p', '--tmpdir') if token == 'mktemp' else ('-path', '-literalpath', '-filepath')
            explicit = next((args[i + 1] for i, arg in enumerate(args[:-1]) if arg.lower() in flags), None)
            if token == 'mktemp':
                explicit = explicit or next((arg.split('=', 1)[1] for arg in args if arg.startswith('--tmpdir=')), None)
                result.append(explicit or '/tmp/')
            elif explicit is not None:
                result.append(explicit)
            else:
                values = [arg for arg in args if not arg.startswith('-')]
                result.extend(values[-1:] if token in ('cp', 'mv') else values if token in ('tee', 'touch', 'mkdir') else values[:1])
        return result
    targets = operands(command)
    python_paths = re.findall(r'Path\(\s*[\'\"]([^\'\"]+)[\'\"]\s*\)\.(?:write_text|write_bytes)\s*\(', command)
    if python_paths:
        targets = python_paths  # The written document may itself contain shell examples.
    targets.extend(paths)
    targets = [re.sub(r'\$(?:env:)?\{?(TMP|TEMP)\}?|%(TMP|TEMP)%',
                      lambda m: os.environ.get((m[1] or m[2]).upper(), ''), target, flags=re.I)
               for target in targets]
    for path in targets:
        if wsl and distro_base is None and path.startswith(('/tmp', '/var/tmp', '/root')):
            continue  # Unknown storage is not proof that the target is on C:.
        windows_temp = bool(re.search(r'/(?:AppData/Local/Temp|Windows/Temp)(?:/|$)', path.replace('\\', '/'), re.I))
        scratch = path.startswith(('/tmp', '/var/tmp')) or 'mktemp' in command or windows_temp
        if scratch and (windows_temp or temporary_volume(path, distro_base=distro_base) == 'c:'):
            return ('No temporary files on C: (operator rule). Target %s resolves to C: or Windows user temp. '
                    'Use H:/PDCL-archive/command-evidence/<TAG>/ or D:/Foundry/tmp; '
                    'WSL paths use the registered distro BasePath.' % path)
    return None


def recent_index_call(transcript, event=None):
    """Native transcript is evidence, not a second hook-state database.

    A query (including failure/empty output) permits scoped direct verification.
    Older evidence is referenced with #idx: reason instead of repeatedly scanned.
    """
    if not transcript:
        return False
    try:
        with Path(transcript).open('rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 65536))
            rows = stream.read().splitlines()
        candidates = set()
        def scope(name, value):
            command = value.get('command', value.get('cmd', ''))
            if not isinstance(command, str):
                return None
            # Comments carry reasons, not target operands. Do not reinterpret
            # quoted shell fragments or expand paths/environment variables.
            command = '\n'.join(line for line in command.splitlines() if not line.lstrip().startswith('#'))
            targets = search_targets(name, value, command)
            return ('targets', tuple(sorted(targets))) if targets else ('command', name, command.strip())
        current = scope(event.get('tool_name'), event.get('tool_input') or {}) if event else None
        current_cwd = ((event.get('tool_input') or {}).get('workdir') or event.get('cwd')) if event else None
        def remember(name, value, identity, cwd):
            if not isinstance(value, dict) or not identity or not current_cwd:
                return
            prior_cwd = value.get('workdir') or cwd
            if not prior_cwd or Path(prior_cwd).resolve() != Path(current_cwd).resolve():
                return
            if INDEX_REASON.search('\n'.join(str(value.get(k, '')) for k in ('command', 'cmd', 'description'))):
                if scope(name, value) == current:
                    candidates.add(identity)
        for raw in rows:
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(row, dict):
                continue
            payload = row.get('payload', row)
            if isinstance(payload, dict) and payload.get('type') == 'function_call' and payload.get('name', '').endswith(TOOLS):
                return True
            if isinstance(payload, dict) and payload.get('type') == 'function_call':
                try:
                    remember(payload.get('name', '').split('.')[-1], json.loads(payload.get('arguments', '{}')),
                             payload.get('call_id'), row.get('cwd'))
                except (ValueError, TypeError):
                    pass
            if isinstance(payload, dict) and payload.get('type') == 'function_call_output' and payload.get('call_id') in candidates:
                output = str(payload.get('output', ''))
                if output and not re.search(r'blocked|denied|is_error["\s:]+true', output, re.I):
                    return True
            message = row.get('message')
            content = message.get('content', []) if isinstance(message, dict) else []
            for item in content if isinstance(content, list) else []:
                if isinstance(item, dict) and item.get('type') == 'tool_use' and item.get('name', '').endswith(TOOLS):
                    return True
                if isinstance(item, dict) and item.get('type') == 'tool_use':
                    remember(item.get('name'), item.get('input'), item.get('id'), row.get('cwd'))
                if (isinstance(item, dict) and item.get('type') == 'tool_result'
                        and item.get('tool_use_id') in candidates and not item.get('is_error')):
                    return True
    except OSError:
        pass
    return False


def search_targets(name, value, command):
    """Collect read targets; only known stdin filters may follow a pipeline."""
    if name in ('Grep', 'Search'):
        path = value.get('path')
        return [path] if isinstance(path, str) and path else None
    try:
        command = '\n'.join(line for line in command.splitlines() if not line.lstrip().startswith('#'))
        lexer = shlex.shlex(command, posix=False, punctuation_chars='|;&')
        lexer.whitespace_split = True
        lexer.commenters = ''
        stages = [[]]
        groups = [stages]
        for token in lexer:
            if token == '|':
                stages.append([])
            elif token in (';', '&&', '||'):
                stages = [[]]
                groups.append(stages)
            else:
                quoted = len(token) >= 2 and token[0] in ('"', "'") and token[-1] == token[0]
                if not quoted and re.search(r'[;&`$<>\n]', token):
                    return None
                if quoted:
                    # Preserve PowerShell/Windows paths; Bash unquotes through shlex.
                    token = (shlex.split(token, posix=True)[0] if name == 'Bash'
                             else token[1:-1])
                stages[-1].append(token)
    except ValueError:
        return None
    if any(not stage for group in groups for stage in group):
        return None

    def operands(tokens):
        tool, *args = tokens
        tool = tool.lower()
        if tool == 'git' and args and args[0] == 'grep':
            args.pop(0); tool = 'grep'
        tool = SEARCH_VERBS.get(tool, tool)
        if tool not in set(SEARCH_VERBS.values()) | {'get-content', 'cat'}:
            return None
        reader = tool in ('get-content', 'cat')
        paths, pattern, positional, recursive = [], reader, False, False
        long_values = {'rg': {'--after-context', '--before-context', '--context', '--glob', '--iglob', '--type', '--type-not', '--max-count', '--regexp', '--file', '--encoding'},
            'grep': {'--after-context', '--before-context', '--context', '--include', '--exclude', '--exclude-dir', '--max-count', '--regexp', '--file'},
            'select-string': {'-path', '-literalpath', '-pattern', '-context', '-encoding'},
            'get-content': {'-path', '-literalpath', '-tail', '-totalcount', '-encoding'}}.get(tool, set())
        short_values = {'rg': 'ABCgtTmef', 'grep': 'ABCmef'}.get(tool, '')
        i = 0
        while i < len(args):
            token = args[i]; i += 1
            if token == '--' and not positional:
                positional = True; continue
            option, separator, attached = token.partition('=')
            if not positional and option.lower() in long_values:
                if separator:
                    arg = attached
                else:
                    if i == len(args): return None
                    arg = args[i]; i += 1
                if option.lower() in ('-path', '-literalpath'): paths.extend(arg.split(','))
                if option.lower() in ('-pattern', '--regexp', '--file'): pattern = True
            elif not positional and tool == 'findstr' and token.lower() in ('/i', '/n', '/r', '/l', '/v', '/x', '/b', '/e', '/s'):
                recursive |= token.lower() == '/s'
                continue
            elif not positional and token.startswith('-') and token != '-':
                # A combined option ends at the first value-taking flag.
                if token.startswith('--') or tool in ('select-string', 'get-content'):
                    recursive |= token == '--recursive'
                    continue
                chars = token[1:]
                for offset, flag in enumerate(chars):
                    if flag in short_values:
                        if offset + 1 == len(chars):
                            if i == len(args): return None
                            i += 1
                        if flag in 'ef': pattern = True
                        break
                    if tool == 'grep' and flag in 'rR': recursive = True
            elif not pattern:
                pattern = True
            else:
                paths.append(token)
        if any(re.search(r'[*?\[\]|]', path) for path in paths):
            return None
        return tool, paths, pattern, recursive

    targets = []
    for stages in groups:
        first = operands(stages[0])
        if first is None or not first[2] or not first[1]:
            return None
        for stage in stages[1:]:
            if stage in (['head'], ['tail']):
                continue  # No operands: consumes only this pipeline's stdin.
            parsed = operands(stage)
            if (parsed is None or parsed[0] not in ('select-string', 'grep', 'findstr', 'rg')
                    or not parsed[2] or any(path != '-' for path in parsed[1])):
                return None
            if parsed[3]:
                first[1].append('.')
        targets.extend(first[1])
    return targets


def check(event, policy):
    name = event.get('tool_name', '')
    value = event.get('tool_input') or {}
    if not isinstance(value, dict):
        return {}
    command = value.get('command', value.get('cmd', ''))
    command = command if isinstance(command, str) else ''
    text = '\n'.join(str(value.get(k, '')) for k in ('description', 'prompt', 'message'))
    reason = None
    warnings = []
    path = str(value.get('file_path') or value.get('path') or '')
    paths = [path] if name in ('Write', 'Edit') else []
    if name == 'apply_patch':
        paths.extend(line.split(':', 1)[1].strip() for line in command.splitlines()
                     if line.startswith(('*** Add File:', '*** Update File:', '*** Delete File:')))
        command = ''  # Patch contents are data; only the operation's target is a write.
    reason = temporary_verdict(command, paths)
    if reason is None and name in ('Agent', 'Task', 'spawn_agent'):
        chosen = str(value.get('model') or '')
        if value.get('resume'):
            return {}  # Existing member retains its recorded model and assignment.
        if not chosen or chosen.lower() in ('inherit', 'default'):
            reason = 'Select the member model explicitly; routine work must not inherit an expensive parent model.'
        else:
            reason = spawn_reason(chosen, text, policy)
            activity = declared_activity(text)
            if reason is None and policy.get('version', 1) >= 4 and (activity == 'review' or
                    activity is None and re.search(r'^\s*(?:please\s+)?(?:review|audit)\b|^\s*(?:审查|评审)', text, re.I)):
                from ._tool_history import member_review_models
                try:
                    participants = member_review_models(event.get('transcript_path'))
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    participants = set()  # Unknown native observations do not block review.
                basis = 'recorded coding-member assignment'
                if not participants and policy.get('version', 1) >= 5 and policy.get('recorded_coding_model'):
                    participants = {policy['recorded_coding_model'], *policy.get('recorded_coding_models', [])}
                    basis = 'frozen coding-model fallback (editing authorship unobserved)'
                if any(chosen == m or tier_for_model(chosen) is not None and tier_for_model(chosen) == tier_for_model(m) for m in participants):
                    reason = ('Reviewer matches a ' + basis + '. Choose another model, preferably another provider; '
                              'the implementation result is retained and needs no replay.')
            if reason is None and policy.get('version', 1) >= 5 and tier_for_model(chosen) == 'haiku' and lookup_assignment(text):
                planned = re.search(r'\[planned_input_tokens:\s*(\d+)\s*\]', text, re.I)
                # A rough brief estimate is advisory, never a fabricated native token count.
                estimate = (len(text.encode('utf-8')) + 2) // 3
                if (planned and int(planned[1]) >= 100000) or estimate >= 100000:
                    basis = f'declared planned total {planned[1]}' if planned and int(planned[1]) >= 100000 else f'brief estimate {estimate}'
                    warnings.append('Haiku retrieval input warning: ' + basis + ' tokens reaches 100k. '
                        'Split into scoped evidence jobs or compact the handoff. Estimate uses UTF-8 bytes/3 and excludes '
                        'unknown inherited context and future tool results; this warning does not cancel the task.')
    is_search = name in ('Grep', 'Search') or (name in ('Bash', 'PowerShell', 'powershell', 'exec_command', 'shell_command') and SEARCH.search(command))
    from ._code_index import outside_index
    non_index_search = is_search and outside_index(search_targets(name, value, command),
        policy.get('index_workspace'), value.get('workdir') or event.get('cwd'))
    if (reason is None and is_search and not non_index_search and policy.get('index_available')
            and not INDEX_REASON.search(command + '\n' + text)
            and not recent_index_call(event.get('transcript_path'), event)):
        reason = ('Query codegraph_explore (relationships) or codegraph_node when available (known file/symbol) first, '
                  'then use scoped direct reads to verify missing/stale results. '
                  'If unavailable, unsuitable, or already checked, give #idx: concrete reason in the command comment '
                  'or tool description. Reuse that reason; do not repeatedly retry a failed index.')
    if reason is None and policy.get('version', 1) >= 3:
        from ._tool_history import reading_check
        try:
            reason = reading_check(event, policy)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            # Optional metering must not turn an unknown native format into a
            # denied operation. The native transcript remains the evidence.
            return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                    'additionalContext': 'Reading observation unavailable; continue the task using scoped evidence.'}}
    if reason:
        return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                'permissionDecision': 'deny', 'permissionDecisionReason': reason}}
    return ({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'additionalContext': '\n'.join(warnings)}}
            if warnings else {})


def encoded(policy):
    return base64.urlsafe_b64encode(json.dumps(policy, ensure_ascii=False).encode()).decode()


def command(policy, provider):
    args = [sys.executable.replace('\\', '/'), '-I', '-m', 'foundry_runtime._tool_discipline',
            provider, encoded(policy)]
    return subprocess.list2cmdline(args) if provider == 'codex' and os.name == 'nt' else shlex.join(args)


def codex_hooks(policy):
    """Trust only this reviewed definition in the session config, never other hooks.

    Native identity: codex-rs/hooks/src/engine/discovery.rs hook_hash plus
    codex-rs/config/src/fingerprint.rs version_for_toml. Verified against hooks/list.
    """
    matcher = 'Bash|PowerShell|Grep|Search|Agent|spawn_agent|apply_patch|Write|Edit'
    if policy.get('version', 1) >= 3:
        matcher += '|Read'
    handler = {'type': 'command', 'command': command(policy, 'codex'), 'timeout': 10, 'async': False}
    identity = {'event_name': 'pre_tool_use', 'matcher': matcher, 'hooks': [handler]}
    digest = 'sha256:' + hashlib.sha256(json.dumps(identity, ensure_ascii=False,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    # Native synthetic_layer_path uses C:\ on Windows even for D:/H: worktrees.
    source = r'C:\<session-flags>\config.toml' if os.name == 'nt' else '/<session-flags>/config.toml'
    key = source + ':pre_tool_use:0:0'
    return {'PreToolUse': [{'matcher': matcher, 'hooks': [handler]}],
            'state': {key: {'trusted_hash': digest}}}


def toml(value):
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(k) + '=' + toml(v) for k, v in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ','.join(toml(v) for v in value) + ']'
    return json.dumps(value, ensure_ascii=False)


def main():
    provider, config = sys.argv[1:]
    policy = json.loads(base64.urlsafe_b64decode(config))
    event = json.loads(sys.stdin.buffer.read().decode('utf-8'))
    result = check(event, policy)
    if not result and provider == 'claude':
        from ._claude_tool_input import update
        result = update(event)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
