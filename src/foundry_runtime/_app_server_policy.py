"""Dispatcher/transport value contract; no execution owner or runtime extension."""
import math

OPTIONAL_CLOCKS = {'overall_seconds', 'inactivity_seconds'}
DEFAULT_CAPACITY_DELAYS = (60, 300, 900, 1800, 3600, 3600)
DEFAULTS = {'overall_seconds': None, 'inactivity_seconds': None,
            'rpc_seconds': 60.0, 'interrupt_grace_seconds': 15.0}
MAXIMA = {'rpc_seconds': 300, 'interrupt_grace_seconds': 60}


def _seconds(key, value):
    if value is None and key in OPTIONAL_CLOCKS:
        return None
    try:
        number = float(value) if not isinstance(value, bool) and isinstance(value, (int, float)) else 0
    except (OverflowError, ValueError):
        number = 0
    if not math.isfinite(number) or number <= 0 or (key in MAXIMA and number > MAXIMA[key]):
        raise ValueError('app-server ' + key + ' must be finite and positive' +
                         (f', <= {MAXIMA[key]}' if key in MAXIMA else ', or null for no deadline'))
    return number


def normalize_policy(policy=None, *, timeout_seconds=None, capacity_defaults=False):
    if policy is not None and not isinstance(policy, dict):
        raise ValueError('app_server_policy must be an object')
    supplied = dict(policy or {})
    delays = supplied.pop('capacity_retry_seconds', None)
    if delays is None and capacity_defaults:
        delays = DEFAULT_CAPACITY_DELAYS
    if supplied.keys() - DEFAULTS.keys():
        raise ValueError('unknown app-server policy option')
    if timeout_seconds is not None:
        try:
            _seconds('overall_seconds', timeout_seconds)
        except ValueError as exc:
            raise ValueError('invalid timeout_seconds: ' + str(exc)) from exc
        if 'overall_seconds' in supplied and supplied['overall_seconds'] != timeout_seconds:
            raise ValueError('timeout_seconds conflicts with overall_seconds')
        supplied.setdefault('overall_seconds', timeout_seconds)
    result = {**DEFAULTS, **supplied}
    for key, value in result.items():
        result[key] = _seconds(key, value)
    if delays is not None:
        if not isinstance(delays, (list, tuple)):
            raise ValueError('capacity_retry_seconds must be a list; [] disables automatic retries')
        result['capacity_retry_seconds'] = [_seconds('capacity_retry_seconds', value) for value in delays]
    return result


def add_policy_arguments(parser):
    parser.add_argument('--app-server-capacity-retry-seconds', type=float, nargs='*',
                        help='per-task capacity retry delays; default 60 300 900 1800 3600 3600; empty disables retries')
    for key in DEFAULTS:
        parser.add_argument('--app-server-' + key.replace('_', '-'), type=float,
                            help=('optional app-server work deadline in seconds; omitted means no cutoff'
                                  if key in OPTIONAL_CLOCKS else
                                  f'app-server operation allowance; default {DEFAULTS[key]:g}s, max {MAXIMA[key]}s'))


def policy_from_arguments(args, parser):
    supplied = {key: getattr(args, 'app_server_' + key, None) for key in (*DEFAULTS, 'capacity_retry_seconds')
                if getattr(args, 'app_server_' + key, None) is not None}
    if not supplied:
        return None
    direct = bool(args.order) ^ bool(args.resume)
    if (not direct or args.launcher != 'app-server' or args.steer
            or args.foundry_analyze):
        parser.error('app-server policy applies only to coding --order/--resume or Foundry goal with --launcher app-server')
    try:
        return normalize_policy(supplied)
    except ValueError as error:
        parser.error(str(error))
