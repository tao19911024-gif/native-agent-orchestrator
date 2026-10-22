"""Serializable project identity; adapters and readers are reconstructed, not serialized."""
from dataclasses import asdict, dataclass
from contextlib import contextmanager
from contextvars import ContextVar
import json


@dataclass(frozen=True)
class ProjectContext:
    project_id: str
    descriptor: str
    descriptor_sha256: str
    git_common_dir: str
    workspace: str
    evidence_root: str
    rule_sources: tuple[str, ...]
    history_roots: tuple[str, ...]
    execution_profiles_json: str
    adapter: str = "descriptor-v1"
    repository_reader: str = "native-git-v1"

    def record(self):
        result = asdict(self)
        result["rule_sources"] = list(self.rule_sources)
        result["history_roots"] = list(self.history_roots)
        result["execution_profiles"] = json.loads(result.pop("execution_profiles_json"))
        return result

    @classmethod
    def descriptor_context(cls, selected):
        p = selected.project
        absolute = lambda name: str((selected.workspace / name).resolve())
        return cls(p["project_id"], str(selected.path), selected.sha256,
                   str(selected.common_dir), str(selected.workspace), p["evidence_root"],
                   tuple(absolute(name) for name in p["rule_sources"]),
                   tuple(dict.fromkeys([p["evidence_root"], *[absolute(name) for name in p.get("history_roots", [])]])),
                   selected.execution_profiles_json)


_invocation_adapter = ContextVar("foundry_invocation_adapter", default=None)


@contextmanager
def adapter_scope(adapter):
    """Bind the selected host policy for this invocation, never to retained JSON."""
    token = _invocation_adapter.set(adapter)
    try:
        yield
    finally:
        _invocation_adapter.reset(token)


def invocation_adapter():
    return _invocation_adapter.get()
