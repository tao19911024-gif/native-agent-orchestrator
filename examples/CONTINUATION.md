# Continue a task from a checkpoint

This workflow uses two completed turns in the same Codex session. The first turn
reads the project and records a checkpoint; the second implements the task.
Use the [native client environment](../docs/LIMITATIONS.md#installation-and-native-clients)
configured for the greeting example.

## Create the project

From the source repository, with the package installed in `.venv`:

```powershell
$RuntimePython = (Resolve-Path .\.venv\Scripts\python.exe).Path
$ExampleRoot = Join-Path (Split-Path $PWD.Path -Parent) 'nao-continuation'
& $RuntimePython .\examples\quickstart.py $ExampleRoot --scenario continuation
$Workspace = Join-Path $ExampleRoot 'project'
$ProjectConfig = Join-Path $ExampleRoot 'project.json'
$TaskOrder = Join-Path $Workspace 'TASK1.md'
```

## Run the checkpoint stage

```powershell
& $RuntimePython -I -m foundry_runtime --project-config $ProjectConfig `
  --order $TaskOrder --tag TASK1 --provider codex --launcher exec
```

Read the returned report. It ends with `CHECKPOINT_READY`; `greet.py` and
`test_greet.py` remain at the baseline. Read this project's single task record:

```powershell
$TaskStatus = & $RuntimePython -I -m foundry_runtime --foundry-project status `
  --project-config $ProjectConfig --tag TASK1 | ConvertFrom-Json
$Run = @($TaskStatus.runs)[0]
$SessionId = $Run.session_id
$Run | Select-Object operation, recorded_state, returncode
```

## Continue the recorded session

```powershell
$PromptFile = Join-Path $Workspace 'CONTINUE.md'
& $RuntimePython -I -m foundry_runtime --project-config $ProjectConfig `
  --resume $SessionId --worktree $Workspace --tag TASK1 `
  --launcher exec --prompt-file $PromptFile
```

The continuation implements the second stage in `TASK1.md`. Inspect both records
and verify the resulting code:

```powershell
$TaskStatus = & $RuntimePython -I -m foundry_runtime --foundry-project status `
  --project-config $ProjectConfig --tag TASK1 | ConvertFrom-Json
$TaskStatus.runs | Select-Object operation, recorded_state, returncode, session_id
Push-Location $Workspace
try {
    & $RuntimePython -m unittest -v
    git diff -- greet.py test_greet.py
} finally {
    Pop-Location
}
```

The two records contain `dispatch` and `resume`, the same session ID, completed
state, and exit code `0`. All four greeting cases pass. The modified source and
tests remain uncommitted. See [demonstration results](../docs/DEMO.md) and
[interrupted-execution recovery](../docs/RECOVERY.md#reconcile-interrupted-execution)
for their respective procedures.
