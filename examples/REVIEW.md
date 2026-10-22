# Implement and independently review a task

The goal controller coordinates one coding role and independent review. Coding
produces a committed candidate; review evaluates that exact candidate against
the task and project rules. Use the
[native client environment](../docs/LIMITATIONS.md#installation-and-native-clients)
configured for the greeting example.

## Create the project

From the source repository, with the package installed in `.venv`:

```powershell
$RuntimePython = (Resolve-Path .\.venv\Scripts\python.exe).Path
$ExampleRoot = Join-Path (Split-Path $PWD.Path -Parent) 'nao-review'
& $RuntimePython .\examples\quickstart.py $ExampleRoot --scenario review
$Workspace = Join-Path $ExampleRoot 'project'
$ProjectConfig = Join-Path $ExampleRoot 'project.json'
$TaskOrder = Join-Path $Workspace 'TASK1.md'
$GoalDirectory = Join-Path $ExampleRoot 'goal'
```

The scenario supplies rules permitting the candidate commit and requiring an
independent review. The generated descriptor retains the baseline unittest
command as preflight.

## Execute the goal

```powershell
& $RuntimePython -I -m foundry_runtime --project-config $ProjectConfig `
  --order $TaskOrder --tag TASK1 --provider codex --launcher exec `
  --foundry-goal --team-profile none --goal-evidence $GoalDirectory
```

`none` selects direct coding. The goal controller selects independent review
assignments and records their results. To inspect the resolved staffing before
starting, add `--print-staffing` to the same new-goal command.

## Inspect the candidate and review

```powershell
& $RuntimePython -I -m foundry_runtime --foundry-project status `
  --status-goal $GoalDirectory
$Outcome = Get-Content (Join-Path $GoalDirectory 'outcome.json') -Raw | ConvertFrom-Json
$Outcome | Select-Object status, candidate_commit
Push-Location $Workspace
try {
    & $RuntimePython -m unittest -v
    git show --stat HEAD
    git status --short
} finally {
    Pop-Location
}
```

`candidate_verified` identifies a candidate that passed the goal's coding and
review checks. The maintainer inspects the four required behaviors, committed
diff, review verdicts, and project acceptance observation. The goal directory
retains `manifest.json`, `outcome.json`, and the referenced coding and review
evidence.

See [demonstration results](../docs/DEMO.md) for the executed workflow and
[continuation and recovery](../docs/RECOVERY.md#recover-a-goal) for retained goals.
