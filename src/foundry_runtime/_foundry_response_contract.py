"""One response-contract owner for graph requests, native transports and host admission."""

from __future__ import annotations

import copy
import json
import re


class CollaborationError(ValueError):
    """Invalid evidence, budget exhaustion or unknown result; never admission."""


def failure_message(outcome):
    """Render persisted responsibility; old records do not establish an owner."""
    owner = outcome.get("failure_owner", "unknown")
    labels = {"host": "host policy refused", "native": "native execution failed",
              "model": "model failed", "unknown": "operation failed (owner not recorded)"}
    return labels.get(owner, labels["unknown"]) + "; no automatic retry: " + outcome["error"]


class CollaborationBusyError(CollaborationError):
    """An identical live invocation already owns this call identity."""


class BudgetAdmissionRefused(CollaborationError):
    """Invalid capacity settings refused before graph execution or call recovery."""
    failure_owner = "host"


class BudgetPaused(CollaborationError):
    """Host capacity stopped scheduling; only a verified pause may extend it."""
    failure_owner = "host"


class SchemaResponseError(CollaborationError):
    """Only a completed, trusted response with a shape error may be corrected."""

    def __init__(self, message, *, path="$", expected=None, observed=None, output=None, repairable=False):
        super().__init__(message)
        self.details = {"path": path, "expected": expected, "observed": observed}
        self.output = output
        self.repairable = repairable


_CORRECTION_STEP = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(0|[1-9][0-9]*)\]")


def _correction_steps(path):
    steps, position = [], 1
    if not isinstance(path, str) or not path.startswith("$"):
        raise CollaborationError("correction path is invalid")
    for match in _CORRECTION_STEP.finditer(path, 1):
        if match.start() != position:
            raise CollaborationError("correction path is invalid")
        steps.append(match[1] if match[1] is not None else int(match[2]))
        position = match.end()
    if position != len(path) or not steps or not isinstance(steps[-1], str):
        raise CollaborationError("correction path is invalid")
    return steps


def apply_correction(original, corrected, error):
    """Admit a correction by taking ONLY the named failing values from it.

    A correction used to be refused whole when anything outside the failing path differed. Two
    measured runs lost every completed investigation that way: asked to shorten one message body,
    the model also tightened the summary (evidence/FOUNDRYV2REVIEW/verdict-a-length-refused); asked
    to shorten one task question, it also appended a task (verdict-reviewer-a2). Now the original
    output is kept and only the named values are copied in, so a correction cannot add, drop or
    rewrite anything else by construction. The named value must still sit in the same object: its
    sibling fields must be identical, or the path may name a different element and it is refused.
    """
    expected = error.get("expected") if isinstance(error, dict) else None
    citations = isinstance(expected, dict) and "paths" in expected
    paths = expected["paths"] if citations else [error.get("path") if isinstance(error, dict) else None]
    label = "citation" if citations else "length"
    refused = CollaborationError(f"{label} correction changed content outside the failing text path")
    if not isinstance(paths, list) or not paths:
        raise refused
    result = copy.deepcopy(original)
    for path in paths:
        steps = _correction_steps(path)
        base, candidate, target = original, corrected, result
        for step in steps[:-1]:
            container = dict if isinstance(step, str) else list
            if (not isinstance(base, container) or not isinstance(candidate, container)
                    or (isinstance(step, str) and (step not in base or step not in candidate))
                    or (isinstance(step, int) and not (step < len(base) and step < len(candidate)))):
                raise refused
            base, candidate, target = base[step], candidate[step], target[step]
        field = steps[-1]
        if (not isinstance(base, dict) or not isinstance(candidate, dict) or base.keys() != candidate.keys()
                or field not in base or not isinstance(base[field], str) or not isinstance(candidate[field], str)
                or any(base[key] != candidate[key] for key in base if key != field)):
            raise refused
        if not citations and len(candidate[field]) >= len(base[field]):
            raise refused
        target[field] = candidate[field]
    return result


