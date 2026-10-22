"""Render existing argv for the operator shell; never execute it."""
import os
import re
import shlex


def command(arguments):
    if os.name != "nt":
        return shlex.join(arguments)
    # This workspace's operator shell is PowerShell. A quoted executable needs
    # the call operator; single quotes also preserve literal $ and backticks.
    return "& " + " ".join(value if re.fullmatch(r"[-A-Za-z0-9_./:\\]+", value)
                            else "'" + value.replace("'", "''") + "'" for value in arguments)
