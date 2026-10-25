"""Release-operator dependency projection; never a task scheduler or state writer."""
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
import os
from pathlib import Path
import re
import subprocess
from time import perf_counter


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _reviewed_evidence(path):
    """Revalidate operator-reviewed legacy evidence, without rewriting old goals.

    The operator reviews the cited primary evidence's meaning; hashes bind that
    review to exact records. This is not automatic proof from a directory name.
    New or changed records never inherit a previous record's classification.
    """
    if path is None:
        return {}
    value = _read(path)
    if value.get('schema_version') != 'foundry-retention-evidence-review-1':
        raise ValueError('unknown retention evidence review')
    checked = set()
    rows = {}
    for item in value['observations']:
        if item['kind'] not in ('fixture', 'runtime', 'artifact') or not item.get('reason') or not item.get('evidence'):
            raise ValueError('review requires a fixture/runtime/artifact observation and primary evidence')
        for fact in [item['record'], *item['evidence']]:
            key = json.dumps(fact, sort_keys=True)
            if key not in checked:
                with Path(fact['path']).open('rb') as stream:
                    stream.seek(fact.get('offset', 0))
                    raw = stream.read(fact.get('length', -1))
                if hashlib.sha256(raw).hexdigest() != fact['sha256']:
                    raise ValueError('reviewed retention evidence changed: ' + fact['path'])
                checked.add(key)
        if 'offset' in item['record'] or 'length' in item['record']:
            raise ValueError('goal binding must cover the entire manifest')
        if item['kind'] == 'runtime' and (not item.get('interpreters')
                or not all(Path(p).is_absolute() for p in item['interpreters'])):
            raise ValueError('runtime observation requires absolute interpreter paths')
        target = str(Path(item['record']['path']).resolve())
        if target in rows:
            raise ValueError('duplicate retention observation: ' + target)
        rows[target] = item
    return rows


def _walk(roots, gaps, metrics=None, *, allowed_roots=None, directory_stamps=None, refresh=False):
    """Unbounded depth, physical-directory deduplication for junctions and cycles."""
    if metrics is None:
        metrics = {'directories': 0, 'records': 0}
    # Canonicalize roots and actual links, not every ordinary descendant. On
    # Windows resolve() opens path handles; repeating it dominated archive I/O.
    pending = [Path(p).resolve() for p in roots]
    allowed = [Path(p).resolve() for p in allowed_roots] if allowed_roots is not None else list(pending)
    # Attribute overlapping roots to the most specific physical root. Aliases
    # share one row, just as they share directory discovery.
    allowed = sorted(set(allowed), key=lambda p: (-len(p.parts), str(p)))
    root_metrics = {p: {'root': str(p), 'directories': 0, 'discovered_records': 0,
                        'reader_seconds': 0.0} for p in allowed}
    metrics['roots'] = list(root_metrics.values())
    seen = set()

    def entries_at(directory):
        started = perf_counter()
        children, records, errors = [], [], []
        stamp = None
        try:
            # Capture before enumeration so an entry added during this read is
            # still discovered by the next refresh of this operation.
            if directory_stamps is not None:
                stamp = directory.stat().st_mtime_ns
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        # Attribute reads and link resolution may perform I/O,
                        # too. Keep them on the existing bounded reader pool.
                        if entry.is_dir(follow_symlinks=True):
                            child = Path(entry.path)
                            if entry.is_symlink() or entry.is_junction():
                                child = child.resolve()
                            children.append(child)
                        elif entry.name in ('manifest.json', 'request.json'):
                            records.append(Path(entry.path))
                    except (OSError, RuntimeError) as exc:
                        if not (refresh and isinstance(exc, FileNotFoundError)):
                            errors.append((entry.path, exc))
        except (OSError, RuntimeError) as exc:
            if not (refresh and isinstance(exc, FileNotFoundError)):
                errors.append((str(directory), exc))
        return directory, stamp, children, records, errors, perf_counter() - started

    # Only filesystem reads overlap. Classification and keep-list decisions stay
    # on the caller thread. Bounded batches avoid retaining an entire archive.
    with ThreadPoolExecutor(max_workers=4) as readers:
        while pending:
            batch = []
            while pending and len(batch) < 32:
                directory = pending.pop()
                identity = os.path.normcase(str(directory))
                if identity in seen:
                    continue
                seen.add(identity)
                owner = next((root for root in allowed if directory.is_relative_to(root)), None)
                if owner is None:
                    gaps.append({'source': str(directory), 'reason': 'junction leaves known evidence roots; '
                                 'add its target explicitly to reconcile, rather than scanning unrelated directories'})
                    continue
                batch.append((directory, owner))
            for (_, owner), result in zip(batch, readers.map(entries_at, (p for p, _ in batch))):
                directory, stamp, children, records, errors, seconds = result
                metrics['directories'] += 1
                cost = root_metrics[owner]
                cost['directories'] += 1
                cost['discovered_records'] += len(records)
                cost['reader_seconds'] += seconds
                # Changed known directories are separate refresh roots. Only
                # new child trees need recursive discovery from this directory.
                pending.extend(child for child in children if not refresh or
                               directory_stamps is None or str(child) not in directory_stamps)
                if directory_stamps is not None and stamp is not None:
                    directory_stamps[str(directory)] = stamp
                for source, error in errors:
                    gaps.append({'source': str(source), 'reason': 'unreadable evidence directory: ' + str(error)})
                for path in records:
                    metrics['records'] += 1
                    yield path
    for row in root_metrics.values():
        row['reader_seconds'] = round(row['reader_seconds'], 6)


