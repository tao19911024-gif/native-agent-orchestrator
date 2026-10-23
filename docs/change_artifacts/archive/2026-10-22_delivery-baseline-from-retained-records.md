# Delivery baseline from retained records

- Date: 2026-10-22
- Status: completed
- Owner: existing task-report reader

## Outcome

Extend `--foundry-usage` with a delivery baseline connecting task identity,
native execution, candidate commits, project checks, independent reviews and
project acceptance through retained records. Automatic round containers and
explicit dispatch selections retain separate counts.

## Architecture and scope

The task-report reader owns traversal and deduplication. The baseline projects
its records through the existing reference reader. Goal outcomes own verdicts;
review checkpoints own candidate context and project checks. Adjacent manifests
and telemetry are identified as current observations. Execution and acceptance
policies retain their existing owners.

Affected surfaces are the task-report projection, offline tests, CLI help,
operator documentation and public CI test selection. Durable record formats,
native clients, recovery and installed runtimes retain their existing contracts.

## Verification

Exercise linked completion, continuation, automatic rounds, review-only results,
unfinished records and missing historical evidence. Check duplicate inputs do
not inflate counts and report generation leaves source bytes unchanged.

The final wheel passed 168 installed-package and focused regression tests.
Baseline extraction covered retained first-use executions and a development
continuation chain. The two selections contain four goal invocations, two
automatic containers and three direct dispatch receipts. Original goal records
retained their content hashes. Implementation review covered ownership,
association correctness, partial-history behavior, counting and documentation.

## Delivery and storage

Commit and push the verified change. Retain compact verification and historical
baseline reports outside the public repository. Remove the disposable test
environment after verification. A source commit revert rolls back this change;
report generation does not migrate task data.
