# Minimal example

The greeting example covers project registration, configuration inspection,
native Codex execution, task status, and result verification. Its Python code and
tests use the standard library.

## Requirements

- Windows, Python 3.13 or newer, and Git.
- Native Agent Orchestrator installed in the repository's `.venv`, following the
  [installation instructions](../README.md#install).
- For task execution: the Codex CLI installed, available on `PATH`, and signed in.
  Model execution uses the signed-in account's allowance.

## 1. Create and register the project

Run these commands from the Native Agent Orchestrator repository:

```powershell
$RuntimePython = (Resolve-Path .\.venv\Scripts\python.exe).Path
$ExampleRoot = Join-Path (Split-Path $PWD.Path -Parent) 'nao-example'
& $RuntimePython .\examples\quickstart.py $ExampleRoot
```

`$ExampleRoot` must name a new directory. The script creates a separate Git
repository, commits the example baseline with a local example identity, runs its
test, and calls the installed runtime's `register`, `check`, and `status` commands.
The generated descriptor configures `python -m unittest -v` as project preflight.
This step performs local setup and inspection without invoking a model.

Expected summary:

```text
Runtime: foundry-runtime 0.1.0a92
Workspace: <example-root>\project
Baseline tests: PASS
Registration: registered
Repository identity matches: True
Rule sources: AGENTS.md
Execution configured: True
Recorded task runs: 0
Descriptor: <example-root>\project.json
```

The directory contains:

| Path | Contents |
| --- | --- |
| `project/` | Greeting function, baseline test, project rules, and `TASK1.md` |
| `project.json` | Generated project identity, rule references, and preflight command |
| `baseline-tests.txt` | Baseline test output |
| `registration.json`, `inspection.json`, `status.json` | Runtime inspection reports |
| `runs/` | Task evidence created when execution starts |

## 2. Execute the task with Codex

The task extends `greet` to trim names and use `world` for empty input. It names
the two editable files and requires tests for each behavior.

The runtime's Codex execution profile uses `danger-full-access` with approval
policy `never`. The executor runs under the current user account in the separate
example workspace.

```powershell
$ProjectConfig = Join-Path $ExampleRoot 'project.json'
$TaskOrder = Join-Path $ExampleRoot 'project\TASK1.md'
& $RuntimePython -I -m foundry_runtime --project-config $ProjectConfig `
  --order $TaskOrder --tag TASK1 --provider codex --launcher exec
```

The dispatcher runs project preflight, records the execution identity, invokes
Codex, and prints the execution log and report locations. Keep these locations
with the project descriptor. This direct execution uses one Codex worker.

## 3. Inspect and verify the result

```powershell
$TaskStatus = & $RuntimePython -I -m foundry_runtime --foundry-project status `
  --project-config $ProjectConfig --tag TASK1 | ConvertFrom-Json
$TaskStatus.runs | Select-Object tag, recorded_state, returncode

Push-Location (Join-Path $ExampleRoot 'project')
try {
    & $RuntimePython -m unittest -v
    git diff -- greet.py test_greet.py
} finally {
    Pop-Location
}
```

Successful verification requires all four input cases from `TASK1.md` to pass.
The completed task leaves the changes uncommitted for inspection. The runtime
status reports execution state; the tests and diff establish the task result.

An identical dispatch request returns its recorded outcome. Use a new example
directory to repeat the demonstration from its original baseline.

The descriptor records absolute paths and the selected interpreter. Keep the
example directory and virtual environment in place while inspecting or continuing
its task. After finishing the demonstration, remove that example directory to
remove its project and local records.