def _contract(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


_STRING = {"type": "string", "minLength": 1}
_TEXT_BYTE_LIMITS = {"reason": 8192, "summary": 8192, "body": 8192, "question": 4096}


def _bounded_text(field):
    # JSON Schema counts Unicode code points, while host evidence limits use
    # UTF-8 bytes. Four bytes per code point guarantees the host cap for all
    # valid Unicode, deliberately also imposing this bound on ASCII responses.
    return {**_STRING, "maxLength": _TEXT_BYTE_LIMITS[field] // 4}


_SOURCE_REF = _contract({"source_id": _STRING,
                         "location": {"type": "string", "pattern": "^([1-9][0-9]{0,9})(?::([1-9][0-9]{0,9}))?$"}})
_MESSAGE = _contract({"recipient": _STRING,
                      "kind": {"enum": ["question", "evidence", "contradiction", "blocked"]},
                      "body": _bounded_text("body"), "reply_to": {"type": ["string", "null"]},
                      "source_refs": {"type": "array", "items": _SOURCE_REF}})
_TASK = _contract({"name": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_.-]*$", "maxLength": 64},
                   "question": _bounded_text("question"),
                   "source_ids": {"type": "array", "items": _STRING, "minItems": 1, "uniqueItems": True},
                   "depends_on": {"type": "array", "items": _STRING, "uniqueItems": True}})
_PLAN = _contract({"decision": {"anyOf": [
    _contract({"status": {"enum": ["investigate"]},
               "tasks": {"type": "array", "items": _TASK, "minItems": 1}}),
    _contract({"status": {"enum": ["ready", "blocked"]}, "tasks": {"const": []}}),
]}, "reason": _bounded_text("reason"), "messages": {"const": []}})
# WHY THREE, NOT TWO. "complete" and "blocked" once carried three facts between them, because
# an analyst that investigated fully and concluded "the supplied evidence does not settle this"
# had only "blocked" available -- the same word as "I could not investigate at all". The graph
# reads "blocked" as a workflow failure, so one unsettleable question destroyed the terminal
# artifact for every other question in the run. Measured: evidence/GOVREV1/analysis2, nine
# investigations returned, one was unsettleable, and no analysis.json was written at all.
#   complete  -- investigated; the question is answered
#   unsettled -- investigated; the evidence does not settle it. A FINDING, not a failure.
#   blocked   -- could NOT investigate: a source is missing, unreadable or unhonourable.
_ANALYSIS = _contract({"status": {"enum": ["complete", "unsettled", "blocked"]},
                       "summary": _bounded_text("summary"),
                       "messages": {"type": "array", "items": _MESSAGE}})
# THE REVIEW VERDICT VOCABULARY, OWNED HERE AND NOWHERE ELSE. It is deliberately disjoint from
# every workflow status: a verdict says what a finished review CONCLUDED, a status says whether
# the workflow RAN. NOT_SETTLED is a conclusion a completed review is entitled to reach.
_VERDICTS = ("PASS", "BLOCK", "NOT_SETTLED")

# THE SEVERITY VOCABULARY AND ITS BLOCKING PARTITION, OWNED HERE WITH THE VERDICTS.
# Severity is the LEVEL and the verdict is the CONCLUSION, so the mapping between them is ONE
# fact. It used to be stored three times: this enum, a literal ("P0", "P1") inside
# foundry_goal._blocking, and a sentence of prose in the review role prompt. Editing one of the
# three is the defect this ticket keeps producing -- round four added NOT_SETTLED to _VERDICTS
# above with no rule written anywhere, and round five found the prompt still telling the model
# that PASS carries no findings after the check had been changed to allow P2/P3. Same shape both
# times. So the partition is stated once, here, and every other site reads it.
_BLOCKING_SEVERITIES = ("P0", "P1")      # actionable: must be resolved before the candidate proceeds
_RECORDED_SEVERITIES = ("P2", "P3")      # worth recording, does not stop the candidate
_SEVERITIES = (*_BLOCKING_SEVERITIES, *_RECORDED_SEVERITIES)

# SYNTHESIS IS A SEPARATE CONTRACT FROM PLANNING, and the only difference is that it may
# carry a verdict. A planner still cannot emit one -- _PLAN has no such property and
# additionalProperties is False -- so a verdict can only be reached at the end of a run.
# null is the honest value for a synthesis that is not reviewing anything, which is what
# goal-mode research is.
_SYNTHESIS = _contract({"decision": _PLAN["properties"]["decision"],
                        "reason": _bounded_text("reason"), "messages": {"const": []},
                        "verdict": {"type": ["string", "null"], "enum": [*_VERDICTS, None]}})

_PACKAGE_ID = {**_STRING, "pattern": "^[A-Za-z][A-Za-z0-9_.-]*$", "maxLength": 64}
_SOLUTION = _contract({
    "architecture": {**_STRING, "maxLength": 4096},
    "work_packages": {"type": "array", "minItems": 1, "items": _contract({
        "id": _PACKAGE_ID, "objective": {**_STRING, "maxLength": 2048},
        "depends_on": {"type": "array", "items": _PACKAGE_ID, "uniqueItems": True},
        "acceptance": {**_STRING, "maxLength": 2048}})},
    "source_refs": {"type": "array", "items": _SOURCE_REF},
    "unresolved": {"type": "array", "items": {**_STRING, "maxLength": 1024}},
})
_REVIEW = _contract({
    "candidate_commit": {"type": "string", "pattern": "^([0-9a-f]{40}|[0-9a-f]{64})$"},
    "packet_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "verdict": {"enum": list(_VERDICTS)},
    "findings": {"type": "array", "items": _contract({
        "file": {**_STRING, "maxLength": 1024}, "line": {"type": "string", "pattern": "^[1-9][0-9]{0,9}$"},
        "severity": {"enum": list(_SEVERITIES)},
        "summary": {**_STRING, "maxLength": 2048}})},
})

# Coordination chooses a worker turn or independent review, never a PASS verdict.
_COORDINATE = _contract({"action": {"enum": ["delegate", "review"]}, "instruction": _STRING})


def _recipient_values(values) -> list[str]:
    if (not isinstance(values, (list, tuple)) or not values
            or any(not isinstance(value, str) or not value for value in values)
            or len(values) != len(set(values)) or "coordinator" not in values):
        raise CollaborationError("invalid admitted message recipients")
    return sorted(values)


def message_recipients_for_request(request: dict) -> list[str]:
    """Derive the analyst's addresses from the graph-owned request roster."""
    if not isinstance(request, dict):
        raise CollaborationError("model request must be an object")
    if request.get("kind") != "analyse":
        return []
    peers = request.get("peers")
    task_id = request.get("task_id")
    if (not isinstance(peers, list) or any(not isinstance(peer, dict) for peer in peers)
            or any(not isinstance(peer.get("id"), str) or not peer["id"] for peer in peers)):
        raise CollaborationError("analyst request has invalid graph peers")
    identities = [peer["id"] for peer in peers]
    if len(identities) != len(set(identities)) or task_id not in identities:
        raise CollaborationError("analyst request is not admitted by its graph roster")
    return _recipient_values(["coordinator", *identities])


def response_contract(kind: str, message_recipients=None, *, correction_routing=False, artifact_delivery=False, expert_consultation=False, operator_input=False, member_tier_decisions=(), review_field_reads=False) -> dict:
    """Return a fresh role contract, optionally bound to one graph recipient roster."""
    contracts = {"plan": _PLAN, "analyse": _ANALYSIS, "synthesize": _SYNTHESIS,
                 "solution": _SOLUTION, "review": _REVIEW, "coordinate": _COORDINATE}
    if kind not in contracts:
        raise CollaborationError("unknown model request kind")
    contract = copy.deepcopy(contracts[kind])
    if kind == 'review' and review_field_reads:
        contract['properties']['read_fields'] = {'type': 'array', 'items': _contract({
            'pointer': _STRING, 'sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}})}
        contract['required'].append('read_fields')
    if member_tier_decisions:
        if kind != 'coordinate':
            raise CollaborationError('member tier decisions belong to coordination')
        contract['properties']['member_tier_decisions'] = _contract({identity: _contract({
            'action': _STRING, 'reason': _STRING}) for identity in member_tier_decisions})
        contract['required'].append('member_tier_decisions')
    if operator_input:
        if kind != 'coordinate':
            raise CollaborationError('operator input belongs to coordination')
        contract['properties']['action']['enum'].append('await_input')
    if expert_consultation:
        if kind != 'coordinate':
            raise CollaborationError('expert requests belong to coordination')
        contract['properties']['action']['enum'].append('consult_expert')
        contract['properties']['expert_request'] = {'anyOf': [{'type': 'null'}, _contract({
            'trigger': {'enum': ['architecture', 'conflicting_evidence', 'recurring_reasoning']},
            'question': _STRING, 'evidence_refs': {'type': 'array', 'items': _STRING, 'minItems': 1}})]}
        contract['required'].append('expert_request')
    if artifact_delivery:
        if kind != "coordinate":
            raise CollaborationError("artifact delivery belongs to the manager")
        contract["properties"]["action"]["enum"].append("deliver_artifacts")
        contract["properties"]["artifacts"] = {"type": "array", "items": _STRING}
        contract["required"].append("artifacts")
    if kind == "review" and correction_routing:
        contract["properties"]["correction_guidance"] = _contract({
            "cause": {"enum": ["delivery", "implementation", "reasoning", "none"]},
            "model": {"enum": ["retain", "expert"]},
            "reason": _bounded_text("reason")})
        contract["required"].append("correction_guidance")
    if kind == "analyse" and message_recipients is not None:
        contract["properties"]["messages"]["items"]["properties"]["recipient"] = {
            "type": "string", "enum": _recipient_values(message_recipients)}
    elif message_recipients not in (None, [], ()):
        raise CollaborationError("only analyst responses have message recipients")
    return contract


def response_contract_for_request(request: dict) -> dict:
    recipients = message_recipients_for_request(request)
    return response_contract(request.get("kind"), recipients if request.get("kind") == "analyse" else None,
                             review_field_reads=request.get('review_field_reads') is True,
                             correction_routing=request.get("correction_routing") is True,
                             artifact_delivery=request.get("artifact_delivery") is True,
                             expert_consultation=request.get('expert_consultation') is True,
                             operator_input=request.get('operator_input') is True,
                             member_tier_decisions=request.get('member_tier_decisions', ()))


def allocation_contract(allocation):
    """Rebuild the response contract from the call's durable allocation."""
    return response_contract(allocation["kind"], allocation["message_recipients"],
        review_field_reads=allocation.get('review_field_reads') is True,
        correction_routing=allocation.get("correction_routing") is True,
        artifact_delivery=allocation.get("artifact_delivery") is True,
        expert_consultation=allocation.get('expert_consultation') is True,
        operator_input=allocation.get('operator_input') is True,
        member_tier_decisions=allocation.get('member_tier_decisions', ()))


def _check_schema(schema):
    """Check every trusted schema branch, including branches the value never visits."""
    supported = {"type", "properties", "required", "additionalProperties", "enum", "const",
                 "minLength", "maxLength", "pattern", "items", "minItems", "uniqueItems", "anyOf"}
    if not isinstance(schema, dict) or set(schema) - supported:
        raise CollaborationError("unsupported response schema")
    if "anyOf" in schema:
        branches = schema["anyOf"]
        if set(schema) != {"anyOf"} or not isinstance(branches, list) or not branches:
            raise CollaborationError("unsupported response schema union")
        for branch in branches:
            _check_schema(branch)
    for child in schema.get("properties", {}).values():
        _check_schema(child)
    if "items" in schema:
        _check_schema(schema["items"])


def validate_response_shape(value, schema: dict, _path="$") -> None:
    """Validate the shared small subset (standalone anyOf nodes), without grants."""
    _check_schema(schema)
    _validate_shape(value, schema, _path)
    if (isinstance(value, dict) and 'expert_request' in schema.get('properties', {})
            and value.get('action') == 'consult_expert' and value['expert_request'] is None):
        raise SchemaResponseError('consult_expert needs its question', path='$.expert_request',
                                  expected={'type': 'object'}, observed='null')


def _validate_shape(value, schema, _path):
    if "anyOf" in schema:
        failures = []
        for branch in schema["anyOf"]:
            try:
                _validate_shape(value, branch, _path)
            except SchemaResponseError as exc:
                failures.append(exc)
        if len(failures) < len(schema["anyOf"]):
            return
        repairable = [error for error in failures if error.repairable]
        # Preserve a selected branch's text-only correction when all alternatives
        # failed an enum discriminator. The corrected object is validated in full.
        if len(repairable) == 1 and all(error.repairable or "enum" in error.details["expected"]
                                        for error in failures):
            raise repairable[0]
        raise SchemaResponseError("response schema anyOf mismatch", path=_path,
                                  expected={"anyOf": len(schema["anyOf"])}, observed=type(value).__name__)
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    actual = {dict: "object", list: "array", str: "string", type(None): "null"}.get(type(value))
    if kind is not None and actual not in kinds:
        raise SchemaResponseError("response schema type mismatch", path=_path, expected={"type": kind}, observed=type(value).__name__)
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaResponseError("response schema enum mismatch", path=_path, expected={"enum": schema["enum"]}, observed=value)
    if "const" in schema and value != schema["const"]:
        raise SchemaResponseError("response schema const mismatch", path=_path, expected={"const": schema["const"]}, observed=value)
    if kind == "object":
        properties = schema["properties"]
        if set(schema["required"]) - set(value) or (schema["additionalProperties"] is False and set(value) - set(properties)):
            raise SchemaResponseError("response schema required/extra fields", path=_path,
                                      expected={"required": schema["required"], "additionalProperties": False}, observed=sorted(value))
        for key in properties:
            if key in value:
                _validate_shape(value[key], properties[key], f"{_path}.{key}")
    elif kind == "array":
        if len(value) < schema.get("minItems", 0):
            raise SchemaResponseError("response schema array too short", path=_path, expected={"minItems": schema["minItems"]}, observed=len(value))
        if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            raise SchemaResponseError("response schema duplicate items", path=_path, expected={"uniqueItems": True}, observed=len(value))
        for index, item in enumerate(value):
            _validate_shape(item, schema["items"], f"{_path}[{index}]")
    elif isinstance(value, str):
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value)):
            bound = "minLength" if len(value) < schema.get("minLength", 0) else "maxLength"
            raise SchemaResponseError("response schema string length", path=_path, expected={bound: schema[bound]}, observed=len(value),
                                      repairable=bound == "maxLength" and _path.rsplit('.', 1)[-1] in _TEXT_BYTE_LIMITS)
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise SchemaResponseError("response schema string pattern", path=_path, expected={"pattern": schema["pattern"]}, observed=value)
