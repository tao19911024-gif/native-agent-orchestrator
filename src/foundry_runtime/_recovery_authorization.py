"""Recovery envelope lifecycle; project policy is injected and never serialized."""
from dataclasses import dataclass
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
import copy
import json
import re
from ._executor_lifecycle import _local_ntfs
from ._execution_bindings import _sha


@dataclass(frozen=True)
class RecoveryAuthorization:
    """One operator-approved bridge from an exact unknown intent to one successor."""

    path: Path
    decision_sha256: str
    _data: dict
    _session_root: Path | None
    _task_key: str
    _worktree: Path
    _session_id: str
    _candidate_head: str
    _amendment: bytes | None
    _historical_readback: bool
    source_verifier: Callable[..., None] | None = None

    @classmethod
    def load(cls, path, *, task_key, worktree, session_id, candidate_head,
             amendment: bytes | None, session_root=None, source_verifier=None):
        return cls._load(path, task_key=task_key, worktree=worktree, session_id=session_id,
                         candidate_head=candidate_head, amendment=amendment,
                         session_root=session_root, historical_readback=False, source_verifier=source_verifier)

    @classmethod
    def _load(cls, path, *, task_key, worktree, session_id, candidate_head,
              amendment: bytes | None, session_root=None, historical_readback, source_verifier=None):
        from ._execution_intent import IntentConflict, _reference_bytes
        # Historical references are immutable journal identities. Reading through
        # a relocated archive junction must not replace that identity with its
        # physical target; the bytes and all provenance are still validated.
        path = Path(path).absolute() if historical_readback else Path(path).resolve(strict=True)
        raw = path.read_bytes()
        if len(raw) > 256 * 1024:
            raise IntentConflict("recovery authorization exceeds 256 KiB")
        try:
            value = json.loads(raw)
        except (UnicodeError, ValueError) as exc:
            raise IntentConflict("recovery authorization is not JSON") from exc
        if candidate_head is None:
            # Resolve the declared snapshot for completed readback. This does not
            # establish that the current checkout is eligible for execution.
            candidate_head = (value.get('successor', {}).get('candidate_head')
                              if isinstance(value, dict) and isinstance(value.get('successor'), dict)
                              else None)
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', str(candidate_head)):
            raise IntentConflict('recovery requires a full candidate commit identity')
        recovery = cls(path, _sha(raw), copy.deepcopy(value),
                       Path(session_root) if session_root is not None else None,
                       task_key, Path(worktree), session_id, candidate_head,
                       bytes(amendment) if amendment is not None else None,
                       historical_readback, source_verifier)
        recovery._validate(task_key, worktree, session_id, candidate_head, amendment)
        return recovery

    @classmethod
    def load_historical(cls, path, *, task_key, worktree, session_id, candidate_head, source_verifier=None):
        """Verify a completed recovery from its retained, producer-bound evidence."""
        return cls._load(path, task_key=task_key, worktree=worktree, session_id=session_id,
                         candidate_head=candidate_head, amendment=None,
                         historical_readback=True, source_verifier=source_verifier)

    @property
    def data(self):
        return copy.deepcopy(self._data)

    @property
    def launcher(self):
        return self._data['successor'].get('launcher', 'exec-resume')

    def authority_context(self):
        return {**self.data, "decision_sha256": self.decision_sha256}

    def _validate(self, task_key, worktree, session_id, candidate_head, amendment):
        from ._execution_intent import IntentConflict, _reference_bytes
        value = self._data
        top = {"schema_version", "decision_id", "authorized_by", "authorized_utc",
               "source", "task_key", "predecessor", "successor"}
        if not isinstance(value, dict) or set(value) != top:
            raise IntentConflict("recovery authorization fields are incomplete or unknown")
        try:
            uuid = __import__("uuid")
            if value["schema_version"] not in {"pdcl-execution-recovery-1", "pdcl-execution-recovery-2", "pdcl-execution-recovery-3", "pdcl-execution-recovery-4", "pdcl-execution-recovery-5"}:
                raise ValueError("schema")
            if str(uuid.UUID(value["decision_id"])) != value["decision_id"]:
                raise ValueError("decision identity")
            stamp = datetime.fromisoformat(value["authorized_utc"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or value["authorized_by"] != "operator":
                raise ValueError("authority")
        except (ValueError, TypeError, KeyError) as exc:
            raise IntentConflict("recovery decision identity/authority is invalid") from exc
        from ._recovery_provenance import verify_source
        verify_source(value, source_verifier=self.source_verifier)
        predecessor, successor = value["predecessor"], value["successor"]
        if (not isinstance(predecessor, dict)
                or set(predecessor) != {"intent_id", "attempt_id", "event_sha256s", "cleanup",
                                        "root_pid", "root_creation_time"}
                or not re.fullmatch(r"[0-9a-f]{64}", str(predecessor["intent_id"]))):
            raise IntentConflict("recovery predecessor identity is invalid")
        try:
            if str(uuid.UUID(predecessor["attempt_id"])) != predecessor["attempt_id"]:
                raise ValueError('noncanonical attempt identity')
        except (ValueError, TypeError, AttributeError) as exc:
            raise IntentConflict("recovery predecessor attempt is invalid") from exc
        hashes = predecessor["event_sha256s"]
        if (not isinstance(hashes, dict) or not hashes or len(hashes) > 4096
                or list(hashes) != [f"{n:08d}.json" for n in range(1, len(hashes) + 1)]
                or any(not re.fullmatch(r"[0-9a-f]{64}", str(digest)) for digest in hashes.values())):
            raise IntentConflict("recovery predecessor event hashes are invalid")
        cleanup_raw = _reference_bytes(predecessor["cleanup"], "cleanup", limit=64 * 1024)
        try:
            cleanup = json.loads(cleanup_raw)
        except (UnicodeError, ValueError) as exc:
            raise IntentConflict("recovery cleanup evidence is invalid") from exc
        if (not isinstance(cleanup, dict) or cleanup.get("attempt_id") != predecessor["attempt_id"]
                or cleanup.get("root_pid") != predecessor["root_pid"]
                or cleanup.get("root_creation_time") != predecessor["root_creation_time"]
                or cleanup.get("cleanup_verified") is not True or cleanup.get("root_exited") is not True
                or cleanup.get("job_active_processes") != 0 or predecessor["root_pid"] is None
                or type(predecessor["root_pid"]) is not int
                or type(predecessor["root_creation_time"]) is not int):
            raise IntentConflict("recovery cleanup/root identity is unverified")
        snapshot = value['schema_version'] == 'pdcl-execution-recovery-5'
        launcher = successor.get('launcher') if isinstance(successor, dict) else None
        modern = value['schema_version'] == 'pdcl-execution-recovery-3' or (snapshot and launcher == 'app-server')
        claude = value['schema_version'] == 'pdcl-execution-recovery-4' or (snapshot and launcher == 'claude')
        fields = {"session_id", "session_header", "worktree", "candidate_head", "amendment_sha256"}
        if modern or claude:
            fields.add('launcher')
        if snapshot:
            fields.add('candidate_snapshot')
            candidate = json.loads(_reference_bytes(successor.get('candidate_snapshot'), 'recovery candidate snapshot'))
            if candidate.get('head') != candidate_head:
                raise IntentConflict('recovery candidate snapshot HEAD differs')
        if isinstance(successor, dict) and 'foundry_manifest' in successor:
            fields.add('foundry_manifest')
            _reference_bytes(successor['foundry_manifest'], 'Foundry recovery manifest')
        if (not isinstance(successor, dict) or set(successor) != fields
                or (modern and successor['launcher'] != 'app-server')
                or (claude and successor['launcher'] != 'claude')):
            raise IntentConflict("recovery successor fields are incomplete")
        if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', str(successor['session_id'])):
            raise IntentConflict('recovery successor session identity is invalid')
        expected_tree = str(_local_ntfs(Path(worktree)))
        if (value["task_key"] != task_key or successor["session_id"] != session_id
                or str(_local_ntfs(Path(successor["worktree"]))) != expected_tree
                or successor["candidate_head"] != candidate_head
                or not re.fullmatch(r"[0-9a-f]{64}", str(successor["amendment_sha256"]))
                or (amendment is not None and successor["amendment_sha256"] != _sha(amendment))):
            raise IntentConflict("recovery task/session/worktree/candidate/amendment changed")
        header_raw = _reference_bytes(successor["session_header"], "session header")
        if modern:
            event = json.loads(header_raw)
            binding = event.get('data', {}).get('turn_binding', {})
            if (event.get('kind') != 'turn_bound' or event.get('task_key') != task_key
                    or event.get('intent_id') != predecessor['intent_id']
                    or predecessor['event_sha256s'].get(Path(successor['session_header']['path']).name) != _sha(header_raw)
                    or any(binding.get(k) != predecessor[k] for k in ('attempt_id', 'root_pid', 'root_creation_time'))
                    or not binding.get('turn_id')):
                raise IntentConflict('app-server recovery requires the original bound turn event: '
                    '--session-header must name the predecessor intent directory/0000000N.json '
                    'whose kind=turn_bound and attempt_id/thread_id/turn_id match; not dispatch.log or a turn/started line')
            bound = binding.get('thread_id')
            if not self._historical_readback:
                from ._native_session import read_session_worktree
                tree = read_session_worktree(bound, self._session_root) if self._session_root else read_session_worktree(bound)
                if tree.resolve() != Path(worktree).resolve():
                    raise IntentConflict('app-server recovery thread belongs to another worktree')
        elif claude:
            # Claude's active journal event already owns session/process identity.
            # Retain it, as app-server retains turn_bound; do not invent a Codex header.
            event = json.loads(header_raw)
            active = event.get('data', {})
            if (event.get('kind') != 'active' or event.get('task_key') != task_key
                    or event.get('intent_id') != predecessor['intent_id']
                    or predecessor['event_sha256s'].get(Path(successor['session_header']['path']).name) != _sha(header_raw)
                    or any(active.get(k) != predecessor[k] for k in ('root_pid', 'root_creation_time'))):
                raise IntentConflict('Claude recovery requires the original active session event')
            bound = active.get('session_id')
        elif self._historical_readback:
            from ._native_session import read_retained_session_header
            bound = read_retained_session_header(successor["session_header"]["path"])
        else:
            from ._native_session import read_session_header
            bound = read_session_header(
                successor["session_header"]["path"], worktree, self._session_root)
        if bound != session_id.lower():
            raise IntentConflict("recovery session header identity changed")

    def revalidate(self):
        from ._execution_intent import IntentConflict, _reference_bytes
        try:
            if _sha(self.path.read_bytes()) != self.decision_sha256:
                raise IntentConflict("recovery authorization bytes changed")
        except OSError as exc:
            raise IntentConflict("recovery authorization is unavailable") from exc
        self._validate(self._task_key, self._worktree, self._session_id,
                       self._candidate_head, self._amendment)

    def candidate_matches(self, *, repository_reader=None, env=None):
        """Check live content separately from completed-history readback."""
        from ._repository_observation import snapshot, git
        from ._execution_intent import _reference_bytes
        options = {'repository_reader': repository_reader or git, 'env': env}
        ref = self._data['successor']['candidate_snapshot']
        return snapshot(self._worktree, **options) == json.loads(_reference_bytes(ref, 'recovery candidate snapshot'))

    def require_live_execution(self):
        """Refuse to turn a retained-history view into a live capability."""
        from ._execution_intent import IntentConflict, _reference_bytes
        if self._historical_readback:
            raise IntentConflict('historical readback cannot authorize live execution')

    def require_execution_source(self):
        """Historical permission is readable, but cannot authorize a new consumption."""
        self.require_live_execution()
        self.revalidate()
        from ._recovery_provenance import verify_source
        verify_source(self._data, current=True, source_verifier=self.source_verifier)
