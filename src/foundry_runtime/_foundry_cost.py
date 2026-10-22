"""Read-only list-price scenarios for deduplicated usage, never account billing.

Rates are a dated official-price snapshot. Unknown models remain unpriced.
Long-context/fast/regional multipliers need per-request data and are not inferred
from a task's cumulative tokens. No prices participate in execution admission.
"""
from __future__ import annotations
import math


PRICE_DATE = "2026-10-10"
SOURCES = ["https://developers.openai.com/api/docs/pricing",
           "https://platform.claude.com/docs/en/about-claude/pricing"]
# USD / million: ordinary input, cache read, cache write min/max, output.
RATES = {
    "gpt-6-astra": (10, 1, 12.5, 12.5, 50),
    "gpt-6-sol": (2, .2, 2.5, 2.5, 10),
    "gpt-6.1-sol": (2, .1, 2.5, 2.5, 10),
    "gpt-5.6-terra": (2, .2, 2.5, 2.5, 12),
    "gpt-6-luna": (.1, .01, .125, .125, .5),
    "claude-opus-5-5": (4, .2, 5, 8, 20),
    "claude-sonnet-5-5": (2, .1, 2.5, 4, 10),
    "claude-sonnet-5": (2, .2, 2.5, 4, 10),
    "claude-haiku-4-5-20251001": (1, .1, 1.25, 2, 5),
    "claude-haiku-5-5": (.1, .01, .125, .2, .5),
    "claude-fable-5": (10, 1, 12.5, 20, 50),
    "claude-fable-5-1": (10, .25, 12.5, 20, 50),
}


def estimate(model, metrics, *, backend):
    """Unknown/missing categories are not free; reasoning is part of output."""
    def counter(name):
        metric = metrics.get(name, {})
        value = metric.get("observed_sum")
        if (type(value) is not int or value < 0 or metric.get("missing_call_ids")
                or metric.get("missing_sessions")):
            raise ValueError("incomplete counter: " + name)
        return value

    row = {"model": model, "backend": backend, "usd_min": None, "usd_max": None,
           "token_breakdown": None}
    try:
        ordinary, output = counter("input_tokens"), counter("output_tokens")
        if backend == "codex":
            read, write = counter("cached_input_tokens"), counter("cache_write_input_tokens")
            ordinary -= read + write
            if ordinary < 0:
                raise ValueError("cache counters exceed input")
        elif backend == "claude":
            read, write = counter("cache_read_input_tokens"), counter("cache_creation_input_tokens")
        else:
            raise ValueError("provider token semantics unknown")
        # The existing role/model rows already own deduplication. Show disjoint
        # counters even for newly released models without a dated price; never
        # add Codex cache input or reasoning output to their inclusive totals.
        row["token_breakdown"] = {
            "ordinary_input_tokens": ordinary, "cache_read_input_tokens": read,
            "cache_write_input_tokens": write, "output_tokens": output,
            "total_input_tokens": ordinary + read + write,
            "scope": "Observed counters for this role/model row; output includes reasoning. "
                     "Claude session totals can span earlier work; missing counters remain unknown. "
                     "Token volume is not account charge or evidence of waste."}
        if model not in RATES:
            raise ValueError("model has no dated price in this snapshot")
        price, cache, write_low, write_high, out = RATES[model]
        base = ordinary * price + read * cache + output * out
        row.update(usd_min=(base + write * write_low) / 1e6,
                   usd_max=(base + write * write_high) / 1e6,
                   output_usd=output * out / 1e6, cache_read_usd=read * cache / 1e6)
        if model == 'claude-haiku-5-5':
            # Session totals cannot establish which requests exceeded 100k.
            row['usd_max'] *= 5
            row['pricing_uncertainty'] = 'Per-request prompt size unavailable: short/over-100k and cache-TTL bounds, not a bill.'
            row['output_usd_max'] = row['output_usd'] * 5
            row['cache_read_usd_max'] = row['cache_read_usd'] * 5
    except ValueError as exc:
        row["reason"] = str(exc)
    return row