def _powershell(script, value=None, *, timeout=120):
    if os.name != 'nt':
        raise OSError('Release cleanup process/registration observation requires Windows')
    executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(executable), '-NoProfile', '-NonInteractive', '-Command', script],
        input=json.dumps(value), text=True, encoding='utf-8', capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW, timeout=timeout)
    if result.returncode:
        raise OSError(result.stderr[-3000:])
    return json.loads(result.stdout.lstrip('\ufeff'))


_ENCODING = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "


def host_snapshot(*, timeout=120):
    return _powershell(_ENCODING + r'''
    $processes = @(Get-CimInstance Win32_Process | Select-Object ProcessId,ExecutablePath,CommandLine)
    $tasks = @(Get-ScheduledTask | Where-Object TaskName -Like 'Foundry-*' | ForEach-Object {
        @{name=$_.TaskName; actions=@($_.Actions | Select-Object Execute,Arguments)}
    })
    @{processes=$processes; registrations=$tasks} | ConvertTo-Json -Depth 6 -Compress
    ''', timeout=timeout)


def known_roots(selection, extra=()):
    """Keep both aliases in the report; the scanner resolves junctions only once."""
    home = Path(selection).resolve().parent
    roots = [Path('D:/PDCL/command/evidence'), Path('H:/PDCL-archive/command-evidence'),
             home / 'projects', home / 'state', *map(Path, extra)]
    gaps = []
    for path in (home / 'projects').glob('*.json'):
        try:
            record = _read(path)
            if isinstance(record, dict) and record.get('schema_version') == 'foundry-project-1':
                roots.append(Path(record['evidence_root']))
                roots.extend(Path(record['workspace']) / name for name in record.get('history_roots', []))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            gaps.append({'source': str(path), 'reason': 'descriptor unreadable: ' + str(exc)})
    return list(dict.fromkeys(roots)), gaps


def _references(record):
    """Only recorded executable/runtime fields, never model prose or dates guessed from mtime."""
    if isinstance(record, dict):
        for key, value in record.items():
            if key in ('python', 'executable', 'environment', 'package_root') and isinstance(value, str):
                yield value
            elif isinstance(value, (dict, list)):
                yield from _references(value)
    elif isinstance(record, list):
        for value in record:
            yield from _references(value)


