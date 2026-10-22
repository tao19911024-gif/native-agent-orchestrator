"""Observational Codex turn usage for ResultReceipt; never execution authority.

The installed 0.153.4 rollout has response-id keyed token_usage_record entries.
Stream a fixed snapshot, not cumulative token_count displays or an account total.
Unavailable/ambiguous observations must not affect dispatch or task acceptance.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re

from ._native_session import _session_root, read_retained_session_header

FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
          "output_tokens", "reasoning_output_tokens", "total_tokens")
REQUIRED = set(FIELDS) - {"cache_write_input_tokens"}
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
TEAM_CALLS = {"spawn_agent", "followup_task", "send_message", "interrupt_agent", "wait_agent", "list_agents"}


def _team_arguments(raw):
    """Retain routing/compute metadata, not another copy of member contexts."""
    try:
        args = json.loads(raw or "{}")
        result = {k: args[k] for k in ("target", "task_name", "model", "reasoning_effort", "fork_turns")
                  if k in args and isinstance(args[k], (str, int))}
        message = args.get("message")
        if isinstance(message, str):
            from ._tool_discipline import declared_activity
            encoded = message.encode("utf-8")
            result.update(stored_message_sha256=hashlib.sha256(encoded).hexdigest(),
                          stored_message_bytes=len(encoded),
                          message_observation="retained argument value; may be encrypted, not plaintext metadata")
            if activity := declared_activity(message):
                result['declared_activity'] = activity
        return result
    except (ValueError, TypeError, AttributeError):
        return {"arguments_observation": "unavailable"}


def _time(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("observation timestamp has no timezone")
    return dt


def _team_event(row):
    payload = row["payload"]
    return {"timestamp": row["timestamp"], "call_id": payload["call_id"],
            "turn_identity_source": payload.get("turn_identity_source",
                "native turn_id" if "turn_id" in payload else "unattributed"),
            "tool": payload["name"].split(".")[-1], **json.loads(payload["arguments"])}


def _bytes(path):
    """Compatibility reader for small JSON receipts, not conversation logs."""
    return Path(path).read_bytes()


def _reference(ref, *, retain=True):
    digest = hashlib.sha256()
    parts = []
    with Path(ref["path"]).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            if retain:
                parts.append(chunk)
    if digest.hexdigest() != ref["sha256"]:
        raise ValueError("retained evidence changed")
    return b"".join(parts) if retain else None


def _snapshot(path, start, end):
    """Hash the initial extent; keep window usage and earlier spawn identities.

    Memory follows relevant records plus the largest JSON line, not the complete
    conversation. A writer appending later cannot extend this observation.
    """
    from ._context_signals import tool_record
    digest, rows, spawn_calls = hashlib.sha256(), [], set()
    prior_calls, prior_spawns, previous_usage = {}, [], {}
    with Path(path).open("rb") as stream:
        size = remaining = os.fstat(stream.fileno()).st_size
        meta = None
        while remaining:
            line = stream.readline(remaining)
            if not line:
                raise ValueError("native snapshot shortened during observation")
            remaining -= len(line)
            digest.update(line)
            if not line.strip():
                continue
            value = json.loads(line)
            if meta is None:
                meta = value
                continue
            timestamp = _time(value["timestamp"])
            kind, payload = value.get("type"), value.get("payload", {})
            if timestamp <= end and kind == "response_item":
                if (timestamp < start and payload.get("type") == "function_call"
                        and payload.get("name", "").split(".")[-1] == "spawn_agent"):
                    prior_calls[payload["call_id"]] = value["timestamp"]
                elif payload.get("type") == "function_call_output" and payload.get("call_id") in prior_calls:
                    try:
                        ack = json.loads(payload.get("output") or "null")
                        agent_path = ack.get("task_name") if isinstance(ack, dict) else None
                        if isinstance(agent_path, str) and agent_path.startswith("/"):
                            prior_spawns.append({"spawn_call_id": payload["call_id"],
                                "timestamp": prior_calls[payload["call_id"]],
                                "acknowledged_at": value["timestamp"], "agent_path": agent_path})
                    except (ValueError, TypeError):
                        pass  # An unresolved follow-up is reported by the child observer.
            if timestamp < start and kind == "token_usage_record" and payload.get("thread_id"):
                previous_usage[payload["thread_id"]] = value["timestamp"]
            if not start <= timestamp <= end:
                continue
            keep = kind in {"token_usage_record", "turn_context"} or (
                kind == "event_msg" and payload.get("type") in {"task_started", "task_complete"})
            if kind == "turn_context":
                value = {**value, "payload": {k: payload[k] for k in
                         ("turn_id", "model", "effort") if k in payload}}
            if kind == "response_item":
                if observation := tool_record(payload):
                    metadata = payload.get('internal_chat_message_metadata_passthrough') or {}
                    turn_id = payload.get('turn_id') or metadata.get('turn_id')
                    rows.append({'type': 'tool_observation', 'timestamp': value['timestamp'],
                                 'payload': {**observation, **({'turn_id': turn_id} if turn_id else {})}})
                item_type = payload.get("type")
                name = payload.get("name", "").split(".")[-1]
                if item_type == "function_call" and name in TEAM_CALLS:
                    keep = True
                    if name == "spawn_agent":
                        spawn_calls.add(payload["call_id"])
                    value = {**value, "payload": {
                        **{k: payload[k] for k in ("type", "name", "call_id")},
                        **({"turn_id": payload["turn_id"]} if "turn_id" in payload else {}),
                        "arguments": json.dumps(_team_arguments(payload.get("arguments")))}}
                elif item_type == "function_call_output" and payload.get("call_id") in spawn_calls:
                    keep = True
            if keep:
                rows.append(value)
    if not meta or meta.get("type") != "session_meta":
        raise ValueError("native snapshot has no session header")
    # Prefer an explicit native turn identity. Timestamp fallback only applies
    # inside an unambiguous open turn; delayed calls remain visibly unattributed.
    active = set()
    for row in sorted(rows, key=lambda r: _time(r["timestamp"])):
        p = row["payload"]
        if row.get("type") == "event_msg":
            if p.get("type") == "task_started":
                active.add(p["turn_id"])
            elif p.get("type") == "task_complete":
                active.discard(p["turn_id"])
        elif ((row.get("type") == "response_item" and p.get("type") == "function_call"
               or row.get('type') == 'tool_observation')
              and "turn_id" not in p and len(active) == 1):
            p["turn_id"] = next(iter(active))
            p["turn_identity_source"] = "open-turn timestamp interval"
    source = {"path": str(path), "snapshot_sha256": digest.hexdigest(),
              "snapshot_bytes": size, "cli_version": meta["payload"].get("cli_version"),
              "previous_usage_timestamps": previous_usage}
    if prior_spawns:
        source["prior_spawns"] = prior_spawns
    return meta, rows, source


def _counters(value):
    if not isinstance(value, dict) or not REQUIRED <= value.keys():
        raise ValueError("incomplete native usage counters")
    result = {k: value[k] for k in FIELDS if k in value}
    if any(type(v) is not int or v < 0 for v in result.values()):
        raise ValueError("invalid native usage counter")
    if (result["cached_input_tokens"] > result["input_tokens"]
            or result["reasoning_output_tokens"] > result["output_tokens"]
            or result["total_tokens"] != result["input_tokens"] + result["output_tokens"]):
        raise ValueError("inconsistent native usage counters")
    return result


def _observe(receipt, until, session_root):
    refs = {r["kind"]: r for r in receipt["evidence_refs"]}
    if len(refs) != len(receipt["evidence_refs"]):
        raise ValueError("ambiguous evidence references")
    _reference(refs["log"], retain=False)
    cleanup = json.loads(_reference(refs["cleanup"]))
    if (not receipt.get("execution_intent_id") or not receipt.get("order_id")
            or cleanup["attempt_id"] != receipt["attempt_id"]
            or type(cleanup.get("root_creation_time")) is not int):
        raise ValueError("usage lacks a bound execution attempt")
    start = datetime.fromtimestamp((cleanup["root_creation_time"] - 116444736000000000) / 10**7,
                                   timezone.utc)
    end = _time(until)
    if end < start:
        raise ValueError("observation ends before process creation")
    turn = None
    if "terminal" in refs:
        terminal = json.loads(_reference(refs["terminal"]))
        binding = terminal["binding"]
        if any(binding[k] != cleanup[k] for k in ("attempt_id", "root_pid", "root_creation_time")):
            raise ValueError("terminal belongs to another execution")
        session, turn = binding["thread_id"], binding["turn_id"]
    else:
        session = read_retained_session_header(refs["log"]["path"])
        _reference(refs["log"], retain=False)  # The header reader must have seen the retained file.
    if not isinstance(session, str) or not UUID.fullmatch(session):
        raise ValueError("invalid native session identity")
    root = Path(session_root) if session_root is not None else _session_root()
    paths = list(root.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/rollout-*-" + session + ".jsonl"))
    if len(paths) != 1:
        raise ValueError("native session unavailable or ambiguous; no archive search performed")
    path = paths[0].resolve()
    path.relative_to(root.resolve())
    meta, rows, source = _snapshot(path, start, end)
    if meta["payload"].get("id") != session:
        raise ValueError("native snapshot session identity differs")
    from ._context_signals import recorded_bound
    source["context_signal_bound"] = recorded_bound(receipt)
    result = _turn_usage(rows, meta, source, session, turn, start, end)
    try:
        result["child_agents"] = _child_usage(rows, result, root, start, end)
    except Exception as exc:
        result["child_agents"] = {"coverage": "uncovered", "observations": [],
                                  "missing_observations": [{"reason": str(exc)[:240]}]}
    return result


def _turn_usage(rows, meta, source, session, turn, start, end):
    # Timestamp bounds are the dispatch process lifetime, not the session lifetime.
    selected = []
    for row in rows:
        if row.get("type") in {"event_msg", "token_usage_record"} and start <= _time(row["timestamp"]) <= end:
            if row["type"] == "token_usage_record" or row["payload"].get("type") in {"task_started", "task_complete"}:
                selected.append(row)
    starts = {r["payload"]["turn_id"] for r in selected
              if r.get("type") == "event_msg" and r["payload"].get("type") == "task_started"}
    if turn is None:
        if len(starts) != 1:
            raise ValueError("exec observation window does not name exactly one turn")
        turn = next(iter(starts))
    elif starts != {turn}:
        raise ValueError("terminal and native observation window disagree")
    responses, totals, final_total = {}, {k: 0 for k in FIELDS}, None
    missing_fields, root_turns = set(), set()
    native_session = meta["payload"].get("session_id", session)
    for r in selected:
        if r.get("type") != "token_usage_record":
            continue
        payload = r["payload"]
        if payload.get("turn_id") != turn:
            continue
        if payload.get("thread_id") != session or payload.get("session_id") != native_session:
            raise ValueError("usage identity disagrees with its native session")
        if isinstance(payload.get("root_turn_id"), str):
            root_turns.add(payload["root_turn_id"])
        response = payload.get("response_id")
        if not isinstance(response, str) or not response:
            raise ValueError("native response identity unavailable")
        counters = _counters(payload["usage"])
        cumulative = _counters(payload["turn_token_usage"])
        observation = (counters, cumulative)
        if response in responses:
            if responses[response] != observation:
                raise ValueError("conflicting duplicate response usage")
            continue
        responses[response] = observation
        missing_fields.update(set(FIELDS) - counters.keys())
        missing_fields.update(set(FIELDS) - cumulative.keys())
        for key, value in counters.items():
            totals[key] += value
        # This also detects missing response records and a reset within the turn.
        if any(totals[k] != cumulative[k] for k in FIELDS if k not in missing_fields):
            raise ValueError("per-response usage does not reconcile with turn totals")
        final_total = cumulative
    if not responses or final_total is None:
        raise ValueError("no attributable per-response usage")
    completed = any(r.get("type") == "event_msg" and r["payload"].get("type") == "task_complete"
                    and r["payload"].get("turn_id") == turn for r in selected)
    from ._context_signals import codex_rows, reading_signals, waiting_signals
    metrics = {k: None if k in missing_fields else totals[k] for k in FIELDS}
    return {"coverage": "observed_native_turn" if completed else "partial_native_turn",
            "session_id": session, "turn_id": turn, "response_count": len(responses),
            "context_signals": codex_rows(selected, turn, source.get("previous_usage_timestamps"), source.get("context_signal_bound", {}).get("tokens")),
            "reading_signals": reading_signals(rows, turn),
            "waiting_signals": waiting_signals(rows, turn),
            "native_session_id": native_session, "native_root_turn_ids": sorted(root_turns),
            "response_ids": list(responses), "usage": metrics,
            "missing_fields": sorted(missing_fields), "native_turn_completed": completed,
            "window": {"start_utc": start.isoformat(), "observed_until_utc": end.isoformat()},
            "source": source,
            "model_observation": _model_observation(rows, turn, start, end),
            "collaboration": {
                "scope": "native tool requests in the retained session window; not delivery acknowledgements or model-call counts",
                "unattributed_call_ids": [r["payload"]["call_id"] for r in rows
                    if r.get("type") == "response_item" and r["payload"].get("type") == "function_call"
                    and "turn_id" not in r["payload"]],
                "events": [_team_event(r)
                           for r in rows if r.get("type") == "response_item"
                           and r["payload"].get("type") == "function_call"
                           and r["payload"].get("name", "").split(".")[-1] in TEAM_CALLS
                           and r["payload"].get("turn_id") == turn
                           and start <= _time(r["timestamp"]) <= end]},
            "scope": "this native turn only; coordinator and child-agent attribution not included",
            "actual_account_charge": "unknown"}


def _model_observation(rows, turn, start, end):
    declarations = []
    active_turn = None
    for row in rows:
        p = row.get("payload", {})
        if row.get('type') == 'event_msg' and p.get('type') == 'task_started':
            active_turn = p.get('turn_id')
        if (row.get("type") == "turn_context" and p.get("turn_id", active_turn) == turn
                and start <= _time(row["timestamp"]) <= end
                and isinstance(p.get("model"), str) and p["model"]):
            item = {"model": p["model"], "effort": p.get("effort")}
            if item not in declarations:
                declarations.append(item)
    return {"source": "native turn_context", "declarations": declarations,
            "coverage": "observed" if declarations else "unavailable",
            "scope": "client-recorded turn settings; not upstream served-model or billing proof"}


def _member_header(meta):
    """Do not cache old sessions' base instructions while locating a member."""
    return {"type": meta.get("type"), "timestamp": meta["timestamp"],
            "payload": {k: meta["payload"][k] for k in ("id", "session_id", "source", "cli_version")
                        if k in meta["payload"]}}


