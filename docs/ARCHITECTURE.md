# Architecture and lifecycle

Native Agent Orchestrator combines a coordinator with a durable task lifecycle. Native
Codex and Claude clients perform model execution and provider-native delegation.
The runtime records custody, continuation, review results, usage, and result receipts.

## Ownership and dependency direction

| Surface | Owner |
| --- | --- |
| Requirements, task order, project rules and preflight | Target project |
| CLI routing, native calls, runtime and session provenance | Runtime dispatcher |
| Goal sequencing and manager/worker coordination | Goal controller |
| Provider authentication and model availability | Installed native client and account |
| Tests, review requirements, acceptance and integration | Target project |
| Optional code index | Project-selected external tool |

Project wrappers call the public CLI or supply their adapter to
`foundry_runtime.cli.main`. Descriptors select an existing project context.
Runtime internals use relative package imports. Historical `pdcl` names remain
in serialized formats and environment settings. The
[configuration reference](CONFIGURATION.md) owns descriptor fields and the
[command reference](CLI.md) owns operator commands.

## One lifecycle

An approved order enters the project's preflight, then the existing dispatch or
goal owner. The runtime records the selected project identity, native provider,
interpreter, execution intent, and evidence. Manager, worker, expert, and review
roles use that retained context. Receipts distinguish verified results, stopped
execution, pending work, unknown outcomes, and project-owned acceptance.

Continuation and recovery reconcile those same records. The
[recovery guide](RECOVERY.md) covers completed sessions, unfinished goals, and
interrupted attempts. A runtime or role transition records its selected successor
through that lifecycle.

## Verification and rollback

The repository includes installed-package and offline lifecycle tests. The
[examples](../examples/README.md) cover direct native execution, session
continuation, and coding with independent review. Their executed results are
recorded in [DEMO.md](DEMO.md). Desktop-host, Windows service, and project
integration checks use their selected environments and project acceptance criteria.

For installation rollback, select the earlier verified virtual environment for
new tasks. Active tasks retain their recorded interpreter and execution records.

## Repository and task records

The repository contains runtime source, anonymized regression fixtures,
documentation, and examples. Project descriptors, native sessions, and task
journals are stored in the selected local project and evidence directories.
Native client executables are installed independently.
