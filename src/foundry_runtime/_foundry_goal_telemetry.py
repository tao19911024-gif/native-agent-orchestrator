"""Observed phase timing for a Foundry goal run: evidence, never authority.

WHY THIS EXISTS.

The goal engine already records WHAT happened to every phase (the journal's
prepared/completed/stopped receipts) and WHAT the host measured (the test
controller's record). Neither records HOW LONG anything took, so every claim
about where a goal run spends its wall time has been an estimate. An estimate
cannot tell you whether a review pair is worth parallelising, whether an
amendment is repaying a cost it already paid, or which acceptance route a run
actually took.

This owner adds the missing observation and nothing else. Three properties keep
it from becoming a second authority:

  * APPEND-ONLY, SEPARATE FILE. Rows land in `telemetry.jsonl` beside the phase
    receipts. No existing receipt's bytes change, so phase identity, result
    hashes, native receipt reconciliation and replay comparison are untouched.

  * BEST-EFFORT. `record` never raises. A full disk, a read-only evidence
    directory or a serialisation failure must not turn a completed phase into a
    failed one, and must never make a caller repeat an external effect it has
    already paid for.

  * DERIVED, NOT DECIDED. `summarize` reads rows back and computes durations.
    Nothing here is consulted by outcome derivation, acceptance, review
    verdicts or recovery. A missing, truncated or corrupt log yields a thinner
    summary and changes no verdict.

Replay is silent by construction: a completed phase returns its cached receipt
without running its action, so no row is appended and no model or test is
re-run to obtain a timing that the original row already carries.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json

FILENAME = "telemetry.jsonl"
SCHEMA_VERSION = "foundry-goal-telemetry-1"


def now():
    return datetime.now(timezone.utc)


# THE CLOCK IS PART OF THE OBSERVER, SO IT OWES THE OBSERVER'S GUARANTEE.
#
# `record` below has always carried "never raises", because a write failure
# obviously must not fail a phase that succeeded. The clock did not, and an
# INDEPENDENT REVIEWER on a different vendor's model found that gap after four
# same-vendor reviews had passed the code. It mattered because both observation
# points run AFTER their phase's authoritative work: the journal observes after
# the phase receipt is written, and the review group observes after the native
# call has been made and paid for. An escaping clock error there left the disk
# saying `completed` while the caller saw a crash, and discarded the result of a
# model call that had already happened.
#
# These two helpers are the whole fix: every observation now begins and ends
# through a surface that cannot raise, and an observation with no usable timing
# is simply not recorded.

def started():
    """When an observation began, or None if the clock is unusable. Never raises."""
    try:
        return now()
    except Exception:
        return None


def measure(start):
    """The timing fields for an observation begun at `start`, or None. Never raises."""
    if start is None:
        return None
    try:
        completed = now()
        return {"started_utc": start.isoformat(), "completed_utc": completed.isoformat(),
                "duration_s": max((completed - start).total_seconds(), 0.0)}
    except Exception:
        return None


def span(start_iso, end_iso):
    """Seconds between two ISO instants; unreadable input yields 0.0, never an error."""
    try:
        return max((datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)).total_seconds(), 0.0)
    except (ValueError, TypeError):
        return 0.0


def record(folder, row):
    """Append one observation. Never raises: telemetry cannot fail a phase."""
    try:
        line = json.dumps({"schema_version": SCHEMA_VERSION, **row},
                          ensure_ascii=False, allow_nan=False, sort_keys=True)
        with (folder / FILENAME).open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    except (OSError, ValueError, TypeError, UnicodeError):
        return


def read(folder):
    """Every well-formed row, in observation order. A bad line is skipped, not fatal."""
    path = folder / FILENAME
    rows = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION:
            rows.append(value)
    return rows


def _phases(rows):
    """Last observation per phase name: a re-crossed phase reports its live cost."""
    latest = {}
    for row in rows:
        name = row.get("phase")
        if row.get("group"):
            continue  # a group is an aggregate of phases, not a phase
        if isinstance(name, str) and isinstance(row.get("duration_s"), (int, float)):
            latest[name] = row
    return latest


def _group_wall_time(rows, prefix):
    """Wall time of a concurrent group: last finish minus first start, not the sum.

    Summing member durations would report serial cost even when the members ran
    at once, which is exactly the number this module exists to stop guessing.
    """
    members = [row for row in rows
               if isinstance(row.get("phase"), str) and row["phase"].startswith(prefix)
               and isinstance(row.get("started_utc"), str) and isinstance(row.get("completed_utc"), str)]
    if not members:
        return None
    try:
        starts = [datetime.fromisoformat(row["started_utc"]) for row in members]
        ends = [datetime.fromisoformat(row["completed_utc"]) for row in members]
    except ValueError:
        return None
    return max((max(ends) - min(starts)).total_seconds(), 0.0)


def summarize(folder):
    """Derive a run's timing view. Absent rows yield absent fields, never a verdict."""
    rows = read(folder)
    phases = _phases(rows)
    summary = {"schema_version": SCHEMA_VERSION,
               "phases": {name: row["duration_s"] for name, row in sorted(phases.items())},
               "observations": len(rows)}
    for name in ("research", "solution", "baseline"):
        if name in phases:
            summary[name + "_s"] = phases[name]["duration_s"]
    coding = {name: row["duration_s"] for name, row in phases.items() if name.startswith("coding-")}
    tests = {name: row["duration_s"] for name, row in phases.items() if name.startswith("tests-")}
    reviews = {name: row["duration_s"] for name, row in phases.items() if name.startswith("review-")}
    summary["coding_s"] = coding
    summary["candidate_tests_s"] = tests
    summary["amendments"] = max(len(coding) - 1, 0)
    # The group owns the review cost: once the native calls move into the
    # concurrent group, a reviewer's phase receipt spans only its journal write,
    # so member timings must come from the group's own observation.
    groups = [row for row in rows if row.get("group") and isinstance(row.get("members"), dict)]
    if groups:
        latest = groups[-1]
        members = {name: value for name, value in latest["members"].items()
                   if isinstance(value, (int, float))}
        summary["reviews_s"] = members
        summary["review_group_wall_s"] = latest.get("duration_s")
        if members:
            summary["review_serial_s"] = sum(members.values())
            summary["review_max_s"] = max(members.values())
            summary["review_join_overhead_s"] = max(
                (latest.get("duration_s") or 0.0) - max(members.values()), 0.0)
    else:
        summary["reviews_s"] = reviews
        group = _group_wall_time(rows, "review-")
        if group is not None and reviews:
            summary["review_group_wall_s"] = group
            summary["review_serial_s"] = sum(reviews.values())
            summary["review_max_s"] = max(reviews.values())
            summary["review_join_overhead_s"] = max(group - max(reviews.values()), 0.0)
    routes = {name: row.get("route") for name, row in phases.items()
              if name.startswith("tests-") and row.get("route")}
    if routes:
        summary["test_routes"] = routes
    concurrency = [row.get("review_concurrency") for row in rows if row.get("review_concurrency")]
    if concurrency:
        summary["review_concurrency"] = concurrency[-1]
    total = [row for row in rows if isinstance(row.get("started_utc"), str)
             and isinstance(row.get("completed_utc"), str)]
    if total:
        try:
            starts = [datetime.fromisoformat(row["started_utc"]) for row in total]
            ends = [datetime.fromisoformat(row["completed_utc"]) for row in total]
            summary["observed_span_s"] = max((max(ends) - min(starts)).total_seconds(), 0.0)
        except ValueError:
            pass
    return summary