def _native_dependencies(record):
    """Follow native_dir exactly as the manager/expert resume owner does."""
    if isinstance(record, dict):
        if record.get('native_dir'):
            manifest = Path(record['native_dir']) / 'native-manifest.json'
            manifests = []
            if manifest.exists() or record.get('last_call_id') or record.get('session_id'):
                manifests.append(manifest)
            if record.get('expert'):
                from ._expert_consultation import native_root
                manifests.extend(native_root(record).glob('*/native-manifest.json'))
            for manifest in manifests:
                native = _read(manifest)
                executable = native.get('executable')
                if not isinstance(executable, str) or not Path(executable).is_absolute():
                    raise ValueError('native manifest lacks absolute executable: ' + str(manifest))
                yield executable, manifest
        for value in record.values():
            if isinstance(value, (dict, list)):
                yield from _native_dependencies(value)
    elif isinstance(record, list):
        for value in record:
            yield from _native_dependencies(value)


def _python_dependencies(interpreter, releases, aliases=None):
    """Installed .pth/venv references can keep a release even outside its process path."""
    path = Path(interpreter)
    if path.name.lower() not in ('python.exe', 'pythonw.exe'):
        return
    home = path.parent.parent if path.parent.name.lower() == 'scripts' else path.parent
    site = home / 'Lib/site-packages'
    sources = [home / 'pyvenv.cfg', *site.glob('*.pth')]
    for source in sources:
        if not source.is_file():
            continue
        value = source.read_text(encoding='utf-8-sig')
        if source.name == 'pyvenv.cfg':
            # CPython's venv launcher/getpath use home. executable/command record
            # the creator, which can be another venv; neither is a runtime edge.
            value = next((line.partition('=')[2].strip() for line in value.splitlines()
                          if line.partition('=')[0].strip().lower() == 'home'), '')
        text = value.replace('\\\\', '\\').replace('/', '\\').casefold()
        for release in releases:
            names = [release, *(aliases or {}).get(release, [])]
            if any(re.search(re.escape(name.replace('/', '\\').casefold()) + r'(?=\\|\s|[\"\x27]|$)', text) for name in names):
                yield release, source
        if source.suffix == '.pth':
            for line in value.splitlines():
                if not line.strip() or line.startswith(('#', 'import ')):
                    continue
                target = (site / line.strip()).resolve()
                for release in releases:
                    if target.is_relative_to(Path(release)):
                        yield release, source


def _test_owners(receipts):
    """Read execution receipts, never infer pytest ownership from a directory name."""
    owners = []
    for path in receipts:
        raw = Path(path).read_bytes()
        record = json.loads(raw)
        run = record.get('run', {})
        argv = run.get('command', {}).get('argv', [])
        if (record.get('schema_version') != 'pdcl-test-run-record-1'
                or not run.get('finished_utc') or type(run.get('exit_code')) is not int
                or not any(argv[i:i + 2] == ['-m', 'pytest'] for i in range(len(argv)))):
            raise ValueError('not a completed pytest execution receipt: ' + str(path))
        bases = [v.split('=', 1)[1] for v in argv if v.startswith('--basetemp=')]
        if '--basetemp' in argv:
            bases.append(argv[argv.index('--basetemp') + 1])
        if len(bases) != 1 or not Path(bases[0]).is_absolute():
            raise ValueError('pytest receipt lacks one absolute basetemp: ' + str(path))
        owners.append({'root': Path(bases[0]).resolve(), 'source': str(path),
                       'sha256': hashlib.sha256(raw).hexdigest()})
    return owners


def _retired_owner(path, record, adapter, authority):
    """Project-owned registration retires history; it does not settle executions."""
    if adapter is None:
        return None
    base = Path(adapter.evidence('')).resolve()
    if not path.is_relative_to(base) or len(path.relative_to(base).parts) < 2:
        return None
    tag = path.relative_to(base).parts[0]
    try:
        row = authority.resolve_task(tag)
    except ValueError:
        return None
    # The existing registry distinguishes these dispositions from execution
    # completion, accepted-but-not-landed work, and still blocked work.
    if row.status not in {'closed', 'dropped', 'superseded', 'landed', 'landed-deployed',
                          'merged', 'deployed', 'merged-and-deployed', 'done-measured'}:
        return None
    if record.get('tag') not in (None, tag):
        return None
    if record.get('worktree') and Path(record['worktree']).resolve() != Path(row.worktree).resolve():
        return None
    order = (record.get('order') or {}).get('path')
    if order:
        from ._task_identity import normalized_line
        try:
            if normalized_line(Path(order)) != tag:
                return None
        except ValueError:
            return None
    if order and Path(order).is_absolute():
        order_path = Path(order).resolve()
        if not (order_path.is_relative_to(base / tag)
                or order_path.parent == authority.path.parent / 'orders' and order_path.name.startswith(tag + '_')):
            return None
    return {'source': str(path), 'reason': 'owner-retired historical evidence', 'tag': tag,
            'status': row.status, 'authority': str(authority.path), 'line': row.line_number,
            'authority_sha256': authority.sha256}


