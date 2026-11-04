# Change Artifact: Task observation and incremental usage

- Date: 2026-11-04
- Slug: task-observation-and-incremental-usage
- Status: complete
- Project: generic
- Trigger Reason: multi-module status and usage contracts with independent installed-package verification

## Goal

- Make existing goal status, usage and delivery reports sufficient to identify current work, pending input, result level and the next recorded command. Provide an incremental usage comparison against a saved report. Present two concrete first-use routes.

## Non-Goals

- No release, version change, production installation, runtime selection or active-task intervention. No new scheduler, task registry, billing service, model probe or execution gate. Larger long-task architecture proposals remain recommendations for operator decision.

## Scope

- expected files: existing dispatch package, goal manifest/status and usage projections; CLI arguments; focused offline regression tests; README, first-use, command, recovery and architecture documentation; changelog.
- protected files: published tags/distributions, installed releases, current runtime selection, native task records and historical evidence.

## Slice Plan

1. Bind current dispatch packages to the existing goal path/run identity and project status through the current attempt journal; add a concise summary and existing continuation command.
2. Publish compact observations from already reconciled usage identities, add a role/provider summary and compare with an immutable saved task-report baseline.
3. Project execution, candidate review and project acceptance separately through the existing delivery report; align the two first-use routes and command examples.
4. Independently review, run installed offline checks and public CI, integrate source, preserve evidence and remove completed generated output.

## Verification Plan

- commands or artifacts: installed-package regressions for goal/dispatch status, current-attempt association, queued input, continuation, usage identity/cumulative sessions and delivery levels; three example initializations; public Windows CI.
- pass conditions: status is read-only and bounded to the selected task; another goal or superseded attempt cannot appear current; running work does not acquire recovery instructions; increment excludes previously observed work, counts cumulative growth once and reports missing/conflicting observations; no real model calls.

## Risks and Approvals

- ask-first boundary: the four previously reported product directions are authorized. Additional major changes are reported, not implemented. Publication remains deferred.
- follow-up risks: source repair does not change released installations. Missing historical bindings remain explicit; status does not reconstruct identity from tag or timestamps.

## Closeout Routing

- reports or docs to update: public CLI/architecture/first-use instructions, changelog; private research, verification and storage record under the creating maintenance operation.
- memory or ADR targets: no personal memory update requested; established ownership remains unchanged.
- archive after complete: yes

## Reference designs

- LangGraph checkpoint base: channel versions, versions seen and pending writes keep replay associated with persisted work. https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint/langgraph/checkpoint/base/__init__.py
- OpenHands ConversationStats: retained per-LLM usage history with compact snapshots for consumers. https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/conversation_stats.py
- Microsoft Agent Framework checkpoint sample: persisted pending human requests resume through the original workflow. https://github.com/microsoft/agent-framework/blob/main/python/samples/03-workflows/checkpoint/checkpoint_with_human_in_the_loop.py

Local adaptation extends existing package/journal, task-report reconciliation and goal input owners. It adds no framework dependency or parallel persistence mechanism.

## Verification results

- Required and affected installed-package checks: 141 passed. Final targeted status and usage cases: 30 passed. Independent implementation review: 30 passed.
- Broader adjacent lifecycle, feedback and coordination checks: 213 passed before the final usage-scope display corrections; the final affected checks above cover those corrections.
- All three example scenarios initialized successfully with their baseline tests, registration and inspection. All validation uses offline synthetic clients.
- Independent review corrections reconcile cross-role Codex sessions, preserve distinct invocation identities and leave unproven overlapping token scope explicit. Status ambiguity selects inspection rather than ordinary waiting.
- Public Windows CI is the source-integration gate in the pull request associated with Issue #44. Version publication, installation and runtime selection remain deferred.
- Further work-package evidence associations and comparative long-task evaluations are proposals only, documented in LONG_TASK_DESIGN_REVIEW.md.
