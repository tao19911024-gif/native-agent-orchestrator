# Long-task execution design review

Review date: 2026-11-04. The comparison focuses on durable progress, effective
continuation, observable usage and verifiable delivery. It uses project source,
retained regression evidence and three public implementations. It does not rank
projects by popularity or infer measured performance from their architecture.

## Reference designs and local application

| Reference | Examined design | Application in this change |
| --- | --- | --- |
| [LangGraph checkpoint model](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint/langgraph/checkpoint/base/__init__.py) | Persisted checkpoint identity, channel versions, versions seen and pending writes | Associate observations with the original goal and current attempt; reuse completed identities instead of counting replay as new work |
| [OpenHands conversation statistics](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/conversation_stats.py) | Retained per-LLM metrics with compact snapshot serialization | Keep detailed reconciled usage while exposing concise summaries and a saved comparison baseline |
| [Microsoft Agent Framework checkpoint workflow](https://github.com/microsoft/agent-framework/blob/main/python/samples/03-workflows/checkpoint/checkpoint_with_human_in_the_loop.py) | Persist pending human requests and resume the same workflow from a checkpoint | Surface the existing input request and recorded continuation command through status |

Related regression surfaces include LangGraph's
[interrupted checkpoint fork tests](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/tests/test_delta_channel_fork.py),
OpenHands'
[conversation statistics tests](https://github.com/OpenHands/software-agent-sdk/blob/main/tests/sdk/conversation/test_conversation_stats.py),
and Agent Framework's
[checkpoint tests](https://github.com/microsoft/agent-framework/blob/main/python/packages/core/tests/workflow/test_checkpoint.py).

These are design references. The implementation extends the existing Foundry
package/journal, goal, usage reconciliation and outcome owners. LangGraph is
already a dependency of the runtime. This change adds no framework dependency,
second scheduler, task registry or billing ledger.

## Implemented product improvements

1. Current goal activity is linked by goal directory, run ID and current attempt.
   Summary mode exposes phase, waiting role, queued input, recorded runtimes,
   evidence and the next existing command.
2. Usage is summarized by provider and role. A saved full report is the baseline
   for native invocation, response, token and reading-signal comparison. Shared
   cumulative sessions are reconciled once.
3. First use has two explicit routes: one client with one worker, and managed
   implementation with independent review. Each route names its clients,
   initialization, execution command and result location.
4. Execution, candidate review and project acceptance are displayed separately
   from the existing outcome and project acceptance observation.

## Further proposals for operator decision

### Associate work-package completion with evidence

The standalone analysis route's solution contract expresses work packages,
dependencies and acceptance. Managed development does not consume that result
as an execution plan: its coordinator works from the original order and retained
decisions. Those decisions, worker evidence, candidate checks and review results
are already persisted. The normal coordination contract and worker-returned
packet do not expose a completion map keyed by work-package identity.
The relevant local surfaces are `_foundry_response_contract._SOLUTION`, the
coordination contract, `_foundry_goal_execution` and `_foundry_manager` handoff.

For a long task, an explicit association could reduce the need to reconstruct
which packages were accepted, which evidence changed and which work remains.
The transferable idea is the checkpoint's record of what has already been
consumed, rather than a new workflow engine.

Proposed scope: select a real multi-stage task whose project already identifies
independently accepted deliverables. Establish those identifiers' project owner
before associating them with candidate/check/artifact references. An execution
attempt or analysis-generated package ID does not establish that acceptance
identity. Reuse the existing goal records and manager handoff if an association
is needed. Package completion remains subject to the project's acceptance
definition.

Verification would interrupt the task after an accepted intermediate package,
resume it, and check that unchanged completed work is reused while changed
requirements or failed evidence cause the affected package to be reconsidered.
This changes coordination contracts and is not implemented in this change.

### Establish a repeatable long-task evaluation set

Existing regressions exercise recovery, input timing, review correction,
execution custody and usage. The repository also retains focused real-task token
comparisons. These establish specific contracts; they do not yet provide a
single comparative evaluation across several long, complex task shapes.

Proposed scope: use three selected tasks covering a multi-module change, a
review-correction chain and an interrupted continuation. Freeze requirements,
project checks, baseline versions and observation scope. Record accepted
deliverables, repeated invocations, repeated reading, uncached input, output and
operator interventions through the existing task reports.

Compare the same task and acceptance criteria across candidates. A shorter run
does not count as an improvement if required delivery is missing. Use retained
offline replay first; any new model-backed evaluation requires its own selected
task, accounts and execution budget. No paid evaluation is started by this
source change.

## Recommended next decision

Select a real case where an unchanged, previously accepted deliverable is
repeated or lost during recovery. Check its existing assignment, candidate,
review and project acceptance references before adding a work-package contract.
Use that case to measure whether the association improves accepted delivery and
reduces repeated work. Avoid adding another planner or automatic retries before
that result is measured.

## Source and retained-record follow-up, 2026-11-16

The a120 source keeps the standalone `--foundry-analyze` route separate from
managed development. `_foundry_analysis.analyze_order` owns the former's
`work_packages`; `_foundry_goal_execution` constructs manager packets from the
order, execution and retained feedback. Adding a package completion map to every
development task would introduce a new coordination contract, rather than finish
an existing field mapping.

The current delivery baseline already joins goal invocation, frozen assignment,
execution attempt, candidate, checks, reviews and project acceptance. Read-only
replay of four retained chains covered 20 invocations and 27 execution assignment
observations representing 20 distinct attempts. The existing joins remained
available. This establishes traceability;
it does not assign separate acceptance verdicts to each intermediate deliverable.

Two additional retained manager chains contained 31 decisions, 29 with nonempty
`remaining_work`. Both final manager decisions had empty lists, and independent
reviews subsequently blocked their candidates for concrete implementation
defects. These records support keeping manager judgment separate from candidate
verification. They do not establish that a package ledger would prevent those
defects or reduce delivery cost.

One observation gap was reproducible: status omitted the already recorded
`remaining_work` while exposing a shortened instruction. Status now projects
that field with the same decision receipt. Historical absence remains `null`;
an empty list remains a manager judgment. No execution state, scheduler,
acceptance rule or recovery contract changes.

The evaluation gap remains: retained traces support failure classification and
offline replay, but are not controlled comparisons of orchestration strategies.
Use the same starting source, order, required checks and acceptance definition
for future paired runs. Separate model/provider usage, cache categories, elapsed
time, repeated work and operator intervention. Measure account cost using an
explicit billing basis; aggregate token count alone is not the objective.
