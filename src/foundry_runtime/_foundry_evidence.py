"""Locations of retained Foundry work; outcomes remain their owners' authority."""
import json
from pathlib import Path


def evidence_roots(roots, tag):
    """Include existing CLI locators without inferring a verdict or session link."""
    roots = set(roots)
    for folder in tuple(roots):
        for receipt in folder.glob("cli-route-*.json"):
            record = json.loads(receipt.read_bytes())
            if not isinstance(record, dict):
                raise ValueError("expected object: " + str(receipt))
            if record.get("tag") == tag and record.get("state") == "foundry_evidence_location":
                roots.add(Path(record["foundry_evidence"]))
    return roots
