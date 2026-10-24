# Scoped project reading

The project execution prompt now directs workers to locate task-relevant
sections, reuse unchanged material already received, and retrieve missing ranges
after truncation. Two repetitions of the reviewer-defect task used **16.06% fewer
total tokens** and **9.23% fewer uncached input tokens** than the two original
3,000-token controls. Repeated guidance reads fell from **19 to 9**.

## Selected repetition

The [four-run output comparison](WORKER_TOKEN_ANALYSIS.md) retained every native
response, tool call and candidate acceptance result. Its initial-reading audit
identified 47 overlapping requests before the first edit: 22 guidance reads,
15 source reads, nine test reads and one other read. Thirty-six overlaps followed
an earlier retained truncation marker.

Workers repeatedly bundled contribution, source-authority, release and storage
guidance with large source reads. They then requested material from the same
files again. The project prompt previously told workers to follow rule references
before editing without distinguishing the applicable sections or the combined
output of an enclosing tool call.

Responses following overlapping initial-read calls accounted for 1,219,425
tokens across the four runs. Responses following repeated deletion attempts
after approval rejection accounted for 404,100 tokens. These windows include
the complete input context and generated response. They identify expensive
repeated-work sequences; the controlled repetitions below measure the resulting
task usage.

## Implementation and ownership

[`ProjectExecution.prompt`](../src/foundry_runtime/_project_execution.py) remains
the owner of descriptor-project execution guidance. Its reading paragraph now
asks the worker to:

- Reuse unchanged rule entries already present in context.
- Locate relevant headings or symbols before reading referenced sections.
- Keep combined returned text within the tool-call output budget and preserve
  large results in task evidence.
- Retrieve the required missing ranges after truncation.
- Expand coverage when evidence is missing or project rules require it.

The task body, rule references, project authority, checks and acceptance remain
with their existing owners. The change adds 680 UTF-8 bytes to this task's
rendered prompt. It adds no retrieval service, cache, hook, dependency or reading
gate. Worker output remains 3,000 tokens.

## Same-task verification

C1 and C2 repeated the [original task](validation/worker-output-task-2026-10-23.md)
from the same a95 source and initial Git tree. The task bytes, dependency versions,
Codex 0.159.3 client, `gpt-6.1-sol` model, high reasoning effort and launch settings
matched the A1 and A2 controls. The candidate dispatcher differed from the a96
wheel in only `_project_execution.py`; all 91 installed source/data files matched
the candidate checkout.

The retained user messages matched after normalizing operation directories, run
labels, project IDs and the changed reading paragraph. The original task and the
remaining project-authority text were identical. A1 and A2 are retained controls
from the preceding experiment; C1 and C2 ran sequentially afterward.

Each candidate passed the same 46 common independent checks. Including their
additional regression cases, C1 passed 77 checks and C2 passed 71: **148 passed,
zero failed or skipped**. Each candidate's 91 installed files matched its source.
The dispatcher passed **126 installed-package checks**, covering project launch,
continuation, historical replay, release selection, review context and feedback.
Original failures and each worker's additional test results remain in the local
experiment evidence.

## Native usage

| Run | Guidance | Total tokens | Uncached input | Model requests | Initial overlapping reads | Overlapping guidance reads |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| A1 | Previous | 3,002,207 | 97,736 | 39 | 15 | 12 |
| A2 | Previous | 1,851,805 | 84,452 | 27 | 22 | 7 |
| C1 | Scoped | 1,778,218 | 80,366 | 27 | 16 | 4 |
| C2 | Scoped | 2,296,067 | 85,014 | 33 | 17 | 5 |

| Aggregate measurement | Previous guidance | Scoped guidance | Change |
| --- | ---: | ---: | ---: |
| Total tokens | 4,854,012 | 4,074,285 | -16.06% |
| Input tokens | 4,826,540 | 4,046,212 | -16.17% |
| Cached input | 4,644,352 | 3,880,832 | -16.44% |
| Uncached input | 182,188 | 165,380 | -9.23% |
| Output tokens | 27,472 | 28,073 | +2.19% |
| Model requests | 66 | 60 | -9.09% |
| Tokens before first edit | 1,533,008 | 1,305,867 | -14.82% |
| Initial overlapping reads | 37 | 33 | -10.81% |
| Overlapping guidance reads | 19 | 9 | -52.63% |
| Overlaps after a retained truncation marker | 28 | 24 | -14.29% |

Usage is reconciled by native response ID to each execution receipt. Cached
input is included in input; reasoning output is included in output. The totals
cover the worker's diagnosis, implementation, verification, storage disposition,
commit and report. Setup, coordinating work and independent local acceptance
commands are recorded separately.

The reading audit measures literal `Get-Content` range overlap before the first
edit. It counts requested ranges; searches and post-edit checks use separate
commands. All audited initial `Get-Content` reads in these four runs were classified.

## Decision and remaining repetition

Retain the scoped guidance. Both candidate runs reduced repeated guidance reads
relative to either control. Aggregate uncached input and total usage also fell.
The smaller guidance footprint is the clearest observed improvement.

Source overlaps increased from 10 to 13, and test-file overlaps increased from
7 to 11. Tool rounds containing any overlap increased from 16 to 18. Source and
test retrieval therefore remain separate optimization candidates. Total usage
ranged from 1.78 to 2.30 million in the new runs; the four-run results support
the measured aggregate comparison rather than a fixed per-task saving.

Keep the 3,000-token worker default and existing explicit project settings.
This source change was exercised through an isolated candidate dispatcher.
Production selection, active installations and recorded recovery entry points
were preserved. Rollback of the source change uses its Git revert; installation
and runtime selection follow the existing [release procedure](RELEASING.md).

[Machine-readable results](validation/worker-reading-2026-10-24.json) contain the
per-run usage, reading categories, comparison controls and evidence hashes.
Raw sessions, candidate commits, original failures and storage inventories stay
with the local experiment owner under the [storage policy](STORAGE_RETENTION.md).
