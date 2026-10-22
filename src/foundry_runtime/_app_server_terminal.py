"""Shared admission projection of the native TurnCompletedNotification contract.

Source: codex-cli 0.153.4 generated v2/TurnCompletedNotification.json, SHA-256
78af2a37391e8e669a4020cb58593e4d3e378756ced79d5fec72374fa69fb94b,
retained in command/evidence/FOUNDRYL1/native-schema. This is not a second
transport or a full ThreadItem parser. Item bodies and schema-permitted extension
fields are retained verbatim; CodexErrorInfo discriminators are closed. No field is
removed from the observed notification by validation.
"""


# CodexErrorInfo.oneOf string enum from the pinned native schema above.
_CODEX_ERROR_STRINGS = (
    'contextWindowExceeded', 'sessionBudgetExceeded', 'usageLimitExceeded',
    'rateLimitExceeded', 'serverOverloaded', 'cyberPolicy',
    'misalignmentPolicyViolation', 'internalServerError', 'unauthorized',
    'badRequest', 'threadRollbackFailed', 'sandboxError', 'other',
)


def _string(value):
    return isinstance(value, str) and bool(value.strip())


def _optional_string(record, key):
    value = record.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError('terminal error field must be string or null: ' + key)


def _error(error):
    if not isinstance(error, dict) or not isinstance(error.get('message'), str):
        raise ValueError('terminal error requires an object with a string message')
    _optional_string(error, 'additionalDetails')
    info = error.get('codexErrorInfo')
    # Native CodexErrorInfo.oneOf closes string and object discriminators.
    # Its known object payloads permit extension fields, not new variant tags.
    if info is not None and not isinstance(info, (str, dict)):
        raise ValueError('terminal codexErrorInfo must be string, object or null')
    if isinstance(info, str) and info not in _CODEX_ERROR_STRINGS:
        raise ValueError('terminal codexErrorInfo has unknown string variant')
    if isinstance(info, dict):
        if len(info) != 1 or not isinstance(next(iter(info.values())), dict):
            raise ValueError('terminal codexErrorInfo requires one object variant')
        variant, payload = next(iter(info.items()))
        if variant in ('httpConnectionFailed', 'responseStreamConnectionFailed',
                       'responseStreamDisconnected', 'responseTooManyFailedAttempts'):
            code = payload.get('httpStatusCode')
            if code is not None and (type(code) is not int or not 0 <= code <= 65535):
                raise ValueError('terminal httpStatusCode must be uint16 or null')
        elif variant == 'activeTurnNotSteerable':
            if payload.get('turnKind') not in ('review', 'compact'):
                raise ValueError('terminal activeTurnNotSteerable requires its native turnKind')
        else:
            raise ValueError('terminal codexErrorInfo has unknown object variant')
    misalignment = error.get('misalignment')
    if misalignment is not None:
        if not isinstance(misalignment, dict):
            raise ValueError('terminal misalignment must be object or null')
        for key in ('detailedExplanation', 'errorType'):
            _optional_string(misalignment, key)
        steer = misalignment.get('steer')
        if steer is not None and (not isinstance(steer, dict) or not isinstance(steer.get('message'), str)):
            raise ValueError('terminal misalignment steer requires a string message')


def validate_terminal(message, thread_id, turn_id):
    """Validate the shared identity/status/items/error admission shape; return status.

    The native Turn requires id, items, status. Every current ThreadItem has
    string id/type; its variant body is evidence, not terminal admission input.
    Optional fields not used here remain available for future native versions.
    """
    if not isinstance(message, dict) or message.get('method') != 'turn/completed':
        raise ValueError('terminal method must be turn/completed')
    params = message.get('params')
    if (not isinstance(params, dict) or not _string(thread_id) or not _string(turn_id)
            or params.get('threadId') != thread_id):
        raise ValueError('terminal thread identity differs from admission')
    turn = params.get('turn')
    if not isinstance(turn, dict) or turn.get('id') != turn_id:
        raise ValueError('terminal turn identity differs from admission')
    status = turn.get('status')
    if status not in ('completed', 'failed', 'interrupted'):
        raise ValueError('terminal status is not a native terminal outcome')
    items = turn.get('items')
    if not isinstance(items, list) or any(
            not isinstance(item, dict) or not isinstance(item.get('id'), str)
            or not isinstance(item.get('type'), str) for item in items):
        raise ValueError('terminal items must be an array of identified item objects')
    error = turn.get('error')
    if error is not None:
        if status != 'failed':
            raise ValueError('terminal error contradicts non-failed status')
        _error(error)
    return status
