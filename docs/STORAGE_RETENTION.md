# Storage retention and cleanup

The release operator owns installed environments. Task owners own their working
changes, recovery records and acceptance evidence. The contributor who creates
test or research output owns its disposition. These owners use existing release
selection, task provenance and evidence records; there is no second cleanup
scheduler or task-state store.

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

Use a selected, installed interpreter and explicit paths:

```powershell
& $RuntimePython -m foundry_runtime.release_selection `
  --selection $SelectionFile --retention-plan `
  --rollback-python $RollbackPython
```

The result is an inventory, not deletion approval or proof that every task is
closed. Add `--evidence-root` for task records outside the discovered roots.
Use `--test-run-record` for completed pytest evidence, or `--evidence-review`
for reviewed legacy records, as described by the command's `--help`.

After resolving the reported dependencies, reuse the same command with
`--prune-release <exact-obsolete-release-directory>` instead of
`--retention-plan`. The existing mechanism refreshes its inventory. Do not
replace it with a version-number, modification-time or blanket-directory delete.

For an already authorized cleanup, pass all exact obsolete release candidates
with repeated `--prune-release` arguments in one invocation, along with the
available `--test-run-record` and `--evidence-review` inputs. The operation
discovers records once, then rereads them and their current dependencies before
removal. A separate planning invocation followed by pruning performs two full
discoveries; a saved planning report is not a fresh dependency observation.

Discovery reads the entire evidence tree, including nested source and test
copies. Four bounded readers perform directory enumeration, attribute reads and
link resolution; dependency classification stays on the caller thread. A failed
entry remains a reported gap while other entries are still inspected. There are
no directory-name exclusions or depth limits. Warm filesystem cache timings do
not predict the cost of a first scan of a large archive. Keep compact test
receipts and remove completed disposable copies as part of experiment closeout
to avoid repeatedly scanning regenerated trees.

## Scratch cleanup and archive verification

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
