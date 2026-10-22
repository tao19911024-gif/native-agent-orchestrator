"""Shared task identity contract; no project registry or host dependencies.

AuthorityError keeps the existing exception identity used by admission callers.
The registry consumes this contract; identity parsing never loads the registry.
"""
from pathlib import Path
import re

_TAG = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")


class AuthorityError(ValueError):
    """A task identity or its owning authority cannot resolve the request."""


def normalized_line(order_or_tag: str | Path) -> str:
    """Whole leading filename token, or a validated bare tag; case insensitive.

    Keep existing compound labels (MODEL-REG-b, NI-B1-D, ST-3g1). A filename
    separates its title with an underscore, whitespace or dot, not a hyphen.
    """
    value = str(order_or_tag)
    if isinstance(order_or_tag, Path) or value.endswith(".md") or "/" in value or "\\" in value:
        value = re.split(r"[_\s.]", Path(value).stem, maxsplit=1)[0]
    if not _TAG.fullmatch(value):
        raise AuthorityError("invalid LINE label")
    return value.upper()
