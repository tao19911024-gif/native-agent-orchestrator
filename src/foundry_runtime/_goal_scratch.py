"""Explicit disposable storage owned by the existing goal lifecycle."""
from pathlib import Path
import stat


def register(folder, manifest):
    root = Path(folder).resolve() / 'disposable'
    root.mkdir()  # A pre-existing directory has no new disposal declaration.
    manifest['disposable_scratch'] = {'path': str(root), 'owner': manifest['run_id']}


def prompt(body, manifest):
    scratch = manifest.get('disposable_scratch')
    if scratch is None:
        return body
    return (body + '\n\nDISPOSABLE TASK STORAGE\n'
            f"Directory: {scratch['path']}\n"
            'Use this directory for reproducible test, build and database scratch. '
            'Set TMP/TEMP and pytest --basetemp in your test commands to child directories here. '
            'Keep reports, original failure logs, deliverables and recovery inputs outside this directory. '
            'Move any required evidence out before requesting review or delivering artifacts. '
            'The goal owner removes this directory after candidate verification or artifact delivery. '
            'Unfinished work retains it. Include this storage contract in delegated assignments.\n')


def reclaim(folder, manifest, outcome, prior=None):
    scratch = manifest.get('disposable_scratch')
    if scratch is None:
        return None
    result = dict(prior or {'path': scratch['path'], 'bytes_reclaimed': 0, 'files_removed': 0})
    result['errors'] = []
    if outcome.get('status') not in {'candidate_verified', 'artifacts_delivered'} or outcome.get('retained_recovery'):
        result['status'] = 'retained'
        return result
    root = Path(folder).resolve() / 'disposable'
    if scratch != {'path': str(root), 'owner': manifest['run_id']}:
        result.update(status='retained', errors=['scratch ownership differs from this goal'])
        return result
    for artifact in outcome.get('artifacts', []):
        path = Path(artifact['path']).resolve()
        if path == root or root in path.parents or path in root.parents:
            result.update(status='retained', reason='delivered artifact includes disposable storage')
            return result

    def remove(path):
        try:
            info = path.lstat()
            linked = bool(stat.S_ISLNK(info.st_mode) or
                          getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
            if linked:
                # Remove the directory entry, never traverse its target.
                if stat.S_ISDIR(info.st_mode):
                    path.rmdir()
                else:
                    path.unlink()
            elif stat.S_ISDIR(info.st_mode):
                for child in path.iterdir():
                    remove(child)
                path.rmdir()
            else:
                path.unlink()
                result['bytes_reclaimed'] += info.st_size
                result['files_removed'] += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            result['errors'].append({'path': str(path), 'error': str(exc)})

    remove(root)
    result['status'] = 'partial' if result['errors'] else 'reclaimed'
    return result
