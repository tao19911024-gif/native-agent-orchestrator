# Changelog

## 0.1.0a95 — 2026-10-23

- Require pointer and value-digest receipts for referenced fields in new review
  checkpoints. Historical review requests and completed verdicts retain their contracts.
- Bound legacy assignment reads to 1 MiB and report malformed or unmatched input
  through the existing manager hand-back outcome.
- Keep usage reporting available when delivery-baseline projection fails.
- Apply the task deadline to Git observations and report expiry as a named goal block.
- Preserve capacity-retry failures without masking their cause or attaching a
  predecessor package to deadline stops.
- Restore the 3,000-token output default for workers and managers; retain native
  output settings for Codex reviewers and preserve explicit or recorded settings.
- Require a version-bound acceptance receipt for default runtime selection.
  Document one source authority, clean tagged builds, publication and rollback.
- Correct the recovered-assignment test's public fixture reference and extend CI.

## 0.1.0a94 — 2026-10-23

- Preserve the exact dispatch package when provider-capacity retry admission
  stops before the goal publishes worker feedback.
- Resolve ambiguous legacy manager assignments from the bound native input,
  frozen request digest and retained manager decision. Exclude worker results
  already recorded in the coding checkpoint.
- Restore verified failed dispatches with their original return code and return
  them to the existing manager-feedback path on explicit recovery.
- Clear consumed pending-dispatch references when publishing a checkpoint.
- Add installed-package regression coverage for failed handback, historical
  ambiguity, stale packets and recovery without repeating completed work.

## 0.1.0a93 — 2026-10-23

This release includes the delivery baseline, retained review feedback, review
context references, evidence-reading policy and native Codex output configuration
described below. It also includes the expanded examples, terminal demonstration,
contribution workflow and Windows installed-package CI introduced after 0.1.0a92.

See the [release and upgrade guide](docs/RELEASING.md) for installation, runtime
selection, project configuration and rollback.

### Native Codex tool-output settings — 2026-10-23

- Default new Codex launches to native tool-output settings through the existing
  launch configuration; preserve explicit limits and frozen historical values.
- Verified reduced requirement and guidance rereads with byte-identical native
  review requests, and published the actual token comparison.
- Added launch-context regressions to public CI and documented zero/default
  selection and explicit-limit restoration.

### Review evidence reading — 2026-10-23

- Added a checkpoint-owned reading policy that groups focused evidence reads
  and reuses inspected content at the same candidate revision.
- Preserved historical request rendering, complete requirement coverage and
  request identity through review recovery and runtime-upgrade history.
- Published native token and tool-loop measurements from the retained
  conflict-response scenario.

### Live delivery validation and stopped-review status — 2026-10-23

- Exposed task, candidate, checkpoint and recorded recovery invocation when a
  review stops before publishing its outcome.
- Verified the generated recovery command with real native reviewers and
  preserved completed review receipts.
- Published measured token usage, original-session correction and recovery
  results for the retained conflict-response defect.

### Review context references — 2026-10-23

- Reused the existing review checkpoint for long task, changed-file, project
  and execution-decision fields in independent review requests.
- Preserved complete context hashes, inline checks and findings, and the
  original rendering of retained checkpoints and short inputs.
- Added installed coverage for field retrieval and interrupted review recovery
  that reuses the completed reviewer.

### Retained review feedback — 2026-10-23

- Connected goal and hosted status to retained task, candidate, review findings
  and artifact references.
- Added the existing recorded-session amendment command to feedback status,
  using the shared interpreter, project-entry and environment formatter.
- Added an anonymized conflict-response correction regression covering PASS
  and BLOCK findings, candidate checks and linked successor results.
- Documented feedback states and continuation steps and included the regression
  in public installed-package CI.

### Delivery baseline — 2026-10-22

- Extended `--foundry-usage` with task, native execution, candidate commit,
  check, review and project acceptance associations from retained records.
- Added invocation counts and recorded status distributions, with separate
  automatic-container and direct-dispatch counts.
- Included retained invocation elapsed time and existing phase timing summaries.
- Added offline coverage for linked records, duplicate selection and partial
  history to the public installed-package CI.

### First-use documentation — 2026-10-22

- Connected the README quickstart directly to task execution, preserving the
  initialized project and interpreter variables.
- Aligned all example guides with source and wheel installation environments.
- Added release extraction steps, checksum verification instructions, and
  official native-client setup links.
- Documented the Claude Opus manager required by the Codex coding/review goal
  and clarified manager selection alongside coding and review providers.
- Recorded fresh installation, native execution, continuation, and independent
  review verification results.

### Project showcase — 2026-10-22

- Added a 24-second terminal demonstration, asciicast replay, and text transcript
  of a completed native Codex continuation and its four result tests.
- Documented a runtime recovery-routing development case with synthetic project
  details, public implementation references, and nine passing regression cases.
- Linked the showcase from the README and included its assets in the source
  distribution.

### Contribution infrastructure — 2026-10-22

- Added public Windows/Python CI for package builds, installed-runtime checks,
  and example initialization, with downloadable reports and distributions.
- Documented contribution setup, test selection, pull request submission, and review.
- Added structured bug and feature forms and a pull request template.

### Documentation update — 2026-10-22

- Added project use cases, an architecture diagram, and package-name mapping.
- Added configuration, CLI, recovery, and troubleshooting references.
- Added runnable checkpoint-continuation and coding-with-review scenarios.
- Recorded live session continuation and two independent review results.
- Updated installation and distribution instructions for GitHub release assets.

## 0.1.0a92 — 2026-10-22

First tagged public release of Native Agent Orchestrator.

### Runtime

- Native Codex and Claude execution through a shared Python runtime.
- Durable task identity, session custody, continuation, and recovery records.
- Goal coordination, independent review, and execution usage reporting.
- Descriptor-based project registration, inspection, and status commands.

### Examples and documentation

- Standalone greeting project with a baseline test and an executable task order.
- Quickstart script using the installed runtime's public CLI.
- Instructions covering project setup, Codex execution, status inspection, and
  verification of the resulting code and tests.
- Recorded native Codex demonstration with four passing result tests.
- Source distribution includes the examples and documentation.

### Distribution

- MIT-licensed source, wheel, and source distribution.
- Python package: `foundry-runtime`; import: `foundry_runtime`.
- Commands: `foundry` and `foundry-recovery`.
- Python 3.13 or newer; Windows is the verified platform for this release.
- Native Codex and Claude clients are installed and authenticated separately.

### Verification

- 20 installed-runtime and project-launch tests passed.
- Quickstart completed from the repository and the extracted source distribution,
  using directory paths containing spaces.
- Native Codex execution completed with exit code `0`; all four task-result
  tests passed.
- Wheel installation, source archive contents, and secret scanning passed.
