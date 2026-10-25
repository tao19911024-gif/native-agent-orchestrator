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

## Correct a reviewed candidate

Goal status includes `feedback`: the original task, reviewed candidate,
deduplicated findings, artifact references and a recorded-session amendment
command. Use that command for completed review feedback, then retain the new
goal directory returned by the correction. The
[feedback guide](FEEDBACK_LOOP.md) documents the status fields, continuation
states and a repeated conflict-response correction scenario.

## Reconcile interrupted execution

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

Codex `Reconnecting... n/m` error events with retries remaining are progress
within the current native turn. Keep the existing operation handle while the
client retries. A final retry notice, failed turn or nonzero process exit
retains its failure outcome. Review completion still requires the native
terminal event and validated result. A successful sibling's result is retained
when the other reviewer fails.

Execution and recovery contracts are implemented by
[`resume_argv` and `status`](../src/foundry_runtime/_project_status.py),
[`prepare`](../src/foundry_runtime/_foundry_recovery_preflight.py), and
[`recover_goal`](../src/foundry_runtime/foundry_goal.py).
