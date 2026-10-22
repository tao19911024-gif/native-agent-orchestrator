"""Task-launch observations owned by the existing intent and native launcher."""
import hashlib
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import time
from copy import deepcopy

from ._execution_intent import ExecutionIntent, IntentConflict
from ._runtime_provenance import observe


def retained_launch(evidence, common_dir, tag, session):
    """Resolve launch policy from this session's existing intent/package owners."""
    from ._native_session import recorded_session
    candidates = []
    for path in Path(evidence).glob('dispatch*.package.json'):
        try:
            package = json.loads(path.read_bytes())
            if package.get('tag') != tag:
                continue
            journal = Path(common_dir) / 'pdcl-dispatch' / ('task-' + tag) / 'intents' / package['intent_id']
            record, _, _ = ExecutionIntent.read_history(journal, task_key=tag)
            log = path.with_name(path.name.removesuffix('.package.json') + '.log')
            if recorded_session(record, log) != session or record.get('attempt_id') != package.get('attempt_id'):
                continue
            context = package.get('launch_context')
            if isinstance(context, dict):
                candidates.append((package.get('written_utc', ''), context))
        except (OSError, ValueError, KeyError, TypeError, IntentConflict):
            continue  # Missing historical observation cannot justify new enforcement.
    return deepcopy(max(candidates, key=lambda row: row[0])[1]) if candidates else None


class DispatchNotStarted(RuntimeError):
    """The still-owned intent proves no native attempt crossed the start boundary."""
    failure_owner = "host"

    def __init__(self, reason, intent):
        super().__init__(reason)
        self.status = "admission_blocked"
        self.evidence_paths = sorted(intent.path.glob("*.json"))
        self.session = intent.frozen.get("session_id")


def read_intent(package, *, project_record=None):
    """Read the journal owner, including packages predating the explicit pointer."""
    if package.get('intent_journal'):
        journal = Path(package['intent_journal'])
    else:
        from ._project_execution import from_record
        from ._foundry_dispatch import _project_adapter
        authority = (from_record(project_record) if project_record is not None
                     else _project_adapter(None).goal_authority())
        journal = authority.common_dir / 'pdcl-dispatch' / ('task-' + package['tag']) / 'intents' / package['intent_id']
    record, _, _ = ExecutionIntent.read_history(journal, task_key=package['tag'])
    if record.get('intent_id') != package['intent_id']:
        raise ValueError('package and intent journal identity differ')
    return journal, record


@contextmanager
def observed_start(intent):
    try:
        yield
    except (Exception, SystemExit) as exc:
        if intent.state == "not_started":
            exc.dispatch_stop = DispatchNotStarted(getattr(exc, "dispatch_reason", str(exc)), intent)
        raise


def record_package(log, tag, operation, intent, body, launch_context=None):
    from ._followup import origin, announce_dispatch
    row = intent.record
    receipt = {
        "tag": tag, "operation": operation, "intent_id": intent.intent_id,
        "attempt_id": row["attempt_id"], "execution_state": row["state"],
        "intent_journal": str(intent.path.resolve()), "originating_session": origin(),
        "root_pid": row["root_pid"],
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "body_bytes": len(body.encode("utf-8")),
        "written_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "runtime_provenance": observe(project_context=intent.frozen.get("project_context")),
        **({"launch_context": launch_context} if launch_context is not None else {}),
    }
    path = log.with_suffix(".package.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    announce_dispatch(path, receipt)
    return path


class PackageLaunch:
    """Write after native-root admission; retain a write failure in the launch log."""
    def __init__(self, intent, log, write):
        self.intent, self.log, self.write = intent, log, write
        self.write_error = None

    def start(self, lease):
        self.intent.start(lease)

    def active(self, lease, session=None):
        self.intent.active(lease, session)
        try:
            self.write()
        except OSError as exc:
            self.write_error = "PACKAGE RECEIPT UNAVAILABLE: " + str(exc)
            print(self.write_error, file=sys.stderr)

    def finish(self):
        # Called after native cleanup, before the intent hashes the completed log.
        if self.write_error:
            try:
                with self.log.open("a", encoding="utf-8") as stream:
                    stream.write("\n" + self.write_error + "\n")
            except OSError as exc:
                print("PACKAGE WARNING COULD NOT BE ADDED TO LOG: " + str(exc), file=sys.stderr)
            finally:
                self.write_error = None


def admitted_binding(common_dir, tag, binding):
    """Package observations cannot change an otherwise identical admitted resume.

    The original task deadline and historical resume rules come from the prior intent. All
    executable inputs, authority, model and policy must still match exactly;
    ExecutionIntent.open rechecks the selected history under its existing lock.
    Current package uncertainty is still recorded in the new resume receipt.
    """
    rules = {'task_deadline_at'}
    if binding.get('operation') == 'resume':
        rules |= {"rules_mode", "rules_layers_sha256", "rules_source_blob"}
    without_rules = lambda value: {k: v for k, v in value.items() if k not in rules}
    matches, deadlines = [], []
    for path in (common_dir / "pdcl-dispatch" / ("task-" + tag) / "intents").glob("*"):
        row, _, _ = ExecutionIntent.read_history(path, task_key=tag)
        if without_rules(row["frozen"]) == without_rules(binding):
            matches.append(row["frozen"])
        session = (row.get('turn_binding') or {}).get('thread_id') or row.get('session_id')
        if session and session == binding.get('session_id') and row['frozen'].get('task_deadline_at') is not None:
            deadlines.append(row['frozen']['task_deadline_at'])
    if len(matches) == 1:
        return matches[0]
    if deadlines:
        if binding.get('task_deadline_at') is not None:
            deadlines.append(binding['task_deadline_at'])
        binding = {**binding, 'task_deadline_at': min(deadlines)}
    return binding
