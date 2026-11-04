"""Compact views of reconciled usage; saved reports own comparison baselines."""
from collections import defaultdict

FIELDS = ('input_tokens', 'uncached_input_tokens', 'cached_input_tokens',
          'cache_write_input_tokens', 'output_tokens', 'total_tokens')
READING = ('observed_read_outputs', 'identical_call_output_repeats', 'truncated_read_outputs')


def _tokens(usage, backend):
    usage = usage or {}
    incoming, outgoing = usage.get('input_tokens'), usage.get('output_tokens')
    cached = usage.get('cached_input_tokens' if backend == 'codex' else 'cache_read_input_tokens')
    written = usage.get('cache_write_input_tokens' if backend == 'codex' else 'cache_creation_input_tokens')
    uncached = incoming - cached if backend == 'codex' and incoming is not None and cached is not None else (
        incoming if backend == 'claude' else None)
    total = usage.get('total_tokens') if backend == 'codex' else (
        sum((incoming, cached, written, outgoing)) if all(v is not None for v in (incoming, cached, written, outgoing)) else None)
    return dict(zip(FIELDS, (incoming, uncached, cached, written, outgoing, total)))


def observations(coding, codex_roles, report, *, attempts=None):
    """Retain identities already verified by task_report, without reading sessions."""
    units, executions, cumulative, session_roles, gaps = [], {}, [], defaultdict(set), []
    def execution(backend, identity, role):
        key = (backend, *identity)
        executions.setdefault(key, set()).add(role)
    for call in coding.get('observations', []):
        observation = call['observation']
        execution(call['backend'], ('turn', observation['session_id'], observation['turn_id'])
                  if call['backend'] == 'codex' else ('attempt', observation['attempt_id']), 'coding')
        if call['backend'] != 'codex':
            continue
        units.append({'identity': ['codex', 'turn', observation['session_id'], observation['turn_id']],
            'backend': 'codex', 'roles': ['coding'], 'cumulative': False,
            'tokens': _tokens(observation.get('usage'), 'codex'),
            'observed_responses': observation.get('response_count'),
            'reading': {key: (observation.get('reading_signals') or {}).get(key) for key in READING}})
    for role, calls in codex_roles.items():
        for call in calls:
            if role != 'review' or call.get('cumulative_session'):
                cumulative.append(call)
                session_roles[call['session_id']].add(role)
                continue
            units.append({'identity': ['codex', 'attempt', call.get('attempt_id') or call['evidence']],
                'backend': 'codex', 'roles': [role], 'cumulative': False, 'tokens': _tokens(call.get('usage'), 'codex'),
                'observed_responses': None, 'reading': {key: None for key in READING}})
    from ._foundry_goal_usage import _select_codex_sessions
    for call in _select_codex_sessions(cumulative, gaps, 'shared_session'):
        turns = [unit for unit in units if unit['identity'][:3] == ['codex', 'turn', call['session_id']]]
        units = [unit for unit in units if unit not in turns]
        roles = session_roles[call['session_id']] | ({'coding'} if turns else set())
        if turns:
            gaps.append({'session_id': call['session_id'],
                         'reason': 'coding turns overlap a cumulative snapshot without proven containment'})
        def observed_sum(values):
            return sum(values) if values and all(value is not None for value in values) else None
        units.append({'identity': ['codex', 'session', call['session_id']], 'backend': 'codex',
            'roles': sorted(roles), 'cumulative': True,
            'tokens': {field: None for field in FIELDS} if turns else _tokens(call.get('usage'), 'codex'),
            'included_turn_ids': sorted(unit['identity'] for unit in turns),
            'observed_responses': observed_sum([unit['observed_responses'] for unit in turns]),
            'reading': {field: observed_sum([unit['reading'][field] for unit in turns]) for field in READING}})
    for role, backends in (attempts or {}).items():
        for backend, calls in backends.items():
            for call in calls:
                execution(backend, ('attempt', call.get('attempt_id') or call['evidence']), role)
    from .foundry_claude import aggregate_sessions
    views = {'coding': coding.get('claude_session_usage', {}),
             **{role: report.get('claude_' + role + '_session_usage', {}) for role in ('review', 'manager', 'expert')}}
    sessions = aggregate_sessions([session for view in views.values() for session in view.get('sessions', [])])
    for session in sessions['sessions']:
        roles = sorted(role for role, view in views.items()
                       if any(row['session_id'] == session['session_id'] for row in view.get('sessions', [])))
        for model in session['models']:
            units.append({'identity': ['claude', 'session_model', session['session_id'], model['model']],
                'backend': 'claude', 'roles': roles, 'cumulative': True,
                'tokens': _tokens(model, 'claude'), 'observed_responses': None,
                'reading': {key: None for key in READING}})
    return {'schema_version': 1, 'units': units,
            'native_executions': [{'identity': list(key), 'backend': key[0], 'roles': sorted(roles)}
                                  for key, roles in sorted(executions.items())],
            'gaps': [*coding.get('missing_observations', []), *sessions['gaps'], *gaps]}


