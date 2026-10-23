# Review context references

Independent review uses the complete candidate context saved in
`review-checkpoint.json`. Long context fields are supplied through references
to that checkpoint, so each review starts with candidate identity, checks and
feedback alongside direct access to the supporting material.

## Input delivery

For new checkpoints, the review request builder examines four fields:
`original_order`, `changed_files`, `project_record` and `execution_decision`.
When a field's UTF-8 JSON representation exceeds 4,096 bytes, the request
replaces its inline value with a JSON pointer under `context_fields`.
`context_reference` supplies the checkpoint path and SHA-256. Short fields
remain inline; requests with no long fields retain their existing rendering.

The complete values remain under the checkpoint's `context` object. The
4,096-byte threshold selects presentation; it imposes no task input limit.
Candidate and base commits, project checks, closure results, prior findings
and the full context hash remain in the request.

Read the complete task requirements and any referenced execution decision
before judging a candidate. Read the relevant project details and changed-file
list from their indicated fields. Extract those fields from the JSON document
instead of printing the whole checkpoint and its coding usage observations.
The original base-to-candidate review scope remains in effect.

## Status and recovery

Use the existing goal status command to locate the checkpoint:

```powershell
$GoalStatus = & $RuntimePython -I -m foundry_runtime --foundry-project status `
    --status-goal $GoalDirectory | ConvertFrom-Json
$GoalStatus.feedback.artifacts.review_checkpoint
```

For an interrupted review before an outcome is available, the goal manifest's
`review_checkpoint` supplies the same reference. Continue through the existing
`--recover-goal` command described in [recovery](RECOVERY.md).

New checkpoints record `context_delivery: reference-v1`. Existing checkpoints
retain their original request format. The goal's review request builder selects
the representation before native admission, so the stored request and recovery
reader share the same envelope. `packet_sha256` continues to identify the full
checkpoint context, including referenced fields. Reviewers echo that value.
Successful review results remain reusable when another reviewer is interrupted.

## Measured retained-record comparison

The comparison uses the retained first-use and development selections from the
[delivery baseline](DELIVERY_BASELINE.md), rendered with the same candidate
context and response contract before and after this change.

| Selection | Previous bytes per reviewer | Referenced bytes per reviewer | Reduction |
| --- | ---: | ---: | ---: |
| First-use candidate | 13,309 | 13,309 | 0% |
| Development candidate | 49,147 | 12,174 | 75.23% |

The development selection moves the task text and changed-file list to
checkpoint references. The two independent review inputs together carry
73,946 fewer bytes. These measurements cover initial rendered request bytes;
subsequent evidence reads and model usage remain in native execution records.
Historical task records retain their original contents and runtime selection.

Installed tests verify both Codex and Claude rendering, complete field
retrieval, full-context verdict binding and unchanged short/legacy requests.
A local subprocess regression exercises the goal review and recovery owners:
one reviewer succeeds, the other is interrupted, and recovery calls only the
unfinished reviewer using the same checkpoint reference.
