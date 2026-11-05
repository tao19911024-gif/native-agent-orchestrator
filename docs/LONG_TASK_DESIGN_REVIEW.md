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

The solution contract already expresses work packages, dependencies and
acceptance. Goal coordination records instructions, worker evidence, candidate
checks and review results. The normal coordination contract and worker-returned
packet do not expose a durable completion map keyed by work-package identity.
The relevant local surfaces are `_foundry_response_contract._SOLUTION`, the
coordination contract, `_foundry_goal_execution` and `_foundry_manager` handoff.

For a long task, an explicit association could reduce the need to reconstruct
which packages were accepted, which evidence changed and which work remains.
The transferable idea is the checkpoint's record of what has already been
consumed, rather than a new workflow engine.

Proposed scope: select one real multi-stage task; associate existing package IDs
with candidate/check/artifact references in the existing goal record; include the
completed and remaining package summary in the existing manager handoff. Package
completion remains subject to the project's own acceptance definition.

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

Start with one work-package evidence association on a real long task, then use
the evaluation set to determine whether it improves accepted delivery and
reduces repeated work. Avoid adding a management interface, another planner or
new automatic retries before that result is measured.
