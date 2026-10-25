"""Explicit contract revisions owned by the retained goal, not a new dispatch."""
import json
import sys
from pathlib import Path


def records(owner, manifest):
    """Verify retained bytes without requiring the mutable authoring files."""
    if manifest.get('mode') == 'auto-amend':
        # The existing active-round pointer owns the effective contract. Do not
        # duplicate revision history in the outer collection.
        child_path = Path(manifest['active_round'])/'manifest.json'
        if not child_path.exists():  # Existing round-start recovery owns creation.
            return manifest['order'], []
        child = json.loads(child_path.read_bytes())
        if child['order'] != manifest['order']:
            upgrade = next((row for row in manifest.get('runtime_upgrades', [])
                            if Path(row['round']).resolve() == child_path.parent.resolve()), None)
            if upgrade is None:
                raise owner.GoalBlocked('active round contract differs without its recorded boundary transition')
            prepared = json.loads(owner.goal_usage._reference(upgrade['input']))
            approved, _ = records(owner, prepared)
            if child['order'] != approved or approved['path'] != manifest['order']['path']:
                raise owner.GoalBlocked('active round contract differs from its retained upgrade input')
        return records(owner, child)
    expected = manifest['order']
    rows = []
    for ref in manifest.get('order_revisions', []):
        row = json.loads(owner.goal_usage._reference(ref))
        request = row['request']
        if (request['run_id'] != manifest['run_id'] or request['order'] != manifest['order']
                or request['previous']['sha256'] != expected['sha256']):
            raise owner.GoalBlocked('order revision does not extend this retained contract')
        for name in ('previous', 'revised', 'authorization'):
            if owner._sha(row[name + '_text'].encode('utf-8')) != request[name]['sha256']:
                raise owner.GoalBlocked('retained order revision bytes changed: ' + name)
        expected = {**manifest['order'], 'sha256': request['revised']['sha256']}
        rows.append((ref, row))
    return expected, rows


def matches(owner, order, manifest):
    expected, _ = records(owner, manifest)
    return expected == owner._fact(order)


def validate(owner, folder, manifest, order, decision):
    """Read-only contract validation shared by preflight and locked admission."""
    expected, rows = records(owner, manifest)
    fact = owner._fact(decision)
    # Retry the same admission without adding history or another manager input.
    if rows and rows[-1][1]['decision'] == fact:
        if expected != owner._fact(order):
            raise owner.GoalBlocked('order changed again after its authorized revision')
        return None
    request = json.loads(owner.goal_usage._reference(fact))
    if (not isinstance(request, dict) or request.get('schema_version') != 1 or request.get('authorized_by') != 'operator'
            or request.get('run_id') != manifest['run_id']
            or Path(request['goal']).resolve() != folder.resolve()
            or request.get('order') != manifest['order']):
        raise owner.GoalBlocked('order revision must name this exact original goal and operator decision')
    outcome_path = folder/'outcome.json'
    outcome = json.loads(outcome_path.read_bytes()) if outcome_path.exists() else {}
    waiting = (manifest.get('manager_phase') == 'awaiting_input'
               and outcome.get('status') == 'awaiting_input')
    failed_feedback = (manifest.get('manager_state') and not manifest.get('review_checkpoint')
                       and manifest.get('manager_phase') == 'feedback_pending'
                       and outcome.get('status') == 'coding_incomplete' and manifest.get('coding_checkpoint'))
    if failed_feedback:
        from ._recovery_reconciliation import completed_coding
        completed_coding(json.loads(owner.goal_usage._reference(manifest['coding_checkpoint'])))
    if (not manifest.get('manager_state') or manifest.get('review_checkpoint')
            or (outcome_path.exists() and not (waiting or failed_feedback))):
        raise owner.GoalBlocked('order revision belongs to an unfinished manager goal; completed work uses its amendment boundary')
    row = {'decision': fact, 'request': request}
    for name in ('previous', 'revised', 'authorization'):
        row[name + '_text'] = owner.goal_usage._reference(request[name]).decode('utf-8')
    if not row['authorization_text'].strip():
        raise owner.GoalBlocked('order revision needs the specific operator ruling')
    current = owner._fact(order)
    if (current['path'] != expected['path'] or request['previous']['sha256'] != expected['sha256']
            or request['revised']['sha256'] != current['sha256']):
        raise owner.GoalBlocked('order revision bytes do not match the previous and current contracts')
    return fact, row


def admit(owner, folder, manifest, order, decision):
    """Called under the existing goal lock. Admission is not execution recovery."""
    prepared = validate(owner, folder, manifest, order, decision)
    if prepared is None:
        return manifest
    fact, row = prepared
    # The existing atomic JSON writer retains exact UTF-8 content (including BOM
    # and line endings) alongside its hashes. No Git dependency or file rollback.
    destination = folder / ('order-revision-' + fact['sha256'] + '.json')
    if destination.exists():
        if json.loads(destination.read_bytes()) != row:
            raise owner.GoalBlocked('retained order revision differs from its admitted decision')
    else:
        owner._write(destination, row)
    manifest = {**manifest, 'order_revisions': [*manifest.get('order_revisions', []), owner._fact(destination)],
                'order_revision_runtime': {'protocol': 1, 'python': sys.executable}}
    owner._write(folder/'manifest.json', manifest)
    return manifest


def manager_input(owner, manifest, packet):
    """A new contract is an operator update, never an old delegation replay."""
    if not pending(manifest):
        return packet, False
    _, rows = records(owner, manifest)
    ref, row = rows[-1]
    return {**packet, 'order_revision': {
        'evidence': ref, 'original_order': manifest['order'],
        'previous_contract': row['previous_text'], 'revised_contract': row['revised_text'],
        'operator_ruling': row['authorization_text'],
        'instruction': 'Reconcile this authorized contract revision before further delegation. '
                       'It does not establish acceptance of the implementation or authorize replaying prior work.'
    }}, True


def pending(manifest):
    rows = manifest.get('order_revisions', [])
    return bool(rows and manifest.get('manager_order_revision') != rows[-1])
