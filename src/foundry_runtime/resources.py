"""Live receipt schema owner, independent of project or checkout paths."""
from importlib.resources import files
import json


def result_receipt_schema():
    return json.loads(files("foundry_runtime").joinpath("resultreceipt.schema.json").read_text(encoding="utf-8"))
