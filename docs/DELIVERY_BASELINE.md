# Delivery baseline

`--foundry-usage` reads retained records and returns usage observations together
with a `delivery_baseline` section. The baseline connects each task invocation
to its native execution, candidate commit, checks, independent reviews and
project acceptance. It uses the existing task-report traversal and reference
reader; generating a report leaves task records unchanged.

## Generate a baseline

Use the installed interpreter selected for reporting and the retained goal
directory:

```powershell
& $RuntimePython -m foundry_runtime --foundry-usage $GoalDirectory
```

Supply several records to compare invocations or include direct execution:

```powershell
$Report = & $RuntimePython -m foundry_runtime --foundry-usage `
    $GoalDirectory $PreviousGoalDirectory $DispatchReceipt | ConvertFrom-Json
$Report.delivery_baseline.summary
$Report.delivery_baseline.invocations | Select-Object task, candidate, status
$Report | ConvertTo-Json -Depth 100 | Set-Content -Encoding utf8 $BaselinePath
```

`$DispatchReceipt` is the retained `*.receipt.json` path. `$BaselinePath` is a
local report destination outside the source repository. Select task records
from the same workload and retain the selected paths, report date and reporting
package revision when comparing runs. Native session identifiers and project
paths remain in local evidence storage.

## Record associations

| Field | Recorded source and interpretation |
| --- | --- |
| `reference` | Selected outcome or unfinished manifest, with its content hash |
| `task` | Task-order reference and run ID from delivery context, supplemented by the observed manifest |
| `manifest_observation` | Manifest reference and configuration observation provenance |
| `runtime_provenance` | Runtime version, interpreter and package identity recorded in that manifest |
| `candidate` | Candidate commit from the outcome and base/candidate context from the matching review checkpoint |
| `coding` | Recorded coding result, evidence references, session identity, retained native executions and usage reference |
| `checks` | Candidate-closure result and checkpoint-owned project-review checks, each with its source reference |
| `reviews` | Recorded verdict, candidate, packet hash, findings summary, reviewer index and retained session ID |
| `project_acceptance` | Result recorded by the project's acceptance owner |
| `artifacts` | Artifact references retained by the outcome |
| `links` | Existing predecessor, continuation, round and native-identity relations |
| `gaps` | Unavailable or unrelated optional records encountered during association |

An unavailable checkpoint leaves the outcome and its reviews visible. A
checkpoint for another invocation or candidate is reported in `gaps`.
Unrecorded results remain `null` or empty lists. Project acceptance is counted
as `not_recorded` when the outcome has no acceptance result.

## Counting and timing

`summary.invocation_count` counts deduplicated goal invocations, including
unfinished invocations and standalone reviews. `status_counts`,
`review_verdict_counts` and `project_acceptance_counts` group their recorded
results. Selecting a goal directory, its manifest and its outcome together
counts that invocation once. Explicit continuation links include prior
invocations as separate rows.

Automatic roots appear in `containers`; their rounds appear in `invocations`.
Direct dispatch receipts appear in `selected_dispatches`, with execution and
verification details. Their counts remain separate from goal invocations.
Native-identity links describe shared sessions or turns. Task and candidate
relationships come from the retained invocation records.

`elapsed` contains the invocation wall time saved with the outcome, including
waits and recovery intervals. `elapsed_observation_count` reports how many
invocations retain that measurement. `phase_timing` reads the adjacent
`telemetry.jsonl` through the existing telemetry summarizer. Its `summary`
contains recorded coding, candidate-test and review phases. Concurrent reviews
have a group wall time and individual durations. Invocation elapsed time and
phase durations are evaluated separately.

Use the baseline to locate long review groups, repeated candidate checks,
continuation chains and incomplete delivery records. Existing `coding`,
`reviews_by_backend`, `api_price_equivalent` and `execution_efficiency` fields
retain their established usage and cost accounting.

## Initial retained-record sample

The 2026-10-22 baseline uses two anonymized selections from existing records.
Private source paths and native session identifiers remain in local reports.

| Selection | Goal invocations | Automatic containers | Direct dispatches | Recorded results |
| --- | ---: | ---: | ---: | --- |
| First-use execution, continuation and independent review | 1 | 1 | 3 | One verified candidate; two PASS reviews; project acceptance `not_evaluated` |
| Development task with two explicit continuations | 3 | 1 | 0 | Two incomplete coding outcomes followed by one verified candidate; two PASS reviews |

The first selection retains runtime version `0.1.0a92`. The development chain
retains `0.1.0a14` and `0.1.0a35`. Each invocation supplies its task identity and
saved elapsed time. The successful candidates supply project-review checks.
The development chain records project acceptance as `not_evaluated` for its
verified candidate and has no acceptance result for its two incomplete runs.
Both selections contain zero retained phase-timing observations. The first
selection records `gpt-6.1-sol` coding usage; the development selection includes
`gpt-6.1-sol`, `gpt-6-astra` and `gpt-6-luna` observations. Model and role usage
remain in the report's existing usage sections.

These selections establish record coverage and traceable continuation order.
Long invocation wall times include recovery and waiting intervals. Further
performance comparisons use recorded phase timing and native usage from the
same workload, alongside its project-owned acceptance result.
