"""Named grant custody for the existing recovery owner; never a grant registry.

Current permission is checked before consumption. Completed histories retain the
original source bytes as evidence, so later grant edits cannot rewrite the past.
"""
from pathlib import Path



def verify_source(decision, *, current=False, source_verifier=None):
    from ._execution_intent import IntentConflict, _reference_bytes
    source = decision['source']
    if isinstance(source, dict) and source.get('authority') in {'named-project-grant', 'named-project-document'}:
        from ._project_recovery import verify_source as verify_project_source
        return verify_project_source(decision, current=current)
    legacy = decision['schema_version'] == 'pdcl-execution-recovery-1'
    keys = {'authority', 'path', 'sha256'}
    if not legacy:
        keys |= {'origin_path', 'grant_anchor', 'operator_authorization'}
    authority = 'operator-approved-local-decision' if legacy else 'named-command-grant'
    if not isinstance(source, dict) or set(source) != keys or source['authority'] != authority:
        raise IntentConflict('recovery source authority is invalid')
    raw = _reference_bytes({k: source[k] for k in ('path', 'sha256')}, 'decision source')
    if legacy:
        if current:
            raise IntentConflict('historical recovery source grants no new execution; prepare a named grant')
        return
    if source_verifier is None:
        raise IntentConflict('named-command recovery requires its project source verifier')
    source_verifier(source, raw, current=current)


def original_decision(authorization, *, source_verifier=None):
    """Resolve the actual envelope bytes, including the pre-reference format.

V1 recorded the digest but omitted the decision filename. Its source evidence
directory is the only historical search boundary; only an exact digest match
can supply that missing address. Embedded JSON alone is not original evidence.
V2 stores an explicit reference and never needs this compatibility lookup.
"""
    from ._execution_intent import IntentConflict, _reference_bytes
    from ._execution_bindings import _sha
    decision = authorization['decision']
    digest = authorization['decision_sha256']
    verify_source(decision, source_verifier=source_verifier)
    if decision['schema_version'] != 'pdcl-execution-recovery-1':
        reference = authorization.get('decision_reference')
        raw = _reference_bytes(reference, 'original decision', limit=256 * 1024)
        if reference['sha256'] != digest:
            raise IntentConflict('recovery original decision digest differs')
        return Path(reference['path']), raw
    parent = Path(decision['source']['path']).parent
    try:
        for path in sorted(parent.glob('*.json')):
            if path.is_file() and path.stat().st_size <= 256 * 1024:
                raw = path.read_bytes()
                if _sha(raw) == digest:
                    return path, raw
    except OSError as exc:
        raise IntentConflict('recovery original decision unavailable') from exc
    raise IntentConflict('recovery original decision bytes not found in source evidence directory')
