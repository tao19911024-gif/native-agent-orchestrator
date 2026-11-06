# Task status, usage and delivery

The existing status and usage commands read retained task records. Summary mode
provides a compact view; the full report retains the linked observations.

## Inspect current work

```powershell
& $RuntimePython -I -m foundry_runtime --foundry-project status `
  --status-goal $GoalDirectory --summary
```

The summary contains the recorded phase, waiting role, queued operator-input
count, runtime versions, evidence paths and next command. A current worker is
joined through its package's exact goal directory, run ID and current journal
attempt. Completed attempts, another goal with the same tag and superseded
attempts are excluded from active work. Historical goals without this binding
retain their original observations.

The active round owns the worker package binding for a managed goal. The
manager's native conversation lock owns its call receipts. A settled outcome
with no continuation action displays the recorded outcome status as its phase.

`evidence.worker_live_event_log` is the absolute path for the uniquely bound
current worker. Claude writes stream events to the matching `.claude.jsonl`;
Codex app-server and exec use the recorded `.log`. The path is available before
the writer creates the file. A watcher reads this field after launch and follows
that file instead of predicting numbered continuation names. The field is `null`
when no unique current execution is recorded. The full dispatch observation
exposes the same path as `live_event_log`.

The summary's `execution` identifies the observed intent, attempt and frozen
manager assignment when recorded. `operator_inputs` shows the queued input and
its native manager request and receipt once admitted. A live request carries
the active execution identity. A request consumed after the worker returns
carries the settled attempt from its coding receipt. A response confirms delivery to the
manager. Worker implementation is evaluated from the subsequent execution and
candidate evidence.

For a deployment using the release selector, include its selection file:

```powershell
& $RuntimePython -I -m foundry_runtime --foundry-project status `
  --status-goal $GoalDirectory --runtime-selection $SelectionFile --summary
```

`runtime.controller` and `runtime.worker` describe recorded execution.
`runtime.observer_python` identifies this inspection command. `runtime.selected`
describes new-task selection from the supplied file. Inspecting a selection does
not change a task's runtime.

`next_action=wait` retains the current execution. Feedback continuation uses the
existing recorded invocation, including its environment and PowerShell form in
`next_invocation`. An input request exposes the original recovery invocation with
an answer-file argument. Fill that file with the operator response and use the
existing [recovery preflight](RECOVERY.md#recover-a-goal) before execution.

## Save a usage baseline

Before the continuation to be measured, save the full report outside disposable
task storage:

```powershell
$ReportDirectory = Join-Path (Split-Path $GoalDirectory -Parent) 'reports'
New-Item -ItemType Directory -Path $ReportDirectory -Force | Out-Null
$SavedReport = Join-Path $ReportDirectory 'before-continuation.json'
& $RuntimePython -I -m foundry_runtime --foundry-usage $GoalDirectory |
  Set-Content -LiteralPath $SavedReport -Encoding utf8
```

After the continuation, compare the same selected task chain:

```powershell
& $RuntimePython -I -m foundry_runtime --foundry-usage $GoalDirectory `
  --usage-since $SavedReport --summary
```

The comparison uses the saved report's native identities and counters. Reading a
mutable predecessor directory again would observe its current contents rather
than the earlier baseline. The result retains the baseline file's SHA-256.

| Field | Meaning |
| --- | --- |
| `by_role_and_backend` | Counters grouped by provider and observed roles |
| `tokens.uncached_input_tokens` | Codex input less cache reads; Claude ordinary input |
| `observed_native_invocations` | Distinct retained coding turns and native call attempts |
| `observed_responses` | Responses with retained per-response evidence |
| `reading.identical_call_output_repeats` | Repeated observed read command/output pairs |
| `reading.truncated_read_outputs` | Read outputs carrying a truncation observation |
| `reused_units`, `added_units`, `changed_units` | Reused identity, new identity and cumulative-growth groups |

Same-session cumulative snapshots contribute their growth once. Sessions shared
across roles remain one shared row. Native invocations and model responses are
separate counts: a client invocation can contain multiple model requests.
Unknown counters remain `null`, with unobserved-unit counts and coverage. Reading
signals describe observed tools; they do not allocate token cost to a file read.
Provider token rows remain separate from actual account charges.

To inspect current totals rather than a change, use
`--foundry-usage $GoalDirectory --summary`. Keep the full report when another
comparison baseline is required.

## Read delivery levels

Goal status exposes `delivery.levels`; full usage records expose the same levels
under each invocation's delivery:

| Level | Authority and evidence |
| --- | --- |
| `execution` | Goal outcome and retained coding evidence |
| `candidate_review` | Candidate commit and independent review verdicts |
| `project_acceptance` | Project-owned acceptance status and evidence |

`candidate_verified` records successful candidate verification.
`project_acceptance.status=not_evaluated` remains visible until the project records
its acceptance. Artifact delivery retains its artifact references and project
acceptance status. These views use the existing outcome; they add no new gate.