def inventory(selection, rollback_python, *, roots=None, extra_roots=(), project_adapter=None,
              test_run_records=(), evidence_review=None, _discovery=None, keep_releases=(), deadline=None):
    """Fresh observations only. Unknown lifetime/provenance protects all possible releases."""
    started = perf_counter()
    metrics = {'directories': 0, 'records': 0, 'directory_readers': 4}
    home = Path(selection).resolve().parent
    releases = home / 'releases'
    gaps, keep, unknown, unfinished, registered = [], {}, [], [], []
    aliases = {}
    for path in releases.iterdir():
        if path.is_dir():
            aliases.setdefault(str(path.resolve()), []).append(str(path))
    versions = {path: [] for path in aliases}
    if roots is None:
        roots, discovered_gaps = known_roots(selection, extra_roots)
        gaps.extend(discovered_gaps)
    roots = list(map(Path, roots))
    classified = []
    test_owners = _test_owners(test_run_records)
    authority = project_adapter.goal_authority() if project_adapter is not None else None
    observations = _reviewed_evidence(evidence_review)

    def protect(path, reason, source):
        if not isinstance(path, str) or not path:
            return
        resolved = Path(path).resolve()
        item = {'reason': reason, 'source': str(source)}
        keep.setdefault(str(resolved), [])
        if item not in keep[str(resolved)]:
            keep[str(resolved)].append(item)
        for version, reasons in versions.items():
            if resolved.is_relative_to(Path(version)) and item not in reasons:
                reasons.append(item)

    for path, label in ((selection, 'current'),):
        try:
            record = _read(path)
            if not isinstance(record.get('python'), str) or not record['python']:
                raise ValueError('missing interpreter')
            protect(record['python'], label, path)
        except (OSError, ValueError, AttributeError) as exc:
            gaps.append({'source': str(path), 'reason': str(exc)})
    protect(str(rollback_python), 'qualified rollback', 'operator rollback selection')
    for release in keep_releases:
        protect(str(Path(release) / 'Scripts/python.exe'), 'active or retained recovery task', 'operator task ownership list')
    if not Path(rollback_python).is_file():
        gaps.append({'source': str(rollback_python), 'reason': 'rollback interpreter missing'})

    # A serialized prune operation discovers the archive once, then refreshes
    # changed directory entries and rereads every discovered record, including
    # previously non-goal records and current checkpoint/native references.
    refreshing = _discovery is not None and 'paths' in _discovery
    metrics['discovery'] = 'known-record refresh' if refreshing else 'full tree'
    root_names = [str(p.resolve()) for p in roots]
    if refreshing:
        paths = _discovery['paths']
        stamps = _discovery['directories']
        changed = []

        def directory_stamp(item):
            name, before = item
            try:
                return name, Path(name).stat().st_mtime_ns != before, None
            except FileNotFoundError:
                # Retain the prior stamp so recreation is observed on the next
                # refresh; removed evidence has no present directory to read.
                return name, False, None
            except OSError as exc:
                return name, False, str(exc)

        directories = list(stamps.items())
        with ThreadPoolExecutor(max_workers=4) as readers:
            for start in range(0, len(directories), 32):
                for name, differs, error in readers.map(directory_stamp, directories[start:start + 32]):
                    if error:
                        gaps.append({'source': name, 'reason': 'unreadable evidence directory: ' + error})
                    elif differs:
                        changed.append(Path(name))
        known = set(map(Path, _discovery['roots']))
        added = list(dict.fromkeys(Path(p) for p in root_names if Path(p) not in known))
        if added:
            # Registration may add projects during a long walk. Extend this
            # operation's discovery, then reread all records; never discard it
            # or omit a new root merely because its ancestor was scanned before.
            _discovery['roots'].extend(map(str, added))
            metrics['added_roots'] = list(map(str, added))
        if changed or added:
            observed = set(paths)
            for path in _walk([*changed, *added], gaps, metrics,
                              allowed_roots=_discovery['roots'], directory_stamps=stamps, refresh=True):
                if path not in observed:
                    paths.append(path)
                    observed.add(path)
            metrics['discovery'] = ('known-record refresh + added roots' if added
                                    else 'known-record refresh + changed directories')
            metrics['changed_directories'] = len(changed)
        # Reordering is immaterial. Removed registrations do not erase records
        # already observed by this operation; their dependencies are reread too.
        metrics['records'] = len(paths)
    else:
        if _discovery is not None:
            _discovery.update(paths=[], roots=root_names, directories={})
        paths = (_walk(roots, gaps, metrics, directory_stamps=(
            _discovery['directories'] if _discovery is not None else None)) if roots else ())
    for path in paths:
        if _discovery is not None and not refreshing:
            _discovery['paths'].append(path)
        try:
            resolved_path = path.resolve()
            observation = observations.get(str(resolved_path))
            # Exact reviews bind content. Completed pytest ownership instead
            # binds the resolved fixture path; its arbitrary contents are not
            # dependency evidence and need not incur an archive read.
            raw = path.read_bytes() if observation else None
            if observation and hashlib.sha256(raw).hexdigest() != observation['record']['sha256']:
                raise ValueError('reviewed manifest changed during scan: ' + str(path))
            if observation and observation['kind'] in ('fixture', 'artifact') and path.name == 'manifest.json':
                classified.append({'source': str(path), 'reason': 'reviewed historical fixture' if observation['kind'] == 'fixture' else 'reviewed non-goal artifact',
                    'review': str(evidence_review), 'basis': observation['reason']})
                continue
            owner = next((r for r in test_owners if resolved_path.is_relative_to(r['root'])), None)
            # Hosted requests retain operational custody even inside a fixture.
            if owner and path.name == 'manifest.json':
                classified.append({'source': str(path), 'reason': 'pytest fixture',
                    'receipt': owner['source'], 'receipt_sha256': owner['sha256']})
                continue
            if raw is None:
                raw = path.read_bytes()
            record = json.loads(raw)
            if not isinstance(record, dict):
                raise ValueError('record is not an object')
            if path.name == 'request.json':
                if str(record.get('task_name', '')).startswith('Foundry-'):
                    finished_path = path.with_name('finished.json')
                    finished = _read(finished_path) if finished_path.exists() else {}
                    registered.append({'path': str(path), 'record': record,
                        'finished': bool(record.get('run_id') and finished.get('run_id') == record['run_id']
                                         and type(finished.get('returncode')) is int)})
                    if not record.get('python'):
                        raise ValueError('hosted registration has no interpreter')
                continue
            goal = (path.parent.name.startswith(('goal-', 'continue-')) or
                    record.get('order') and (record.get('run_id') or record.get('mode') == 'auto-amend'))
            if not goal:
                continue
            retired = _retired_owner(path, record, project_adapter, authority)
            if retired:
                retired['record_sha256'] = hashlib.sha256(raw).hexdigest()
                classified.append(retired)
                continue
            from ._foundry_goal_auto import outcome_path as selected_outcome_path
            outcome_path = selected_outcome_path(path.parent, record)
            outcome = _read(outcome_path) if outcome_path.exists() else {}
            # A blocked round may say completed; only successful outcomes close it.
            if outcome.get('status') in ('candidate_verified', 'artifacts_delivered'):
                continue
            unfinished.append(str(path))
            provenance = record.get('runtime_provenance') or {}
            python = provenance.get('python')
            if observation and observation['kind'] == 'runtime':
                for interpreter in observation['interpreters']:
                    protect(interpreter, 'reviewed runtime dependency evidence', path)
                if not python:
                    python = observation['interpreters'][0]
                classified.append({'source': str(path), 'reason': 'reviewed runtime dependency evidence',
                    'review': str(evidence_review), 'interpreters': observation['interpreters'],
                    'runtime_scope': observation.get('runtime_scope', 'recorded interpreter'),
                    'basis': observation['reason']})
            for value in _references(record):
                protect(value, 'unfinished goal dependency', path)
            for value, source in _native_dependencies(record):
                protect(value, 'unfinished native session dependency', source)
            # Checkpoints can retain a different provider executable/runtime.
            for name in ('coding-checkpoint.json', 'review-checkpoint.json'):
                checkpoint = path.with_name(name)
                if checkpoint.exists():
                    saved = _read(checkpoint)
                    for value in _references(saved):
                        protect(value, 'unfinished checkpoint dependency', checkpoint)
                    for value, source in _native_dependencies(saved):
                        protect(value, 'unfinished native session dependency', source)
            if not isinstance(python, str) or not python or not Path(python).is_absolute():
                unknown.append({'source': str(path), 'reason': 'unfinished goal lacks authoritative runtime provenance; '
                    'no trustworthy activation/lifetime interval, so every installed release is a possible dependency'})
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            if not (refreshing and isinstance(exc, FileNotFoundError)
                    and exc.filename is not None and Path(exc.filename) == path):
                gaps.append({'source': str(path), 'reason': 'unreadable evidence: ' + str(exc)})

    # A long archive scan must not carry a retired registration across a reopen.
    if project_adapter is not None:
        latest = project_adapter.goal_authority()
        for row in classified:
            if row['reason'] != 'owner-retired historical evidence':
                continue
            try:
                path = Path(row['source'])
                raw = path.read_bytes()
                current = _retired_owner(path, json.loads(raw), project_adapter, latest)
                if (hashlib.sha256(raw).hexdigest() != row['record_sha256'] or current is None
                        or current['status'] != row['status']):
                    raise ValueError('retired owner or record changed during scan')
            except (OSError, ValueError, TypeError) as exc:
                gaps.append({'source': row['source'], 'reason': str(exc)})
    # Recheck reviewed source slices as well as records after the long walk.
    _reviewed_evidence(evidence_review)
    if _test_owners(test_run_records) != test_owners:
        gaps.append({'source': 'pytest receipts', 'reason': 'fixture ownership changed during scan'})
    try:
        selected_now = _read(selection)
        protect(selected_now['python'], 'current (refreshed after scan)', selection)
        host = (host_snapshot() if deadline is None else
                host_snapshot(timeout=max(0.01, min(120, deadline - perf_counter()))))
        names = {r['name'] for r in host['registrations']}
        for row in registered:
            if not row['finished'] or row['record']['task_name'] in names:
                protect(row['record']['python'], 'hosted registration (not retired)', row['path'])
        for registration in host['registrations']:
            for action in registration['actions']:
                protect(action['Execute'], 'scheduled registration', registration['name'])
                arguments = str(action.get('Arguments') or '').replace('/', '\\').casefold()
                for version, reasons in versions.items():
                    if any(name.replace('/', '\\').casefold() + '\\' in arguments for name in [version, *aliases[version]]):
                        reasons.append({'reason': 'scheduled action argument', 'source': registration['name']})
        for process in host['processes']:
            text = '\n'.join(str(process.get(k) or '') for k in ('ExecutablePath', 'CommandLine')).replace('/', '\\').casefold()
            for version, reasons in versions.items():
                if any(name.replace('/', '\\').casefold() + '\\' in text for name in [version, *aliases[version]]):
                    reasons.append({'reason': 'live process', 'source': 'pid:' + str(process['ProcessId'])})
            first = re.match(r'^\s*(?:"([^"]+)"|(\S+))', str(process.get('CommandLine') or ''))
            for interpreter in [process.get('ExecutablePath'), next((x for x in first.groups() if x), None) if first else None]:
                if interpreter:
                    for dependency, source in _python_dependencies(interpreter, versions, aliases):
                        protect(dependency, 'live Python environment dependency', source)
        for interpreter in list(keep):
            for dependency, source in _python_dependencies(interpreter, versions, aliases):
                protect(dependency, 'retained Python environment dependency', source)
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        gaps.append({'source': 'Windows host', 'reason': str(exc)})
    # Missing evidence never turns an unknown runtime into an unused one. No
    # fabricated creation-date interval and no implicit closing of old goals.
    for reasons in versions.values():
        reasons.extend(unknown + gaps)
    metrics['seconds'] = round(perf_counter() - started, 3)
    return {'observed_utc': datetime.now(timezone.utc).isoformat(), 'read_only': True, 'scan': metrics,
        'selection': str(Path(selection).resolve()), 'rollback_python': str(rollback_python),
        'evidence_roots': [str(p.absolute()) for p in roots], 'scan_depth': 'unbounded; resolved-directory deduplication',
        'discovery_roots': list(_discovery['roots']) if _discovery is not None else root_names,
        'unfinished_goals': unfinished, 'hosted_registrations': [r['path'] for r in registered],
        'unknown_provenance': unknown, 'gaps': gaps,
        'classified_history': classified, 'release_aliases': aliases,
        'interpreters_and_dependencies': [{'path': p, 'reasons': reasons} for p, reasons in sorted(keep.items())],
        'keep': [{'release': p, 'reasons': reasons} for p, reasons in sorted(versions.items()) if reasons],
        'candidates': [p for p, reasons in sorted(versions.items()) if not reasons],
        'note': 'Observation only; saved JSON is never deletion authorization. Serialized prune discovers the archive once, refreshes changed directories and newly registered roots, then rereads discovered records and current dependencies before every candidate.'}


