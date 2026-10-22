# Changelog

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
