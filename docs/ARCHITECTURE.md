# Architecture and lifecycle

Native Agent Orchestrator combines a coordinator with a durable task lifecycle. Native
Codex and Claude clients perform model execution and provider-native delegation.
The runtime records custody, continuation, review results, usage, and result receipts.

Explicit disposable storage is registered in the owning goal manifest by
`--disposable-scratch` or the project's new-goal default. The goal records reclamation in the existing usage
record before publishing its completed outcome and final usage reference. Worker
instructions carry the storage path, and the frozen launch context carries the
coding worker's temporary-directory environment. Review and recovery retain the directory
until the owning goal completes. The lifecycle contract is defined in
[Storage retention](STORAGE_RETENTION.md#explicit-disposable-storage-for-new-goals).

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

[Source authority](SOURCE_AUTHORITY.md) identifies the release repository and
historical migration. The release selector owns new-task deployment and records
the version-bound acceptance receipt. Task provenance continues to own recovery.
Project admission retries remain in the project adapter's repository.

For new goals, the CLI resolves an explicit coding selection, then the descriptor
or adapter's optional `default_provider`, then the task-kind default. Both project
forms expose their defaults through the existing `ProjectExecution` contract.
The existing goal preparation records the selected provider and its `project`
source. Recovery and amendments retain their recorded configuration.

The same project contract exposes `default_staffing` for omitted compute,
manager, native-team and reviewer profiles. New Claude staffing separates the
Sonnet implementation session from Opus coordination and selects Opus plus the
catalogue-resolved Codex Sol tier for independent review. The goal controller
reselects conflicting automatic review slots; it retains completed verdicts.
Reviewer escalation uses existing tier decisions, correction rounds and
`review_transfers`, including a single higher-tier attempt after a measured Sol
quota refusal. Review launch failures retain an outcome and the existing review
checkpoint for recovery.

## One lifecycle

An approved order enters the project's preflight, then the existing dispatch or
goal owner. The runtime records the selected project identity, native provider,
interpreter, execution intent, and evidence. Manager, worker, expert, and review
roles use that retained context. Receipts distinguish verified results, stopped
execution, pending work, unknown outcomes, and project-owned acceptance.

Live coordination uses the same manager decision contract as returned-worker
coordination. Delegation can deliver guidance to the running worker. Awaiting
input, review and artifact delivery finish live coordination and take effect
after the current native turn and its cleanup settle. The controller persists
the coding checkpoint before publishing a question or advancing the next stage.
Recovery consumes a saved readiness decision without repeating the manager call.

Protocol-message coalescing follows the observed working contents and native
evidence generation across message delivery. Known tool observations compare the
same member and request with its latest result; a new item ID alone is not new
evidence. Different requests, errors, results, source changes and explicit user
input retain normal coordination. Unknown observations remain actionable.
Only outer protocol-report whitespace is normalized; report bodies remain exact.
Repeated review refusal compares the actual
dirty source snapshot. Repeated completed delegation compares the semantic
assignment, source snapshot and final report. Identical observations reach the
existing awaiting-input outcome; changed contents, results or an explicit answer
permit further work. Receipt timestamps and manager call identities do not
establish progress.

Automatic correction compares candidate trees, blocking findings and measured
active failures through its existing round history. New source trees, findings
or measurements and recorded escalation opportunities permit further correction.
Recurring content and unchanged results reach the recorded correction stop.
Expert consultation compares the answer actually admitted to the manager with
the requested question, named evidence and expert assignment. Repeating an
already answered question with unchanged evidence publishes an operator question.
New questions, changed evidence and an explicit new attempt retain their existing
consultation path. Native call identities and receipt replay remain owned by the
native execution layer.

Continuation and recovery reconcile those same records. The
[recovery guide](RECOVERY.md) covers completed sessions, unfinished goals, and
interrupted attempts. A runtime or role transition records its selected successor
through that lifecycle.

The dispatcher owns the terminal and cleanup proof for each attempt. The goal
owns the pending package reference and consumes it when publishing worker
feedback. A capacity-retry admission error retains the last completed package;
a started successor retains its own package. Legacy manager handback resolves
an ambiguous assignment through its bound native input, frozen request digest
and current decision. The goal preserves failed return codes and passes settled
feedback to the manager on explicit recovery before further worker dispatch.

An explicit coding-provider transfer uses the existing answer continuation and
retained manager input. Pending transfer options survive interruption before
dispatch. Normal return and completed-dispatch recovery both consume those
options when publishing the replacement worker's session; later assignments
resume that session through the same manager.

An exited native root initiates cleanup of its owned process group. The existing
output readers drain buffered bytes after owned writers exit and before their
handles close. Process exit, protocol terminal and verified cleanup remain
separate observations; a missing terminal retains an unknown execution result.

Normal worker return and recovered feedback use one coding-checkpoint publisher.
It retains the previous checkpoint bytes before replacing the current file, then
publishes the new manifest reference. The manifest's prior hash remains readable
if execution stops between those writes.

Goal and hosted status expose [review feedback](FEEDBACK_LOOP.md) by reading
the existing outcome and manifest. The delivery projection reuses the goal's
finding deduplicator and amendment settings. Recovery and amendment advice
share the recorded-entry command formatter; the existing amendment controller
resumes the coding session and records the successor candidate and reviews.

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

The task-report reader traverses explicit predecessor and round references and
deduplicates retained records. Its [delivery baseline](DELIVERY_BASELINE.md)
projects task, execution, candidate, check and review associations through the
same reference reader. Outcomes supply verdicts and project acceptance;
review checkpoints supply candidate context and project-check observations.
Adjacent manifests and phase telemetry retain their observation provenance.

The review checkpoint also owns the complete review input. For new goals, the
review request builder [references long context fields](REVIEW_CONTEXT.md)
before native admission. Rendering and recovery consume the same request;
verdict validation remains bound to the complete checkpoint context hash.
The checkpoint also retains the native evidence-reading policy. Normal review,
recovery and upgrade-history readers reconstruct the same request from that
policy; historical checkpoints preserve their original rendering.
Declared review-author models remain in that checkpoint context and participate
in the same reviewer-independence check after recovery or provider handoff.
