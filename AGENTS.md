# Repository rules

Follow [CONTRIBUTING.md](CONTRIBUTING.md) for changes, installed-package checks
and public pull requests. Keep private task data out of this repository.

This repository is the sole runtime source authority. Follow
[docs/SOURCE_AUTHORITY.md](docs/SOURCE_AUTHORITY.md) and
[docs/RELEASING.md](docs/RELEASING.md) for source lineage, tagged builds,
acceptance and runtime selection. Preserve historical release tags.

## Storage lifecycle

Follow [docs/STORAGE_RETENTION.md](docs/STORAGE_RETENTION.md) when creating
environments, running experiments, publishing releases or cleaning artifacts.
Storage disposition is part of completing that work, not a separate future task.
Read [skills/foundry-release-rotation/SKILL.md](skills/foundry-release-rotation/SKILL.md)
when publishing, adopting or cleaning runtime versions.

- Reuse a compatible idle test environment when possible. Do not install into a
  runtime owned by an active task. Give temporary outputs an identifiable owner
  and destination; retain the operation handle until completion.
- Keep the selected production runtime, one qualified rollback runtime and
  every environment still required by active or explicitly retained recovery
  work. Use the release-rotation skill and the existing directory rotation command.
- Preserve source changes and primary evidence. After verification, remove
  reproducible scratch, duplicate build outputs and completed disposable test
  environments; retain compact results, commands, provenance and failure logs.
- Record created output roots with the existing operation evidence. Close those
  outputs before release rotation. Use current task holds and combine unused
  version directories in one operation. Do not scan historical archives during
  routine cleanup. Stop rotation after ten minutes and retain its native handle.
- Measure large output trees before expanding them. If space is constrained,
  use a suitable scratch/archive volume rather than silently adding copies to
  the execution volume. Do not stop another task to recover space.
- Record retained exceptions with their owner, reason and removal condition.
  Historical uncertainty requires a scoped investigation, not indefinite
  retention of every old environment.
- Report before/after bytes, deleted paths, archive location and verification,
  and unresolved dependencies. Do not call an inventory a completed cleanup.