def installed_inventory(selection, rollback_python, *, keep_releases=(), deadline=None):
    """File-management view: registered installations, explicit task holds and host use."""
    report = inventory(selection, rollback_python, roots=[], keep_releases=keep_releases, deadline=deadline)
    report['scan']['discovery'] = 'installed versions and current references'
    report['note'] = 'Task holds are supplied from current owner records. Historical archives are outside daily rotation.'
    return report


def _remove_release(path, root, selection, *, physical=None, timeout=120):
    """One native PowerShell operation, exact path and fresh process check before removal.

    Never use an alternate executor after a host rejection. The caller records
    the failure and stops; this function is not used for production qualification.
    """
    return _powershell(_ENCODING + r'''
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $root = (Resolve-Path -LiteralPath $request.root).ProviderPath.TrimEnd('\')
    $item = Get-Item -LiteralPath $request.path -Force
    $target = [IO.Path]::GetFullPath($request.physical)
    $alias = $item.FullName
    $linked = [bool]($item.Attributes -band [IO.FileAttributes]::ReparsePoint)
    if ($item.Parent.FullName.TrimEnd('\') -ne $root) { throw 'Not a registered release directory' }
    if ($linked) {
        $destination = @($item.Target)[0]
        if (-not [IO.Path]::IsPathRooted($destination)) { $destination = Join-Path $item.Parent.FullName $destination }
        if ([IO.Path]::GetFullPath($destination) -ne $target -or [IO.Path]::GetFileName($target) -ne $item.Name) {
            throw 'Release alias changed; retained'
        }
    } elseif ($target -ne $item.FullName) { throw 'Release physical path changed; retained' }
    if ((Get-Item -LiteralPath $target -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'Physical release root is a link; reconcile before cleanup'
    }
    $items = @(Get-ChildItem -LiteralPath $target -Force -Recurse)
    if (@($items | Where-Object {
        $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count) { throw 'Release contains a link; reconcile before cleanup' }
    $needles = @(($target.TrimEnd('\')+'\'), ($alias.TrimEnd('\')+'\'))
    function HasReference($text) {
        if ($text) {
            foreach ($needle in $needles) {
                if ($text.Replace('/','\').IndexOf($needle,[StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
            }
        }
        return $false
    }
    $selected = (Get-Content -LiteralPath $request.selection -Raw | ConvertFrom-Json).python
    if (-not $selected) { throw 'Current interpreter unavailable' }
    $references = @($selected) + @(Get-ScheduledTask | Where-Object TaskName -Like 'Foundry-*' | ForEach-Object {
        $_.Actions | ForEach-Object { $_.Execute; $_.Arguments }
    })
    if (@($references | Where-Object { HasReference $_ }).Count) {
        throw 'Release acquired a selection or registration reference; retained'
    }
    $live = @(Get-CimInstance Win32_Process | Where-Object {
        (HasReference $_.ExecutablePath) -or (HasReference $_.CommandLine)
    })
    if ($live.Count) { throw 'Release acquired a process reference; retained' }
    Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction Stop
    if (Test-Path -LiteralPath $target) { throw 'Physical release remains after removal' }
    if ($linked) {
        $remaining = Get-Item -LiteralPath $alias -Force
        $destination = @($remaining.Target)[0]
        if (-not [IO.Path]::IsPathRooted($destination)) { $destination = Join-Path $remaining.Parent.FullName $destination }
        if ([IO.Path]::GetFullPath($destination) -ne $target) { throw 'Release alias changed after physical cleanup' }
        Remove-Item -LiteralPath $alias -Force -ErrorAction Stop
    }
    @{removed=$target; alias=$alias; bytes_removed=($items | Where-Object { -not $_.PSIsContainer } | Measure-Object Length -Sum).Sum;
      exists=((Test-Path -LiteralPath $target) -or (Test-Path -LiteralPath $alias))} | ConvertTo-Json -Compress
    ''', {'path': str(path), 'root': str(root), 'physical': str(physical or path), 'selection': str(selection)}, timeout=timeout)


