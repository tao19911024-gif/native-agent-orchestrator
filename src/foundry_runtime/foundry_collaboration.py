"""Shared Foundry response contracts and JSON checkpoint serialization.

The former packet-scoped research graph lived here. Foundry agents now inspect
the target repository themselves, so only the contracts used to record native
results and graph progress remain.
"""
from __future__ import annotations

import json

from ._foundry_response_contract import (
    CollaborationBusyError,
    CollaborationError,
    SchemaResponseError,
    BudgetPaused,
    apply_correction,
    response_contract,
    response_contract_for_request,
    validate_response_shape,
)


class _JsonCheckpointSerializer:
    """Serialize LangGraph state as JSON without dynamic class loading."""

    def dumps_typed(self, value):
        from langgraph.types import Send

        def encode(item):
            if item is None or type(item) in (str, int, bool, float):
                return ["scalar", item]
            if type(item) is dict and all(type(key) is str for key in item):
                return ["dict", [[key, encode(val)] for key, val in item.items()]]
            if type(item) in (list, tuple):
                kind = "list" if type(item) is list else "tuple"
                return [kind, [encode(val) for val in item]]
            if type(item) is Send:
                return ["send", item.node, encode(item.arg)]
            raise CollaborationError("checkpoint contains a non-JSON Python object")

        return "json", json.dumps(encode(value), ensure_ascii=False, allow_nan=False).encode("utf-8")

    def loads_typed(self, value):
        from langgraph.types import Send

        if value[0] != "json":
            raise CollaborationError("only JSON checkpoints are supported")

        def decode(item):
            if not isinstance(item, list) or not item:
                raise CollaborationError("invalid checkpoint envelope")
            kind = item[0]
            if kind == "scalar" and len(item) == 2 and (
                    item[1] is None or type(item[1]) in (str, int, bool, float)):
                return item[1]
            if kind in ("list", "tuple") and len(item) == 2 and isinstance(item[1], list):
                result = [decode(val) for val in item[1]]
                return tuple(result) if kind == "tuple" else result
            if kind == "dict" and len(item) == 2 and isinstance(item[1], list):
                result = {}
                for pair in item[1]:
                    if (not isinstance(pair, list) or len(pair) != 2
                            or type(pair[0]) is not str or pair[0] in result):
                        raise CollaborationError("invalid checkpoint dictionary")
                    result[pair[0]] = decode(pair[1])
                return result
            if kind == "send" and len(item) == 3 and item[1] == "analyst":
                return Send(item[1], decode(item[2]))
            raise CollaborationError("unknown checkpoint envelope type")

        return decode(json.loads(
            value[1].decode("utf-8"),
            parse_constant=lambda constant: (_ for _ in ()).throw(
                CollaborationError("nonfinite checkpoint")),
        ))
