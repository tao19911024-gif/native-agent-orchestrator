# Core lifecycle audit

The audit used release `v0.1.0a98` at commit
`49ef530f05644d4ea5855256b042c35c81f7f542` as the installed reproduction baseline.
It examined execution custody, native transport completion, goal recovery,
review independence, project hooks, hosted launch configuration and release
retention. Fixes extend the existing owners and records.

## Findings and resulting behavior

| Finding | Trigger and prior behavior | Correction | Regression evidence |
| --- | --- | --- | --- |
| P1: Root exit waits for descendant-held pipes | A native root exits while a child retains stdout or stderr. Waiting for pipe EOF before owned-process cleanup leaves the controller waiting, even after an explicit root stop. | Observe root exit, stop remaining owned processes, drain buffered output, then close pipe handles and validate the native terminal. | Codex and Claude manager replies, app-server startup exit, incomplete turn, explicit root stop and buffered successful terminal. |
| P1: Interrupted checkpoint replacement loses recovery input | A second worker replaces `coding-checkpoint.json`, then execution stops before the manifest pointer is updated. The manifest's old hash no longer has readable bytes. | Normal publication and feedback recovery share one publisher that retains the preceding checkpoint before replacement. | Interrupt the second worker's publication, recover both completed turns and verify that no worker is replayed. |
| P2: Standalone review handoff loses author exclusion | An interrupted standalone review transfers to a provider whose selected model is a declared author. Initial selection excluded that author, but recovered selection did not. | Apply the existing independence check to declared authors from the retained review context as well as observed implementation models. | Refuse the author-model transfer before a review call; recover successfully with a different model. |
| P2: Project observation ignores the task deadline | A project review or acceptance hook starts after an explicit deadline, or continues beyond it. | Check the existing task clock before admission and during polling, then use the existing cleanup and observation path. | Both hook types cover expired admission, expiration while running and operation without a deadline. |
| P1: Pruning overlooks newly created recovery records | A new goal or hosted request appears under a directory already traversed by a long prune operation. Refreshing only previously discovered files omits its interpreter dependency. | Keep directory signatures in the existing operation-local discovery state, enumerate changed directories and new child trees, and assess the new records through the original dependency classifier. | Goal and request creation in existing and new nested directories, changes during enumeration, directory read errors and unchanged archive reuse. |
| P2: Hosted submission loses a wait override | A caller sets `FOUNDRY_WAIT_INTERVAL_SECONDS`, then submits through the scheduler. The launch envelope omitted the setting. | Retain it with the existing explicit environment settings in the launch request. | The submitted value remains recorded after the caller changes its environment. |
| P2: Project defaults lack shared storage resolution | Disposable storage has no project-level default. Project adapters may already select providers through their own entry configuration. | Resolve both optional defaults through the existing project execution contract for new goals. | Descriptor and adapter defaults, explicit role precedence, retained-task exclusions and the project composition entry. |
| P2: Registered scratch is absent from worker environments | A coding worker uses standard temporary-file APIs or pytest defaults; only its prompt names the disposable directory. | Freeze worker-specific temporary-directory values in the existing launch context and apply them at all three coding transports. | Real temporary-file and nested pytest execution for Codex exec, app-server and Claude, continuation, parallel isolation and separate amendment rounds. |

## Architecture and ownership

The follow-up review after a99 reproduced a managed coding-provider transfer
refusal, invalid adapter defaults falling back silently, and vanished evidence
blocking retention refresh. Regression coverage now includes both transfer
directions, interruptions before and after replacement execution, and interruption
before replacement checkpoint publication. The same manager receives retained
failure evidence and subsequent assignments resume the replacement session.

Adapter defaults are checked at the project boundary. A new goal can override
enabled disposable storage with `--no-disposable-scratch`. Removed records are
absent during retention refresh; missing dependencies of a present record remain
visible. Recreated evidence directories are discovered on later refreshes.

Artifact regression tests confirm that the existing delivery boundary rejects
invalid declarations and resolves relative paths against the project workspace
before reclamation. The reclamation owner consumes those normalized artifacts.
Failed goals continue to retain storage for recovery. Reclaiming abandoned work
requires its owner's recorded closure and resolved recovery dependencies under
the existing storage disposition procedure.

`OwnedProcess` remains the sole owner of process-group termination and handle
closure. Its output-drain callback joins the transport's existing readers after
owned writers exit; it does not introduce a second supervisor or a task timeout.
Native terminal validation and cleanup verification retain their separate roles.

The goal manifest continues to select the authoritative coding checkpoint. Its
hash-addressed predecessor uses the existing recovery fallback. Reviewer identity
continues to come from the retained review request and the existing model
selection rules. Project observation commands retain their project ownership.

Project defaults are resolved once for a new goal. The goal's launch context
retains its worker environment through intent, execution package and checkpoint
records. Coding transports merge those recorded values into a process-local
environment. Managers, reviewers and the controller retain their own environment.
Each declared automatic correction round registers its own directory.

Retention discovery state exists only during the current prune operation. It
does not create a persistent index or background scanner. An unchanged refresh
enumerates zero directories; adding a new goal does not enumerate unchanged
archive descendants. Existing process and registration checks still run before
release removal.

## Additional review findings

The review also checked artifact handling and a reported legacy manager-handback
failure. The delivery boundary converts relative artifact paths to absolute
paths and rejects a declaration without a usable path before reclamation.
A delivered artifact inside disposable storage retains the whole registered
directory under the existing ownership contract. Completion publication already
follows cleanup and usage finalization in a98.

The three reported legacy manager-handback cases passed using the referenced
source revision and retained a97 interpreter. The failure was not reproduced.
The new worker environment governs standard temporary-file APIs and pytest's
default parent; explicit application output paths continue to select their
specified locations.

## Verification

The original defects were reproduced against the installed a98 baseline. The
worker-environment integration also reproduced inherited-directory failures on
candidate builds before fixing continuation and standalone-review amendment
paths. Public regression fixtures use synthetic repositories, local native
protocol processes, real Windows process ownership and isolated evidence
directories.

The new cases are included in the public CI selection. Additional existing
suites exercise native continuation, provider errors, manager and goal recovery,
runtime transitions, deadline propagation and launch-context replay. Candidate
qualification also builds both distributions, checks installed module bytes,
checks both CLI entry points and initializes all three quickstart scenarios.

The final installed candidate passed all 333 public offline cases in 253.00
seconds. A further 29 launch-replay, coding-configuration and Claude stream-event
cases passed in 17.10 seconds; four native-client configuration probes were
outside that synthetic selection. All 91 installed Python modules matched the
candidate source byte for byte. Wheel and source-archive builds, dependency
consistency, both CLI help commands and the three example initializations passed.

The local record retains original failures, fixture corrections, process-query
permission failures and their qualified reruns. Test assertions and production
deadline defaults remain unchanged by environment corrections.
The stop fixture waits for its controller's published active event before reading
the journal, avoiding the interval between directory creation and first-event
publication while preserving the original stop assertions and deadline.

## Delivery and rollback

Changes are recorded under `Unreleased`. Publication and new-task runtime
selection follow [the release procedure](RELEASING.md). Installed a98 and its
qualified rollback remain at their recorded paths; this source audit does not
migrate active or historical tasks. Source rollback uses the focused fix commits.
