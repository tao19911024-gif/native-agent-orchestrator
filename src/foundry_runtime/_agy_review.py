"""Antigravity structured review transport for Foundry's native analyst."""
import json
import re
from pathlib import Path

from ._foundry_response_contract import SchemaResponseError, validate_response_shape


def diff_command(request):
    packet = request.get("packet") or {}
    endpoints = [packet.get("base_commit"), packet.get("candidate_commit")]
    if all(isinstance(oid, str) and re.fullmatch(r"[a-f0-9]{40,64}", oid) for oid in endpoints):
        return "git diff --no-ext-diff --no-textconv " + " ".join(endpoints)


def review_permissions(request):
    rules = []
    ref = request.get("context_reference") or {}
    if ref.get("path"):
        rules.append("read_file(" + Path(ref["path"]).resolve().as_posix() + ")")
    command = diff_command(request)
    if command:
        rules.append("command(regex:^" + command + "$)")
    return {"deny": ["write_file(*)"], "allow": rules}


def review_argv(executable, schema, model, session, permissions):
    args = [str(executable), "--mode", "plan", "--model", model,
            "--input-format", "stream-json", "--output-format", "stream-json",
            "--json-schema", json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
            "--settings", json.dumps({"permissions": permissions})]
    if session:
        args.extend(["--conversation", session])
    return args


class ReviewProtocol:
    def __init__(self, schema):
        self.schema = schema
        self.init = self.result = self.decoded = self.schema_failure = None

    def accept(self, row):
        from .foundry_native import NativeError
        event = row.get("event")
        if self.result is not None:
            raise NativeError("unexpected AGY event after terminal result")
        if event == "init" and self.init is None:
            session = row.get("conversation_id")
            if isinstance(session, str) and session:
                self.init = {"session_id": session, "model": row["init"].get("model")}
                return
        elif self.init and event in ("result", "step_update"):
            payload = row[event]
            if payload.get("conversation_id") != self.init["session_id"]:
                raise NativeError("AGY reviewer conversation changed")
            if event == "step_update":
                return
            if payload.get("status") != "SUCCESS":
                raise NativeError("AGY review returned " + str(payload.get("status")))
            self.result = payload
            self.decoded = payload.get("structured_output")
            if not isinstance(self.decoded, dict):
                if payload.get("denied_actions"):
                    raise NativeError("AGY review permission denied: " + json.dumps(payload["denied_actions"]))
                raise NativeError("AGY review has no structured response")
            try:
                validate_response_shape(self.decoded, self.schema)
            except SchemaResponseError as error:
                self.schema_failure = error
            return
        raise NativeError("unexpected AGY review event")

    def finish(self):
        from .foundry_native import NativeError
        if self.result is None or self.decoded is None:
            raise NativeError("incomplete AGY review stream")
        return self.decoded, self.init, self.result
