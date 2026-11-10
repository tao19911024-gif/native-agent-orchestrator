# Continuation and recovery

Continuation uses a retained task and native session. Recovery first reconciles
the recorded execution state, then selects the continuation supported by that
state. Keep the project descriptor, task order, evidence, and interpreter with
the task.

## Inspect the task

```powershell
$TaskStatus = & $RuntimePython -I -m foundry_runtime --foundry-project status `
  --project-config $ProjectConfig --tag TASK1 | ConvertFrom-Json
$TaskStatus.runs | Select-Object operation, recorded_state, returncode, session_id
```

| Recorded state | Operator action |
| --- | --- |
| `starting` or `active` | Inspect the retained process and provider observations, then follow `next_action` |
| `completed` | Read the result and tests; continue its recorded session when more work is required |
| `unknown` | Inspect recovery commands for the exact intent and reconcile its process and terminal evidence |
| `not_started` | Follow the recorded retry instruction for that original attempt |

The response includes `errors`, `next_action`, native observations, evidence
references, and recovery or resume commands when available. Use those bindings
to select the task rather than choosing a run by directory modification time.

## Continue a completed direct session

Select the session from the intended run, then provide a UTF-8 instruction file:

```powershell
& $RuntimePython -I -m foundry_runtime --project-config $ProjectConfig `
  --resume $SessionId --worktree $Workspace --tag TASK1 `
  --launcher exec --prompt-file $PromptFile
```

Preserve the original tag, workspace, descriptor, interpreter, and transport.
Ordinary continuation resolves the recorded provider. The dispatcher records
the new continuation and keeps the same native session.

The [checkpoint example](../examples/CONTINUATION.md) provides a complete
reproduction: inspect the code, end the first turn at `CHECKPOINT_READY`, then
resume the same session to implement and test the task.

An identical dispatch or continuation request returns its retained result.
New work uses new continuation instructions. A fresh demonstration starts from
a new example directory.

## Recover a goal

An abandoned execution closed through reconciliation remains closed. To begin
an explicitly new goal for the same order, use the normal project invocation
with `--foundry-goal` and a new goal directory, without `--recover-goal` or an
old `--execution-intent-id`. The new goal retains its run identity for retries
and allocates numbered dispatch evidence in the existing task directory.
Original journals, logs and reports remain available. An unresolved execution
continues through reconciliation and recovery before new work.

Read the exact goal returned by the launch:

```powershell
& $RuntimePython -I -m foundry_runtime --foundry-project status `
  --status-goal $GoalDirectory
```

Inspect the retained runtime, context, and native calls before continuing:

```powershell
& $RuntimePython -I -m foundry_runtime --project-config $ProjectConfig `
  --order $TaskOrder --foundry-goal --recover-goal $GoalDirectory `
  --recovery-preflight
```

For unfinished coding that is ready for an operator instruction:

```powershell
& $RuntimePython -I -m foundry_runtime --project-config $ProjectConfig `
  --order $TaskOrder --foundry-goal --recover-goal $GoalDirectory `
  --prompt-file $PromptFile
```

The controller uses retained project and role records. Its response identifies
the next state and any `continuation_evidence` directory. Retain that directory
as the binding for the continued goal.

`--recover-goal` selects goal recovery before project launch defaults are resolved;
the explicit `--foundry-goal` flag in the examples can be omitted. It never selects
a fresh order dispatch. `--retained-only` reads the retained recovery result
without starting model work.

A live manager request for a commander ruling ends coordination after the
current native turn settles and publishes the question in `outcome.json`.
Reading status or recovering without an answer reuses that outcome. Submit the
answer through the command above to continue the same native session.

Repeated review of an unchanged dirty candidate, or the same assignment returning
an identical report against unchanged source, also publishes an awaiting-input
question. The coding checkpoint retains the completed work. Inspect the stated
observation, resolve the project prerequisite or provide an explicit instruction;
restarting the goal does not provide new evidence.

A repeated expert question whose answer was already received against unchanged
evidence also publishes an awaiting-input question. Specify the unresolved point
or authorize a new attempt with the existing answer command. A new question or
changed named evidence follows the normal expert-consultation path.

New manager requests carry a versioned scope-question contract. Before requesting
a scope or PERMIT decision, the worker investigates the complete proposed fix,
trials the authorized portion and lists every currently known additional file
with its reason and evidence in the existing handoff. The manager consolidates
that list into one question through the existing input route. Investigation
retains current editing permissions. A later question identifies genuinely new
evidence and its newly required change. Retained requests without this contract
preserve their recorded instructions.

