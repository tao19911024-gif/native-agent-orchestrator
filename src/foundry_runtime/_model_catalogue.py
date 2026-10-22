"""The concrete model behind each staffing tier, as its vendor currently names it.

Staffing tables name a tier, never a dated model: Claude fable/opus/sonnet/haiku and Codex
astra/sol/terra/luna. New staffing resolves each tier here, so a vendor upgrade reaches new
work without a code change (operator 2026-09-29: 「以后这些模型要随着厂商升级做同步」).
Recorded goals keep the concrete models they froze; recovery and amendment never re-resolve.

The vendors own the answer. Claude Code resolves its own model aliases and names the resolved
model in the session's init event. Asking costs one minimal request per alias, so the answer is
cached per installed Claude Code executable for at most 24 hours. The Codex
app-server lists its models with the vendor's upgrade pointers: the newest generation of a
tier's name wins, and a vendor upgrade pointer is followed when it names a visible model of the same
tier (a pointer into another tier would change cost and role silently). SNAPSHOT is what both vendors
reported on SNAPSHOT_DATE, with separately dated additions in SNAPSHOT_MODEL_DATES.
It answers whenever a vendor cannot be asked, and whenever
PDCL_MODEL_CATALOGUE=snapshot (tests, offline work). Prices are not synced: a new model stays
unpriced in _foundry_cost until its dated list price is added.

The Codex list is the installed client's: the vendor lists a model only to a CLI that knows it (2026-09-30:
Codex 0.156.0 listed no gpt-6.1-sol, 0.159.2 listed it as its default). So each Codex answer notes when the
npm-installed Codex is behind the registry's latest release. Staffing never installs one: an npm install that
fails midway (measured the same day, on a full disk) leaves no runnable Codex for any dispatch.

The same Codex session also reads the account's usage windows (account/rateLimits/read) for observation.
Quota pacing does not change the provider selected for new staffing.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import urllib.request
from http.client import HTTPException
import uuid
from pathlib import Path
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

SNAPSHOT_DATE = "2026-09-30"
SNAPSHOT_MODEL_DATES = {"claude": {"fable": "2026-10-08", "haiku": "2026-10-10"}}
SNAPSHOT = {
    "claude": {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5",
               "fable": "claude-fable-5-1"},
    "codex": {"astra": "gpt-6-astra", "sol": "gpt-6.1-sol", "terra": "gpt-5.6-terra", "luna": "gpt-6-luna"},
}
BACKEND = {tier: backend for backend, tiers in SNAPSHOT.items() for tier in tiers}
VENDOR_SECONDS = 60
CLAUDE_CACHE_SECONDS = 24 * 60 * 60
CODEX_RELEASE = "https://registry.npmjs.org/@openai/codex/latest"
_resolved: dict = {}
_catalogue_scope = ContextVar("foundry_catalogue_scope", default=None)


def tier_for_model(name):
    """Family classification, not availability or proof of the served model."""
    name = str(name or '').lower().removesuffix('[1m]')
    for tier in BACKEND:
        if name == tier or re.fullmatch(r'(?:claude-|gpt-\d+(?:\.\d+)*-)' + tier + r'(?:-\d+)*', name):
            return tier
    return None


class _ProbeCleanupError(RuntimeError):
    """An unsettled probe must stop discovery, not start another alias probe."""


@contextmanager
def catalogue_scope(cache_path):
    """One invocation's project-selected cache and in-memory vendor observations.

    No account identity is currently supplied by both vendors, so separate
    project caches are retained rather than sharing answers across projects.
    Library callers without a scope use no persistent cache.
    """
    token = _catalogue_scope.set((Path(cache_path), {}))
    try:
        yield
    finally:
        _catalogue_scope.reset(token)


def _answers():
    scope = _catalogue_scope.get()
    return scope[1] if scope is not None else _resolved


def model(tier: str) -> str:
    """The vendor's current model for one tier, resolved once per process and backend."""
    backend = BACKEND[tier]
    answers = _answers()
    if backend not in answers:
        answers[backend] = _resolve(backend)
    return answers[backend]["models"][tier]


def codex_quota() -> dict | None:
    """The Codex account's usage windows, read with its model list; None when the dated answer stood in."""
    model("sol")
    return _answers()["codex"].get("quota")


