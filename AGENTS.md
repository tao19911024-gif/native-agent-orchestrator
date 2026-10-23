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

- Reuse a compatible idle test environment when possible. Do not install into a
  runtime owned by an active task. Give temporary outputs an identifiable owner
  and destination; retain the operation handle until completion.
- Keep the selected production runtime, one qualified rollback runtime and
  every environment still required by active or explicitly retained recovery
  work. Use the existing release retention planner for older installed versions.
- Preserve source changes and primary evidence. After verification, remove
  reproducible scratch, duplicate build outputs and completed disposable test
  environments; retain compact results, commands, provenance and failure logs.
- Measure large output trees before expanding them. If space is constrained,
  use a suitable scratch/archive volume rather than silently adding copies to
  the execution volume. Do not stop another task to recover space.
- Record retained exceptions with their owner, reason and removal condition.
  Historical uncertainty requires a scoped investigation, not indefinite
  retention of every old environment.
- Report before/after bytes, deleted paths, archive location and verification,
  and unresolved dependencies. Do not call an inventory a completed cleanup.