Completed Claude and Codex executions both restore manager feedback from their
original execution journal. The controller selects packages bound to the active
round by directory and run ID before considering packages from older runtimes
that lack the binding. A verified retained native terminal can be reconciled
after interrupted completion publication. An attempt without terminal evidence
uses its recorded interrupted-execution procedure; cleanup alone establishes
process exit, not successful work. A closed historical intent remains closed.

## Correct a reviewed candidate

Goal status includes `feedback`: the original task, reviewed candidate,
deduplicated findings, artifact references and a recorded-session amendment
command. Use that command for completed review feedback, then retain the new
goal directory returned by the correction. The
[feedback guide](FEEDBACK_LOOP.md) documents the status fields, continuation
states and a repeated conflict-response correction scenario.

## Reconcile interrupted execution

An explicitly authorized `--order-revision` can reconcile the retained contract
at an operator-input boundary or a settled failed-worker feedback boundary.
The revision JSON contains `schema_version: 1`, `authorized_by: "operator"`,
the active round's `goal` directory, `run_id` and original `order` reference.
Supply `previous`, `revised` and `authorization` as objects with `path` and
`sha256`. The previous file holds the exact last admitted order bytes. The
revised file holds the current order bytes. The authorization file records
the operator ruling. An identity mismatch reports the field and expected value.
The existing dispatch completion
verifier checks the original checkpoint, terminal and cleanup before admission.
Original goal identity and checkpoint evidence remain unchanged. Contract
revision and runtime upgrade are separate recorded operations; neither establishes
business acceptance.

When candidate closure fails, use `--recover-goal` without `--prompt-file` to
retry closure and review of the same commit. For a recorded coding session, pass
`--prompt-file` with the correction to continue coding through the original goal.

After a manager consumes an answer, goal status follows the retained answer
chain. `continuation_goal` identifies the goal that supplies the current outcome
and recovery command. The original question remains in its original outcome.

When a review slot fails, the outcome retains any completed verdicts and marks
review completion as required. Use the existing `--recover-goal` invocation
from status to retry the unfinished slots against the same frozen candidate.

For an `unknown` direct attempt, project status returns `recovery_commands` when
the original package and native identities are available. The generated
`reconcile` command measures the recorded processes and cleanup state. Where
explicit recovery authorization is required, its `authorize` and `resume`
commands identify the instruction file and project grant to supply.

Run the commands produced for that exact attempt. Reconciliation, authorization,
and execution are separate operations recorded by the existing lifecycle.
`python -m foundry_runtime.recovery --help` lists the recovery-record options.

For a goal, use its recovery entry to reconcile completed coding and return
control to the goal controller. The goal status and recovery preflight identify
the relevant checkpoint and native call.

Worker checkpoint replacement preserves the preceding bytes by their recorded
hash until the new manifest reference is durable. Recovery after an interrupted
publication reconciles the completed worker from its dispatch evidence and
returns its feedback to the retained manager without launching that worker again.

Registered worker temporary directories are retained in the launch context and
reused when continuing within the same goal. A continuation or amendment that
creates a new goal directory inherits the recorded storage policy and binds its
own directory when the predecessor has `worker_environment`; a new opt-in flag
is not required. Recovery does not apply current project defaults to an existing
goal. Historical contexts without a worker environment keep their recorded behavior.

## Runtime and role transitions

Ordinary recovery routes through the recorded interpreter. Runtime upgrades use
`--upgrade-runtime` at a settled goal boundary. Provider or role transfers use
`--handoff-provider`, `--handoff-role`, and their retained recovery inputs.
Inspect `--help` and the exact task's recovery advice for these operations.

If amendment or recovery encounters a removed dependency inside the recorded
runtime environment, the refusal includes an explicit invocation using
`--upgrade-runtime current`. Supply the next-round decision in `--prompt-file`
and invoke it through the selected installed runtime. The existing upgrade
owner verifies the settled boundary and retains the original goal history.
Missing project evidence continues through its original error path.

Plain continuation retains the latest recorded session launch policy. When
packages share a second-resolution timestamp, their existing continuation
numbers determine publication order, including an explicit runtime upgrade's
new settings.

A settled failed coding turn in a managed goal can transfer with
`--handoff-provider claude --handoff-role coding --prompt-file <answer>`
through the goal's existing `--recover-goal` entry. The answer continuation
retains the manager's native session and presents the failed worker's evidence
before its next decision. The replacement coding provider starts a new native
worker session; subsequent assignments resume that session. The original goal
and its evidence remain unchanged. The prepared transfer is recorded in the
existing execution options and consumed when the replacement dispatch settles,
including recovery after interrupted checkpoint publication.

