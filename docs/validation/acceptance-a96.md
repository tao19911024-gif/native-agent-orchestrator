# a95 acceptance follow-up

The independent a95 acceptance identified three runtime defects. This release
corrects their existing owners and adds installed-package regression coverage.

| Finding | Resolution | Verification |
| --- | --- | --- |
| Review-group members used worker output defaults | The review group passes the reviewer role to launch capture; backend identity uses the captured settings | A member receives native Codex output settings while a worker retains 3,000 tokens |
| Git observations without a task deadline could wait indefinitely | The existing Git observation owner drains both output streams and stops after 300 seconds without output | A silent child produces a named block and exits; a progressing child runs beyond one inactivity interval |
| Non-object verdicts raised AttributeError | Response shape validation precedes field-receipt inspection | Null, list, string and numeric responses produce the schema error |
| Legacy manager recovery fixture omitted its outcome file | The execution fixture persists the outcome that real execution publishes | All manager-presence, parent-layout and task-kind combinations retain their original assertions |

Git observations with an explicit task deadline retain that deadline. Without
one, output on stdout or stderr resets the 300-second inactivity interval. This
is an observable-output bound: a silent operation lasting five minutes is
reported as stalled even if internal CPU or filesystem activity continues.
The runtime terminates and reaps the owned Git process before returning the block.

Legacy manager assignment lookup reads the first 1 MiB of the native log. An
assignment outside that prefix remains unmatched; the refusal names the bound.

Capacity retry attaches the predecessor dispatch package only when the resume
call fails before producing its own package. Errors while inspecting the prior
intent or validating prior completion remain errors of that prior evidence.
They do not identify a newly attempted dispatch. Task-deadline stops carry no
predecessor package. The a95 behavior is retained.

Rollback selects a runtime for new tasks. Tasks with a95 review allocations keep
their recorded a95 runtime; a94 cannot read the `review_field_reads` contract.
Existing task environments and recorded launch settings remain unchanged.

Worker output consumption remains unmeasured in a paired real-task experiment.
Workers retain the 3,000-token default. This release corrects reviewer routing
and reports no worker token-saving result.