def prune(selection, rollback_python, targets=None, *, keep_releases=(), deadline=None):
    """Rotate installation directories; historical audits never precede deletion."""
    root = Path(selection).resolve().parent / 'releases'
    result = {'deleted': [], 'retained': [], 'errors': [], 'inventories': [], 'removals': []}
    started = perf_counter()
    deadline = min(deadline if deadline is not None else started + 600, started + 600)
    if targets is None:
        targets = [p for p in root.iterdir() if p.is_dir() and re.fullmatch(r'\d+\.\d+\.\d+(?:[ab]\d+)?', p.name)]
    targets = [Path(p).absolute() for p in targets]
    for raw in targets:
        path = raw
        physical = path.resolve()
        linked = path.is_junction() or path.is_symlink()
        if (path.parent != root or not re.fullmatch(r'\d+\.\d+\.\d+(?:[ab]\d+)?', path.name)
                or physical.name != path.name or (physical.parent != root and not linked)):
            raise ValueError('Cleanup accepts only exact direct version directories under ' + str(root))
        try:
            if perf_counter() >= deadline:
                raise TimeoutError('Release rotation reached its ten-minute limit')
            report = installed_inventory(selection, rollback_python, keep_releases=keep_releases, deadline=deadline)
            result['inventories'].append(report)
            if perf_counter() >= deadline:
                raise TimeoutError('Release rotation reached its ten-minute limit')
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as exc:
            result['errors'].append({'path': str(path), 'reason': str(exc)})
            break
        if str(path.resolve()) not in report['candidates']:
            result['retained'].append(str(path))
            continue
        try:
            remaining = deadline - perf_counter()
            if remaining <= 0:
                raise TimeoutError('Release rotation reached its ten-minute limit')
            removed = _remove_release(path, root, selection, timeout=min(120, remaining),
                                      **({'physical': physical} if linked else {}))
            if removed.get('exists') is not False or path.exists() or physical.exists():
                raise OSError('Removal did not confirm absence')
            result['deleted'].append(str(path))
            result['removals'].append(removed)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            result['errors'].append({'path': str(path), 'reason': str(exc)})
            break  # A rejection/partial failure is not an invitation to retry elsewhere.
    result['seconds'] = round(perf_counter() - started, 3)
    result['remaining'] = [str(p) for p in targets if str(p) not in result['deleted'] + result['retained']]
    return result
