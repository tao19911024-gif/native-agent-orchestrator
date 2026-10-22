"""Repository analysis execution; project admission belongs to the calling entry."""
from pathlib import Path
import json
from ._compute_selection import compute_kwargs
from .foundry_native_support import resolve_executable


def analyze_order(order: Path, evidence_dir: Path, workspace: Path, executable: Path | None = None,
                  *, backend="codex", model=None, effort=None, resume=False) -> dict:
    """Let one native agent inspect the repository and propose the work."""
    from .foundry_collaboration import response_contract
    from .foundry_native import NativeAnalyst
    request = {
        "kind": "solution",
        "packet": {"original_order": order.read_text(encoding="utf-8-sig")},
        "response_contract": response_contract("solution"),
        "call_id": __import__("hashlib").sha256(order.read_bytes()).hexdigest(),
    }
    analyst = NativeAnalyst(executable or resolve_executable(backend), evidence_dir,
                            backend=backend, resume=resume, workspace=workspace,
                            timeout_seconds=900,
                            **compute_kwargs(model, effort, backend=backend))
    try:
        result = analyst(request)
        snapshot = analyst.drain()
    except BaseException as exc:
        analyst.cancel(str(exc))
        analyst.drain()
        raise
    report = {"status": "ready", "mode": "analysis", "result": result,
              "evidence_dir": str(evidence_dir.resolve()), "native": snapshot}
    (evidence_dir / "analysis.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
