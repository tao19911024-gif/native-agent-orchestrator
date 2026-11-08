# Review selection and closure storage

Release: 0.1.0a115. Pull request: #57.

## Problem and ownership

A candidate implemented by models from both providers can conflict with every
named review profile even when independent catalogue models remain available.
The CLI now accepts an explicit pair and converts it into the existing review
assignments. Native model resolution, author exclusion, frozen review slots and
review execution retain their existing owners.

Candidate closure previously chose a temporary volume from the evidence path.
Archived evidence therefore placed disposable checkout and test files on the
archive volume. Closure now consumes the existing project scratch-root setting
and retains its unique child, owned process and exact-child cleanup lifecycle.
There is no second storage configuration or cleanup service.

The architecture review also found two upgrade-boundary gaps. Explicit review
pairs now participate in the existing request identity and command projection.
The successor preserves recorded scratch policy while its goal execution owner
registers a fresh directory. Native launch refresh no longer erases this policy.

Independent reviewers could previously execute tests without the registered
temporary environment used by coding workers. Review attempts now consume the
goal manifest's existing environment through their own role-specific launch
context. Managers, expert consultations and provider transfers retain the same
environment. Historical native calls continue to replay their recorded context.

A provider-interrupted review pair can include a completed BLOCK verdict. The
existing instructed amendment path now accepts that state and forwards the
completed findings to the original coding session. The new candidate still
requires the complete independent review pair.

## Contract

- `--review-model MODEL_1 MODEL_2` selects a new goal or standalone review pair.
  Catalogue tiers resolve before the pair is frozen. Explicit model IDs retain
  their identity. Both slots use high effort and the existing author exclusion.
- Named profiles and shared analyst overrides remain mutually exclusive with
  the explicit pair. Recorded recovery assignments remain unchanged.
- Closure scratch uses `default_disposable_scratch_root`, or the existing host
  temporary location. Evidence receipts remain under the goal evidence root.
- Each closure overrides TMP, TEMP, TMPDIR and the pytest temporary parent for
  its owned process. It removes only its unique child after process cleanup.

## Verification and rollback

Regression fixtures cover mixed-author review selection, new-goal assignment,
recorded recovery, configured closure storage and inherited temporary paths.
The published baseline fails the initial five regression cases. The candidate is checked from
an isolated wheel installation using synthetic native processes.

This change does not modify installed versions, current selection, running
tasks or historical records. Reverting the source commit restores the prior
CLI and temporary-location behavior; release selection remains separately
owned by the release operation.
