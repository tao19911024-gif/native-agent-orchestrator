"""Resolved native settings shared by intent recording and provider transports.

These are the already selected trusted executor values, not a new permissions
choice. Projects receive snapshots; transports consume the selected snapshot.
"""

def coding_policy():
    return {'contract': 'trusted-executor-v1', 'default_permissions': ':danger-full-access',
            'approval_policy': 'never', 'sandbox_mode': 'danger-full-access'}


def claude_policy():
    return {'contract': 'trusted-executor-v1', 'provider': 'claude',
            'permission_mode': 'bypassPermissions'}


CLAUDE_BASH_GUIDANCE = (
    "Set the Bash tool's timeout in milliseconds before starting a long foreground command, "
    "using its expected duration and a margin (e.g. 4500000 for a 50-minute suite). "
    "Give short probes a short timeout; close database connections in finally and return a failure exit code on errors."
)


def claude_environment():
    return {'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS': '1',
            'BASH_MAX_TIMEOUT_MS': str(24 * 3600 * 1000),
            'BASH_DEFAULT_TIMEOUT_MS': str(20 * 60 * 1000)}


def profiles():
    return {'codex': coding_policy(), 'claude': claude_policy(),
            'claude_environment': claude_environment()}
