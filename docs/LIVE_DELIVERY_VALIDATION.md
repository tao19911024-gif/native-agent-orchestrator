# Live delivery, feedback and recovery validation

On 2026-10-23, a retained conflict-response defect was replayed through real
native coding, independent review, original-session correction and recovery.
Recovery reused completed work and feedback reached the original coding
session. The matched review comparison recorded a 0.86% input-token reduction
and a 17.38% output-token increase despite a 35.37% smaller initial request.

The run also exposed a status gap before outcome publication. A stopped review
now exposes its candidate, checkpoint and recorded recovery invocation through
the existing feedback view.

The subsequent [review token analysis](REVIEW_TOKEN_ANALYSIS.md) examines these
native traces and compares a focused reading policy against the same corrected
candidate. It reports separate matched runs and retains the measurements below.

## Workload and comparison

The historical finding concerned a conflict response that omitted `commandKey`
and `latest` after retries were exhausted. An isolated Python project reproduced
the response-construction contract with anonymized inputs. The initial coding
turn recorded the existing failures and committed the deficient candidate for
review. Correction subsequently changed the same response function:

```python
def response(command_key, latest):
    return {"status": 409, "commandKey": command_key, "latest": latest}
```

The task covered exact command identity, latest state including `None`, Unicode,
input preservation, the existing function boundary and retained failure evidence.
Its order contained 5,013 UTF-8 bytes. The project used executable unittest
checks. Native results and usage counters came from installed runtimes and
signed-in clients.

The comparison supplied the same candidate and complete context hash to two
independent reviewers in each group. Coding was configured as `gpt-6.1-sol/high`
and review as `gpt-6-astra/high`. The retained inline runtime and referenced
runtime used matching native review-owner and adapter source after newline
normalization; the request-rendering difference was the context-reference
instruction introduced by the third batch.

## Measured review tokens

Values below are sums for the two reviewers in each group.

| Observation | Inline | Referenced | Change |
| --- | ---: | ---: | ---: |
| Initial request bytes | 27,612 | 17,846 | -35.37% |
| Input tokens | 366,897 | 363,730 | -0.86% |
| Cached input tokens | 292,224 | 290,048 | -0.74% |
| Uncached input tokens | 74,673 | 73,682 | -1.33% |
| Output tokens | 2,854 | 3,350 | +17.38% |

Codex input counters include cached input and accumulate native tool-loop
requests. Cached input is therefore a subset of input, not an additional total.
The complete requirements were read from the referenced checkpoint. Native
traces also included project rules, execution evidence and repeated field reads.
Moving text out of the initial request did not remove those reading costs.

This comparison contains one candidate and two reviews per representation.
The observed token differences support reporting actual counters; they do not
establish a stable percentage saving. The earlier 75.23% result remains a
separate retained-record measurement of initial request bytes.

## Feedback and repeated work

The first review pair returned two BLOCK verdicts for the missing fields. Status
associated those findings with the original task and candidate and supplied
`feedback.continuation.invocation`. Executing that invocation resumed the
original coding session and produced a new committed candidate.

The correction preserved the original assertions and added a focused input
preservation check. All five tests passed. Both independent reviews returned
PASS without findings. The original goal's 26 retained files kept their hashes.
The delivery baseline traversed the existing predecessor relation and reported
two invocations, two candidates, two BLOCK reviews and two PASS reviews.

| Coding stage | Native turns | Input tokens | Cached input | Uncached input | Output tokens |
| --- | ---: | ---: | ---: | ---: | ---: |
| Baseline preparation | 1 | 255,411 | 204,288 | 51,123 | 1,748 |
| Original-session correction | 1 | 288,057 | 268,544 | 19,513 | 2,245 |

The continuation reused one session but performed a new coding turn. Its higher
cached-input share and lower uncached-input count are observations of these
different stages, not a fresh-session comparison. The existing amendment path
also supplied a Claude Opus manager for this original single-round API goal.
Its two calls recorded 8 input tokens, 36,928 cache-creation tokens, 139,290
cache-read tokens and 2,980 output tokens. Those Claude fields are reported
separately from the Codex counting convention.

## Recovery and the status correction

A controlled interruption occurred before the second reviewer's native
admission. The first reviewer completed a real call. Recovery then issued only
the second reviewer's call; all 10 files belonging to the first reviewer kept
their hashes. Coding was not repeated.

Before the status correction, this stopped goal had no `outcome.json`, so status
returned the stopped state without a feedback view. The operator had to assemble
the recovery command from retained task information.

The existing feedback projection now reads the complete review checkpoint when
the controller has stopped before publishing an outcome. It returns:

- The recorded task and candidate.
- The manifest, review checkpoint and coding-evidence references.
- `outcome_status: null` and `artifacts.outcome: null` until publication.
- `continuation.status: review_completion_required` and the existing recovery
  invocation with its recorded interpreter and project entry.

A second real review of the corrected candidate verified this command directly.
The generated invocation completed only the unfinished reviewer, preserved the
first reviewer's 10 files and returned two PASS verdicts. Project hooks recorded
five passing tests and `project_acceptance: accepted`. Repeating recovery after
completion created zero additional review receipts and changed zero goal files.

The final selected baseline contains the original two-invocation correction
chain plus this independent verification: three invocations, two BLOCK reviews,
four PASS reviews and one project acceptance. The first two invocations retain
their original `not_evaluated` acceptance observations.

## Evidence and verification

[Machine-readable measurements](validation/live-delivery-2026-10-23.json)
include the comparison, all six Codex execution stages, Claude coordination,
recovery file checks and delivery summary. Counts describe native invocations;
each can contain several provider requests. Raw transcripts, session identities
and local paths remain in private experiment evidence.

The installed regression passed 169 tests, including all 122 public CI tests.
The recovery regression now invokes the command generated by status after an
interrupted review. It also verifies that an active controller does not expose
that stopped-review continuation and that completed review files remain intact.

The correction extends the existing feedback projection and recovery command
formatter. Execution and checkpoint ownership remain in the existing goal and
native review owners. Reverting the source change restores the prior status
presentation; existing task runtime selections remain recorded in their manifests.
