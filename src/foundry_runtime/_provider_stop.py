"""Provider-reported temporary stops shared by worker and native analysts."""
import re

APP_SERVER_CAUSES = {'usageLimitExceeded': 'provider_limit',
                    'rateLimitExceeded': 'provider_limit',
                    'serverOverloaded': 'provider_capacity'}
CONTINUABLE = frozenset(APP_SERVER_CAUSES.values())


def reported_stop(event):
    """Classify only error events; retain reset text without guessing its date/zone."""
    error = event.get('error') if isinstance(event.get('error'), dict) else {}
    category = APP_SERVER_CAUSES.get(error.get('codexErrorInfo')) if isinstance(error.get('codexErrorInfo'), str) else None
    status = event.get('api_error_status', error.get('status'))
    category = category or ({429: 'provider_limit', 529: 'provider_capacity'}.get(status) if type(status) is int else None)
    code = error.get('type') or error.get('code')
    if isinstance(code, str):
        category = category or {'rate_limit_error': 'provider_limit', 'overloaded_error': 'provider_capacity'}.get(code)
    text = event.get('result') or error.get('message') or event.get('message') or ''
    text = text if isinstance(text, str) else ''
    if (not error.get('codexErrorInfo')
            and (error or event.get('type') in {'error', 'turn.failed'} or event.get('is_error') is True)
            and text.strip() == 'Selected model is at capacity. Please try a different model.'):
        category = category or 'provider_capacity'
    # CLI session-limit messages have no HTTP status. Keep this narrow: token
    # length, budgets and formatter attempt limits are different outcomes.
    if re.search(r"you(?:'|’)ve hit your (?:session|usage|weekly) limit", text, re.I):
        category = category or 'provider_limit'
    if category is None:
        return None
    reset = None
    for source, row in (('event', event), ('error', error)):
        for key in ('reset_at', 'resets_at', 'retry_after'):
            if isinstance(row.get(key), (str, int, float)) and not isinstance(row[key], bool):
                reset = {'value': row[key], 'source': source + '.' + key,
                         'timezone': row.get('timezone'), 'interpretation': 'provider-reported'}
                break
        if reset is not None:
            break
    if reset is None and (match := re.search(r'\bresets\s+([^\r\n]+)', text, re.I)):
        reset = {'value': match.group(1).strip(), 'source': 'provider message',
                 'timezone': None, 'interpretation': 'unresolved text; not a current quota observation'}
    return {'category': category, 'reset': reset}
