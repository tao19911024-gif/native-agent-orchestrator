"""Project source adapter for the existing recovery producer and verifier."""
import json
from pathlib import Path

from ._named_grant import operator_block, ProducerError


def grant_path(project):
    value = project['execution'].get('recovery_grants')
    if not isinstance(value, str) or not value.strip():
        raise ValueError('project execution.recovery_grants must reference its existing operator grant document')
    return (Path(project['workspace']) / value).resolve()


def attribution(anchor, raw, origin):
    return operator_block(anchor, raw.decode('utf-8-sig'), source=str(origin))


def source_fields(project, output):
    """Snapshot the selected descriptor beside the existing producer's evidence."""
    from ._execution_bindings import _sha
    project.verify_unchanged()
    raw = project.path.read_bytes()
    path = Path(str(output) + '.project.json')
    with path.open('xb') as stream:
        stream.write(raw)
    return {'project_descriptor': {'path': str(path), 'sha256': _sha(raw)},
            'project_origin_path': str(project.path)}


def verify_source(decision, *, current=False):
    from ._execution_intent import IntentConflict, _reference_bytes
    from ._project_execution import open_project
    source = decision['source']
    try:
        required = {'authority', 'path', 'sha256', 'origin_path', 'grant_anchor',
                    'operator_authorization', 'project_descriptor', 'project_origin_path'}
        if set(source) != required or source['authority'] not in {'named-project-grant', 'named-project-document'}:
            raise ValueError('invalid project recovery source fields')
        raw = _reference_bytes({k: source[k] for k in ('path', 'sha256')}, 'project grant')
        descriptor_bytes = _reference_bytes(source['project_descriptor'], 'project descriptor')
        project = json.loads(descriptor_bytes)
        descriptor = Path(source['project_origin_path'])
        origin = Path(source['origin_path'])
        if (not descriptor.is_absolute() or not origin.is_absolute()
                or (source['authority'] == 'named-project-grant' and grant_path(project) != origin.resolve())
                or Path(project['workspace']).resolve() != Path(decision['successor']['worktree']).resolve()):
            raise ValueError('project grant/workspace differs from descriptor')
        if source['operator_authorization'] != attribution(source['grant_anchor'], raw, origin):
            raise ValueError('project grant attribution differs')
        # The original journal, not a user-supplied project label, owns this work.
        first = (Path(project['git_common_dir']) / 'pdcl-dispatch' /
                 ('task-' + decision['task_key']) / 'intents' / decision['predecessor']['intent_id'] / '00000001.json')
        data = json.loads(_reference_bytes({'path': str(first),
            'sha256': decision['predecessor']['event_sha256s']['00000001.json']}, 'project predecessor'))['data']['frozen']
        context = data.get('project_context', {})
        if (context.get('descriptor') != str(descriptor) or context.get('project_id') != project['project_id']
                or Path(data['repository_id']).resolve() != Path(project['git_common_dir']).resolve()
                or Path(data['worktree']).resolve() != Path(project['workspace']).resolve()):
            raise ValueError('recovery predecessor belongs to a different project')
        if current:
            live = open_project(descriptor)
            if live.path.read_bytes() != descriptor_bytes or origin.read_bytes() != raw:
                raise ValueError('project descriptor or grant changed before recovery')
    except (OSError, ValueError, KeyError, TypeError, ProducerError) as exc:
        raise IntentConflict('project recovery source invalid: ' + str(exc)) from exc


def require_context(project, recovery):
    recovery.revalidate()
    source = recovery.data['source']
    if (source.get('authority') not in {'named-project-grant', 'named-project-document'}
            or source.get('project_origin_path') != str(project.path)):
        raise ValueError('recovery source does not belong to this project descriptor')
