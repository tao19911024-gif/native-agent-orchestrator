# Command reference

The main entry is `python -m foundry_runtime`; the installed `foundry` command
calls the same parser. Commands below use `$RuntimePython` for the installed
environment's absolute interpreter path and `$ProjectConfig` for its descriptor.

```powershell
& $RuntimePython -I -m foundry_runtime --help
& $RuntimePython -I -m foundry_runtime.recovery --help
```

## Project setup and observation

| Operation | Options |
| --- | --- |
| Inspect a Git workspace | `--foundry-project inspect --worktree $Workspace` |
| Create a descriptor | `--foundry-project register --worktree $Workspace --project-config $ProjectConfig` |
| Inspect a descriptor | `--foundry-project check --project-config $ProjectConfig` |
| Read project task history | `--foundry-project status --project-config $ProjectConfig` |
| Read one task's history | Add `--tag TASK1` to project status |
| Read an exact dispatch receipt | `--foundry-project status --status-dispatch $PackagePath` |
| Read an exact goal | `--foundry-project status --status-goal $GoalDirectory` |

Setup and status operations perform local inspection. `register` writes the
descriptor; task dispatch is a separate invocation. Exact dispatch and goal
status use their retained paths directly, without `--project-config`.

## Execute one task

```powershell
& $RuntimePython -I -m foundry_runtime --project-config $ProjectConfig `
  --order $TaskOrder --tag TASK1 --provider codex --launcher exec
```

| Option | Meaning |
| --- | --- |
| `--order` | Path to the task's UTF-8 Markdown instructions |
| `--tag` | Task key used in execution records and continuations |
| `--provider` | `codex` or `claude` for new work |
| `--launcher` | `exec` or `app-server` for Codex; `claude` for Claude |
| `--coding-model`, `--coding-effort` | Explicit native model assignment; see [configuration](CONFIGURATION.md#provider-selection) |
| `--project-preflight-input` | Path to the project's JSON invocation inputs |
| `--task-deadline` | Absolute ISO-8601 deadline including a timezone |

Descriptor-based Codex dispatch defaults to `app-server`. The examples select
`exec` explicitly to demonstrate terminal execution. `--provider claude` selects
the Claude transport. The runtime prints the log, report, session, and follow-up
binding for the invocation.

## Implement and independently review

```powershell
& $RuntimePython -I -m foundry_runtime --project-config $ProjectConfig `
  --order $TaskOrder --tag TASK1 --provider codex --launcher exec `
  --foundry-goal --team-profile none --goal-evidence $GoalDirectory
```

`--foundry-goal` coordinates coding, candidate verification, and independent
review. `--team-profile none` selects direct coding with review coordinated by
the goal controller. `adaptive` selects the native team path and manager.
`--goal-evidence` names the exact directory retained for this goal.

To inspect staffing before launching the same new goal, add `--print-staffing`.
It resolves models through the installed clients and returns before admission,
coding, or review. `--task-kind` and `--compute-profile` select staffing; their
accepted values are listed in `--help`.

For a committed range with an existing task order, use `--foundry-review` with
`--review-base`, `--review-head`, and `--goal-evidence`. This selects independent
review of that candidate. See the [coding and review example](../examples/REVIEW.md).

## Continue and recover

| Operation | Options |
| --- | --- |
| Continue a completed direct session | `--project-config $ProjectConfig --resume $SessionId --worktree $Workspace --tag TASK1 --launcher exec --prompt-file $PromptFile` |
| Inspect goal recovery | `--project-config $ProjectConfig --order $TaskOrder --foundry-goal --recover-goal $GoalDirectory --recovery-preflight` |
| Continue an unfinished goal | Use the recorded goal invocation with `--recover-goal` and `--prompt-file` |
| Read usage records | `--foundry-usage $GoalDirectory` or paths to retained manifests and receipts |

Use the original provider and interpreter for ordinary continuation. The
[recovery guide](RECOVERY.md) explains retained identity, interrupted execution,
and goal recovery. The main CLI starts execution; `foundry-recovery` produces
or reconciles recovery records through its own documented options.

## Hosted execution

Append `--hosted $LaunchDirectory` to a task invocation to use Windows hosted
execution. `$LaunchDirectory` identifies one launch. Read that launch with
`--hosted-status $LaunchDirectory` and retain the returned binding. The
[operating environment](LIMITATIONS.md) describes host requirements and delivery.

The CLI contract is owned by [`main`](../src/foundry_runtime/cli.py),
[`add_arguments` and `handle_mode`](../src/foundry_runtime/foundry_entry.py), and
the [project command handler](../src/foundry_runtime/_foundry_project.py).
