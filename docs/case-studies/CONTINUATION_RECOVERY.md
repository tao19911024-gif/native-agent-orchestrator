# Recover an answered goal without repeating completed work

This case describes a defect addressed during development of Native Agent
Orchestrator 0.1.0a92. The runtime's recovery code and regression tests provide
the implementation evidence. Project names, directories, questions, and native
identifiers are represented by synthetic fixtures.

## Development problem

A manager paused a goal for operator input. The operator answered, and a
continuation consumed that answer and selected a committed candidate for review.
Host-side candidate verification then failed before review could finish.

The original goal's `outcome.json` still recorded `awaiting_input`. That record
was intentionally immutable: it described the earlier question. Recovery through
the original goal directory therefore needed to find the continuation that had
already consumed the answer. Reading only the original outcome could return an
obsolete question or prevent a runtime upgrade at the current verification
boundary.

The required behavior was to resume verification of the existing candidate
without issuing another manager decision or coding assignment.

## Evidence and ownership

| Record or component | Responsibility |
| --- | --- |
| Original outcome | Preserve the historical question and its status. |
| Continuation manifest | Identify its predecessor through `continuation.prior_record`. |
| Native manager receipts | Identify the latest manager call and the continuation that consumed it. |
| Candidate closure record | Identify the committed candidate and the outstanding host verification. |
| Goal recovery and upgrade | Resolve those records and invoke the existing verification/review lifecycle. |

The defect involved a mismatch between the historical entry directory and the
current continuation. Modification times and directory names did not establish
which answer had been consumed.

## Implementation

The correction extended the existing goal lifecycle:

1. Resolve the active round, including nested and upgraded rounds.
2. Follow continuation manifests through their predecessor references, matching
   the task, workspace, and native manager history.
3. Select the continuation whose recorded manager call matches the latest
   consumed call.
4. Retry the outstanding candidate verification and review through their
   existing owners. Runtime upgrade carries the same candidate range forward.
5. Preserve the original outcome and completed native call records.

The recorded-runtime delegation path receives the resolved continuation
directory. Repeating the completed upgrade request returns its retained result.

| Implementation | Public source |
| --- | --- |
| Continuation resolution | [`consumed_continuation`](../../src/foundry_runtime/_foundry_goal_continuation.py) |
| Active-round selection | [`active_round`](../../src/foundry_runtime/_foundry_goal_auto.py) |
| Goal recovery and candidate retry | [`recover_goal`](../../src/foundry_runtime/foundry_goal.py) |
| Upgrade preparation and candidate range | [`prepare` and `prepare_invocation`](../../src/foundry_runtime/_foundry_goal_upgrade.py) |
| Recorded-runtime delegation | [`execute_recorded`](../../src/foundry_runtime/_foundry_recovery_preflight.py) |

## Reproducible verification

From the repository root, install the runtime and test dependencies as described
in [Contributing](../../CONTRIBUTING.md#prepare-a-contribution). Use the installed
package's interpreter:

```powershell
$PreviousCatalogue = $env:PDCL_MODEL_CATALOGUE
try {
    $env:PDCL_MODEL_CATALOGUE = 'snapshot'
    $CaseTests = @(
        'tests/test_closure_recovery_routing.py::test_plain_recovery_follows_consumed_answer_and_retries_only_closure'
        'tests/test_closure_recovery_routing.py::test_upgrade_resolves_consumed_closure_boundary'
        'tests/test_closure_recovery_routing.py::test_upgrade_retries_closure_without_another_manager_or_worker'
        'tests/test_closure_recovery_routing.py::test_multiple_answer_generations_follow_references_not_old_question'
        'tests/test_closure_recovery_routing.py::test_recorded_runtime_receives_resolved_continuation'
    )
    & $RuntimePython -m pytest @CaseTests -q
    if ($LASTEXITCODE -ne 0) { throw 'Continuation recovery regression failed' }
} finally {
    $env:PDCL_MODEL_CATALOGUE = $PreviousCatalogue
}
```

This command passed **nine parameterized cases** on October 22, 2026, using
Windows, Python 3.13, and the installed 0.1.0a92 wheel. The tests create temporary
Git projects, use synthetic native responses and candidate-verification results,
and exercise the runtime's retained-record and recovery paths.

| Assertion | Observed result |
| --- | --- |
| Ordinary and nested goals resolve the consumed continuation | Passed for both layouts. |
| Recovery retries the same candidate's verification | Candidate commit unchanged; manager and worker call counts unchanged. |
| Upgrade continues from the outstanding verification boundary | Candidate range retained; repeated request adds no manager or worker call. |
| Multiple answers form a continuation chain | Recovery reaches the latest pending question through predecessor references. |
| Recorded-runtime delegation uses the resolved directory | Passed for separate and inline `--recover-goal` arguments; Unicode task path and redirected streams retained. |
| Historical records remain intact | Original question and its retained files remain unchanged. |

The assertions are maintained in
[`tests/test_closure_recovery_routing.py`](../../tests/test_closure_recovery_routing.py).
The [native workflow demonstration](../DEMO.md) records the separate live Codex
execution and continuation results.

## Engineering result

Recovery now follows the consumed answer to the outstanding candidate
verification. The original question remains available as history, and completed
manager and worker calls remain the source records for their work. This keeps
recovery, review, and runtime upgrade within the existing goal lifecycle.
