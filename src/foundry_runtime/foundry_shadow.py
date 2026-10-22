"""Host-authored shadow advice; it observes an assignment and never routes it."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re


class ShadowError(ValueError):
    pass


class _ShadowInputs:
    """Private immutable capture; mapping views are deliberately reconstructed."""
    __slots__ = ("__captured", "__identity")

    def __setattr__(self, name, value):
        if name in {"_ShadowInputs__captured", "_ShadowInputs__identity"} and hasattr(self, name):
            raise AttributeError("captured shadow inputs are immutable")
        object.__setattr__(self, name, value)

    def __init__(self, captured, identity):
        self.__captured = tuple((name, captured[name]) for name in ("policy", "context", "catalog"))
        self.__identity = tuple((name, identity[name]["path"], identity[name]["sha256"], identity[name]["bytes"])
                                for name in ("policy", "context", "catalog"))

    def identity(self):
        return {name: {"path": path, "sha256": sha256, "bytes": size}
                for name, path, sha256, size in self.__identity}

    def captured(self):
        return dict(self.__captured)

    def derive(self):
        captured, identity = self.captured(), self.identity()
        return (_policy(_json(captured["policy"], identity["policy"]["path"])),
                _context(_json(captured["context"], identity["context"]["path"])),
                _catalog(_json(captured["catalog"], identity["catalog"]["path"])), identity)

    def __getitem__(self, name):
        policy, context, catalog_support, identity = self.derive()
        if name == "policy":
            return policy
        if name == "context":
            return context
        if name == "catalog_support":
            return catalog_support
        if name == "identity":
            return identity
        if name == "captured":
            return self.captured()
        raise KeyError(name)


def _capture(path):
    path = Path(path).resolve()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ShadowError("shadow input is unavailable: " + str(path)) from exc
    return raw, {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _json(raw, path):
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShadowError("invalid shadow input: " + str(path)) from exc


def _pair(value, name):
    if not isinstance(value, dict) or set(value) != {"backend", "model", "effort"}:
        raise ShadowError(name + " must be an explicit backend/model/effort assignment")
    if any(not isinstance(value[key], str) or not value[key].strip() for key in value):
        raise ShadowError(name + " contains an invalid assignment")
    return dict(value)


def _policy(value):
    if not isinstance(value, dict) or value.get("schema_version") != 2:
        raise ShadowError("shadow policy schema_version must be 2")
    if not isinstance(value.get("policy_id"), str) or not value["policy_id"].strip() or not isinstance(value.get("version"), str) or not value["version"].strip():
        raise ShadowError("shadow policy requires policy_id and version")
    tiers, phases = value.get("tiers"), value.get("phases")
    if not isinstance(tiers, dict) or not tiers:
        raise ShadowError("shadow policy requires tiers")
    normalized = {name: _pair(pair, "tier " + str(name)) for name, pair in tiers.items()
                  if isinstance(name, str) and name.strip()}
    if len(normalized) != len(tiers):
        raise ShadowError("shadow policy tier names must be nonempty strings")
    if not isinstance(phases, dict) or not phases:
        raise ShadowError("shadow policy requires phases")
    phase_rules = {}
    for phase, rule in phases.items():
        risk_tiers = rule.get("risk_tiers") if isinstance(rule, dict) and set(rule) == {"risk_tiers"} else None
        if not isinstance(phase, str) or not phase.strip() or not isinstance(risk_tiers, dict) or set(risk_tiers) != {"routine", "elevated", "high"}:
            raise ShadowError("shadow policy phase requires routine, elevated and high risk tiers")
        phase_rules[phase] = {}
        for risk, selection in risk_tiers.items():
            if not isinstance(selection, dict) or set(selection) != {"tier", "reason"} or selection["tier"] not in normalized or not isinstance(selection["reason"], str) or not selection["reason"].strip():
                raise ShadowError("shadow policy risk tier requires a known tier and reason")
            phase_rules[phase][risk] = dict(selection)
    return {**value, "tiers": normalized, "phases": phase_rules}


def _context(value):
    if not isinstance(value, dict) or set(value) != {"schema_version", "phases"} or value["schema_version"] != 2 or not isinstance(value["phases"], dict) or not value["phases"]:
        raise ShadowError("shadow context has an invalid schema")
    phases = {}
    for phase, row in value["phases"].items():
        if not isinstance(phase, str) or not phase.strip() or not isinstance(row, dict) or set(row) != {"risk", "quality", "failure"}:
            raise ShadowError("shadow context phases must be named risk, quality and failure records")
        quality, failure = row["quality"], row["failure"]
        if row["risk"] not in {"routine", "elevated", "high"} or not isinstance(quality, dict) or set(quality) != {"state", "records"} or quality["state"] not in {"missing", "host-asserted", "measured"} or not isinstance(quality["records"], list):
            raise ShadowError("shadow context quality state is invalid")
        if quality["state"] == "missing" and quality["records"]:
            raise ShadowError("missing quality evidence cannot carry records")
        if quality["state"] != "missing" and not quality["records"]:
            raise ShadowError("reported quality evidence requires records")
        for record in quality["records"]:
            if not isinstance(record, dict) or set(record) != {"ref", "sha256"} or not isinstance(record["ref"], str) or not record["ref"].strip() or not isinstance(record["sha256"], str) or not re.fullmatch(r"[0-9a-fA-F]{64}", record["sha256"]):
                raise ShadowError("shadow context quality records require ref and SHA-256")
        if not isinstance(failure, dict) or set(failure) != {"kind", "reason"} or failure["kind"] not in {"none", "schema", "infrastructure", "substantive"} or not isinstance(failure["reason"], str) or not failure["reason"].strip():
            raise ShadowError("shadow context failure state is invalid")
        phases[phase] = {"risk": row["risk"], "quality": {"state": quality["state"], "records": list(quality["records"])}, "failure": dict(failure)}
    return {"schema_version": 2, "phases": phases}


def _catalog(value):
    if not isinstance(value, dict) or value.get("backend") != "codex" or not isinstance(value.get("pages"), list):
        raise ShadowError("shadow catalog must be a Codex catalog with pages")
    support = set()
    for page in value["pages"]:
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise ShadowError("shadow catalog page is invalid")
        for row in page["data"]:
            if not isinstance(row, dict) or not isinstance(row.get("model"), str) or not isinstance(row.get("supportedReasoningEfforts"), list):
                raise ShadowError("shadow catalog model record is invalid")
            for effort in row["supportedReasoningEfforts"]:
                if not isinstance(effort, dict) or not isinstance(effort.get("reasoningEffort"), str):
                    raise ShadowError("shadow catalog effort record is invalid")
                support.add((row["model"], effort["reasoningEffort"]))
    return support


def load_inputs(policy_path, context_path, catalog_path):
    """Capture each input once; advice and persisted evidence share those exact bytes."""
    captured, identity = {}, {}
    for name, path in (("policy", policy_path), ("context", context_path), ("catalog", catalog_path)):
        raw, fact = _capture(path)
        captured[name], identity[name] = raw, fact
    inputs = _ShadowInputs(captured, identity)
    inputs.derive()  # Reject malformed host input before any caller can launch work.
    return inputs


def verify_inputs(identity):
    if not isinstance(identity, dict) or set(identity) != {"policy", "context", "catalog"}:
        raise ShadowError("shadow input identity is invalid")
    actual = {}
    try:
        for name, fact in identity.items():
            if not isinstance(fact, dict) or set(fact) != {"path", "sha256", "bytes"}:
                raise ShadowError("shadow input identity is invalid")
            _, actual[name] = _capture(fact["path"])
    except (KeyError, TypeError) as exc:
        raise ShadowError("shadow input identity is invalid") from exc
    if actual != identity:
        raise ShadowError("shadow input identity changed")


def persist_inputs(inputs, evidence_dir, *, resume):
    """Keep the original policy/context/catalog bytes beside the immutable run evidence."""
    if not isinstance(inputs, _ShadowInputs):
        raise ShadowError("captured shadow inputs are invalid")
    captured = inputs.captured()
    root = Path(evidence_dir) / "shadow-inputs"
    if resume:
        for name, raw in captured.items():
            try:
                if (root / (name + ".json")).read_bytes() != raw:
                    raise ShadowError("frozen shadow input differs from captured input: " + name)
            except OSError as exc:
                raise ShadowError("frozen shadow input is unavailable: " + name) from exc
        return root
    try:
        root.mkdir(parents=True)
        for name, raw in captured.items():
            with (root / (name + ".json")).open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
    except OSError as exc:
        raise ShadowError("cannot persist frozen shadow inputs") from exc
    return root


def _actual(backend, pair):
    return {"backend": backend, "pair": copy.deepcopy(pair), "source": "explicit" if pair else "inherited-default"}


def _abstain(actual, identity, *, reason, context=None, diagnosis=None):
    result = {"status": "abstain", "actual": actual, "recommended": None, "quality_status": "unavailable",
              "reason": reason, "policy": identity}
    if context is not None:
        result["risk"] = context["risk"]
        result["quality_state"] = context["quality"]["state"]
        result["failure_diagnosis"] = diagnosis
    return result


def advise(inputs, *, phase, actual, backend):
    """Return evidence-labelled advice; no returned field is an execution command."""
    if not isinstance(inputs, _ShadowInputs):
        raise ShadowError("shadow inputs are invalid")
    actual = _actual(backend, actual)
    policy, context, catalog_support, identity = inputs.derive()
    if phase not in policy["phases"]:
        return _abstain(actual, identity, reason="policy has no advice for phase")
    if phase not in context["phases"]:
        return _abstain(actual, identity, reason="context has no record for phase")
    state = context["phases"][phase]
    diagnoses = {"none": "none", "schema": "format", "infrastructure": "infrastructure", "substantive": "substantive"}
    diagnosis = diagnoses[state["failure"]["kind"]]
    if backend != "codex":
        return _abstain(actual, identity, reason="no catalog evidence for backend", context=state, diagnosis=diagnosis)
    if state["failure"]["kind"] == "schema":
        return _abstain(actual, identity, reason="schema failure requires format diagnosis", context=state, diagnosis=diagnosis)
    if state["failure"]["kind"] == "infrastructure":
        return _abstain(actual, identity, reason="infrastructure failure requires existing handling", context=state, diagnosis=diagnosis)
    if state["quality"]["state"] == "missing":
        return _abstain(actual, identity, reason="quality evidence is missing", context=state, diagnosis=diagnosis)
    rule = policy["phases"][phase][state["risk"]]
    recommendation = policy["tiers"][rule["tier"]]
    if recommendation["backend"] != backend:
        return _abstain(actual, identity, reason="policy backend differs from explicit backend", context=state, diagnosis=diagnosis)
    if (recommendation["model"], recommendation["effort"]) not in catalog_support:
        return _abstain(actual, identity, reason="catalog does not support recommended pair", context=state, diagnosis=diagnosis)
    quality = "host-reported-measurement-unvalidated" if state["quality"]["state"] == "measured" else "host-assertion-unvalidated"
    return {"status": "advice", "actual": actual, "recommended": copy.deepcopy(recommendation), "tier": rule["tier"],
            "reason": rule["reason"], "risk": state["risk"], "quality_state": state["quality"]["state"],
            "quality_status": quality, "quality_records": copy.deepcopy(state["quality"]["records"]),
            "failure_diagnosis": diagnosis, "failure_reason": state["failure"]["reason"],
            "policy": identity}
