"""Canonical serialized bindings and identity for execution intents."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from ._executor_lifecycle import _local_ntfs
from ._app_server_policy import normalize_policy


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _identity(task_key, frozen, *, generation=None):
    if generation is None:
        generation = 2 if frozen.get('launcher') == 'app-server' else 1
    return _sha(_bytes({"schema_version": generation, "task_key": task_key, "frozen": frozen}))


def _same_invocation(left, right):
    # HEAD, registry, policy and preamble describe an admission snapshot, not
    # fresh instructions. A worker's commit must not renew its own authorization.
    fields = ("operation", "repository_id", "worktree", "session_id", "order_sha256")
    return (all(left[key] == right[key] for key in fields)
            and left.get("continuation_sha256") == right.get("continuation_sha256"))


def _frozen(record):
    if not isinstance(record, dict):
        raise ValueError("execution bindings must be a JSON object")
    value = json.loads(_bytes(record))
    if len(_bytes(value)) > 256 * 1024:
        raise ValueError("execution bindings exceed 256 KiB")
    fields = {"operation", "repository_id", "authority_sha256", "order_sha256",
              "base_commit", "worktree", "authorization_sha256", "session_id"}
    legacy_source = {"preamble_sha256", "source_packet_sha256"} <= value.keys()
    if fields - value.keys() or ("task_sha256" not in value and not legacy_source):
        raise ValueError("execution bindings lack required authority/source identities")
    if value["operation"] not in {"dispatch", "resume"}:
        raise ValueError("unknown execution operation")
    for key in fields:
        if key.endswith("sha256") and not re.fullmatch(r"[0-9a-f]{64}", str(value[key])):
            raise ValueError("invalid digest: " + key)
    if "continuation_sha256" in value and not re.fullmatch(r"[0-9a-f]{64}", str(value["continuation_sha256"])):
        raise ValueError("invalid digest: continuation_sha256")
    if not isinstance(value["repository_id"], str) or not value["repository_id"]:
        raise ValueError("repository identity is required")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(value["base_commit"])):
        raise ValueError("full base commit identity is required")
    if value["session_id"] is not None and (not isinstance(value["session_id"], str) or not value["session_id"]):
        raise ValueError("invalid session identity")
    if value["operation"] == "resume" and value["session_id"] is None:
        raise ValueError("resume requires the original session identity")
    value["worktree"] = str(_local_ntfs(Path(value["worktree"])))
    if 'task_deadline_at' in value:
        from ._task_deadline import normalize
        value['task_deadline_at'] = normalize(value['task_deadline_at'])
    if 'app_server_policy' in value:
        if value.get('launcher') != 'app-server':
            raise ValueError('app-server policy requires the app-server launcher')
        value['app_server_policy'] = normalize_policy(value['app_server_policy'])
    return value
