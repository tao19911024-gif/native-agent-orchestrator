"""Observe the selected release and policy owner without changing execution identity."""
import hashlib
from importlib import metadata
from pathlib import Path
import sys

from ._project_context import invocation_adapter


def observe(*, adapter=None, project_context=None):
    """Additive evidence only: missing provenance is a gap, never a launch decision."""
    result = {"schema_version": 1, "distribution": "foundry-runtime",
              "version": None, "python": sys.executable, "environment": sys.prefix,
              "package_root": str(Path(__file__).resolve().parent), "adapter": None,
              "gaps": [], "scope": "runtime observation, not cross-version resume authorization"}
    from ._launch_context import settings
    result["launch_context"] = settings()
    try:
        dist = metadata.distribution("foundry-runtime")
        result["version"] = dist.version
        location = Path(dist.locate_file("foundry_runtime")).resolve()
        result["distribution_package_root"] = str(location)
        if location != Path(__file__).resolve().parent:
            result["gaps"].append("imported package root differs from installed distribution")
        record = dist.read_text("RECORD")
        if record is not None:
            result["distribution_record_sha256"] = hashlib.sha256(record.encode()).hexdigest()
        else:
            result["gaps"].append("installed distribution RECORD unavailable")
    except (metadata.PackageNotFoundError, OSError, ValueError) as exc:
        result["gaps"].append(str(exc))
    if adapter is None and project_context is not None:
        from ._project_execution import ProjectExecution
        kind = ProjectExecution
    else:
        selected = adapter if adapter is not None else invocation_adapter()
        kind = type(selected) if selected is not None else None
    if kind is not None:
        value = {"module": kind.__module__, "class": kind.__qualname__}
        module = sys.modules.get(kind.__module__)
        source = getattr(module, "__file__", None)
        if source:
            try:
                path = Path(source).resolve()
                value.update(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            except (OSError, ValueError) as exc:
                result["gaps"].append("adapter source: " + str(exc))
        else:
            result["gaps"].append("adapter source unavailable")
        result["adapter"] = value
    else:
        result["gaps"].append("selected adapter unavailable")
    return result