def resolved() -> dict:
    """Where this process's answers came from, for the printed staffing (the roles name the models)."""
    return {backend: {key: value for key, value in answer.items() if key != "models"}
            for backend, answer in sorted(_answers().items())}


def _resolve(backend: str) -> dict:
    dated = {"date": SNAPSHOT_DATE, **({"model_dates": SNAPSHOT_MODEL_DATES[backend]}
                                     if backend in SNAPSHOT_MODEL_DATES else {})}
    if os.environ.get("PDCL_MODEL_CATALOGUE") == "snapshot":
        return {"source": "snapshot", **dated, "models": dict(SNAPSHOT[backend])}
    try:
        models, notes, *quota = _claude() if backend == "claude" else _codex()
        return {"source": "vendor", **({"notes": notes} if notes else {}),
                **({"quota": quota[0]} if quota and quota[0] else {}), "models": models}
    except Exception as exc:
        # Staffing proceeds on the dated answer; the reason is printed with the staffing.
        return {"source": "snapshot", **dated, "reason": f"{type(exc).__name__}: {exc}",
                "models": dict(SNAPSHOT[backend])}


def codex_models(rows):
    """Pick each Codex tier from one model/list answer; notes say where the dated model was kept."""
    listed = {row["id"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)}
    models, notes = {}, []
    for tier, dated in SNAPSHOT["codex"].items():
        family = {}
        for name, row in listed.items():
            match = re.fullmatch(r"gpt-(\d+(?:\.\d+)*)-" + tier, name)
            if match and not row.get("hidden"):
                family[name] = tuple(int(part) for part in match[1].split("."))
        if not family:
            models[tier] = dated
            notes.append(f"{tier}: no visible gpt-<version>-{tier} listed; kept {dated}")
            continue
        chosen = max(family, key=family.get)
        upgrade = listed[chosen].get("upgrade")
        models[tier] = upgrade if upgrade in family else chosen
    return models, notes


def client_note(installed, released):
    """A note when the installed Codex CLI is older than the released one; None when it is current."""
    def version(text):
        return tuple(int(part) for part in text.split("-")[0].split("."))
    if version(installed) < version(released):
        return (f"Codex CLI {installed} is behind the released {released}; a model the vendor offers only to newer "
                f"clients stays unlisted until `npm install -g @openai/codex@{released}`")
    return None


def _client_note(env):
    """Compare the npm-managed Codex package with the registry's latest release; other installs have none."""
    root = env.get("CODEX_MANAGED_PACKAGE_ROOT")
    if not root:
        return None
    try:
        installed = json.loads((Path(root) / "package.json").read_bytes())["version"]
        with urllib.request.urlopen(CODEX_RELEASE, timeout=15) as answer:
            released = json.load(answer)["version"]
        return client_note(installed, released)
    except (OSError, HTTPException, ValueError, KeyError, TypeError) as exc:
        return f"Codex release check unavailable: {type(exc).__name__}: {exc}"


def _codex() -> dict:
    from ._codex_command import codex_env, resolve_codex
    executable = resolve_codex()
    env = codex_env(executable)
    release = _client_note(env)
    process = _spawn([executable, "app-server"], stdin=subprocess.PIPE, env=env)
    try:
        answers = _json_lines(process.stdout, time.monotonic() + VENDOR_SECONDS)

        def call(number, method, params):
            process.stdin.write(json.dumps({"id": number, "method": method, "params": params}) + "\n")
            for row in answers:
                if row.get("id") == number:
                    if "error" in row:
                        raise RuntimeError(f"{method}: {row['error']}")
                    return row.get("result") or {}
            raise RuntimeError(f"Codex app-server ended before answering {method}")

        call(1, "initialize", {"clientInfo": {"name": "pdcl-model-catalogue", "version": "1"}})
        process.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
        rows, cursor, seen, number = [], None, set(), 2
        while True:
            page = call(number, "model/list", {"includeHidden": True, "limit": 100,
                                               **({"cursor": cursor} if cursor else {})})
            rows.extend(page.get("data", []))
            cursor, number = page.get("nextCursor"), number + 1
            if not cursor:
                break
            if cursor in seen:
                raise RuntimeError("model/list repeated its pagination cursor")
            seen.add(cursor)
        models, notes = codex_models(rows)
        try:
            quota = call(number, "account/rateLimits/read", None).get("rateLimits")
        except (RuntimeError, TimeoutError) as exc:  # the models stand without it; staffing then keeps Codex
            quota, notes = None, [*notes, f"Codex usage windows unread: {exc}"]
        return models, [release, *notes] if release else notes, quota
    finally:
        _stop(process)


