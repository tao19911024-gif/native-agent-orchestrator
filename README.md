# Native Agent Orchestrator

Native Agent Orchestrator is a Python runtime for executing long-running development tasks with
native Codex and Claude clients. It coordinates manager and worker roles, records
execution evidence and usage, and resumes work through its recorded lifecycle.

The runtime owns execution, task and session custody, continuation, and receipts.
Its existing Python package is `foundry_runtime`, distributed as `foundry-runtime`;
the `foundry` and `foundry-recovery` commands retain their existing names.
Each project owns its requirements, preflight, tests, review, acceptance, and
integration.

This is an early alpha, version **0.1.0a92**. The public source starts with a
reviewed snapshot of the existing runtime. Private project histories, operational
records, credentials, and installed native clients are outside this repository.

## Install

Use Python 3.13 or newer and Git. Windows is the primary development environment;
some lifecycle operations still assume the maintainer's Windows layout.

```powershell
git clone https://github.com/tao19911024-gif/native-agent-orchestrator.git
cd native-agent-orchestrator
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install '.[test]'
& .\.venv\Scripts\python.exe -m foundry_runtime --help
& .\.venv\Scripts\python.exe -m foundry_runtime.recovery --help
```

Codex or Claude must be installed and authenticated separately for model-backed
execution. Foundry does not include a model subscription or API credits. Installing
and inspecting a project does not require a provider login.

## Start with project inspection

Run the [minimal example](examples/README.md) to create a standalone Python
project, register it, and inspect its execution configuration. The example also
includes a task order for a complete Codex execution and verification cycle.
See the [recorded demonstration](docs/DEMO.md) for commands and results.

Set `$Workspace` to an existing Git working tree. Keep the descriptor and runtime
evidence in a local directory outside the public source repository.

```powershell
$Python = (Resolve-Path .\.venv\Scripts\python.exe).Path
$Workspace = 'D:/projects/example'
$Config = 'D:/foundry-local/example.json'
& $Python -m foundry_runtime --foundry-project register `
  --worktree $Workspace --project-config $Config
& $Python -m foundry_runtime --foundry-project check --project-config $Config
```

Registration and inspection use Git and local files. They do not start a model
task. See [getting started](docs/GETTING_STARTED.md) before enabling execution.

## Architecture and boundaries

- [Architecture and lifecycle](docs/ARCHITECTURE.md)
- [Current limitations](docs/LIMITATIONS.md)
- [Release history](CHANGELOG.md)
- [Contributing and offline checks](CONTRIBUTING.md)
- [Dependency and native-client notices](THIRD_PARTY_NOTICES.md)

Public entrypoints are `python -m foundry_runtime`, `foundry`, and
`foundry-recovery`. Underscored Python modules are implementation details.
Installing this source does not upgrade an already running task; recovery retains
the runtime and native session recorded by that task.

## License

Foundry's source and documentation are available under the [MIT license](LICENSE).
Third-party dependencies and native clients retain their own licenses and terms.
