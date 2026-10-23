# Review token analysis and evidence reading

The 2026-10-23 conflict-response replay measured a 20.44% reduction in input
tokens after adding focused reading guidance to the existing review request.
Uncached input fell 12.74% and output fell 11.24%. Eight independent native
reviews covered the same corrected candidate and complete context: four using
the prior request and four using the new reading policy. All returned PASS.

A subsequent comparison retained that reading policy and removed the default
3,000-token Codex tool-output override. Input fell another 29.91% against its
matched control, and output fell 40.44%; uncached input increased 1.86%. The
[native output follow-up](#native-tool-output-follow-up) records these separate
runs and their remaining tradeoff.

## Sources of context growth

The diagnosis examined the eight review sessions retained by the
[live delivery validation](LIVE_DELIVERY_VALIDATION.md). They contained 44
provider requests and 1,502,870 input tokens. Initial requests accounted for
224,014 tokens; subsequent tool-loop requests accounted for 1,278,856, or 85.09%.
Those subsequent requests include necessary review work as well as repeated
context. They identify the main area to optimize, not a removable token total.

| Source | Recorded observation | Effect |
| --- | --- | --- |
| Repeated tool-loop input | Initial inputs ranged from 26,952 to 29,413 tokens; later inputs grew to 35,130–41,632 | Each additional model request carries the accumulated review context |
| Repeated evidence reads | Requirements were extracted again; tests and implementation were printed after their diffs; candidate identity and file inventories were queried repeatedly | Already available material returns to the conversation |
| Broad log searches | Searches matched both actual test results and commands or narrative embedded in execution logs | Reviewers received unrelated historical text before locating a short result |
| Guidance and truncation recovery | Long review guidance was followed by overlapping section reads; two historical tool-result messages contained truncation notices | Additional reads and model rounds were needed to recover missing material |

The initial native input also contains client instructions, project rules,
skill discovery and memory context. The runtime's rendered request is only one
part of that input. The selected optimization reduces unnecessary rounds over
this existing context while preserving required project and review instructions.

## Selected change

The existing goal checkpoint records `review_read_policy: 1`. Its request builder
passes the policy to the native prompt renderer. The guidance asks reviewers to:

1. Use supplied inline fields and read complete required references.
2. Group independent small reads and candidate checks within the output capacity.
3. Read long guidance and logs in bounded sections and retrieve missing sections
   after truncation.
4. Reuse inspected content at the same revision and investigate uncovered criteria,
   changed dependencies or contradictory observations.
5. Record concise coverage and return the verdict once the full review is complete.

Normal review, interrupted-review recovery and runtime-upgrade history use the
same stored policy. Historical checkpoints retain their original request bytes.
The complete context hash and verdict contract remain unchanged. This extends
the existing prompt and checkpoint path without adding an executor, evidence
store, reading quota or model call.

## Matched native comparison

The workload is the corrected conflict-response candidate from the prior replay.
It requires status 409, exact `commandKey`, supplied `latest` including `None`,
Unicode preservation, unchanged inputs, retained assertions and the existing
function boundary. Its recorded verification contains five passing tests.

Groups ran in A-B-B-A order, with two independent reviewers in each group.
A used the installed prior runtime; B used the installed candidate runtime.
All groups used the same candidate, complete checkpoint file and context hash,
configured `gpt-6-astra/high`, native adapter, review owner and user-configuration
digest. Each review used a distinct native session.

| Pair | Prior input tokens | Selected input tokens | Prior model requests | Selected model requests |
| --- | ---: | ---: | ---: | ---: |
| A then B | 440,887 | 354,279 | 12 | 10 |
| B then A | 396,418 | 311,870 | 11 | 9 |

Aggregate values below cover four reviews per representation.

| Measurement | Prior request | Selected policy | Change |
| --- | ---: | ---: | ---: |
| Initial request bytes | 62,692 | 66,464 | +6.02% |
| Input tokens | 837,305 | 666,149 | -20.44% |
| Cached input tokens | 671,360 | 521,344 | -22.35% |
| Uncached input tokens | 165,945 | 144,805 | -12.74% |
| Output tokens | 7,186 | 6,378 | -11.24% |
| Model requests | 23 | 19 | -17.39% |
| Tool-result rounds | 19 | 15 | -21.05% |
| Visible tool-output bytes | 211,046 | 174,724 | -17.21% |

The selected policy adds reading guidance to the initial request. Total input
falls because fewer tool-loop requests carry the accumulated context. Native
traces show that reviewers reused matching test summaries and focused candidate
evidence. Both prior P1 findings were checked, and all eight contract requirements
were covered in every verdict.

Some repetition remained: one selected-policy session produced a truncated
output and recovered its requirements; another repeated a combined read. The
observed gain is fewer rounds across both pairs, not complete elimination of
duplicate reads. These percentages describe this candidate and these eight
reviews. Cached input is included in total input; token reductions are reported
separately from account charges.

## Verification and evidence

[Machine-readable measurements](validation/review-token-2026-10-23.json) contain
per-review counters, per-request input counts, aggregates and historical diagnosis.
Provider request counts come from native token events. Tool-result rounds count
returned tool messages; one such message can contain several commands. Raw
conversations, paths and session identities remain in local experiment evidence.

Installed regression passed 154 tests with zero failures or skips. Tests cover
complete reference reconstruction for both native backends, historical prompt
hashes, full-context verdict identity, policy persistence, and recovery through
the status-generated command. Completed reviewer files remain unchanged; only
the unfinished reviewer runs. Upgrade-history reconstruction uses the same
policy. All 91 packaged runtime files match the tested installation.

New goals select the policy through their checkpoint. Existing tasks keep their
recorded runtime and request. Source rollback restores the previous default for
new goals; retained task interpreters preserve recovery behavior.

## Native tool-output follow-up

After the reading-policy change, all four selected-policy traces reused the
existing test summaries instead of broadly searching execution logs. The
remaining repeated reads involved requirements and review guidance. The runtime
still injected `tool_output_token_limit=3000` on every default Codex launch. One
required guidance read reported 3,658 tokens before other evidence was added.
The command's `max_output_tokens` and the native outer tool-result setting are
separate controls. A complete raw command result in the retained transcript does
not establish that its entire content reached the model.

The selected change uses the existing launch-context owner: new Codex launches
default `FOUNDRY_TOOL_TOKENS` to zero and omit the native override. The client
uses its selected settings. Explicit positive values and historical frozen
settings continue to produce their recorded argument. Claude output controls,
reading-policy text and evidence formats retain their existing behavior.

The follow-up used the same corrected conflict-response candidate, complete
checkpoint, model configuration and A-B-B-A arrangement. Both sides used reading
policy 1. All eight rendered review requests were byte-identical; configuration
digests matched, and `_launch_context.py` was the only differing installed source
module. The invocations confirmed `tool_output_token_limit=3000` for A and no
override for B. All eight independent reviews returned PASS and covered the
eight contract requirements and both resolved P1 findings.

| Pair | Prior input tokens | Native settings input tokens | Prior model requests | Native settings model requests |
| --- | ---: | ---: | ---: | ---: |
| A then B | 307,346 | 244,774 | 9 | 7 |
| B then A | 386,275 | 241,385 | 11 | 7 |

Aggregate values cover four reviews per representation.

| Measurement | 3,000-token override | Native settings | Change |
| --- | ---: | ---: | ---: |
| Initial request bytes | 66,464 | 66,464 | 0% |
| Input tokens | 693,621 | 486,159 | -29.91% |
| Cached input tokens | 568,320 | 358,528 | -36.91% |
| Uncached input tokens | 125,301 | 127,631 | +1.86% |
| Output tokens | 6,768 | 4,031 | -40.44% |
| Model requests | 20 | 14 | -30.00% |
| Tool-result rounds | 16 | 10 | -37.50% |
| Visible tool-output bytes | 171,381 | 127,737 | -25.47% |
| Requirement extractions | 8 | 4 | -50.00% |
| Review-guidance reads | 9 | 4 | -55.56% |

Each selected-policy reviewer extracted the complete requirements and read the
review guidance once. Every control reviewer extracted requirements twice, and
all revisited guidance. Broader source and diff inspection still occurred where
needed for full acceptance coverage. The improvement removes repeated input
and output across fewer requests; uncached input did not improve in aggregate.
The percentages describe this paired workload, with cached input included in
total input.

[Follow-up measurements](validation/native-output-2026-10-23.json) retain each
review's usage and request counts. Requirement extractions count commands reading
`context.original_order`; guidance reads include full reads and section lookups.
Installed regression passed 221 tests with zero failures or skips. Tests cover
exec, app-server and review argument generation; native and explicit values;
retained settings under environment changes; and the existing feedback/recovery
paths. All 91 packaged runtime files match the tested installation. Public CI
now includes the launch-context tests. The
[configuration reference](CONFIGURATION.md#native-launch-settings) documents
selection and restoration of an explicit output limit.
