# Storage retention and cleanup

The release operator owns installed environments. Task owners own their working
changes, recovery records and acceptance evidence. The contributor who creates
test or research output owns its disposition. These owners use existing release
selection, task provenance and evidence records; there is no second cleanup
scheduler or task-state store.

## Maintenance sequence

Use this order for experiment closeout and release maintenance:

1. Start with the creating operation's output list, completed test receipts and
   existing dependency observations. Inspect those exact paths first. Remove
   finished, reproducible test copies and build environments after preserving
   commands, source revisions, results and original failures. A receipt proves
   that its test run ended; resolve any current task or recovery references
   before removing its temporary root.
2. Complete storage disposition in the same operation that created the output.
   Record each created environment or output root when it is created. At
   closeout, record `keep`, `remove` or `archive`, the actual result, and any
   retained dependency's owner and removal condition. Keep these records with
   the existing experiment or release evidence. For new goals, use the existing
   disposable-scratch declaration described below.
3. For authorized release deletion, reuse the complete, previously verified
   invocation: interpreter, project adapter, selection, qualified rollback,
   evidence roots, test receipts and reviewed evidence. Confirm that its inputs
   still describe the current operation. Save the exact invocation and pass all
   obsolete candidates in one prune operation. Use planning separately when
   dependencies or deletion scope still need investigation.
4. Use that operation's scan statistics to locate remaining traversal cost.
   Compare directory counts and timings with the prior result. Do not start
   another archive-wide scan solely to obtain a size breakdown. Investigate the
   largest reported root using its recorded ownership and completion evidence.

Keep the native operation handle until completion and inspect its saved output.
After an error, reconcile the previous outcome and correct the reported input
before retrying. Do not resubmit an unchanged operation while its first instance
is running. If the host denies deletion, record the exact path and denial; do
not retry through another shell, executable or a directory move.

## Keep, remove, or archive

| Material | Disposition |
| --- | --- |
| Selected production environment | Keep at its recorded path |
| One qualified rollback environment | Keep until its replacement is qualified |
| Runtime, native client or evidence used by active tasks | Keep; do not move, truncate or replace |
| Old runtime referenced by unresolved recovery | Determine the exact dependency and recovery owner; retain that environment while resolving it |
| Source, Git history, uncommitted changes, authoritative descriptors | Keep; do not classify as generated output |
| Completed test environment, build tree, pytest scratch or duplicate extracted distribution | Remove when its owner has finished and required evidence is retained |
| Test results, original failures, release hashes and commands | Retain compact primary evidence; do not preserve the entire test environment merely to keep a result |
| Completed research transcripts and large historical evidence | Archive to a suitable volume after checking recovery references and retaining a restore manifest |
| Running task logs, checkpoints, native sessions and databases | Owned by the task; not generic cache cleanup |

Age or a directory name alone does not establish that a task is closed. A live
process is not the only dependency: queued hosted launches, project selection,
recorded interpreter paths and recoverable goals also matter. Conversely, an
old reference in a completed synthetic test is not a reason to retain all
production versions forever. Use the test receipt or reviewed primary evidence
to resolve that exact reference.

## Before creating more output

Check available space and the expected peak footprint for large tests, builds
and copies. Reuse an idle compatible environment, or select another scratch
volume. Keep build products out of Git. A storage observation does not cancel
work, impose a model-call limit, or change a running task's environment.

At experiment completion and release qualification, classify created outputs.
Delete disposable outputs after preserving useful results. A retained exception
must name its owner, dependency and removal condition. Revisit these exceptions
at the next related maintenance or release operation. Do not create recurring
archives containing another copy of an unchanged environment.

## Existing release maintenance command

Use a selected, installed interpreter and explicit paths. For a dependency
investigation, run the planner:

```powershell
& $RuntimePython -m foundry_runtime.release_selection `
  --selection $SelectionFile --retention-plan `
  --rollback-python $RollbackPython
```

The result is an inventory, not deletion approval or proof that every task is
closed. Add `--evidence-root` for task records outside the discovered roots.
Use `--test-run-record` for completed pytest evidence, or `--evidence-review`
for reviewed legacy records, as described by the command's `--help`.

After resolving the reported dependencies and authorizing deletion, reuse the
complete invocation with
`--prune-release <exact-obsolete-release-directory>` instead of
`--retention-plan`. The existing mechanism refreshes its inventory. Do not
replace it with a version-number, modification-time or blanket-directory delete.

For an already authorized cleanup, pass all exact obsolete release candidates
with repeated `--prune-release` arguments in one invocation, along with the
available `--test-run-record` and `--evidence-review` inputs. The operation
discovers the directory tree, then rereads records and their current dependencies
before removal. It also compares the observed directories' modification times
and enumerates changed directories and newly discovered child trees. New goals
and hosted requests under an already known evidence root therefore join the same
dependency assessment. Unchanged archived directories reuse their discovery.
If project registration adds evidence roots during that operation, it scans the
added roots (including new roots nested under an already scanned root) and merges
their records before considering removal. Reordered roots do not
restart discovery; removing a registration does not discard its already
observed dependencies. This discovery state lives only within the operation.
An already observed directory or record removed between refreshes is absent,
rather than unreadable. A recreated directory is discovered on a later refresh.
Permission errors and missing native dependencies of a record that still exists
remain reported gaps.
A separate planning invocation followed by pruning performs two initial full
discoveries; use direct pruning when the deletion scope is already authorized.
A saved planning report is not a fresh dependency observation. Save the command
and structured result outside the source repository. Restore all required
evidence-root options when reusing an earlier command, including explicit
targets of junctions that leave the registered evidence roots.

