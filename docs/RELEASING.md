# Release and runtime upgrade

The release operator builds an installed distribution from a reviewed commit,
runs the installed-package checks in [CONTRIBUTING.md](../CONTRIBUTING.md), and
publishes the wheel, source archive and `SHA256SUMS.txt` under the matching Git
tag. Python prereleases such as `0.1.0a94` are marked as prereleases on GitHub.

## Install a versioned runtime

Download the assets from the matching [GitHub release](https://github.com/tao19911024-gif/native-agent-orchestrator/releases).
Compare each file's `Get-FileHash -Algorithm SHA256` result with `SHA256SUMS.txt`.
Create a new environment for the new version:

```powershell
$RuntimeRoot = 'C:/AgentRuntime'
$Version = '0.1.0a94'
$ReleaseRoot = Join-Path $RuntimeRoot "releases/$Version"
python -m venv $ReleaseRoot
$RuntimePython = Join-Path $ReleaseRoot 'Scripts/python.exe'
& $RuntimePython -m pip install "./foundry_runtime-$Version-py3-none-any.whl"
& $RuntimePython -I -m foundry_runtime --help
& $RuntimePython -I -m foundry_runtime.recovery --help
```

Install into a distinct versioned directory. Retain the previous qualified
environment as the rollback runtime, including its original interpreter path.

## Select the runtime and project entry points

Wrappers that read the shared selection file use the selected interpreter for
new work. Save the previous selection, then use the existing selection command:

```powershell
$Selection = Join-Path $RuntimeRoot 'current.json'
$RollbackPython = (Get-Content -LiteralPath $Selection -Raw | ConvertFrom-Json).python
& $RuntimePython -m foundry_runtime.release_selection `
  --selection $Selection --python $RuntimePython
```

Check each project wrapper's interpreter selection. An explicit project runtime
pointer must be updated through its existing selection mechanism. Descriptor
projects can use the selected interpreter with their existing `--project-config`.
Run the project configuration check with the new interpreter before new work:

```powershell
& $RuntimePython -m foundry_runtime --foundry-project check `
  --project-config 'C:/Projects/example/foundry-project.json'
```

For new Codex work, `launch.FOUNDRY_TOOL_TOKENS=0` selects native output settings
in 0.1.0a93. An explicit positive value continues to override the native setting.
If a project retains an explicit `3000` value, select `0` in its new-work
configuration to adopt the release default. Keep task-bound descriptors and
frozen launch settings at their recorded values.

Running tasks retain their loaded runtime. Recovery and continuation use the
interpreter and launch settings recorded in task provenance. Preserve their
versioned environments and evidence; updating the shared selection does not
migrate an existing task. Project recovery wrappers must continue to pass the
recorded interpreter where required by their recovery interface.

## Rollback and retention

Select the retained interpreter to roll back new work:

```powershell
& $RuntimePython -m foundry_runtime.release_selection `
  --selection $Selection --python $RollbackPython
```

Restore any changed project runtime pointers and new-work launch settings from
the release record. Tasks started with the new release retain that environment
for their own recovery. Apply [storage retention](STORAGE_RETENTION.md) to build
outputs and older releases after checking their recorded dependencies.