def _claude() -> dict:
    from .foundry_native_support import resolve_executable
    executable = Path(resolve_executable("claude"))
    stat = executable.stat()
    identity = {"executable": str(executable), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    scope = _catalogue_scope.get()
    cache = scope[0] if scope is not None else None
    try:
        record = json.loads(cache.read_bytes()).get("claude", {}) if cache else {}
        observed = datetime.fromisoformat(record['observed_utc']).timestamp()
        if (record.get("identity") == identity and 0 <= time.time() - observed < CLAUDE_CACHE_SECONDS
                and set(record.get("models", {})) == set(SNAPSHOT["claude"])):
            return record["models"], record.get("notes", [])
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        pass
    models, notes = {}, []
    for tier, fallback in SNAPSHOT['claude'].items():
        try:
            models[tier] = _claude_alias(executable, tier)
        except _ProbeCleanupError:
            raise
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            # One unavailable family must not revert successful observations of
            # other tiers. Cache the disclosed failure too, avoiding paid probes
            # on every dispatch; retry after expiry or a CLI upgrade.
            models[tier] = fallback
            date = SNAPSHOT_MODEL_DATES.get('claude', {}).get(tier, SNAPSHOT_DATE)
            notes.append(f'{tier}: alias unavailable; kept snapshot {fallback} ({date}); {type(exc).__name__}: {exc}')
    if cache is None:
        return models, notes
    cache.parent.mkdir(parents=True, exist_ok=True)
    partial = cache.with_name(f"{cache.name}.{os.getpid()}.{uuid.uuid4().hex}.partial")
    partial.write_text(json.dumps({"claude": {"identity": identity, "models": models, "notes": notes,
                                              "observed_utc": datetime.fromtimestamp(time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}},
                                  indent=2), encoding="utf-8")
    os.replace(partial, cache)
    return models, notes


def _claude_alias(executable: Path, alias: str) -> str:
    """Claude Code names the model an alias resolves to in its init event, before any answer."""
    from .foundry_claude import environment
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as cwd:
        process = _spawn([str(executable), "-p", "Reply OK.", "--model", alias, "--output-format",
                          "stream-json", "--verbose", "--max-turns", "1", "--tools", ""],
                         stdin=subprocess.DEVNULL, cwd=cwd, env=environment())
        try:
            for row in _json_lines(process.stdout, time.monotonic() + VENDOR_SECONDS):
                if row.get("type") == "system" and row.get("subtype") == "init":
                    name = row.get("model")
                    # Only a plain model id is passed on to later --model arguments and price lookups.
                    if isinstance(name, str) and re.fullmatch(r"claude-[a-z0-9]+(?:-[a-z0-9]+)*", name):
                        if alias == 'fable' and not name.startswith('claude-fable-'):
                            raise RuntimeError(f'Fable alias resolved to another family: {name}')
                        return name
                    raise RuntimeError(f"Claude init event for {alias} names no plain Claude model id: {name!r}")
            raise RuntimeError(f"Claude ended before its init event for {alias}")
        finally:
            _stop(process)


def _spawn(argv, *, stdin, env, cwd=None):
    from ._executor_lifecycle import OwnedProcess
    return OwnedProcess.spawn(argv, cwd=cwd or tempfile.gettempdir(), stdin=stdin, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, env=env, attempt_id="model-catalogue-" + uuid.uuid4().hex)


def _stop(process):
    """End the probe's owned Job; an unverified cleanup replaces its answer with the dated one and the reason."""
    receipt = process.stop_and_verify(drain_seconds=0, kill_seconds=5)
    if not receipt.cleanup_verified:
        raise _ProbeCleanupError("vendor probe cleanup unverified: " + receipt.reason)


def _json_lines(stream, deadline):
    """JSON rows from a child's stdout until it closes or the deadline passes."""
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: ([lines.put(line) for line in stream], lines.put(None)), daemon=True).start()
    while True:
        try:
            line = lines.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            raise TimeoutError("vendor did not answer within %d s" % VENDOR_SECONDS) from None
        if line is None:
            return
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row
