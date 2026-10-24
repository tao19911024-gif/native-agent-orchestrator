# Coding-worker output comparison

The worker default remains **3,000 tool-output tokens**. Native output settings
reduced repeated initial reads in both pairs, but total usage moved in opposite
directions between pairs and aggregate uncached input increased. Existing explicit
project settings continue to select their recorded behavior.

## Task and controls

This experiment replayed two confirmed Foundry defects from `v0.1.0a95`, commit
`3abb2f21f3f6008a81c20ef0f343759fc5cd6bd0`: review-group members inherited worker
settings instead of the reviewer role, and malformed review values raised
`AttributeError` before schema validation. Each worker repaired the full repository,
added regression coverage, installed its candidate and committed it. The
[task instructions](validation/worker-output-task-2026-10-23.md) specify the same
allowed files and acceptance contract for every run.

Four fresh sessions ran sequentially as **A1, B1, B2, A2**. A used
`FOUNDRY_TOOL_TOKENS=3000`; B used `0`, which omits the native override. The client
was Codex 0.159.3 with `gpt-6.1-sol` and high reasoning effort. Its retained model
metadata selected a 10,000-token tool-output policy when the override was absent.
The dispatcher was the installed a96 runtime. Each run used one coding worker;
there were no model-based manager or review calls.

All four source trees, task files and dependency versions matched before work
started. Rendered user messages matched after substituting only the isolated
directory label and project ID. Other launch settings matched, including the
reading policy and disabled optional code index. Each candidate used its own
installation. Runtime selection and consuming-project entry points were unchanged.

Usage comes from native `token_usage_record` entries, deduplicated by response ID
and reconciled with the execution receipt. Input includes cached input; output
includes reasoning output. Neither subset is added again to the total. Setup,
independent local tests and the coordinating conversation are outside the worker
measurements. Worker diagnosis, implementation, tests, storage disposition,
commit and final report are included.

## Completed work

Every candidate passed the same **46 independent checks**: 39 existing checks
for launch settings, acceptance feedback and review context, plus seven held-out
checks for the reported defects. The held-out checks failed seven times on a95
and passed seven times on the accepted a96 installation before the experiment.

The independent candidate runs passed 90, 72, 74 and 64 checks respectively,
including their additional regression cases: **300 passed, zero failed or
skipped**. Each installed candidate matched all 91 packaged source/data files.
Diff inspection confirmed the same four implementation owners and preservation
of existing assertions. Additional worker-selected tests had historical-environment
skips; those are separate from the common independent acceptance result.

## Native usage

| Run | Output setting | Input | Cached input | Uncached input | Output | Total | Model requests |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A1 | 3,000 | 2,987,080 | 2,889,344 | 97,736 | 15,127 | 3,002,207 | 39 |
| B1 | Native | 1,831,799 | 1,732,992 | 98,807 | 14,152 | 1,845,951 | 22 |
| B2 | Native | 2,082,609 | 1,996,032 | 86,577 | 14,097 | 2,096,706 | 25 |
| A2 | 3,000 | 1,839,460 | 1,755,008 | 84,452 | 12,345 | 1,851,805 | 27 |

| Aggregate, two workers per setting | 3,000 | Native | Native change |
| --- | ---: | ---: | ---: |
| Input tokens | 4,826,540 | 3,914,408 | -18.90% |
| Cached input tokens | 4,644,352 | 3,729,024 | -19.71% |
| Uncached input tokens | 182,188 | 185,384 | +1.75% |
| Output tokens | 27,472 | 28,249 | +2.83% |
| Total tokens | 4,854,012 | 3,942,657 | -18.78% |
| Model requests | 66 | 47 | -28.79% |

The A1/B1 pair favored native output by **38.51%** total tokens. In the reversed
B2/A2 pair, native output used **13.22% more**. Before storage disposition began,
the second pair still used 1,345,473 native tokens versus 1,304,346 control tokens,
an increase of 3.15%. The reversed result is therefore present before cleanup
and final reporting.

## Repeated reads and truncation

The read audit follows literal `Get-Content` requests before the first edit.
A repeated read requests a line range overlapping an earlier request to the
unchanged file. Searches, later test logs and post-edit verification are excluded.
This counts requested overlap; it does not assume every requested line reached
the model.

| Initial-reading measurement | A1 | B1 | B2 | A2 |
| --- | ---: | ---: | ---: | ---: |
| File/range read requests | 42 | 32 | 30 | 52 |
| Requests overlapping an earlier range | 15 | 5 | 5 | 22 |
| Tool rounds containing overlapping reads | 6 | 3 | 3 | 10 |
| Overlaps following an earlier retained truncation marker | 12 | 5 | 3 | 16 |
| Model requests before first edit | 16 | 6 | 8 | 13 |
| Tokens before first edit | 871,426 | 307,179 | 430,546 | 661,582 |

Native output reduced overlapping requests from **37 to 10** and initial-reading
tokens from **1,533,008 to 737,725**. A1 repeatedly requested contribution,
source-authority, release and storage documents, then narrowed its reads. A2
also revisited unchanged guidance and source ranges. Native runs still reread
transport, launch and review material after large combined outputs.

Retained outputs carried eight truncation markers in the control runs and
thirteen in the native runs. These counts describe a different boundary from
the client's later history truncation. A command or code-mode result can be
truncated before retention; the client can further truncate that retained
result when constructing model history. Upstream
[history processing](https://github.com/openai/codex/blob/main/codex-rs/core/src/context_manager/history.rs)
and [output truncation](https://github.com/openai/codex/blob/main/codex-rs/utils/output-truncation/src/lib.rs)
describe these distinct operations. Retained character counts and marker counts
are therefore reported separately from native token usage and follow-up reads.

## Where the tokens went

Phase boundaries use the first edit request and the first storage-disposition
command in each retained trace. Every response belongs to exactly one phase.

| Phase, both runs combined | 3,000 tokens | Native tokens | 3,000 requests | Native requests |
| --- | ---: | ---: | ---: | ---: |
| Before first edit | 1,533,008 | 737,725 | 29 | 14 |
| Repair and verification before storage disposition | 1,860,655 | 1,614,810 | 22 | 18 |
| Storage disposition, commit and final report | 1,460,349 | 1,590,122 | 15 | 15 |

Larger output reduced initial follow-up reads, then left more context in later
requests. With the same 15 closeout requests per setting, native output consumed
more tokens in that phase. Across the whole task, average input per request was
73,129 tokens for the control and 83,285 for native output. The aggregate saving
comes from fewer requests, principally repeated processing of cached history;
uncached input and generated output did not decrease.

## Configuration decision

Retain the a96 worker default of 3,000 and preserve existing explicit project
values. The two paired totals disagree, while the reduction in initial rereads
is consistent. The next bounded optimization target is the task's initial
guidance and source-reading scope, using the existing reading instructions.
The [scoped-reading follow-up](WORKER_READING_ANALYSIS.md) implements that target
through the existing project prompt and records two further same-task runs.
The evidence does not support a universal worker switch based on this task.

Reviewer defaults retain their separately measured native setting. Manager
defaults retain 3,000. Historical tasks continue with captured settings. This
experiment changes documentation and measurement records; there is no runtime
release or configuration migration.

[Machine-readable results](validation/worker-output-2026-10-23.json) include
per-run usage, phase boundaries, acceptance counts and evidence hashes. Raw
sessions, candidate commits, original failures and maintenance inventories remain
with the local experiment owner under the [storage policy](STORAGE_RETENTION.md).