Standalone review keeps its declared author-model exclusions when reviewers
resume or transfer providers. Select a reviewer outside those recorded authors;
the same independence rule applies before issuing the resumed review request.

Review launch and recovery use the same recorded model-selection policy.
When frozen default assignments conflict with observed implementation or
declared author models, recovery selects eligible defaults through the existing
catalogue and records replacements in `review_transfers`. The original review
checkpoint remains unchanged. Completed eligible slots retain their verdicts;
only unfinished slots receive replacement assignments. Explicit model choices
and provider transfers retain their recorded selection rules.

A review that cannot start records `review_completion_required`, its reason and
the candidate in the outcome, with the initial failure retained in
`review-refusal.json`. Recovery completes the existing review checkpoint before
corrections. Transferring one unfinished slot applies before independence
selection for the remaining automatic slots. Completed verdicts and the frozen
checkpoint remain available throughout that transition.

When an independent review fails, the goal first reads its settled native
receipts and reuses any complete, validated verdict. For explicit DNS resolution
or connection-refused failures, it can retry the failed slot within five minutes
of the first failed receipt. Delays are 15, 30, 60, 60, 60 and 60 seconds, with at
most six automatic attempts. Failed-call time counts toward this window. An
attempt already performing a review retains the ordinary task deadline; the
retry admission window does not interrupt that work.

Automatic replay requires a complete, receipt-bound stream containing only
startup events and explicit pre-connection failures. A final DNS error does not
override recorded earlier errors or work. Request timeouts, connection
resets, TLS interruptions and response loss have uncertain delivery, including
when no output reached the client; they stop automatic replay. Model output,
tool activity, usage, unfamiliar progress or incomplete cleanup also stop it.
A native session identifier alone does not establish whether the server accepted
a request. These CLI review paths provide no request-result lookup or idempotent
resubmission contract. Inspect the retained attempt before authorizing another
request through explicit recovery. Resuming a conversation supplies another
input; it is not a lookup of the lost response.

Classification is limited to events captured by the native client. Internal
requests or retries that the client does not report cannot be distinguished;
this mechanism does not provide exactly-once server execution. The connection
retry path is verified for Codex exec error events. Claude assistant/result
error responses remain uncertain delivery and do not trigger automatic replay.

The existing goal manifest records `review_transport_retries` with the slot,
failed receipt, delay, scheduled time and original deadline before waiting.
Each record reserves one attempt, including an interrupted wait; actual calls
and usage remain in native receipts. This preserves the window across a restart
during the first delay.
Each slot waits and retries on its existing review thread. Manifest writes are
serialized; a peer's running review does not hold that slot's retry schedule.
Controller cancellation wakes the delays and prevents new attempt admission.
The `reviews/review-N/attempt-*` receipts and predecessor references retain every
attempt and its usage. Restart and provider transfer do not reset the allowance.
An a118 one-shot record remains spent when read by this implementation. The
candidate, request, provider and model stay fixed, completed slots retain their
verdicts, and a real BLOCK continues to block delivery. Existing model-tier
selection and explicit recovery retain their separate rules.

New staffing separates the non-editing coordinator from observed coding
participants. Its automatic reviewer choices and tier decisions are described in
[Configuration](CONFIGURATION.md). Older checkpoints retain their recorded
participant exclusions and explicit choices.

Review exclusions include model-bearing coding responses, native model-usage
observations and acknowledged team assignments. These participation records
remain distinct from observed editing authors. Configured roles without usage
or an acknowledged assignment do not establish participation. Older reviews can
reconcile the existing retained coding stream against its recorded hash; the
effective exclusions are reported without rewriting the original checkpoint.
When only some default slots need replacement, selection excludes models in
retained slots and requests only the number of unfinished replacements.

Codex `Reconnecting... n/m` error events with retries remaining are progress
within the current native turn. Keep the existing operation handle while the
client retries, including before `thread.started`. A final retry notice, failed
turn or nonzero process exit
retains its failure outcome. Review completion still requires the native
terminal event and validated result. A successful sibling's result is retained
when the other reviewer fails.

Execution and recovery contracts are implemented by
[`resume_argv` and `status`](../src/foundry_runtime/_project_status.py),
[`prepare`](../src/foundry_runtime/_foundry_recovery_preflight.py), and
[`recover_goal`](../src/foundry_runtime/foundry_goal.py).
