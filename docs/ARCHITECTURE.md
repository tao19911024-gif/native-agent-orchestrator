# Architecture and lifecycle

Native Agent Orchestrator combines a coordinator with a durable task lifecycle. Native
Codex and Claude clients perform model execution and provider-native delegation.
The runtime records custody, continuation, review results, usage, and result receipts.

Explicit disposable storage is registered in the owning goal manifest by
`--disposable-scratch` or the project's new-goal default. Projects can place
registered scratch on a separate volume from the evidence root. The goal records reclamation in the existing usage
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

Goal status joins only packages bound to the exact active goal directory and run
ID, with the journal's current attempt. The goal manifest records its existing
dispatch evidence directory. Package publication records the goal binding;
inspection reads it without updating the manifest or creating a task index.
Runtime selection is an optional read-only input, separate from recorded
controller/worker provenance. Status summaries reuse existing feedback and input
continuation commands, including their recorded environment.

Root-goal observation follows the active round's existing
`continuation.prior_record` links to the unique continuation tip, including
coding and provider-interruption continuations. The original goal identity stays
in the report; `continuation_goal` and `active_round` identify its current work.
Link traversal is shared with answered-question recovery. Observation does not
require a new manager receipt, while recovery retains its consumption checks.
Repeated answers sharing a prior record use the existing manager receipt chain
to resolve their current tip. Unresolved branches are reported as observation
errors without choosing by timestamp.

Usage summaries project already reconciled native identities. Saved full reports
provide fixed comparison baselines; cumulative sessions contribute their growth
once, including sessions shared across roles. The session selector is shared with
the existing usage owner. Native invocations, observed responses and token units
remain distinct. Delivery levels project execution, candidate review and
project-owned acceptance from the same outcome. See [task observation](TASK_OBSERVATION.md).

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

An explicitly closed abandoned execution retains its terminal journal. A new
goal can use the same task and order: its persisted run ID selects a distinct
execution when the original frozen identity is closed. Retries of that goal
retain the same successor. Dispatch allocates numbered evidence families under
the existing task mutex; old logs, reports, packages and cleanup remain intact.
Completed invocation replay and unresolved-execution recovery keep their existing
semantics.

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

New manager requests distinguish a stage handback from completion of the whole
order. Intermediate independent design or slice reviews use ordinary delegation
to the existing worker and a separate qualified member. Their findings return
to the manager before implementation continues. Expert consultation remains
advice, not independent review. The `review` action is the final candidate route.

The manager records `remaining_work` in its existing decision. A final `review`
or `deliver_artifacts` request with unfinished work returns through the existing
review-refusal feedback phase. Repeating the same incomplete handoff without
new evidence uses the existing no-progress observation. An empty list is the
manager's completion judgment; independent final reviewers assess the complete
original order and its explicit revisions, including missing implementation.
Document-only orders retain their own completion boundary. Already admitted
manager requests and review checkpoints retain their frozen contracts. Newly
created final-review checkpoints use the whole-order review guidance, including
review-only and amendment runs.

Correction feedback uses the retained amendment or continuation's prior-review
reference for manager, expert and final-review inputs. The last two review
results follow those same links. An automatic goal summary resolves to its
recorded final round and is not counted as another review. Missing historical
references retain the existing context-gap behavior; they do not introduce a
new task admission rule or a review-round limit. Without a manager, an unchanged
review question reuses the expert answer retained in the execution decision;
a changed question requests a new consultation. Existing trigger precedence
remains recurring blocking findings, tier escalation, then reviewer guidance.

Within a manager-assigned slice, the execution root owns implementation,
focused checks and the committed candidate handoff. It can work directly and
make reversible implementation decisions within that scope. Delegation serves
independent deliverables, distinct expertise or useful context separation. Scope
changes, unresolved cross-boundary decisions and conflicting requirements return
to the manager, which owns delivery readiness. Candidate review retains its
separate admission and independence rules. Native team guidance is frozen at
launch; retained tasks keep their recorded instructions.

For Codex execution, the existing member activity observer owns live membership,
member turn identities, running commands and mailbox waits. The AppServer
transport consumes its completion and quiet-wait observations and owns delivery.
New member turns reset their earlier live work; terminal parent reports retire
the member's commands and waits together. A successful edit or changed command
result re-arms quiet-team delivery through the existing tool-observation digest.
An empty root mailbox is eligible for delivery on the next observation tick.
Running commands, active members and pending input retain their existing owners;
a waiting team retains its quiet-progress interval. A command completion that
already owns a wait's wake-up suppresses an additional empty-wait observation.
The goal manifest and journal retain durable task and attempt ownership.

Operator input is incorporated before coordination and checked again before its
returned decision advances execution. Updates arriving during a native call or
quota wait return to the existing feedback phase before delegation, review,
artifact delivery or awaiting-input publication. Live coordination consumes the
same updates before steering. Work already owned by a native turn retains that
turn's lifecycle.

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

Whole-goal upgrade request identity includes explicit review model pairs; replay
and operator advice consume the same recorded overrides. An upgraded round
inherits the prior round's disposable-storage policy and registers its own
directory through the existing goal execution owner. Capturing new native
launch settings does not erase the retained storage policy.

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
The CLI inherits omitted amendment preflight inputs from that retained project
record before validating the supplied invocation. Launch preflight and resume
preflight remain distinct, including an explicitly empty resume input. Explicit
input differences and descriptor differences name their affected fields.

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
Each slot validates its own response and packet. The review-pair owner validates
the shared candidate after both calls settle, including HEAD, index and
untracked files. A temporary reproduction created by an active reviewer is not
a completed-pair workspace observation. Both verdicts remain recorded when the
final candidate check fails, together with the observed dirty workspace.
The goal owns a bounded connection-retry window per slot, recorded in the
existing manifest. Native receipt-bound streams classify recorded connection
failures, uncertain-delivery errors and prior work. The existing review panel
first recovers complete verdicts and binds each admitted native attempt to its
predecessor receipts. The existing review threads retry each slot
independently, while the goal serializes manifest writes and journal completion.
No worker, closure or successful reviewer is dispatched again. Explicit recovery remains available after another failure.
The checkpoint also retains the native evidence-reading policy. Normal review,
recovery and upgrade-history readers reconstruct the same request from that
policy; historical checkpoints preserve their original rendering.
Declared review-author models remain in that checkpoint context and participate
in the same reviewer-independence check after recovery or provider handoff.
