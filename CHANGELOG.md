# Changelog

## Project showcase — 2026-10-22

- Added a 24-second terminal demonstration, asciicast replay, and text transcript
  of a completed native Codex continuation and its four result tests.
- Documented a runtime recovery-routing development case with synthetic project
  details, public implementation references, and nine passing regression cases.
- Linked the showcase from the README and included its assets in the source
  distribution.

## Contribution infrastructure — 2026-10-22

- Added public Windows/Python CI for package builds, installed-runtime checks,
  and example initialization, with downloadable reports and distributions.
- Documented contribution setup, test selection, pull request submission, and review.
- Added structured bug and feature forms and a pull request template.

## Documentation update — 2026-10-22

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