Version entries registered directly under `releases` may point to relocated
physical installations. Pass the registered entry to `--prune-release`.
Inventory keeps the physical path as its candidate identity and records
`release_aliases`; process arguments, scheduled actions and Python environment
references match both forms. Removal rechecks that the entry still points to
the observed physical version, performs the existing dependency checks, removes
the physical directory and then removes its alias. Both paths must be absent
for a completed deletion. A failure remains recorded and stops the operation.

Discovery reads the entire evidence tree, including nested source and test
copies. Four bounded readers perform directory enumeration, attribute reads and
link resolution; dependency classification stays on the caller thread. A failed
entry remains a reported gap while other entries are still inspected. There are
no directory-name exclusions or depth limits. Warm filesystem cache timings do
not predict the cost of a first scan of a large archive. Keep compact test
receipts and remove completed disposable copies as part of experiment closeout
to avoid repeatedly scanning regenerated trees.

The structured result's `scan.roots` rows report enumerated directories,
discovered records and cumulative reader seconds for each physical evidence
root. Aliases share one row; nested roots receive their own subtree's counts.
Reader seconds sum work across concurrent readers and are not wall-clock time.
During refresh, these rows describe directory enumeration; `scan.records`
includes all known records reread by the operation, and `scan.seconds` also
includes directory-stamp checks and dependency classification.

Completed pytest receipts classify fixture manifests by their resolved path,
so those manifest bodies need not be read. Hosted requests still require content
inspection even inside a fixture tree. Exact operator-reviewed records retain
their hash checks; a test receipt does not bypass an existing exact review.

## Scratch cleanup and archive verification

### Explicit disposable storage for new goals

Add `--disposable-scratch` to a new `--foundry-goal` invocation, or set the
project descriptor or adapter's `default_disposable_scratch` to `true`, to register
`<goal-evidence>/disposable` in that goal's manifest. Registration creates a new
directory; it does not adopt an existing directory. The option applies to the
new goal only. Recovery uses the recorded declaration, and historical goals
without that declaration retain their existing storage behavior.

Use `--no-disposable-scratch` on a new goal to override a project's enabled
default for that invocation. Both explicit storage flags apply only to new
goals; recovery and amendment preserve their recorded policy.

The worker receives the exact directory in its execution instructions. New
registrations create `tmp` and `pytest` children and freeze `TMPDIR`, `TMP` and
`TEMP` to the former and `PYTEST_DEBUG_TEMPROOT` to the latter in the existing
launch context. Codex exec, Codex app-server and Claude coding workers inherit
these values, including native continuation. Standard temporary-file APIs and
pytest's default temporary directory use those children. An explicit pytest
`--basetemp` or a program's explicit output path still selects its stated location.
Reports, original failures, deliverables and recovery inputs belong
outside the disposable directory. Workers move required evidence out before
requesting review or delivering artifacts. No host-wide environment changes
are made. Manager and reviewer environments retain their existing settings.
Historical launch contexts without `worker_environment` preserve their original
environment behavior.

The existing goal owner retains this directory during execution, review,
interruption and recovery. After obtaining `candidate_verified` or
`artifacts_delivered`, it reclaims the registered directory and records the
observed logical bytes and file count in `usage.json` under `scratch`. A named
delivered artifact inside that directory retains the entire directory with its
reason. Filesystem links are removed without traversing their targets. A file
that cannot be removed remains in place with the cleanup error recorded; the
task result keeps its original verdict. The completed outcome is published once
the usage reference is final. An interruption before publication resumes through
the existing review or manager checkpoint without repeating completed work.
If deletion was interrupted before its counters were saved, usage marks scratch
accounting as `partial_after_interruption`; the counters contain only recorded
removals, rather than an estimate of lost observations.

This declaration is per goal directory. Same-goal recovery reuses its recorded
directory. A continuation or amendment in a new directory inherits the storage
policy only when its predecessor has a recorded `worker_environment`; it registers
its own directory and rebinds the environment without requiring a new opt-in flag.
Historical records without that field retain their existing behavior. A new goal
does not inherit authority to delete its predecessor's storage.
The predecessor remains with its recovery owner until that dependency
is resolved through the existing maintenance process. There is no background
cleanup job or search of historical `tmp` directories. Logical bytes count
successfully removed regular files; they do not measure physical free space.

### Manual cleanup and archives

1. Resolve each exact source path and its expected parent. Inspect reparse
   points; do not recurse into a linked external tree as if it were local data.
2. Check the creating operation has ended, process references and relevant
   launch registrations. Preserve retained handles, terminal/cleanup evidence,
   results, source revisions and the original failures before deleting scratch.
3. For archives, select a destination with enough capacity. Record original and
   destination paths, file count, byte count, per-file hashes and restore steps.
   Verify the complete copy before removing its source. If verification fails,
   retain the source and report the partial copy.
4. Do not move records whose absolute paths are still needed by active work.
   Closed historical evidence can be restored to its original layout before a
   later audit or recovery. Keep a small discoverable archive manifest locally.
5. Use native filesystem operations on exact paths. Do not stop unrelated
   processes, alter a database, discard uncommitted work or rewrite task records
   to make cleanup appear safe.
6. Re-measure and record what actually changed. Logical file sizes and volume
   free-space changes can differ because of links, compression and concurrent
   writes; report both without promising reclaimed bytes that were not measured.

Cleanup is normally irreversible for disposable output; regeneration uses the
retained source revision, dependency specification and command. Archive rollback
means verified restoration from the recorded destination. Installation rollback
uses the single retained qualified runtime and the existing selection command.

## Public evidence

Public reports use synthetic paths and task identifiers. Keep raw business logs,
native conversations, credentials, machine inventories and private archive
manifests outside the public repository. Link public CI evidence when available;
do not publish a local disk inventory as a public regression fixture.
