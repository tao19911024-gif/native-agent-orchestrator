"""Read named grant attribution from its supplied source; no project registry."""
import re


class ProducerError(RuntimeError):
    """A refusal that names one exact recovery action."""

    def __init__(self, message: str, resolution: str) -> None:
        super().__init__(message)
        self.message = message
        self.resolution = resolution


def parse_grants(text: str) -> dict[str, dict[str, str]]:
    """Read every ``## anchor`` section and its Authority / Granted / Scope bullets.

    The bullets are matched by their bold label exactly as GRANTS.md's own "How to cite one"
    section documents them, so a grant that does not carry all three is reported as incomplete
    rather than silently yielding an empty scope.
    """
    grants: dict[str, dict[str, str]] = {}
    sections = re.split(r"^##\s+(\S[^\n]*)$", text, flags=re.M)
    # sections[0] is the preamble; then (anchor, body) pairs.
    for index in range(1, len(sections) - 1, 2):
        anchor = sections[index].strip()
        body = sections[index + 1]
        fields: dict[str, str] = {}
        for label in ("Authority", "Granted", "Scope"):
            m = re.search(
                r"^-\s+\*\*%s[^:*]*:\*\*\s*(.+?)(?=^\s*-\s+\*\*|\Z)" % label,
                body, re.M | re.S)
            if m:
                fields[label.lower()] = " ".join(m.group(1).split())
        grants[anchor] = fields
    return grants


def operator_block(anchor: str, grants_text: str, *, source: str) -> dict[str, str]:
    """Build the operator_authorization block from the FILE, never from the caller."""
    grants = parse_grants(grants_text)
    if anchor not in grants:
        known = ", ".join(sorted(grants)) or "(none)"
        raise ProducerError(
            "no grant is recorded under the anchor %r" % anchor,
            "cite a grant that exists in GRANTS.md, or record the grant there first. "
            "Anchors present: " + known)
    fields = grants[anchor]
    missing = [k for k in ("authority", "granted", "scope") if not fields.get(k)]
    if missing:
        raise ProducerError(
            "the grant %r is missing %s" % (anchor, ", ".join(missing)),
            "complete that section of GRANTS.md; an authorization may not supply what the "
            "grant itself does not state.")
    return {
        "authority": fields["authority"],
        "granted": fields["granted"],
        "scope": fields["scope"],
        "evidence_ref": source + "#" + anchor,
    }
