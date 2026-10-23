# Referenced review context

- Date: 2026-10-23
- Status: complete
- Owner: existing review checkpoint and goal request builder

## Outcome and scope

Reduce repeated inline review context by referencing long fields in the existing
review checkpoint. A retained development sample has approximately 44 KB of
context, including 38 KB of task text and changed-file names. Both independent
reviewers receive that context.

The request builder references long task text, changed-file lists, project records and
execution decisions while retaining candidate identity, checks and feedback
inline. Full context stays in the existing checkpoint. Review validation binds
the complete context hash. Existing checkpoints keep their original rendering.

## Architecture and implementation

The goal controller records the delivery selection when creating a checkpoint.
The existing review request builder selects inline fields or JSON field
references before native admission. The native renderer explains how to read
them. Recovery and runtime-upgrade readers use the checkpoint's recorded
selection and reference. Dispatch, native sessions and review completion retain
their existing owners.

This cross-module request change requires a scope and verification record.
Affected surfaces are checkpoint creation, review request construction, native
rendering, recovery and upgrade readers, installed regression tests, public CI
and review documentation.
No new evidence store, model call, scheduler or execution gate is introduced.

## Verification

The built wheel passed 166 related installed tests, including all 121 tests in
the public CI selection. The retained-interpreter migration test was initially
skipped without its required environment selection, then passed separately with
the retained interpreter selected. Context and runtime-upgrade coverage passed
40 tests. Wheel and source archive builds completed successfully.

Both native renderers preserve complete field retrieval, Unicode, full-context
verdict binding and short/legacy rendering. A real local subprocess reads the
saved requirement. The interruption regression reuses the successful reviewer,
continues the unfinished reviewer and preserves the recorded reference. The
upgrade reader then restores the same native review history.

Read-only comparison of retained baseline selections reduced the development
sample's initial request from 49,147 to 12,174 bytes per reviewer (75.23%). The
first-use sample remained at 13,309 bytes. Historical files retained their hashes.

Implementation review covered complete-context identity, request admission,
recovery, upgrade, lifecycle ownership, repeated serialization and public data
scope. No unresolved implementation findings remain.

## Delivery and rollback

Publish formal operator documentation and an anonymized regression. Retain
compact private measurements and original failures. Reuse the idle environment
from the preceding batch; its prior cleanup denial remains recorded. Remove
new disposable outputs when permitted. Revert the source commit for new work;
existing tasks continue with their recorded runtime and checkpoint.
