"""Bounded native CLI analyst: frozen input and explicit provider role profiles.

Only OwnedProcess owns process lifetime. The model run deadline excludes the
separately bounded cleanup interval. Native prices are estimates, not a verified
account spending ceiling. This adapter is not an unattended repair launcher.
Graph calls always carry a stable 64-hex call_id for receipt recovery. Direct
ordinal calls without call_id are compatibility-only and cannot be resumed.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
from pathlib import Path
import subprocess
import threading
import time
import uuid

from ._compute_selection import compute_kwargs
from . import _compute_selection as compute_owner
from .foundry_claude import (provider_schema, _claude_argv, NATIVE_COST_THRESHOLD_USD, FORMATTER_ATTEMPTS,
                            bound_formatter_rejection, validate_result_header)
from ._executor_lifecycle import CleanupReceipt, OwnedProcess, SpawnFailure
from .foundry_collaboration import CollaborationError, SchemaResponseError, response_contract, validate_response_shape
from ._foundry_response_contract import message_recipients_for_request, response_contract_for_request
from . import _foundry_native_request as _native_request_owner
from . import _foundry_response_contract as _response_contract_owner
from .foundry_native_support import executable_sha256, native_manifest_policy, native_policy_matches, continuation_error
from .foundry_native_support import accounting_observations
from . import _foundry_native_transport as native_transport
from ._provider_stop import CONTINUABLE, reported_stop


class NativeError(RuntimeError):
    failure_owner = "native"
    def __init__(self, message, *, process=None, evidence=None, analyst=None):
        super().__init__(message)
        self.process, self.evidence = process, evidence
        self.analyst = analyst


class NativeProviderError(NativeError):
    outcome = "provider_failure"

    def __init__(self, message, *, event=None, **kwargs):
        super().__init__(message, **kwargs)
        self.provider_stop = reported_stop(event or {})
        if self.provider_stop:
            self.outcome = self.provider_stop['category']


class NativeBusyError(NativeError):
    """A live duplicate or occupied capacity; no admission or cancellation."""


class NativeSchemaError(NativeError, SchemaResponseError):
    def __init__(self, error, output, *, process=None, evidence=None):
        NativeError.__init__(self, str(error), process=process, evidence=evidence)
        self.details, self.output, self.repairable = error.details, output, error.repairable


def task_usage_from_snapshot(snapshot):
    """Return receipt-derived facts without inventing provider retries or charges."""
    calls = snapshot.get("calls", []) if isinstance(snapshot, dict) else []
    if not isinstance(calls, list):
        calls = []
    non_success = [row.get("call_id") for row in calls
                   if isinstance(row, dict) and row.get("outcome") not in (None, "success")]
    schema_failures = [row.get("call_id") for row in calls
                       if isinstance(row, dict) and row.get("outcome") == "schema_failure"]
    observed = [row.get("call_id") for row in calls
                if isinstance(row, dict) and row.get("provider_usage_observed") is True]
    metrics = {}
    for field in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                  "cache_read_input_tokens", "cache_creation_input_tokens",
                  "output_tokens", "reasoning_output_tokens"):
        values, missing = [], []
        for row in calls:
            usage = row.get("usage") if isinstance(row, dict) else None
            value = usage.get(field) if isinstance(usage, dict) else None
            if (isinstance(row, dict) and row.get("provider_usage_observed") is True
                    and type(value) is int and value >= 0):
                values.append(value)
            else:
                missing.append(row.get("call_id") if isinstance(row, dict) else None)
        metrics[field] = {"observed_sum": sum(values) if values else None,
                          "observed_calls": len(values), "missing_call_ids": missing}
    backends = {row.get("backend") or "unknown" for row in calls if isinstance(row, dict)}
    mixed = len(backends) > 1
    if mixed:
        for metric in metrics.values():
            metric.update(observed_sum=None, aggregation="separate provider counting contracts; see by_backend")
    return {"coverage": "native-receipts" if isinstance(snapshot, dict) and "input_bytes" in snapshot else "uncovered",
            "input_bytes": snapshot.get("input_bytes") if isinstance(snapshot, dict) else None,
            "capture_bytes": snapshot.get("capture_bytes") if isinstance(snapshot, dict) else None,
            "calls": calls,
            "receipt_call_count": len(calls),
            "non_success_call_ids": non_success,
            "schema_failure_call_ids": schema_failures,
            "provider_usage_observed_call_ids": observed,
            "provider_usage_metrics": metrics,
            **({"by_backend": {backend: task_usage_from_snapshot({"calls": [row for row in calls
                if isinstance(row, dict) and (row.get("backend") or "unknown") == backend], "input_bytes": None})
                for backend in sorted(backends)}} if mixed else {}),
            "usage_scope": "this native snapshot only; raw provider counters, not a normalized total; missing is not zero; do not sum successive snapshots or mix backend counting contracts",
            "provider_counting": "receipt usage is preserved per call; native fields do not certify retry attempts or account charge",
            "actual_account_charge": "unknown"}


def _json(raw):
    def pairs(items):
        result = {}
        for name, value in items:
            if name in result:
                raise NativeError(f"duplicate JSON key {name}")
            result[name] = value
        return result

    def constant(value):
        raise NativeError(f"nonfinite JSON number {value}")

    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, TypeError, RecursionError) as exc:
        raise NativeError(f"invalid native JSON: {exc}") from exc


_STABLE_CALL_FOLDER_HEX = 16


def _call_key(call_id):
    return 'call-' + call_id


def _call_folder(root, call_id):
    """Map a full stable identity to its compact physical folder.

    New writes use a 64-bit prefix to preserve Windows path budget. Existing
    full-ID folders remain readable; reservation.json is the identity owner.
    """
    root = Path(root)
    compact = root / ('call-' + call_id[:_STABLE_CALL_FOLDER_HEX])
    legacy = root / ('call-' + call_id)
    for candidate in (compact, legacy):
        reservation = candidate / 'reservation.json'
        if not reservation.exists():
            continue
        try:
            if _json(reservation.read_bytes()).get('call_id') == call_id:
                return candidate
        except (OSError, NativeError):
            return candidate
    if legacy.exists() and not compact.exists():
        return legacy
    return compact


def _check_call_path_budget(folder):
    folder = Path(folder).resolve()
    if os.name == 'nt' and (len(str(folder)) >= 248 or len(str(folder / 'response-schema.json')) >= 260):
        raise NativeError('native evidence path is too long for this Windows host; use a shorter evidence root')


def _event_json(raw, backend):
    if backend == 'codex':
        from .foundry_codex import decode_event
        return decode_event(raw)
    return _json(raw)


class _StreamProtocol:
    """One event validator shared by incremental capture and complete parsing."""

    def __init__(self, expected_schema=None):
        self.schema = expected_schema
        self.init = self.result = self.decoded = None
        self.tool_id = self.tool_input = None
        self.acknowledged = False
        self.schema_failure = None
        self.tool_schema_error = None
        self.formatter_attempts = 0
        self.formatter_ids = set()
        self.retry_allowed = False
        self.tool_events = []

    def accept(self, row):
        if not isinstance(row, dict):
            raise NativeError("native event is not an object")
        kind = row.get('type')
        if (kind not in {'system', 'assistant', 'user', 'result', 'error'}
                or (kind == 'system' and row.get('subtype') != 'init')
                or (kind in {'assistant', 'user'} and row.get('parent_tool_use_id') is not None)):
            # Progress/tool/child extensions are retained in stdout. Only the
            # root session's formatter and terminal result supply a verdict.
            if kind in {'assistant', 'user'}:
                self.tool_events.append(dict(row))
            return
        if kind == 'error':
            raise NativeProviderError('native provider failure: ' + str(row.get('error', row.get('message'))), event=row)
        if self.result is not None:
            raise NativeError("events after result make the native stream ambiguous")
        kind = row.get("type")
        if self.init is None:
            if kind != "system" or row.get("subtype") != "init":
                raise NativeError("native stream must start with one init")
            if (not isinstance(row.get("session_id"), str) or not row["session_id"]
                    or not isinstance(row.get("model"), str) or not row["model"]):
                raise NativeError("native init lacks model/session identity")
            self.init = row
            return
        if row.get("session_id", self.init["session_id"]) != self.init["session_id"]:
            raise NativeError("native stream changed session identity")
        if kind == "system":
            raise NativeError("native stream must contain one init")
        elif kind == "assistant":
            message = row.get("message")
            if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
                raise NativeError("unexpected assistant message identity")
            content = message.get("content")
            if not isinstance(content, list):
                raise NativeError("invalid assistant content")
            for block in content:
                if not isinstance(block, dict):
                    raise NativeError("tool or unexpected assistant content")
                field = block.get("type")
                if field == "tool_use" and self.schema is not None and block.get("name") == "StructuredOutput":
                    if (self.acknowledged or self.formatter_attempts >= FORMATTER_ATTEMPTS
                            or (self.tool_id is not None and not self.retry_allowed)
                            or not isinstance(block.get("id"), str) or not block["id"]
                            or block["id"] in self.formatter_ids
                            or not isinstance(block.get("input"), dict)
                            or set(block) - {"type", "id", "name", "input", "caller"}
                            or block.get("caller", {"type": "direct"}) != {"type": "direct"}):
                        raise NativeError("formatter retry requires one bound schema rejection; "
                                          f"at most {FORMATTER_ATTEMPTS} attempts")
                    self.tool_schema_error = None
                    try:
                        validate_response_shape(block["input"], self.schema)
                    except SchemaResponseError as exc:
                        # The CLI validates formatter input and may issue a
                        # schema-error tool_result before one bounded retry.
                        # Keep the original object; no host-side normalization.
                        self.tool_schema_error = exc
                    self.tool_id, self.tool_input = block["id"], block["input"]
                    self.formatter_ids.add(self.tool_id)
                    self.formatter_attempts += 1
                    self.retry_allowed = False
                    continue
                if field == "tool_use":
                    self.tool_events.append(dict(block))
                    continue
                if field not in ("text", "thinking"):
                    continue
                if not isinstance(block.get(field), str):
                    raise NativeError("invalid assistant text/thinking")
        elif kind == "user" and self.schema is not None:
            message = row.get("message")
            content = message.get('content') if isinstance(message, dict) else None
            # The CLI can batch ordinary tool results or emit a text notice.
            # Only acknowledgements naming our formatter affect the verdict.
            blocks = content if isinstance(content, list) else []
            acknowledgements = [block for block in blocks if isinstance(block, dict)
                and block.get('type') == 'tool_result' and block.get('tool_use_id') in self.formatter_ids]
            for block in blocks:
                if block not in acknowledgements and isinstance(block, dict):
                    self.tool_events.append(dict(block))
            if not acknowledgements:
                return
            if len(acknowledgements) != 1:
                raise NativeError('formatter acknowledgement must be unique')
            block = acknowledgements[0]
            if (self.tool_id is None or self.acknowledged or self.retry_allowed):
                raise NativeError("unexpected native inbound user content")
            if (not isinstance(block, dict)
                    or set(block) - {"type", "tool_use_id", "content", "is_error"}
                    or block.get("type") != "tool_result" or block.get("tool_use_id") != self.tool_id):
                raise NativeError("formatter acknowledgement must match the current formatter call")
            if block.get("is_error") is True:
                content = block.get("content")
                if self.formatter_attempts >= FORMATTER_ATTEMPTS:
                    raise NativeError(f"formatter rejected all {FORMATTER_ATTEMPTS} attempts: {str(content)[:300]}")
                if not bound_formatter_rejection(self.tool_input, self.tool_schema_error, content,
                                                 row.get("tool_use_result")):
                    raise NativeError("formatter retry lacks a bound schema or parse rejection")
                self.retry_allowed = True
                return
            ack = "Structured output provided successfully"
            if (block.get("content") != ack or block.get("is_error", False) is not False
                    or row.get("tool_use_result", ack) != ack):
                raise NativeError("formatter acknowledgement must match the current formatter call")
            if self.tool_schema_error is not None:
                if not self.tool_schema_error.repairable:
                    raise NativeError(f"structured response schema failed: {self.tool_schema_error}")
                # Unsupported provider length bounds keep their existing host
                # repair path, only after a complete successful CLI exchange.
                self.schema_failure = self.tool_schema_error
            self.acknowledged = True
        elif kind == "result":
            validate_result_header(row, NativeError, lambda message: NativeProviderError(message, event=row))
            if self.schema is not None:
                if not self.acknowledged:
                    raise NativeError("formatter result lacks its acknowledgement")
                decoded = row.get("structured_output")
                if not isinstance(decoded, dict) or decoded != self.tool_input:
                    raise NativeError("structured output differs from the formatter input or is missing")
                # Installed CLI emits the same serialized object in result; never
                # let competing free-text JSON substitute for the structured field.
                if row["result"] and _json(row["result"]) != decoded:
                    raise NativeError("native text and structured output disagree")
            else:
                if row.get("structured_output") is not None:
                    raise NativeError("unrequested structured result in legacy text profile")
                decoded = _json(row["result"])
                if not isinstance(decoded, dict):
                    raise NativeError("nested native result is not an object")
            self.decoded, self.result = decoded, {**row, 'accounting_observations': accounting_observations(row, 'claude')}
        else:
            raise NativeError("unexpected native user/peer/tool event")
    def finish(self):
        if self.init is None or self.result is None:
            raise NativeError("native stream lacks a unique init/result")
        return self.decoded, self.init, self.result


def _protocol(schema, backend):
    if backend == "codex":
        from .foundry_codex import CodexStreamProtocol
        return CodexStreamProtocol(schema)
    if backend == "claude":
        return _StreamProtocol(schema)
    raise NativeError("unknown analyst backend")


def _parsed(raw: bytes | Path, max_bytes: int | None, expected_schema=None, backend="claude", *, receipt=None):
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes <= 0):
        raise NativeError("max_bytes must be a positive integer or None")
    if not isinstance(raw, (bytes, Path)):
        raise NativeError("native stream exceeds byte limit or is not bytes")
    size = raw.stat().st_size if isinstance(raw, Path) else len(raw)
    if max_bytes is not None and size > max_bytes:
        raise NativeError("native stream exceeds byte limit or is not bytes")
    import io
    protocol = _protocol(expected_schema, backend)
    parsed_bytes, parsed_hash = 0, hashlib.sha256()
    with (raw.open("rb") if isinstance(raw, Path) else io.BytesIO(raw)) as stream:
        try:
            for line in stream:
                parsed_bytes += len(line)
                parsed_hash.update(line)
                if max_bytes is not None and parsed_bytes > max_bytes:
                    raise NativeError("native stream exceeds byte limit")
                if line.strip():
                    protocol.accept(_event_json(line, backend))
        except UnicodeError as exc:
            raise NativeError("native stream is not UTF-8") from exc
    if receipt is not None and (parsed_bytes != receipt.get("stdout_bytes")
            or parsed_hash.hexdigest() != receipt.get("stdout_sha256")):
        raise NativeError("native recovery parsed stream differs from receipt")
    parsed = protocol.finish()
    if receipt is not None:
        observed = getattr(protocol, "formatter_attempts", None)
        saved = receipt.get("observed_formatter_attempts")
        if type(saved) is not type(observed) or saved != observed:
            raise NativeError("native recovery formatter attempt count differs from captured stream")
        if "tool_events" in receipt and receipt["tool_events"] != getattr(protocol, "tool_events", []):
            raise NativeError("native recovery tool-event record differs from captured stream")
    if protocol.schema_failure:
        raise NativeSchemaError(protocol.schema_failure, parsed[0])
    return parsed


def parse_native_output(raw: bytes, *, max_bytes: int, expected_schema=None, backend="claude") -> dict:
    """Legacy text parsing is compatibility only; production requires a schema."""
    return _parsed(raw, max_bytes, expected_schema, backend)[0]


def _prompt(request, backend="claude", *, max_input_bytes=_native_request_owner.RENDERED_REQUEST_BYTES):
    from ._foundry_native_request import build_prompt
    return build_prompt(request, backend, NativeError, max_input_bytes=max_input_bytes)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _write_json(path, data):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())

def _native_adapter_sha256():
    implementation = hashlib.sha256()
    for path in (Path(__file__), Path(_native_request_owner.__file__), Path(_response_contract_owner.__file__),
                 Path(compute_owner.__file__), Path(native_transport.__file__),
                 *(Path(__file__).with_name(name) for name in ("_launch_context.py", "_code_index.py", "_tool_discipline.py", "_foundry_native_recovery.py", "_provider_stop.py", "_provider_activity.py", "_task_deadline.py")),
                 Path(__file__).with_name("_execution_profile.py"), Path(__file__).with_name("foundry_native_support.py")):
        implementation.update(path.read_bytes())
    return implementation.hexdigest()


from ._foundry_response_contract import allocation_contract as _allocation_contract


class NativeAnalyst:
    max_concurrency = 1  # Public callback capacity; the graph checks before reserving.

    def __init__(self, executable: Path, evidence_dir: Path, *, timeout_seconds=180,
                 max_input_bytes=_native_request_owner.RENDERED_REQUEST_BYTES, max_output_bytes=None, kill_seconds=5, resume=False,
                 max_concurrency=1, max_total_input_bytes=16 * 1024 * 1024,
                 max_total_output_bytes=None, backend="codex", model=None, effort=None,
                  shadow_identity=None, role_compute=None, workspace=None, execution_profiles=None, launch_context=None,
                  recorded_coding_model=None):
        if backend not in ("codex", "claude"):
            raise NativeError("unknown analyst backend")
        self.backend = backend
        self.launch_context = json.loads(json.dumps(launch_context)) if launch_context is not None else None
        self.recorded_coding_model = recorded_coding_model
        self.execution_profiles = json.loads(json.dumps(execution_profiles)) if execution_profiles is not None else None
        self._compute = compute_kwargs(model, effort, backend=backend)
        role_policy = compute_owner.role_assignments(role_compute, backend=backend, coding_backend=None)
        if role_policy is not None and self._compute:
            raise NativeError("role-compute conflicts with the legacy analyst assignment")
        self._role_compute = compute_owner.freeze_roles(role_policy)
        self._role_policy = role_policy
        if shadow_identity is not None:
            try:
                shadow_identity = json.loads(json.dumps(shadow_identity, sort_keys=True, allow_nan=False))
            except (TypeError, ValueError) as exc:
                raise NativeError("shadow identity must be JSON data") from exc
        if type(resume) is not bool:
            raise NativeError("resume must be boolean")
        for name, value, maximum in (("timeout_seconds", timeout_seconds, 1800), ("kill_seconds", kill_seconds, 30)):
            if name == 'timeout_seconds' and value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= maximum:
                raise NativeError(f"{name} must be finite, positive, at most {maximum}")
        if type(max_input_bytes) is not int or not 1 <= max_input_bytes <= 4 * 1024 * 1024:
            raise NativeError("max_input_bytes must be a positive integer at most 4 MiB")
        if type(max_concurrency) is not int or not 1 <= max_concurrency <= 10:
            raise NativeError("max_concurrency must be an integer from 1 to 10")
        if max_total_input_bytes is not None and (type(max_total_input_bytes) is not int
                or not 1 <= max_total_input_bytes <= 512 * 1024 * 1024):
            raise NativeError("max_total_input_bytes must be None or a positive integer at most 512 MiB")
        for name, value in (("max_output_bytes", max_output_bytes),
                            ("max_total_output_bytes", max_total_output_bytes)):
            if value is not None and (type(value) is not int or value <= 0):
                raise NativeError(f"{name} must be a positive integer or None")
        # An explicitly supplied aggregate ceiling also bounds an otherwise
        # unlimited call. Ordinary production calls have neither byte ceiling.
        if max_output_bytes is None and max_total_output_bytes is not None:
            max_output_bytes = max_total_output_bytes
        executable = Path(executable)
        if not executable.is_absolute() or executable.suffix.lower() != ".exe" or not executable.is_file():
            raise NativeError(continuation_error("native analyst requires an existing absolute .exe", evidence_dir) if resume else "native analyst requires an existing absolute .exe")
        self.executable, self.evidence_dir = executable, Path(evidence_dir)
        self.workspace = Path(workspace).resolve() if workspace is not None else None
        if self.workspace is not None and not self.workspace.is_dir():
            raise NativeError("native agent workspace is not a directory")
        self.timeout_seconds, self.kill_seconds = timeout_seconds, kill_seconds
        self.max_input_bytes, self.max_output_bytes = max_input_bytes, max_output_bytes
        self.max_concurrency = max_concurrency
        self.max_total_input_bytes, self.max_total_output_bytes = max_total_input_bytes, max_total_output_bytes
        self.last_evidence = None
        self._calls, self._poisoned, self._call_lock = 0, False, threading.RLock()
        self._contexts, self._cancelled = {}, threading.Event()
        self._cancel_reason = self._cancel_persistence_error = None
        try:
            self.executable_sha256 = executable_sha256(executable)
        except OSError as exc:
            raise NativeError(f"native executable identity unavailable: {exc}") from exc
        self.backend_identity = self._backend_identity()
        policy = native_manifest_policy(
            executable=executable, executable_sha256=self.executable_sha256,
            adapter_sha256=_native_adapter_sha256(),
            timeout_seconds=timeout_seconds, kill_seconds=kill_seconds,
            max_input_bytes=max_input_bytes, max_output_bytes=max_output_bytes,
            max_concurrency=max_concurrency, max_total_input_bytes=max_total_input_bytes,
            max_total_output_bytes=max_total_output_bytes, backend=backend,
            backend_identity=self.backend_identity, requested_compute=self._compute, role_compute=role_policy,
            shadow_identity=shadow_identity, workspace=self.workspace)
        try:
            if resume:
                previous = _json((self.evidence_dir / "native-manifest.json").read_bytes())
                if not native_policy_matches(previous, policy):
                    raise NativeError(continuation_error("native recovery policy changed", self.evidence_dir))
            else:
                self.evidence_dir.mkdir(parents=True, exist_ok=False)
                _write_json(self.evidence_dir / "native-manifest.json", policy)
        except OSError as exc:
            raise NativeError(f"analysis evidence must be a fresh directory: {exc}") from exc
        if resume:
            self._restore_allocations()

    def cancel(self, reason):
        """Close admission atomically; only each call's worker stops its Job."""
        with self._call_lock:
            if not self._cancelled.is_set():
                self._cancel_reason = str(reason)
                self._cancelled.set()
                try:
                    number, _ = self._latest_cancellation()
                    name = 'cancellation.json' if number == 0 else f'cancellation-{number + 1:08d}.json'
                    _write_json(self.evidence_dir / name, {"reason": self._cancel_reason,
                                                                         "observed_utc": _now()})
                except OSError as exc:
                    self._cancel_persistence_error = str(exc)

    def _latest_cancellation(self):
        """Keep stop records append-only when an explicit continuation opens a new turn."""
        first = self.evidence_dir / 'cancellation.json'
        latest = (1, first) if first.exists() else (0, first)
        for path in self.evidence_dir.glob('cancellation-*.json'):
            match = re.fullmatch(r'cancellation-([0-9]+)\.json', path.name)
            if match and int(match[1]) > latest[0]:
                latest = int(match[1]), path
        return latest

    @property
    def unresolved_processes(self):
        with self._call_lock:
            return tuple(row["process"] for row in self._contexts.values()
                         if row["process"] is not None and row["state"] != "settled")

    def evidence_snapshot(self):
        with self._call_lock:
            calls = []
            for key, row in sorted(self._contexts.items()):
                process = row["process"]
                calls.append({"call_id": row["allocation"].get("call_id"), "kind": row["allocation"].get("kind"),
                               "backend": self.backend, "requested_compute": compute_owner.compute_for_kind(
                                   self._role_compute, row["allocation"].get("kind") if row["allocation"].get("kind")
                                   in compute_owner.ROLE_BY_KIND else None, self._compute) or None,
                               **({'cumulative_session': row.get('session_id'), 'ended_utc': row.get('ended_utc')}
                                  if self.backend == 'codex' and row['allocation'].get('persistent_session') else {}),
                               "evidence": str(row["folder"]),
                               # Thread identity is transient controller state, never
                               # receipt evidence. Hiding it after settlement makes a
                               # recovered terminal snapshot comparable byte-for-byte.
                               "state": row["state"],
                               "worker_identity": row.get("worker_identity") if row["state"] != "settled" else None,
                              "root_pid": process.pid if process else row.get("root_pid"),
                              "root_creation_time": process.creation_time if process else row.get("root_creation_time"),
                              "cleanup": row.get("cleanup"), "error": row.get("error"),
                               "input_bytes": row["allocation"]["input_bytes"], "usage": row.get("usage"),
                               "provider_usage_observed": row.get("usage") is not None, "outcome": row.get("outcome"),
                               "accounting_observations": row.get('accounting_observations', []),
                               **({'recovered_verdict': row['recovered_verdict']} if row.get('recovered_verdict') else {}),
                              "capture_bytes": row.get("capture_bytes"),
                              "capture_allowance_bytes": row["allocation"]["capture_allowance_bytes"]})
            return {"cancelled": self._cancelled.is_set(), "cancel_reason": self._cancel_reason,
                    "cancellation_persistence_error": self._cancel_persistence_error,
                    "input_bytes": sum(row["input_bytes"] for row in calls),
                    "capture_bytes": sum(row["capture_bytes"] or 0 for row in calls),
                    "capture_reserved_bytes": (None if any(row["capture_allowance_bytes"] is None
                                                   and row["capture_bytes"] is None for row in calls)
                        else sum(row["capture_allowance_bytes"] for row in calls if row["capture_bytes"] is None)),
                    "max_total_input_bytes": self.max_total_input_bytes,
                    "max_total_output_bytes": self.max_total_output_bytes,
                    "calls": calls, "unresolved_call_ids": [row["call_id"] for row in calls if row["state"] == "unknown"]}

    def drain(self):
        """Wait for admitted workers without racing their process cleanup."""
        deadline = (time.monotonic() + self.timeout_seconds + 2 * self.kill_seconds + 1
                    if self.timeout_seconds is not None else None)
        with self._call_lock:
            contexts = list(self._contexts.values())
        for row in contexts:
            row["done"].wait(max(0, deadline - time.monotonic()) if deadline is not None else None)
        with self._call_lock:
            for row in contexts:
                if not row["done"].is_set():
                    row["state"], row["error"] = "unknown", "native worker did not drain within its bounded lifetime"
                    self.cancel(row["error"])
        return self.evidence_snapshot()

    @classmethod
    def inspect_call(cls, evidence_dir, request, workspace, settings):
        from ._foundry_native_recovery import inspect_call
        return inspect_call(cls, evidence_dir, request, workspace, settings)

    def _inspect_receipt(self, context, *, verify_retry=True, archived=False, record=None):
        """One identity/stream verifier for settlement and restart accounting."""
        folder, allocation = context["folder"], context["allocation"]
        self._validate_allocation(allocation, folder)
        invocation = _json((folder / "invocation.json").read_bytes())
        # Reconciliation validates its unpublished failure record through this
        # same identity/stream authority before publishing it atomically.
        if record is None:
            record = _json((folder / "receipt.json").read_bytes())
        if not isinstance(invocation, dict) or not isinstance(record, dict):
            raise NativeError("native receipt identity records must be objects")
        if any(invocation.get(key) != allocation.get(key) for key in
               ("call_id", "attempt_id", "stdin_sha256", "input_bytes")):
            raise NativeError("native allocation/invocation identity changed")
        if any(record.get(key) != allocation.get(key) for key in ("call_id", "attempt_id", "input_bytes")):
            raise NativeError("native allocation/receipt identity changed")
        schema = _allocation_contract(allocation)
        contract_sha256 = hashlib.sha256(json.dumps(
            schema, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if (invocation.get("schema_version") != 2 or record.get("schema_version") != 2
                or invocation.get("backend") != self.backend or record.get("backend") != self.backend
                or invocation.get("backend_identity") != self.backend_identity
                or (not archived and not native_transport.same_argv(invocation.get("argv"), self._argv(folder, allocation["attempt_id"], schema, allocation["kind"], launch_context=invocation["launch_context"],
                    **({'resume_session': allocation['resume_session']} if 'resume_session' in allocation else {}),
                    **({'persistent': True} if allocation.get('persistent_session') else {}))))
                or invocation.get("response_contract") != schema
                or record.get("response_contract_sha256") != contract_sha256
                or any(invocation.get(key) != value or record.get(key) != value
                       for key, value in compute_owner.call_compute_metadata(self, allocation["kind"]).items())):
            raise NativeError(continuation_error("native backend, command or protocol identity changed", folder))
        if self.backend == "codex":
            from .foundry_codex import codex_schema
            captured_schema = _json((folder / "response-schema.json").read_bytes())
            if not archived and captured_schema != codex_schema(schema):
                raise NativeError("native response schema file changed")
        if archived:
            argv = invocation.get('argv')
            if (not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv)
                    or argv[0] != str(self.executable) or invocation.get('cwd') != str(self.workspace)):
                raise NativeError('archived review invocation does not match its recorded policy')
            if self.backend == 'codex' and '--output-schema' in argv and (
                    argv[argv.index('--output-schema') + 1:argv.index('--output-schema') + 2]
                    != [str((folder / 'response-schema.json').resolve())]):
                raise NativeError('archived review schema reference changed')
        with (folder / "stdin.txt").open("rb") as stream:
            raw_input = stream.read(self.max_input_bytes + 1)
        if (len(raw_input) != allocation["input_bytes"]
                or hashlib.sha256(raw_input).hexdigest() != allocation["stdin_sha256"]):
            raise NativeError("native captured input identity changed")
        cleanup = record.get("cleanup")
        if (not isinstance(cleanup, dict) or cleanup.get("cleanup_verified") is not True
                or cleanup.get("root_exited") is not True or type(cleanup.get("job_active_processes")) is not int
                or cleanup["job_active_processes"] != 0 or cleanup.get("attempt_id") != allocation["attempt_id"]):
            raise NativeError("native cleanup unknown")
        if cleanup.get("root_pid") is not None:
            identity = _json((folder / "process.json").read_bytes())
            if (not isinstance(identity, dict)
                    or any(type(identity.get(key)) is not int or identity[key] <= 0
                           for key in ("root_pid", "root_creation_time"))
                    or any(identity.get(key) != cleanup.get(key) for key in
                           ("attempt_id", "root_pid", "root_creation_time"))):
                raise NativeError("native process receipt identity changed")
        elif (cleanup.get("root_creation_time") is not None or (folder / "process.json").exists()
              or record.get("outcome") not in ("failure", "cancelled")):
            raise NativeError("native no-spawn receipt identity changed")
        streams, total = {}, 0
        for name, filename in (("stdout", "stdout.jsonl"), ("stderr", "stderr.txt")):
            path = folder / filename
            size, digest = 0, hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(64 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
            total += size
            allowance = allocation["capture_allowance_bytes"]
            if ((allowance is not None and total > allowance) or type(record.get(name + "_bytes")) is not int
                    or size != record[name + "_bytes"]
                    or digest.hexdigest() != record.get(name + "_sha256")):
                raise NativeError("native recovery stream evidence changed")
            streams[name] = path
        if verify_retry and 'retry_of' in allocation:
            self._retry_source(allocation['retry_of'], archived=archived)
        return record, streams, total

    def _validate_allocation(self, saved, folder, *, stable_only=False):
        if (not isinstance(saved, dict) or set(saved) - {"resume_session", "retry_of", "review_field_reads", "correction_routing", "persistent_session", "artifact_delivery", "expert_consultation", "operator_input", "member_tier_decisions"} != {"schema_version", "call_id", "attempt_id", "kind",
                                                          "stdin_sha256", "input_bytes", "capture_allowance_bytes",
                                                          "message_recipients"}
                or type(saved.get("schema_version")) is not int or saved["schema_version"] != 2
                or type(saved.get("input_bytes")) is not int or not 0 < saved["input_bytes"] <= self.max_input_bytes
                or (saved.get("capture_allowance_bytes") is not None
                    and type(saved["capture_allowance_bytes"]) is not int)
                or saved["capture_allowance_bytes"] != self.max_output_bytes
                or not isinstance(saved.get("stdin_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", saved["stdin_sha256"])
                or not isinstance(saved.get("attempt_id"), str)
                or not re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", saved["attempt_id"])):
            raise NativeError("unknown native allocation identity")
        if 'resume_session' in saved and ((self.backend == 'codex' and saved['kind'] not in {'coordinate', 'review'})
                or not isinstance(saved['resume_session'], str) or not saved['resume_session']):
            raise NativeError('native continuation session identity is invalid')
        call_id = saved["call_id"]
        if call_id is None and not stable_only:
            valid = re.fullmatch(r"call-[0-9]{4,}", folder.name)
        else:
            valid = (isinstance(call_id, str) and re.fullmatch(r"[0-9a-f]{64}", call_id)
                     and folder.name in {_call_key(call_id), 'call-' + call_id[:_STABLE_CALL_FOLDER_HEX]})
        if not valid:
            raise NativeError("unknown native allocation call identity")
        _allocation_contract(saved)
        if 'correction_routing' in saved and (saved['correction_routing'] is not True or saved['kind'] != 'review'):
            raise NativeError('native review correction routing identity is invalid')
        if 'retry_of' in saved and saved['kind'] != 'coordinate':
            raise NativeError('manager continuation allocation has another role')

    def _retry_source(self, reference, *, archived=False):
        """An explicit manager continuation retains its failed receipt, never replaces it."""
        first, seen = None, set()
        while True:
            if (not isinstance(reference, dict)
                    or set(reference) != {'call_id', 'receipt_sha256', 'cancellation_sha256'}
                    or any(not isinstance(reference[k], str) or not re.fullmatch(r'[0-9a-f]{64}', reference[k])
                           for k in ('call_id', 'receipt_sha256'))
                    or (reference['cancellation_sha256'] is not None and
                        (not isinstance(reference['cancellation_sha256'], str) or
                         not re.fullmatch(r'[0-9a-f]{64}', reference['cancellation_sha256'])))
                    or reference['call_id'] in seen):
                raise NativeError('manager recovery reference is invalid')
            seen.add(reference['call_id'])
            folder = _call_folder(self.evidence_dir, reference['call_id'])
            if hashlib.sha256((folder / 'receipt.json').read_bytes()).hexdigest() != reference['receipt_sha256']:
                raise NativeError('manager recovery predecessor receipt changed')
            saved = _json((folder / 'reservation.json').read_bytes())
            self._validate_allocation(saved, folder, stable_only=True)
            record, streams, _ = self._inspect_receipt({'folder': folder, 'allocation': saved}, verify_retry=False, archived=archived)
            if saved['kind'] != 'coordinate' or record['outcome'] == 'success':
                raise NativeError('manager recovery requires a failed coordinate call')
            if reference['cancellation_sha256'] is None and record['outcome'] not in CONTINUABLE:
                raise NativeError('only a provider stop can continue without cancellation evidence')
            if first is None:
                first = saved, record, streams
            reference = saved.get('retry_of')
            if reference is None:
                return first

    def manager_request(self, request):
        """Select a recorded decision or one new attempt on explicit goal re-entry.

        No loop executes here: failures remain failures until the caller invokes
        the goal again. Success and unknown receipts retain their normal behavior.
        """
        if request.get('kind') != 'coordinate':
            raise NativeError('manager continuation requires coordinate requests')
        request = dict(request)
        while (row := self._contexts.get('call-' + request['call_id'])) is not None:
            if not row['done'].is_set():
                raise NativeBusyError('manager decision is still running', analyst=self)
            if row['allocation']['stdin_sha256'] != hashlib.sha256(
                    _prompt(request, self.backend, max_input_bytes=self.max_input_bytes)).hexdigest():
                raise NativeError('manager continuation request changed')
            record, _, _ = self._inspect_receipt(row)
            if record['outcome'] == 'success':
                return request
            request['retry_of'] = request.pop('call_id')
            request['call_id'] = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False,
                separators=(',', ':')).encode()).hexdigest()
        return request

    def _manager_retry(self, request):
        if 'retry_of' not in request:
            return None
        if request.get('kind') != 'coordinate':
            raise NativeError('only the manager owner may continue an interrupted decision')
        if any(row['state'] != 'settled' or not row['done'].is_set() for row in self._contexts.values()):
            raise NativeError('manager continuation awaits settled native calls')
        previous = request['retry_of']
        if not isinstance(previous, str) or not re.fullmatch(r'[0-9a-f]{64}', previous):
            raise NativeError('manager predecessor call identity is invalid')
        source = _call_folder(self.evidence_dir, previous)
        stop = self._latest_cancellation()[1]
        reference = {'call_id': previous,
            'receipt_sha256': hashlib.sha256((source / 'receipt.json').read_bytes()).hexdigest(),
            'cancellation_sha256': hashlib.sha256(stop.read_bytes()).hexdigest() if stop.exists() else None}
        saved, record, streams = self._retry_source(reference)
        original = {k: v for k, v in request.items() if k != 'retry_of'}
        if saved.get('retry_of'):
            original['retry_of'] = saved['retry_of']['call_id']
        if saved['stdin_sha256'] != hashlib.sha256(_prompt(
                original, self.backend, max_input_bytes=self.max_input_bytes)).hexdigest():
            raise NativeError('manager continuation must retain the interrupted request')
        # Session identity can survive an unfinished result. It does not certify success.
        protocol = _protocol(_allocation_contract(saved), self.backend)
        with streams['stdout'].open('rb') as stream:
            for line in stream:
                if line.strip():
                    if record.get('reconciliation') and not line.endswith(b'\n'):
                        break  # Controller died before even the first session event completed.
                    protocol.accept(_event_json(line, self.backend))
                    break
        observed = protocol.init.get('session_id') if protocol.init else None
        expected = saved.get('resume_session')
        if observed and expected and observed != expected:
            raise NativeError('interrupted manager returned another session')
        session = observed or expected
        # A process can stop before the provider emits a session identifier.
        # The verified cleanup and exact retained request still authorize the
        # explicit recovery attempt; start a fresh native session in that case.
        return reference, session

    def _restore_allocations(self):
        from ._foundry_native_recovery import restore_allocations
        restore_allocations(self)

    def _decode_receipt(self, record, streams, allocation):
        if (record.get("outcome") not in ("success", "schema_failure")
                or type(record.get("returncode")) is not int or record["returncode"] != 0
                or record.get("truncated") is not False or record["cleanup"].get("root_pid") is None):
            raise NativeError("native recovery is unknown or failed; no automatic re-invocation")
        try:
            decoded, init, result = _parsed(streams["stdout"], self.max_output_bytes,
                                            _allocation_contract(allocation),
                                            self.backend, receipt=record)
        except NativeSchemaError as exc:
            if record["outcome"] != "schema_failure" or record.get("schema_error") != exc.details:
                raise NativeError("native recovery schema outcome disagrees") from exc
            raise
        if record["outcome"] != "success":
            raise NativeError("native recovery outcome disagrees with stream")
        if allocation.get('resume_session') and init.get('session_id') != allocation['resume_session']:
            raise NativeError('native continuation returned another session')
        if (record.get("session_id") != init.get("session_id") or record.get("model") != init.get("model")
                or record.get("usage") != result.get("usage")
                or record.get("list_price_usd") != result.get("total_cost_usd")):
            raise NativeError("native recovery metadata differs from captured stream")
        return decoded

    def native_session(self, request):
        """Return an identity only after read-only canonical receipt verification."""
        _, session = self.recover(request, _include_session=True)
        if not isinstance(session, str) or not session:
            raise NativeError("native session identity unavailable")
        return session

    def recover(self, request, *, _include_session=False):
        """Read-only receipt reconciliation: never spawn after an uncertain call."""
        self._verify_executable()
        call_id = request.get("call_id")
        if not isinstance(call_id, str) or not re.fullmatch(r"[0-9a-f]{64}", call_id):
            raise NativeError("native recovery needs a stable host call_id")
        folder = _call_folder(self.evidence_dir, call_id)
        self.last_evidence = folder
        try:
            allocation = _json((folder / "reservation.json").read_bytes())
            if (not isinstance(allocation, dict) or allocation.get("call_id") != call_id
                    or allocation.get("message_recipients") != message_recipients_for_request(request)
                    or allocation.get("stdin_sha256") != hashlib.sha256(_prompt(
                        request, self.backend, max_input_bytes=self.max_input_bytes)).hexdigest()):
                raise NativeError("native recovery request identity changed")
            record, streams, _ = self._inspect_receipt({"folder": folder, "allocation": allocation})
            value = self._decode_receipt(record, streams, allocation)
            return (value, record.get("session_id")) if _include_session else value
        except NativeSchemaError as exc:
            exc.evidence, exc.analyst = folder, self
            raise
        except OSError as exc:
            raise NativeError("native recovery unknown: complete receipt unavailable", evidence=folder, analyst=self) from exc

    def __call__(self, request):
        context = None
        try:
            prompt = _prompt(request, self.backend, max_input_bytes=self.max_input_bytes)
            if len(prompt) > self.max_input_bytes:
                raise NativeError("native input byte limit exceeded")
            call_id = request.get("call_id")
            if call_id is not None and (not isinstance(call_id, str) or not re.fullmatch(r"[0-9a-f]{64}", call_id)):
                raise NativeError("invalid host call_id")
            fingerprint = hashlib.sha256(prompt).hexdigest()
            with self._call_lock:
                key = _call_key(call_id) if call_id else f"call-{self._calls + 1:04d}"
                prior = self._contexts.get(key)
                if prior:
                    if prior["allocation"].get("stdin_sha256") != fingerprint:
                        raise NativeError("native call request identity changed")
                    if not prior["done"].is_set():
                        raise NativeBusyError("native call busy; identical live duplicate refused", analyst=self)
                else:
                    retry = self._manager_retry(request)
                    if (self._cancelled.is_set() and retry is None) or self._poisoned:
                        raise NativeError("native admission cancelled or cleanup unknown: " + str(self._cancel_reason))
                    if sum(not row["done"].is_set() for row in self._contexts.values()) >= self.max_concurrency:
                        raise NativeBusyError("native analyst busy; concurrent capacity occupied", analyst=self)
                    snapshot = self.evidence_snapshot()
                    if (self.max_total_input_bytes is not None
                            and snapshot["input_bytes"] + len(prompt) > self.max_total_input_bytes):
                        raise NativeError("aggregate native input byte limit exhausted")
                    if (self.max_total_output_bytes is not None
                            and snapshot["capture_bytes"] + snapshot["capture_reserved_bytes"] + self.max_output_bytes > self.max_total_output_bytes):
                        raise NativeError("aggregate native capture byte limit exhausted")
                    self._verify_executable()
                    session = retry[1] if retry is not None else self._continuation_session(request)
                    self._calls += 1
                    folder = _call_folder(self.evidence_dir, call_id) if call_id else self.evidence_dir / key
                    _check_call_path_budget(folder)
                    context = {"allocation": {"schema_version": 2, "call_id": call_id, "attempt_id": str(uuid.uuid4()),
                                               "kind": request["kind"], "stdin_sha256": fingerprint,
                                               "input_bytes": len(prompt), "capture_allowance_bytes": self.max_output_bytes,
                                               "message_recipients": message_recipients_for_request(request),
                                               **({'retry_of': retry[0]} if retry is not None else {}),
                                               **({'correction_routing': True}
                                                  if request.get('correction_routing') is True else {}),
                                               **{k: True for k in ('artifact_delivery', 'expert_consultation', 'operator_input', 'review_field_reads') if request.get(k) is True},
                                               **({'member_tier_decisions': request['member_tier_decisions']} if request.get('member_tier_decisions') else {}),
                                               **({'persistent_session': True}
                                                  if request['kind'] == 'review' and self.backend == 'codex' else {}),
                                               **({'resume_session': session} if session else {})},
                               "folder": folder, "state": "reserved", "process": None, "capture_bytes": None,
                               "done": threading.Event(), "worker_identity": threading.get_ident()}
                    self._contexts[key] = context
                    self.last_evidence = folder
                    if folder.exists():
                        raise NativeError("native recovery unknown: unregistered call evidence exists")
                    folder.mkdir(exist_ok=False)
                    _write_json(folder / "reservation.json", context["allocation"])
                    if retry is not None:
                        # The durable successor records exactly which old stop it continues.
                        # Old cancellation and failed-call files stay intact; a later cancel
                        # still stops this new attempt and is not inherited as permission.
                        self._cancelled.clear()
                        self._cancel_reason = None
            if prior:
                return self.recover(request)
            return self._run(request, prompt, context)
        except NativeBusyError:
            raise
        except NativeSchemaError as exc:
            exc.analyst = self
            if context is not None and not exc.repairable:
                self.cancel(str(exc))
            raise
        except BaseException as exc:
            if context is not None and context.get('outcome') not in CONTINUABLE:
                self.cancel(str(exc))
            if isinstance(exc, NativeError):
                exc.analyst = self
                raise
            raise NativeError(str(exc), evidence=context["folder"] if context else None, analyst=self) from exc
        finally:
            if context is not None:
                with self._call_lock:
                    if context["state"] in ("reserved", "running"):
                        context["state"] = "unknown"
                    context["done"].set()

    @staticmethod
    def review_repair_request(request, error, *, source=None):
        """One formatting turn, with the prior answer retained in its conversation."""
        repaired = {**request, 'schema_repair': {'details': error.details, 'output': error.output,
            'instruction': 'Reformat the retained review to satisfy the response schema. Preserve the verdict and findings; do not repeat the investigation.'},
            'continue_from': (request['call_id'] if source is None else
                              {'evidence_dir': str(Path(source).resolve()), 'request': request})}
        repaired['call_id'] = hashlib.sha256(json.dumps(repaired, sort_keys=True,
            ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        return repaired

    def _continuation_session(self, request):
        """A new turn in a verified conversation; recover() only reads an old turn.

        The host supplies the predecessor call ID, not a model-produced session ID.
        The existing allocation owns the new operation and prevents replay on readback.
        The caller keeps manager and reviewer instances separate.
        """
        if 'continue_from' not in request:
            return None
        previous = request['continue_from']
        if isinstance(previous, dict) and request.get('kind') == 'review':
            prior = self.inspect_call(previous['evidence_dir'], previous['request'], self.workspace,
                {'backend': self.backend, **self._compute, 'role_compute': self._role_policy})
            if not prior['schema_error'] or not prior['persistent'] or not prior['session']:
                raise NativeError('review formatting continuation needs its retained failed-format session')
            if request != self.review_repair_request(previous['request'], prior['schema_error'], source=previous['evidence_dir']):
                raise NativeError('review formatting continuation differs from recorded result')
            return prior['session']
        if self.workspace is None or (self.backend == 'codex' and request.get('kind') not in {'coordinate', 'review'}):
            raise NativeError('conversation continuation requires its recorded workspace and supported role')
        if (not isinstance(previous, str) or not re.fullmatch(r'[0-9a-f]{64}', previous)
                or not request.get('call_id') or previous == request['call_id']):
            raise NativeError('continuation needs distinct stable current and predecessor call IDs')
        parent = self._contexts.get('call-' + previous)
        if parent is None:
            raise NativeError('continuation predecessor is not recorded in this native run')
        if not parent['done'].is_set():
            raise NativeBusyError('continuation predecessor is still running', analyst=self)
        record, streams, _ = self._inspect_receipt(parent)
        try:
            self._decode_receipt(record, streams, parent['allocation'])
        except NativeSchemaError as exc:
            if (request.get('kind') != 'review' or not exc.repairable
                    or request.get('schema_repair', {}).get('details') != exc.details
                    or request.get('schema_repair', {}).get('output') != exc.output):
                raise
        session = record.get('session_id')
        if not isinstance(session, str) or not session:
            raise NativeError('continuation predecessor has no native session')
        if any(not row['done'].is_set() and row['allocation'].get('resume_session') == session
               for row in self._contexts.values()):
            raise NativeBusyError('another turn is already using this conversation', analyst=self)
        return session

    def _verify_executable(self):
        if executable_sha256(self.executable) != self.executable_sha256:
            raise NativeError("native executable identity changed; use an explicitly authorized new run")
        if self._backend_identity() != self.backend_identity:
            raise NativeError("native backend configuration identity changed")

    def _metadata(self):
        if self.backend == "codex":
            from .foundry_codex import codex_metadata
            return {**codex_metadata(), "requested_compute": self._compute or None}
        return {"protocol_profile": "claude-repository-agent-v1",
                "capability_profile": "repository-agent", "max_formatter_attempts": None,
                "cli_max_turns_argument": None, "native_cost_threshold_usd": None,
                "usage_metadata_profile": "cli-reported; not-a-model-request-count",
                # Stream-json does not certify effort after native fallback or organization caps.
                "effective_effort": None, "requested_compute": self._compute or None}

    def _backend_identity(self):
        if self.backend == "codex":
            from .foundry_codex import backend_identity
            identity = backend_identity(self.executable, scope_root=self.workspace or self.evidence_dir)
            return native_transport.bind_profile(identity, self.execution_profiles, backend=self.backend, launch_context=self.launch_context)
        from . import foundry_claude
        return native_transport.bind_profile({"metadata": self._metadata(),
                "adapter_sha256": hashlib.sha256(Path(foundry_claude.__file__).read_bytes()).hexdigest()}, self.execution_profiles, backend=self.backend, launch_context=self.launch_context)

    def _argv(self, folder, attempt_id, schema, kind=None, *, resume_session=None, persistent=False, launch_context):
        return native_transport.argv(self, _claude_argv, folder, attempt_id, schema, kind,
                                     resume_session=resume_session, persistent=persistent, launch_context=launch_context)

    def _environment(self, kind=None, launch_context=None):
        return native_transport.environment(self, kind, launch_context)

    def _run(self, request, prompt, context):
        schema = response_contract_for_request(request)
        call_id = request.get("call_id")
        folder = context["folder"]
        attempt_id = context["allocation"]["attempt_id"]
        invocation = native_transport.invocation(self, request, prompt, context, schema, _now())
        argv, working_dir = invocation["argv"], Path(invocation["cwd"])
        metadata = compute_owner.call_compute_metadata(self, request["kind"])
        started = time.monotonic()
        deadline = started + self.timeout_seconds if self.timeout_seconds is not None else None
        from ._task_deadline import remaining
        task_left = remaining()
        if task_left is not None:
            deadline = min(deadline, started + task_left) if deadline is not None else started + task_left
        process = None
        cleanup = None
        readers = []
        outcome, error, returncode = "failure", None, None
        overflow, read_error, protocol_failure = threading.Event(), threading.Event(), threading.Event()
        io_errors = []
        protocol_errors = []
        protocol = _protocol(schema, self.backend)
        byte_lock = threading.Lock()
        stored = 0
        counts = {"stdout": 0, "stderr": 0}
        hashes = {"stdout": hashlib.sha256(), "stderr": hashlib.sha256()}
        decoded = init = native_result = None

        def pump(pipe, name, destination):
            nonlocal stored
            pending = bytearray()
            try:
                with destination.open("xb") as output:
                    while True:
                        # OwnedProcess exposes unbuffered FileIO: one read returns
                        # available pipe bytes, without waiting to fill this size.
                        chunk = pipe.buffer.read(16384)
                        if not chunk:
                            break
                        with byte_lock:
                            keep = chunk if self.max_output_bytes is None else chunk[:max(0, self.max_output_bytes - stored)]
                            stored += len(keep)
                            counts[name] += len(keep)
                            hashes[name].update(keep)
                            if len(keep) != len(chunk):
                                overflow.set()
                        output.write(keep)
                        if overflow.is_set():
                            break
                        if name == "stdout":
                            pending.extend(keep)
                            while b"\n" in pending:
                                line, _, rest = pending.partition(b"\n")
                                pending = bytearray(rest)
                                if line.strip():
                                    protocol.accept(_event_json(line, self.backend))
                    if name == "stdout" and pending.strip() and not overflow.is_set():
                        protocol.accept(_event_json(pending, self.backend))
            except NativeError as exc:
                protocol_errors.append(exc)
                protocol_failure.set()
            except BaseException as exc:
                io_errors.append(str(exc))
                read_error.set()

        try:
            if self.backend == "codex":
                from .foundry_codex import codex_schema
                _write_json(folder / "response-schema.json", codex_schema(schema))
            _write_json(folder / "invocation.json", invocation)
            with (folder / "stdin.txt").open("xb") as stream:
                stream.write(prompt)
                stream.flush()
                os.fsync(stream.fileno())
            with (folder / "stdin.txt").open("rb") as source:
                with self._call_lock:
                    from ._task_deadline import check
                    check()
                    if self._cancelled.is_set():
                        outcome = "cancelled"
                        raise NativeError("native admission cancelled before spawn")
                    try:
                        process = OwnedProcess.spawn(argv, cwd=working_dir, stdin=source,
                                                     cleanup_path=folder / 'receipt.json',
                                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                     attempt_id=attempt_id, env=self._environment(request["kind"], invocation["launch_context"]))
                    except SpawnFailure as exc:
                        context["process"] = exc.process
                        raise
                    context.update(process=process, state="running")
            _write_json(folder / "process.json", {"attempt_id": attempt_id, "root_pid": process.pid,
                                                 "root_creation_time": process.creation_time,
                                                  "cwd": str(working_dir), "observed_utc": _now()})
            for name in ("stdout", "stderr"):
                reader = threading.Thread(target=pump, args=(getattr(process, name), name,
                                          folder / ("stdout.jsonl" if name == "stdout" else "stderr.txt")),
                                          name=f"foundry-{attempt_id}-{name}", daemon=True)
                readers.append(reader)
                reader.start()
            while True:
                if overflow.is_set():
                    outcome = "output_limit"
                    raise NativeError("native output byte limit exceeded")
                if protocol_failure.is_set():
                    outcome = "protocol_failure"
                    raise NativeError("native protocol rejected: " + "; ".join(map(str, protocol_errors)))
                if read_error.is_set():
                    raise NativeError("native output capture failed: " + "; ".join(io_errors))
                if self._cancelled.is_set():
                    outcome = "cancelled"
                    raise NativeError("native call cancelled: " + str(self._cancel_reason))
                returncode = process.poll()
                if returncode is not None and all(not thread.is_alive() for thread in readers):
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    outcome = "timeout"
                    raise NativeError("native model run deadline expired")
                time.sleep(min(0.01, max(0, deadline - time.monotonic())) if deadline is not None else 0.01)
            if returncode != 0:
                outcome = "nonzero_exit"
                raise NativeError(f"native analyst exited {returncode}")
            decoded, init, native_result = protocol.finish()
            if (context['allocation'].get('resume_session')
                    and init.get('session_id') != context['allocation']['resume_session']):
                outcome = 'protocol_failure'
                raise NativeError('native continuation returned another session')
            outcome = "schema_failure" if protocol.schema_failure else "success"
            if protocol.schema_failure:
                error = str(protocol.schema_failure)
        except SpawnFailure as exc:
            process, cleanup, error = exc.process, exc.cleanup, str(exc)
        except Exception as exc:
            error = str(exc)
        finally:
            cleanup_started = time.monotonic()
            if process is not None:
                try:
                    cleanup = process.stop_and_verify(drain_seconds=0, kill_seconds=self.kill_seconds)
                except Exception as exc:
                    cleanup = CleanupReceipt(attempt_id, process.pid, process.creation_time, False, None, False,
                                             "cleanup failed: " + str(exc))
                # Successful cleanup closes owned writers, so pipe pumps reach EOF.
                # Join is bounded even when a broken cleanup leaves IO unresolved.
                for thread in readers:
                    if thread.ident is not None:
                        thread.join(timeout=max(0, self.kill_seconds - (time.monotonic() - cleanup_started)))
                if any(thread.is_alive() for thread in readers):
                    cleanup = CleanupReceipt(attempt_id, process.pid, process.creation_time,
                                             cleanup.root_exited, cleanup.job_active_processes, False,
                                             "native output drain thread did not exit within cleanup interval")
                if not cleanup.cleanup_verified:
                    outcome, self._poisoned = "cleanup_unknown", True
                    self.cancel("native cleanup unknown")
            else:
                cleanup = CleanupReceipt(attempt_id, None, None, True, 0, True, "no process created")
            if overflow.is_set() and outcome != "cleanup_unknown":
                outcome = "output_limit"
            elif protocol_failure.is_set() and outcome in {'failure', 'protocol_failure', 'nonzero_exit'} and not read_error.is_set():
                outcome, error = getattr(protocol_errors[0], "outcome", "protocol_failure"), "; ".join(map(str, protocol_errors))
            elif read_error.is_set() and outcome in {'success', 'failure', 'protocol_failure', 'nonzero_exit'}:
                outcome, error = "failure", "; ".join(io_errors)
            if outcome not in {'success', 'schema_failure', *CONTINUABLE}:
                self.cancel(error or outcome)
            record = {"schema_version": 2, "attempt_id": attempt_id, "outcome": outcome, "error": error,
                      "provider_stop": getattr(protocol_errors[0], 'provider_stop', None) if protocol_errors and outcome in CONTINUABLE else None,
                      "call_id": call_id,
                      "response_contract_sha256": hashlib.sha256(json.dumps(
                          schema, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                      "returncode": returncode, "cleanup": asdict(cleanup), "ended_utc": _now(),
                      "elapsed_seconds": time.monotonic() - started,
                      "run_timeout_seconds": self.timeout_seconds, "cleanup_timeout_seconds": self.kill_seconds,
                      "input_bytes": len(prompt), "stdout_bytes": counts["stdout"], "stderr_bytes": counts["stderr"],
                      "stdout_sha256": hashes["stdout"].hexdigest(), "stderr_sha256": hashes["stderr"].hexdigest(),
                      "truncated": overflow.is_set(), "model": init.get("model") if init else None,
                      "session_id": init.get("session_id") if init else None,
                      "usage": native_result.get("usage") if native_result else None,
                      "accounting_observations": native_result.get('accounting_observations', []) if native_result else [],
                      "list_price_usd": native_result.get("total_cost_usd") if native_result else None,
                      "backend": self.backend, **metadata,
                      "observed_formatter_attempts": getattr(protocol, "formatter_attempts", None),
                      "tool_events": getattr(protocol, "tool_events", []),
                      "schema_error": protocol.schema_failure.details if outcome == "schema_failure" else None,
                      "cost_basis": ("not reported by native Codex JSON; actual account charge unknown"
                                     if self.backend == "codex" else
                                     "native CLI list-price estimate; actual account charge unknown")}
            # Keep observed cleanup in memory even if durable receipt publication
            if outcome == 'provider_limit' and request.get('quota_recovery') == 1 and cleanup.cleanup_verified:
                # Startup identity survives a provider stop, not a successful result.
                if record['session_id'] is None and protocol.init:
                    record['session_id'] = protocol.init.get('session_id')
                from ._provider_activity import native_reset
                from ._task_deadline import current
                due = native_reset(folder / 'stdout.jsonl', self.backend, record['session_id'], self.workspace,
                                   since=time.time() - record['elapsed_seconds'])
                record['provider_stop'] = {**(record.get('provider_stop') or {}),
                                          'task_deadline_at': current(), 'observed_reset_at': due,
                                          'recovery_decision': ('reset_unavailable' if due is None else
                                              'deadline_prevents_retry' if current() is not None and due >= current() else
                                              'reset_not_future' if due <= time.time() else 'scheduled')}
                if due is not None and due > time.time() and (current() is None or due < current()):
                    record['provider_stop']['automatic_resume_at'] = due
            # Keep observed cleanup in memory even if durable receipt publication
            # fails. Unknown persistence never discards the owned custodian.
            with self._call_lock:
                context["cleanup"] = asdict(cleanup)
                context["usage"] = record.get("usage")
                context['accounting_observations'] = record['accounting_observations']
                context['session_id'], context['ended_utc'] = record.get('session_id'), record.get('ended_utc')
                context["outcome"] = outcome
                context["error"] = error
            try:
                # A pre-spawn cancellation or reader-start failure still needs
                # identity-bound empty streams for conservative recovery.
                if cleanup.cleanup_verified and all(not thread.is_alive() for thread in readers):
                    for filename in ("stdout.jsonl", "stderr.txt"):
                        path = folder / filename
                        if not path.exists():
                            with path.open("xb"):
                                pass
                _write_json(folder / "receipt.json", record)
                total = self._inspect_receipt(context)[2] if cleanup.cleanup_verified else None
                with self._call_lock:
                    if cleanup.cleanup_verified:
                        context.update(state="settled", capture_bytes=total)
                    else:
                        context["state"] = "unknown"
            except Exception as exc:
                self.cancel("native evidence write or settlement failed")
                raise NativeError(f"native evidence write failed after cleanup: {exc}",
                                  process=process, evidence=folder, analyst=self) from exc
        if outcome != "success":
            if outcome == "schema_failure":
                raise NativeSchemaError(protocol.schema_failure, decoded, process=process, evidence=folder)
            raise NativeError(f"{outcome}: {error or cleanup.reason}", process=process, evidence=folder)
        return decoded