def _child_usage(parent_rows, parent, root, start, end):
    """Join explicit spawn acknowledgements to native parent/path metadata.

    Headers use the UTC window plus adjacent local-calendar dates. A shared cwd,
    close timestamp, nickname or matching task text is never an attribution key.
    Full conversation objects remain local to this reader, not in the receipt.
    """
    observations, missing, visited = [], [], {parent["session_id"]}
    unattributed = []
    headers, scanned_days = [], set()
    members, pending_followups, linked_followups = {}, [], []

    def candidates(since=start):
        # Rollout directories use local calendar dates, while rows use UTC.
        # Include adjacent dates for either offset sign (also across DST or
        # a later host timezone change); exact row timestamps still select usage.
        day = since.astimezone(timezone.utc).date() - timedelta(days=1)
        last = end.astimezone(timezone.utc).date() + timedelta(days=1)
        while day <= last:
            if day in scanned_days:
                day += timedelta(days=1)
                continue
            scanned_days.add(day)
            for path in sorted((root / day.strftime("%Y/%m/%d")).glob("rollout-*.jsonl")):
                try:
                    with path.open("rb") as stream:
                        header = json.loads(stream.readline())
                    source = header.get("payload", {}).get("source")
                    if header.get("type") == "session_meta" and isinstance(source, dict):
                        spawn = source.get("subagent", {}).get("thread_spawn")
                        if isinstance(spawn, dict):
                            headers.append((path, _member_header(header), spawn))
                except (OSError, ValueError, TypeError, AttributeError):
                    continue  # A missing expected spawn is reported below.
            day += timedelta(days=1)
        return headers

    def collect(path, meta, binding, child_start, *, reused=False):
        session = meta["payload"]["id"]
        if not isinstance(session, str) or not UUID.fullmatch(session) or session in visited:
            raise ValueError("invalid or repeated child session identity")
        if meta["payload"].get("session_id", None if reused else parent["native_session_id"]) != parent["native_session_id"]:
            raise ValueError("child belongs to another native session group")
        visited.add(session)
        path = path.resolve(); path.relative_to(root.resolve())
        child_meta, child_rows, source = _snapshot(path, child_start, end)
        if _member_header(child_meta) != meta:
            raise ValueError("child header changed during observation")
        unattributed.extend({"session_id": session, "source": source, **_team_event(r)}
            for r in child_rows if r.get("type") == "response_item"
            and r["payload"].get("type") == "function_call"
            and r["payload"].get("name", "").split(".")[-1] in TEAM_CALLS
            and "turn_id" not in r["payload"])
        turns = {r["payload"]["turn_id"] for r in child_rows
                 if r.get("type") == "event_msg" and r["payload"].get("type") == "task_started"}
        if not turns:
            raise ValueError("child has no observed native turn")
        accepted = set()
        roots = set(parent.get("native_root_turn_ids", []))
        for turn in sorted(turns):
            try:
                turn_rows = [r for r in child_rows if r.get("payload", {}).get("turn_id") == turn]
                from ._context_signals import previous_for
                turn_source = {**source, "context_signal_bound": parent["source"].get("context_signal_bound", {}),
                    "previous_usage_timestamps": previous_for(child_rows, turn, source.get("previous_usage_timestamps", {}))}
                result = _turn_usage(turn_rows, meta, turn_source, session, turn, child_start, end)
                if reused and (not roots or set(result["native_root_turn_ids"]) != roots or any(
                        r["payload"].get("root_turn_id") not in roots for r in turn_rows
                        if r.get("type") == "token_usage_record")):
                    raise ValueError("follow-up turn lacks this execution's native root-turn identity")
                result["parent_binding"] = binding
                result["execution"] = {"process_outcome": "native_turn_completed" if result["native_turn_completed"]
                                       else "native_turn_incomplete"}
                observations.append(result); accepted.add(turn)
            except Exception as exc:
                missing.append({**binding, "session_id": session, "turn_id": turn, "reason": str(exc)[:240]})
        if accepted:
            members[session] = {"created": _time(meta["timestamp"]), "since": child_start,
                                "binding": binding, "source": source}
        visit(child_rows, session, child_start, accepted if reused else turns, binding["agent_path"], source)

    def visit(rows, owner, window_start, allowed_turns, owner_path="/root", owner_source=None):
        calls, outputs, output_times, followups, linked_paths = {}, {}, {}, {}, set()
        activities = {}
        for row in rows:
            if not window_start <= _time(row["timestamp"]) <= end:
                continue
            payload = row.get("payload", {})
            if row.get("type") != "response_item":
                continue
            if (payload.get("type") == "function_call"
                    and payload.get("name", "").split(".")[-1] in {"spawn_agent", "followup_task"}
                    and payload.get("turn_id") not in allowed_turns):
                if "turn_id" not in payload:
                    missing.append({"parent_session_id": owner, "call_id": payload["call_id"],
                                    "reason": "child request has no attributable native turn"})
                continue
            if payload.get("type") == "function_call" and payload.get("name", "").split(".")[-1] == "spawn_agent":
                calls[payload["call_id"]] = row["timestamp"]
                activities[payload['call_id']] = json.loads(payload.get('arguments', '{}')).get('declared_activity')
            elif payload.get("type") == "function_call" and payload.get("name", "").split(".")[-1] == "followup_task":
                args = json.loads(payload.get("arguments", "{}"))
                target = args.get("target", "unknown")
                target = target if target.startswith("/") or UUID.fullmatch(target) else owner_path + "/" + target
                followups.setdefault(target, []).append({"call_id": payload["call_id"], "timestamp": row["timestamp"]})
            elif payload.get("type") == "function_call_output":
                outputs[payload["call_id"]] = payload.get("output")
                output_times[payload["call_id"]] = row["timestamp"]
        for call_id, timestamp in calls.items():
            binding = {"parent_session_id": owner, "spawn_call_id": call_id}
            if activities.get(call_id):
                binding['declared_activity'] = activities[call_id]
            try:
                output = json.loads(outputs.get(call_id) or "null")
                agent_path = output.get("task_name") if isinstance(output, dict) else None
                if not isinstance(agent_path, str) or not agent_path.startswith("/"):
                    raise ValueError("spawn has no supported retained task-name acknowledgement")
                binding["agent_path"] = agent_path
                binding["spawn_acknowledged_at"] = output_times[call_id]
                linked_paths.add(agent_path)
                matches = [(p, m) for p, m, s in candidates()
                           if s.get("parent_thread_id") == owner and s.get("agent_path") == agent_path
                           and _time(timestamp) <= _time(m["timestamp"]) <= end]
                if len(matches) != 1:
                    raise ValueError("child native session unavailable or ambiguous")
                path, meta = matches[0]
                session = meta["payload"]["id"]
                linked_paths.add(session)
                collect(path, meta, binding, _time(meta["timestamp"]))
            except Exception as exc:
                missing.append({**binding, "reason": str(exc)[:240]})

        reused_members = {}
        for target in sorted(followups.keys() - linked_paths):
            binding = {"parent_session_id": owner, "target": target,
                       "followup_call_ids": [f["call_id"] for f in followups[target]]}
            requested_at = min(_time(f["timestamp"]) for f in followups[target])
            try:
                history = [s for s in (owner_source or {}).get("prior_spawns", [])
                           if (s["agent_path"] == target or UUID.fullmatch(target))
                           and _time(s["acknowledged_at"]) <= requested_at]
                matches = {}
                for spawn in history:
                    for path, meta, s in candidates(_time(spawn["timestamp"])):
                        if (s.get("parent_thread_id") == owner and s.get("agent_path") == spawn["agent_path"]
                                and (not UUID.fullmatch(target) or meta["payload"].get("id") == target)
                                and _time(spawn["timestamp"]) <= _time(meta["timestamp"]) <= requested_at):
                            matches[(str(path), spawn["spawn_call_id"])] = (path, meta, spawn)
                if len(matches) != 1:
                    raise ValueError("follow-up has no unique historical spawn and member binding")
                path, meta, spawn = next(iter(matches.values()))
                session = meta["payload"]["id"]
                binding.update(agent_path=spawn["agent_path"], spawn_call_id=spawn["spawn_call_id"],
                    spawn_acknowledged_at=spawn["acknowledged_at"],
                    identity_source="historical spawn acknowledgement and native member header",
                    spawn_source={k: owner_source[k] for k in ("path", "snapshot_sha256", "snapshot_bytes")})
                if session in reused_members:
                    old = reused_members[session]
                    old[2]["followup_call_ids"].extend(binding["followup_call_ids"])
                    old[3] = min(old[3], requested_at)
                else:
                    reused_members[session] = [path, meta, binding, requested_at]
            except Exception as exc:
                pending_followups.append((binding, requested_at, str(exc)[:240]))
        for path, meta, binding, requested_at in reused_members.values():
            try:
                collect(path, meta, binding, requested_at, reused=True)
            except Exception as exc:
                missing.append({**binding, "reason": str(exc)[:240]})

    visit(parent_rows, parent["session_id"], start, {parent["turn_id"]}, owner_source=parent["source"])
    # A member can continue a sibling it did not spawn. Resolve these edges only
    # after walking the whole acknowledged team, so traversal order is irrelevant.
    # Usage already belongs to the target's native turns; never collect it again.
    for binding, requested_at, reason in pending_followups:
        target = binding["target"]
        matches = [(session, member) for session, member in members.items()
                   if target in (session, member["binding"]["agent_path"])
                   and member["created"] <= requested_at and member["since"] <= requested_at
                   and _time(member["binding"]["spawn_acknowledged_at"]) <= requested_at]
        if len(matches) == 1:
            session, member = matches[0]
            linked_followups.append({**binding, "session_id": session,
                "identity_source": "already observed team member; usage counted under native turns",
                "member_binding": member["binding"],
                "member_source": {k: member["source"][k] for k in ("path", "snapshot_sha256", "snapshot_bytes")}})
        else:
            missing.append({**binding, "reason": reason})
    complete = not (missing or unattributed) and parent["native_turn_completed"] and all(r["native_turn_completed"] for r in observations)
    return {"coverage": "observed_linked_descendants" if complete else "partial",
            "observations": observations, "missing_observations": missing,
            "linked_followups": linked_followups,
            "unattributed_requests": unattributed,
            "scope": "newly spawned and explicitly reused descendants in this dispatch window; parent excluded"}


