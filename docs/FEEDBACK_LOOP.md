# Review feedback and correction

Goal status connects retained review findings to the task, candidate and
supporting artifacts. It also supplies the existing amendment command for
continuing the recorded coding session. The goal controller owns correction,
candidate checks and independent review; status reads their saved results.

## Inspect feedback

Use the installed reporting interpreter and the goal directory returned by the
task invocation:

```powershell
$GoalStatus = & $RuntimePython -I -m foundry_runtime --foundry-project status `
    --status-goal $GoalDirectory | ConvertFrom-Json
$Feedback = $GoalStatus.feedback
$Feedback | Select-Object task, candidate_commit, outcome_status, predecessor
$Feedback.findings | ConvertTo-Json -Depth 10
$Feedback.artifacts | ConvertTo-Json -Depth 10
$Feedback.continuation.status
```

| Field | Content |
| --- | --- |
| `task` | Task-order reference and run ID from the selected manifest |
| `candidate_commit` | Commit examined by the retained reviews |
| `outcome_status` | Recorded goal result |
| `findings` | Deduplicated findings with their reviewer indices or `host` source |
| `predecessor` | Prior outcome referenced by the amendment or continuation |
| `artifacts` | Outcome, manifest, coding evidence, review checkpoint, rounds, usage and delivered artifact references |
| `continuation` | Correction state and the recorded-session invocation when available |

File references include their recorded paths and content hashes. Read checks
and reviews through the review checkpoint and outcome. Hosted status exposes
the same `feedback` value at the top level and under `execution`.
The [review context guide](REVIEW_CONTEXT.md) explains how reviewers read long
task and candidate details through that checkpoint.

## Continue the correction

| `continuation.status` | Next action |
| --- | --- |
| `available` | Inspect the findings and run the supplied amendment command |
| `no_recorded_findings` | Read the completed result; new requirements use explicit operator instructions |
| `review_completion_required` | Complete the retained review through goal recovery; a stopped review without an outcome supplies the recovery invocation |
| `coding_session_required` | Identify the original coding session from retained execution evidence |
| `unavailable` | Resolve the reported missing record or runtime configuration |

For `available`, display the complete PowerShell invocation:

```powershell
$Feedback.continuation.invocation.powershell
```

Copy that command into PowerShell to run the correction. It includes the
recorded interpreter, project entry, task order, `--amend-from` outcome and
`--coding-session`. Retained environment settings are scoped to the invocation.
The `argv` and `environment` fields provide the same inputs for programmatic
callers. The amendment owner checks the task and current candidate when the
command runs.

The controller supplies the original findings to the same coding session,
records a new goal directory and checks the corrected candidate. Automatic
goals retain the selected root outcome as the feedback source and resolve the
final round's execution settings. Keep the new result directory returned by
the command, then inspect its status with the command above. Its `predecessor`
connects the result to the original outcome. The
[delivery baseline](DELIVERY_BASELINE.md) traverses this existing relation to
report both candidates, their checks and reviews.

When review stops before outcome publication, the same feedback view supplies
the task, candidate and checkpoint. `outcome_status` and `artifacts.outcome` are
`null`. Use `continuation.invocation` to complete that review; feedback findings
are populated from the published outcome. The
[live validation report](LIVE_DELIVERY_VALIDATION.md) records this path with real
native review calls and an original-session correction.

## Repeated development scenario

A retained development review identified an incomplete conflict response:
after retries were exhausted, the response omitted the command identifier and
latest state. The public regression reproduces that response contract in a
separate Git project with synthetic paths and identifiers.

1. Candidate A returns a conflict status without `commandKey` and `latest`.
   An executable check fails; both review results retain the same finding.
2. Status presents one finding with both reviewer indices, candidate A and its
   task, artifact references and the recorded-session correction command.
3. The generated command resumes the original coding session. Candidate B
   preserves both fields; the executable check passes and both reviews return
   PASS with no findings.
4. Candidate B's status links to candidate A's outcome. Candidate A's retained
   files remain byte-for-byte unchanged.

[`test_feedback_loop.py`](../tests/test_feedback_loop.py) exercises both PASS
with a P2 finding and BLOCK with a P1 finding. The test uses real temporary Git
commits, local subprocess checks and controlled native protocol responses.
Historical task records supply the scenario and are inspected read-only.
