#!/usr/bin/env python3
"""Write the machine-readable ResultReceipt a dispatch can HONESTLY declare.

WHY THIS IS A SEPARATE MODULE AND NOT LINES IN dispatch.py
-----------------------------------------------------------
`dispatch.py` is 952 lines against a 1,000-line EXTRACT rule, and the operator's standing
instruction is that a large file gains a caller, never a feature. So the receipt is built
here and `dispatch.py` gains one call.

WHAT IT MAY SAY, AND WHAT IT MAY NOT
--------------------------------------
Prompt section 12 gives the receipt five states -- completed, partial, candidate, blocked,
unknown. **Those are TASK verdicts, and a dispatcher cannot produce one.** It knows the
process exited; it does not know whether the work was done. `_execution_intent.py` is
already careful about exactly this and stamps `verification: "not_evaluated"` beside every
finish -- and then records `outcome = "process_completed" if returncode == 0`, which every
machine consumer has read as success ever since. A CLAUSE 0 stop exits 0. A run that
stopped having written nothing and a run that finished its work are the same record.

So this module writes TWO axes and never collapses them:

    execution     what the journal proves: state, process outcome, returncode.  DECLARED.
    verification  whether anything judged the task.  Default `not_evaluated`, with the
                  reason stated, because an empty field reads as an oversight and
                  `not_evaluated` with a reason reads as a fact.

and it holds the invariant that makes the pair safe: **while the task is not evaluated,
`status` is `unknown`.** Not "probably completed", not the process outcome wearing a task
outcome's name. If that invariant is ever relaxed, this module has become the very defect
it was written against.

WHAT IT DELIBERATELY DOES NOT DO
----------------------------------
It does not read the executor's prose. `design/workorder_v2/receipt.py` does that and its
output is an INFERENCE -- five independent cross-model reviews defeated five separate
versions of those heuristics, which is the argument for keeping inference out of the
dispatch path entirely. A derived receipt is stamped `derived_by`; this one is stamped
`execution.declared_by`, and the two must never be merged into one field.

It also adds no gate and blocks nothing: a receipt that cannot be written is reported and
skipped, never raised. Writing a record must not be able to fail the work it records.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import _execution_usage

SCHEMA_VERSION = "pdcl-resultreceipt-1"
DECLARED_BY = "bin/result_receipt.py"

# The one sentence a reader needs when no verdict is present. It names the mechanism rather
# than apologising, so nobody has to rediscover why the field is empty.
WHY_NOT = ("the dispatcher observes the process, not the task: a CLAUSE 0 stop exits 0, so "
           "the exit code cannot distinguish a run that stopped having written nothing from "
           "one that finished its work")


def declare(record: dict, *, report_ref: str | None = None, observed_until: str | None = None) -> dict:
    """Read the live intent projection, or the legacy journal-event envelope."""
    data = record if "data" not in record else record["data"]
    evidence = data.get("evidence") or {}
    result = evidence.get("result") or {}
    log = evidence.get("log") or {}

    refs = []
    for name in ("log", "result", "cleanup", "terminal"):
        ref = evidence.get(name)
        if isinstance(ref, dict) and ref.get("path"):
            refs.append({"kind": name, "path": ref["path"], "sha256": ref.get("sha256")})

    receipt = {
        "schema_version": SCHEMA_VERSION,
        "order_id": record.get("task_key"),
        "execution_intent_id": record.get("intent_id"),
        "attempt_id": (data.get("cleanup") or {}).get("attempt_id"),
        # The invariant. See the module docstring; changing this line is changing the claim.
        "status": "unknown",
        "execution": {
            "state": record.get("state"),
            "process_outcome": data.get("outcome"),
            "returncode": data.get("returncode"),
            "returncode_source": data.get("returncode_source", "native_exit"),
            "declared_by": DECLARED_BY,
        },
        "verification": {
            "task_status": "not_evaluated",
            "evaluated_by": None,
            "why_not": WHY_NOT,
        },
        "stop_code": "EXECUTION_UNKNOWN",
        "stop_category": None,
        "remediation_owner": None,
        "acceptance": {},
        "evidence_refs": refs,
        "report_ref": report_ref or result.get("path") or log.get("path") or "",
        "derived_by": None,
    }
    # A retained finish event has its own end bound. Live projections get the
    # writer's capture time; neither uses file mtime or the newest session total.
    until = observed_until or (record.get("created_utc") if record.get("kind") == "finish" else None)
    receipt["coding_usage"] = _execution_usage.observe(receipt, until=until)
    return receipt


def annotate_capacity(log_path, observation):
    """Enrich the existing derived receipt without changing its journal evidence."""
    target = Path(log_path).with_suffix('.receipt.json')
    try:
        receipt = json.loads(target.read_bytes())
        if receipt.get('provider_recovery') == observation:
            return
        receipt['provider_recovery'] = observation
        _store(target, json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        print('capacity recovery receipt unavailable: ' + str(exc), flush=True)


def _store(target, blob):
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix='.receipt.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write(record: dict, log_path, *, report_ref: str | None = None):
    """Write `<log>.receipt.json` beside the dispatch log. Returns the path, or None.

    NEVER RAISES. A dispatch that succeeded must not be reported as failed because its
    receipt could not be stored, and a dispatch that failed must not have its real error
    replaced by one from this module. The caller gets None and a printed line.
    """
    try:
        target = Path(log_path).with_suffix(".receipt.json")
        blob = json.dumps(declare(record, report_ref=report_ref,
                                  observed_until=datetime.now(timezone.utc).isoformat()),
                          ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        # Atomic: a half-written receipt is worse than none, because it reads as a record.
        _store(target, blob)
        return target
    except BaseException as exc:                                    # noqa: BLE001
        print("  receipt not written (%s: %s); the dispatch result is unaffected"
              % (type(exc).__name__, str(exc)[:120]))
        return None


# ---------------------------------------------------------------- controls

_RECORD = {
    "task_key": "TESTORD1",
    "intent_id": "a" * 64,
    "state": "completed",
    "data": {
        "returncode": 0,
        "outcome": "process_completed",
        "verification": "not_evaluated",
        "cleanup": {"attempt_id": "att-1"},
        "evidence": {
            "log": {"path": "D:/x/evidence/T/dispatch.log", "sha256": "1" * 64},
            "result": {"path": "D:/x/evidence/T/dispatch.result.md", "sha256": "2" * 64},
            "cleanup": {"path": "D:/x/evidence/T/dispatch.cleanup.json", "sha256": "3" * 64},
        },
    },
}


def _selftest() -> int:
    import copy

    results = []

    def check(ok, label, note):
        results.append((bool(ok), label, note))

    r = declare(_RECORD)
    check(r["status"] == "unknown" and r["verification"]["task_status"] == "not_evaluated",
          "a clean exit-0 run is `unknown`, never `completed`",
          "process_completed is a PROCESS fact; a CLAUSE 0 stop exits 0 too, so asserting a "
          "task verdict here would install the exact defect this module exists against")

    failed = copy.deepcopy(_RECORD)
    failed["data"].update(returncode=1, outcome="process_failed")
    rf = declare(failed)
    check(rf["status"] == "unknown" and rf["execution"]["process_outcome"] == "process_failed",
          "a non-zero exit is recorded but still not a task verdict",
          "a failed launcher says the process died, not that the work is wrong; the "
          "distinction is kept on the execution axis")

    check(r["execution"]["declared_by"] == DECLARED_BY and r["derived_by"] is None,
          "a declaration names its author and is not marked derived",
          "`derived_by` belongs to design/workorder_v2/receipt.py, whose status is an "
          "INFERENCE; merging the two fields would make them indistinguishable")

    check([x["kind"] for x in r["evidence_refs"]] == ["log", "result", "cleanup"]
          and all(x["sha256"] for x in r["evidence_refs"]),
          "every evidence reference carries its sha256",
          "a path alone can be replaced; the hash is what makes the reference a record")

    from foundry_runtime.resources import result_receipt_schema
    ok_schema = False
    try:
        props = result_receipt_schema()["properties"]
        ok_schema = (set(r) <= set(props)
                     and set(r["execution"]) <= set(props["execution"]["properties"])
                     and set(r["verification"]) <= set(props["verification"]["properties"]))
    except (OSError, ValueError, KeyError, TypeError):
        pass  # A missing/unreadable packaged schema is a failed selftest, never success.
    check(ok_schema, "every field written is a field the schema declares",
          "a drift check in the direction that matters: this module inventing a field the "
          "schema never described would produce records nothing else can read")

    # The control that protects the dispatch itself.
    class _Exploding:
        def __getitem__(self, key):
            raise RuntimeError("journal unreadable")
        def get(self, *a, **k):
            raise RuntimeError("journal unreadable")
    check(write(_Exploding(), "D:/definitely/not/a/path/dispatch.log") is None,
          "a receipt that cannot be built returns None instead of raising",
          "writing a record must never be able to fail the work it records; dispatch.py "
          "must come out of this call the way it went in")

    for ok, label, note in results:
        print("  [%s] %s" % ("PASS" if ok else "FAIL", label))
        print("         %s" % note)
    n = sum(1 for ok, *_ in results if ok)
    print("\n%d of %d controls fire." % (n, len(results)))
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    import argparse
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true", help="prove the two axes stay separate")
    ap.add_argument("--show", action="store_true", help="print the receipt for a sample record")
    ap.add_argument("--usage-event", type=Path,
                    help="read a retained intent finish event and report usage without rewriting history")
    a = ap.parse_args()
    if a.usage_event:
        event = json.loads(a.usage_event.read_bytes())
        if event.get("kind") != "finish":
            ap.error("--usage-event requires a retained finish event")
        print(json.dumps(declare(event), ensure_ascii=True, indent=2, sort_keys=True))
        raise SystemExit(0)
    if a.show:
        print(json.dumps(declare(_RECORD), ensure_ascii=False, indent=2, sort_keys=True))
        raise SystemExit(0)
    raise SystemExit(_selftest() if a.selftest else _selftest())