def observe(receipt, *, until, session_root=None):
    """Usage is optional evidence, never a condition of a successful dispatch."""
    try:
        if not until:
            raise ValueError("no recorded observation boundary")
        terminal_ref = next((r for r in receipt.get("evidence_refs", []) if r.get("kind") == "terminal"), None)
        if terminal_ref:
            proof = json.loads(_reference(terminal_ref))
            if proof.get("schema_version") == "pdcl-claude-terminal-1":
                from ._dispatch_claude import verify_terminal
                from .foundry_claude import model_usage
                result = verify_terminal(proof, session=proof["session_id"],
                    returncode=receipt["execution"]["returncode"], attempt_id=receipt["attempt_id"])
                models = result.get("modelUsage")
                if not isinstance(models, dict) or not models:
                    raise ValueError("Claude result has no modelUsage")
                from ._context_signals import observe_claude, recorded_bound, observe_work
                return {"context_signal_bound": recorded_bound(receipt, "claude"),
                    "context_signals": observe_claude(proof["stdout"]["path"], recorded_bound(receipt, "claude")["tokens"]), "coverage": "claude_session_snapshot", "backend": "claude", "usage": None,
                    "session_id": proof["session_id"], "ended_utc": until,
                    "attempt_id": receipt["attempt_id"], "models": model_usage(models),
                    'work_observation': observe_work(proof['stdout']['path']),
                    "source": {"terminal": terminal_ref, "stdout": proof["stdout"]},
                    "scope": "CLI cumulative session snapshot; no per-turn or child-role attribution"}
        return _observe(receipt, until, session_root)
    except Exception as exc:
        return {"coverage": "uncovered", "usage": None,
                "reason": f"{type(exc).__name__}: {str(exc)[:240]}", "actual_account_charge": "unknown"}