def summarize(view):
    groups = defaultdict(list)
    invocation_counts = defaultdict(int)
    for row in view.get('native_executions', []):
        invocation_counts[(row['backend'], tuple(row['roles']))] += 1
    for unit in view['units']:
        groups[(unit['backend'], tuple(unit['roles']))].append(unit)
    def metric(values):
        known = [value for value in values if value is not None]
        return {'observed_sum': sum(known) if known else None, 'unobserved_units': len(values) - len(known)}
    return {'by_role_and_backend': [
        {'backend': backend, 'roles': list(roles), 'observed_units': len(units),
         'tokens': {field: metric([unit['tokens'].get(field) for unit in units]) for field in FIELDS},
         'observed_responses': metric([unit.get('observed_responses') for unit in units]),
         'reading': {field: metric([unit.get('reading', {}).get(field) for unit in units]) for field in READING}}
        for (backend, roles), units in sorted(groups.items())],
        'observed_native_invocations': len(view.get('native_executions', [])),
        'native_invocations': [{'backend': backend, 'roles': list(roles), 'count': count}
                               for (backend, roles), count in sorted(invocation_counts.items())],
        'coverage': 'partial' if view.get('gaps') else 'observed_units', 'gap_count': len(view.get('gaps', []))}


def incremental(current, baseline):
    """Subtract matching cumulative identities; immutable turns are counted once."""
    if baseline.get('schema_version') != 'pdcl-foundry-task-report-1' or 'usage_observations' not in baseline:
        raise ValueError('--usage-since requires a saved task report containing usage_observations')
    prior = {tuple(unit['identity']): unit for unit in baseline['usage_observations']['units']}
    observed = current['usage_observations']
    latest = {tuple(unit['identity']): unit for unit in observed['units']}
    gaps = list(observed.get('gaps', []))
    gaps.extend({'identity': list(key), 'reason': 'baseline observation absent from current selection'}
                for key in prior.keys() - latest.keys())
    delta, reused = [], 0
    for key, unit in latest.items():
        old = prior.get(key)
        if old is None:
            if any(tuple(turn) in prior for turn in unit.get('included_turn_ids', [])) or (
                    key[1] == 'turn' and (key[0], 'session', key[2]) in prior):
                unit = {**unit, 'tokens': {field: None for field in FIELDS},
                        'reading': {field: None for field in READING}, 'observed_responses': None}
                gaps.append({'identity': list(key), 'reason': 'baseline and current token scopes differ'})
            delta.append(unit)
            continue
        if old == unit:
            reused += 1
            continue
        changed = {**unit, 'tokens': {}, 'reading': {}, 'observed_responses': None}
        for category, fields in (('tokens', FIELDS), ('reading', READING)):
            for field in fields:
                before, after = old.get(category, {}).get(field), unit.get(category, {}).get(field)
                valid = (unit['cumulative'] and type(before) is int and type(after) is int and after >= before)
                changed[category][field] = after - before if valid else None
                if before != after and not valid:
                    gaps.append({'identity': list(key), 'field': field, 'reason': 'increment is not established'})
        delta.append(changed)
    before_executions = {tuple(row['identity']) for row in baseline['usage_observations'].get('native_executions', [])}
    new_executions = [row for row in observed.get('native_executions', []) if tuple(row['identity']) not in before_executions]
    result = summarize({'units': delta, 'gaps': gaps, 'native_executions': new_executions})
    result.update(reused_units=reused, added_units=len(latest.keys() - prior.keys()), gaps=gaps,
                  changed_units=len(delta) - len(latest.keys() - prior.keys()),
                  scope='Observed change since the saved report; shared sessions remain shared; unobserved counters remain unknown.')
    if current.get('coverage') == 'partial' or baseline.get('coverage') == 'partial':
        result['coverage'] = 'partial'
    result['source_gap_counts'] = {'current': len(current.get('gaps', [])), 'baseline': len(baseline.get('gaps', []))}
    return result
