"""Observed activity projection of installed 0.153.4 v2 notifications.

This is shape validation, never a semantic progress score. Unknown projections
remain in the wire log. Native terminal truth stays in _app_server_terminal.
"""


def activity_key(message, thread_id, turn_id):
    params = message.get('params')
    if (not thread_id or not turn_id or not isinstance(params, dict)
            or params.get('threadId') != thread_id or params.get('turnId') != turn_id):
        return None
    method = message.get('method')
    deltas = {'item/agentMessage/delta': None, 'item/commandExecution/outputDelta': None,
              'item/reasoning/textDelta': 'contentIndex',
              'item/reasoning/summaryTextDelta': 'summaryIndex'}
    if method in deltas:
        index = deltas[method]
        if (not isinstance(params.get('itemId'), str) or not params['itemId'].strip()
                or not isinstance(params.get('delta'), str) or not params['delta'].strip()
                or (index and (type(params.get(index)) is not int
                               or not -(2**63) <= params[index] < 2**63))):
            return None
        # Deltas are incremental, not state reports; identical adjacent chunks
        # can be legitimate repeated text. Lifecycle reports are deduped below.
        return ('delta',)
    if method not in ('item/started', 'item/completed'):
        return None
    timestamp = 'startedAtMs' if method == 'item/started' else 'completedAtMs'
    if type(params.get(timestamp)) is not int or not -(2**63) <= params[timestamp] < 2**63:
        return None
    item = params.get('item')
    if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'].strip():
        return None
    if item.get('type') in ('agentMessage', 'plan'):
        if not isinstance(item.get('text'), str) or not item['text'].strip():
            return None
    elif item.get('type') == 'reasoning':
        arrays = [item.get(key, []) for key in ('summary', 'content')]
        if (any(not isinstance(a, list) or any(not isinstance(v, str) for v in a) for a in arrays)
                or not any(v.strip() for a in arrays for v in a)):
            return None
    else:
        return None  # Other item variants aren't part of this declared projection.
    return ('state', item['id'], method)
