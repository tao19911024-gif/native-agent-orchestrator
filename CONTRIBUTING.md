# Contributing

Contributions include reproducible bug reports, workflow proposals, documentation,
examples, tests, and runtime changes. Use GitHub Issues to discuss a problem or
proposal and a pull request to submit the resulting change.

## Report a problem or propose a change

Choose [Bug report or Feature request](https://github.com/tao19911024-gif/native-agent-orchestrator/issues/new/choose).
Bug reports describe the environment, reproduction commands, expected behavior,
actual behavior, and relevant output. Feature requests explain the use case,
proposed behavior, and how the result would be verified.

For a runtime or architecture change, discuss the affected lifecycle and contract
in an issue before implementation. Documentation corrections and bounded bug
fixes can be submitted directly with their reproduction or verification.

Use synthetic project names, paths, and identifiers in public examples and log
excerpts. Retain private task history, credentials, account files, and native
transcripts in their local environments. Diagnostic commands are listed in
[troubleshooting](docs/TROUBLESHOOTING.md).

## Prepare a contribution

Fork the repository, clone the fork, and create a branch from `main` for one
change. Install the source in a dedicated environment:

```powershell
git switch -c fix/greeting-workflow
python -m venv .venv
$RuntimePython = (Resolve-Path .\.venv\Scripts\python.exe).Path
& $RuntimePython -m pip install '.[test]'
```

Runtime dispatch owns execution and provider session custody. Each target project
owns its requirements, checks, and acceptance. Extend these owners and update
the affected contracts, examples, and operator documentation with the change.

## Run installed-package checks

Run the offline checks from the repository root after installation:

```powershell
$PreviousCatalogue = $env:PDCL_MODEL_CATALOGUE
try {
    $env:PDCL_MODEL_CATALOGUE = 'snapshot'
    & $RuntimePython -m pytest tests/test_installed_runtime.py tests/test_project_launch.py
} finally {
    $env:PDCL_MODEL_CATALOGUE = $PreviousCatalogue
}
```

These checks cover installed imports, CLI entry points, project configuration,
and descriptor-based execution with synthetic native processes. Imports from
the installed distribution are part of their contract.

For changes to example setup, run each scenario with a new destination:

```powershell
foreach ($Scenario in @('execution', 'continuation', 'review')) {
    $ExampleRoot = Join-Path $env:TEMP ('nao-' + [guid]::NewGuid().ToString('N'))
    & $RuntimePython examples/quickstart.py $ExampleRoot --scenario $Scenario
    if ($LASTEXITCODE -ne 0) { throw "Example initialization failed: $Scenario" }
}
```

Select additional tests for the changed contracts. Native-client, desktop-host,
Windows service, and real-project integration checks use their selected
environments. Record each command, result, and the behavior verified.

To verify distribution changes, build the wheel and source archive:

```powershell
& $RuntimePython -m pip install build
& $RuntimePython -m build
```

## Public CI

The [CI workflow](.github/workflows/ci.yml) runs for pushes to `main`, pull
requests targeting `main`, and manual workflow dispatch. It uses Windows and
Python 3.13 to build both distributions, install the wheel in an independent
environment, check command entry points, run the offline tests, and initialize
all three example projects.

Open a run in [GitHub Actions](https://github.com/tao19911024-gif/native-agent-orchestrator/actions/workflows/ci.yml)
to inspect step logs and the `windows-python313` artifact. The artifact contains
distribution files, the JUnit report, and example registration, inspection,
status, and baseline-test reports. Example initialization performs local project
setup; native workflow execution follows the separate example guides.

## Submit and review a pull request

1. Commit the focused change with a message describing its purpose.
2. Push the branch to the fork and open a pull request targeting `main`.
3. Complete the [pull request template](.github/pull_request_template.md) with the
   problem, resulting behavior, affected components, verification, and documentation.
4. Review the public CI result and address failures caused by the change.
5. Respond to review findings and update verification when the implementation changes.

The maintainer reviews correctness, lifecycle ownership, contract consistency,
tests, and operator documentation before merging. CI results and native
integration evidence are evaluated for the behavior each check covers.

## License and notices

Submit code and documentation that can be distributed under this repository's
MIT license. Preserve applicable third-party notices. Runtime tests and
anonymized regression fixtures belong in the source repository; project-specific
acceptance remains with the target project.
