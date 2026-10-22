"""Opt-in two-phase installed-runtime maintenance check, without model calls."""
import json
import os
from pathlib import Path
import sys

import pytest


@pytest.mark.skipif(not os.environ.get('FOUNDRY_MAINTENANCE_FIXTURE'), reason='two-phase installation fixture')
def test_same_interpreter_maintenance_preserves_native_contract(monkeypatch):
    from foundry_runtime import foundry_native as native, _launch_context
    monkeypatch.setenv('PDCL_MODEL_CATALOGUE', 'snapshot')
    root = Path(os.environ['FOUNDRY_MAINTENANCE_FIXTURE'])
    root.mkdir(parents=True, exist_ok=True)
    record = root / 'before.json'
    old = json.loads(record.read_bytes()) if record.exists() else None
    current = {'python': sys.executable, 'adapter': native._native_adapter_sha256(),
               'argv': _launch_context.claude_args(launch={
                   'settings': _launch_context.settings(), 'server': None})}
    if old is not None:
        assert current == old
    native.NativeAnalyst(Path(sys.executable), root / 'native', backend='claude',
                         workspace=root, resume=old is not None)
    if old is None:
        record.write_text(json.dumps(current))
