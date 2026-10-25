---
name: foundry-release-rotation
description: Publish, adopt, roll back and clean Foundry runtime versions using version directories. Use for Foundry releases, default-runtime upgrades and obsolete installed-version cleanup.
---

# Foundry release rotation

Read [storage retention](../../docs/STORAGE_RETENTION.md) and, for builds or
selection, [releasing](../../docs/RELEASING.md). Those documents own the commands.

Use the authoritative repository and record its exact commit. Build public
releases only from clean detached release tags. Never modify active installs.

Keep current selection, one qualified rollback and explicit current-task holds.
Determine holds from existing owner records and current execution/active-round
provenance. Completed historical records do not retain an installation. Preserve
paused tasks that still require their version. Publication does not select a
runtime; use the existing acceptance receipt and selection mechanism.

For each release, close generated output, retire proven completed/nonrunning
hosted registrations through the existing hosting owner, then use the same
`release_selection` entry point for directory rotation. Pass additional task
versions with `--keep-release` and remove unused versions with `--prune-unused`.
No archive traversal, historical transcript replay, new dependency registry or
alternative deleter belongs in routine rotation. `--audit-history` is a separate
explicit investigation, never a prerequisite for deleting a version directory.

Keep the native handle. The entire cleanup operation, including registration
retirement and deletion, has a ten-minute limit. Pass its remaining monotonic
deadline to the existing `prune` owner when combining maintenance steps. On
expiry stop, retain the unfinished targets and report them; do not restart.
A host deletion denial remains blocked without another shell, executable or move.

Preserve source, public distributions and primary task evidence. Record exact
removed paths, measured bytes and timings; name remaining owners and release
conditions. Verify the retained selection and rollback still resolve. Do not
claim that a plan, an interrupted operation or an existing directory was deleted.
