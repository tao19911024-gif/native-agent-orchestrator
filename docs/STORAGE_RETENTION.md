# Storage retention

Runtime installations are versioned directories. Keep the selected runtime,
one qualified rollback and versions explicitly required by current execution or
retained recovery work. Preserve source, release distributions and task evidence
separately from disposable installations.

## Release rotation

Use [the release-rotation skill](../skills/foundry-release-rotation/SKILL.md) for
publishing, adopting and cleaning runtime versions.

1. Build and verify a new version following [RELEASING.md](RELEASING.md).
2. After acceptance, save the previous selection and select the new runtime.
   The previously selected qualified version becomes the rollback version.
3. Resolve current task holds from existing project ownership records. Follow
   the task's current execution or active round; an old root provenance record
   alone does not retain an installation after the execution has been upgraded.
   Pass required version directories with `--keep-release`.
4. Check live processes and Foundry scheduled registrations. Retire completed,
   nonrunning registrations through the existing hosting owner after matching
   their request and completion identities. Keep unfinished registrations.
5. Remove installed directories outside the current, rollback and task holds.
   Run one rotation operation and retain its handle. The total rotation budget
   is ten minutes; on expiry stop and report completed and remaining work.
6. Save removed paths, bytes, timings and retained reasons with the private
   release record. Close completed build/test output in the same release.

Publication and default selection remain separate. A candidate installation
without a current task owner does not receive an additional permanent slot.
Published assets and immutable source tags remain available after its disposable
installation is removed.

## Commands

The existing entry point manages installation directories:

```powershell
$TaskHolds = @('--keep-release', 'C:/AgentRuntime/releases/0.1.0a99')
& $RuntimePython -m foundry_runtime.release_selection `
  --selection $SelectionFile --rollback-python $RollbackPython `
  --retention-plan @TaskHolds

& $RuntimePython -m foundry_runtime.release_selection `
  --selection $SelectionFile --rollback-python $RollbackPython `
  --prune-unused @TaskHolds
```

Use an empty `$TaskHolds` array when no additional version is required. The plan
and deletion inspect only installed versions, explicit task holds, current
selection, rollback, scheduled registrations, live processes and their Python
environment dependencies. `--prune-release <registered-version-directory>` limits
removal to an exact version; `--prune-unused` rotates all unheld versions.

Use an interpreter outside the removal targets. A maintenance operator may use
the reviewed authoritative source with its exact commit recorded; production
installations retain their released bytes. No model execution is required.

Registered version directories may be junctions to relocated installations. Pass
the registered entry. The existing native remover binds its physical target,
checks current host references and removes the physical installation and its
alias. Both paths must be absent for completed deletion. A host denial or partial
failure stops the operation; never retry through another executor or move.

## Historical investigation

Archive-wide discovery is an explicit investigation, separate from daily
rotation. It does not run before a version directory is deleted:

```powershell
& $RuntimePython -m foundry_runtime.release_selection `
  --selection $SelectionFile --rollback-python $RollbackPython `
  --audit-history --evidence-root 'C:/Projects/extra-evidence'
```

`--test-run-record` and `--evidence-review` apply to this diagnostic. Keep its
private records and timings. Do not launch a historical audit to obtain ordinary
version sizes or repeat it when a rotation dependency is missing. Resolve only
the affected task/version from its existing owner record.

Each explicit history audit enumerates its current requested roots afresh.
Directory timestamps do not establish unchanged contents: filesystem updates can
coalesce those timestamps while adding records. The audit uses the existing
bounded directory readers and physical-path deduplication. Routine installed
version rotation supplies no history roots and performs no archive traversal.

## Output disposition

Keep source changes, immutable tags, release packages, checksums, original
failures and compact verification. Delete completed disposable environments,
build trees and test scratch after preserving the results needed to reproduce
them. Register exact output roots with the creating operation before expansion.
Do not move or delete running task logs, native sessions, checkpoints or databases.
A retained installation must name its current owner and removal condition.

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