def report_cost(report):
    """Use existing identity reconciliation, not raw attempts or root+round sums."""
    rows = []
    for group in report.get("coding_model_usage", []):
        rows.append({"role": "coding", "position": group["position"], "model_basis": "native client turn setting; served model unobserved",
            **estimate(group["model"], group["provider_usage_metrics"], backend="codex")})
    for role, field in (("review", "review_requested_model_usage"),
                        ("manager", "manager_requested_model_usage"), ("expert", "expert_requested_model_usage")):
        for group in report.get(field, []):
            if group["backend"] == "codex":
                rows.append({"role": role, "model_basis": group["model_basis"],
                    **estimate(group["requested_model"], group["provider_usage_metrics"], backend="codex")})
    # Claude attempt counters can include previous resumed turns. Combine the
    # already observed sessions through the existing cumulative-session owner.
    from .foundry_claude import aggregate_sessions
    views = {"coding": report.get("coding", {}).get("claude_session_usage", {}),
             "review": report.get("claude_review_session_usage", {}),
             "manager": report.get("claude_manager_session_usage", {}),
             "expert": report.get("claude_expert_session_usage", {})}
    sessions = aggregate_sessions([s for view in views.values() for s in view.get("sessions", [])],
                                  [g for view in views.values() for g in view.get("gaps", [])])
    roles_by_session = {}
    for role, view in views.items():
        for session in view.get("sessions", []):
            roles_by_session.setdefault(session["session_id"], set()).add(role)
    by_roles = {}
    for session in sessions["sessions"]:
        roles = tuple(sorted(roles_by_session[session["session_id"]]))
        by_roles.setdefault(roles, []).append(session)
    for roles, selected in sorted(by_roles.items()):
        # A session shared across roles is priced once, without inventing a split.
        for group in aggregate_sessions(selected)["by_model"]:
            values = [row.get('list_price_usd') for session in selected for row in session['models']
                      if row['model'] == group['model']]
            observed = [v for v in values if type(v) in (int, float) and math.isfinite(v) and v >= 0]
            native_cost = sum(observed) if observed else None
            rows.append({"role": "claude " + "/".join(roles) + " sessions", "roles": list(roles),
                "model_basis": "native session models",
                "provider_reported_list_usd": native_cost,
                "provider_reported_cost_missing_sessions": len(values) - len(observed),
                **estimate(group["model"], group["metrics"], backend="claude")})
    known = [r for r in rows if r["usd_min"] is not None]
    gaps = [*report.get("gaps", []), *report.get("coding", {}).get("missing_observations", []), *sessions["gaps"]]
    partial = bool(gaps or len(known) != len(rows) or report.get("coverage") == "partial")
    native_rows = [r for r in rows if r.get('provider_reported_list_usd') is not None]
    native_subtotal = sum(r['provider_reported_list_usd'] for r in native_rows) if native_rows else None
    missing_cost_sessions = sum(r.get('provider_reported_cost_missing_sessions', 0) for r in rows)
    native_cost = {'currency': 'USD', 'observed_subtotal_usd': native_subtotal,
        'coverage': 'unavailable' if not native_rows else
                    'partial' if gaps or missing_cost_sessions or len(native_rows) != len(rows) or report.get('coverage') == 'partial'
                    else 'selected_claude_sessions',
        'unobserved_group_count': len(rows) - len(native_rows),
        'missing_cost_session_observations': missing_cost_sessions,
        'member_role_attribution': 'unavailable', 'actual_account_charge': 'unknown',
        'scope': 'Native Claude modelUsage costUSD, latest selected cumulative session snapshots counted once. '
                 'Includes native team members without a leader/worker/expert split. '
                 'Separate from dated token-price scenarios; never add these two views. '
                 'Unselected continuations and direct CLI are not discovered or included.'}
    result = {"price_date": PRICE_DATE, "sources": SOURCES, "currency": "USD", "rows": rows,
        "provider_reported_cost": native_cost,
        "coverage": "unavailable" if not known else "partial" if partial else "priced_observations",
        "observed_subtotal_usd_min": sum(r["usd_min"] for r in known) if known else None,
        "observed_subtotal_usd_max": sum(r["usd_max"] for r in known) if known else None,
        "usage_gaps": gaps,
        "unpriced_group_count": len(rows) - len(known), "session_gaps": sessions["gaps"],
        "actual_account_charge": "unknown",
        "scope": "Selected coding/review/manager/expert observations only; direct CLI, unobserved descendants, "
                 "tool charges and human work excluded. Cumulative sessions may include earlier work. "
                 "Claude sessions shared across roles are priced once in a shared-role row, not split by role. "
                 "Failed attempts with observed usage are included. This is not a full-task bill or savings claim.",
        "assumptions": "Dated API list-price scenarios; Haiku 5.5 includes short/over-100k bounds when request sizes are absent. Other fast/regional/long-context multipliers are not inferred. "
                       "Claude cache-write TTL is absent from model summaries, so show the 5m-to-1h range. "
                       "Missing models/counters stay unpriced, never zero. Do not infer quality from this estimate."}
    return result
