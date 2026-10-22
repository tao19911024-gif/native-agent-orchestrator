# Architecture and lifecycle

Foundry combines a coordinator with an explicit, durable task lifecycle. Native
Codex and Claude clients perform model execution and provider-native delegation.
Foundry records custody, continuation, review results, usage, and result receipts.

## Ownership and dependency direction

| Surface | Owner |
| --- | --- |
| Requirements, task order, project rules and preflight | Target project |
| CLI routing, native calls, runtime and session provenance | Foundry runtime |
| Goal sequencing and manager/worker coordination | Existing Foundry goal lifecycle |
| Provider authentication and model availability | Installed native client and account |
| Tests, review requirements, acceptance and integration | Target project |
| Optional code index | Project-selected external tool |

Project wrappers call the public CLI or supply their adapter to
`foundry_runtime.cli.main`. Descriptors select an existing project context.
Runtime internals use relative package imports; the extracted runtime does not
require the original development checkout. Historical `pdcl` names remain in
some serialized formats and environment settings.

## One lifecycle

An approved order enters the project's preflight, then the existing dispatch or
goal owner. The runtime records the selected project identity, native provider,
interpreter, execution intent, and evidence. Manager, worker, expert, and review
roles use that retained context. Receipts distinguish verified results, stopped
execution, pending work, unknown outcomes, and project-owned acceptance.

Continuation and recovery reconcile those same records. They do not create a
parallel scheduler or infer acceptance from a model's final message. Existing
tasks keep recorded versions unless an explicitly supported lifecycle transition
changes them.

## Verification and rollback

The repository includes installed-package and offline lifecycle tests. Native
client, desktop-host, Windows service, and real-project integration require their
own environment-specific evidence. Passing fixture tests does not establish those
integrations or production acceptance.

For new installations, keep an earlier independently qualified virtual
environment available if rollback is needed. Stop selecting a new installation
for new work to roll it back. Do not repoint an active task's interpreter or edit
its journal as a rollback technique.

## Source publication boundary

This repository starts from a clean source snapshot, with regression identifiers
anonymized. It does not import the private development Git history, operational
case notes, project descriptors, installed environments, account files, or actual
task journals. Native client executables are installed independently.
