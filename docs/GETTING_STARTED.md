# Installation and project setup

## Install from source

Use Windows, Python 3.13 or newer, and Git. Run these commands in the directory
selected for the source checkout:

```powershell
git clone https://github.com/tao19911024-gif/native-agent-orchestrator.git
cd native-agent-orchestrator
python -m venv .venv
$RuntimePython = (Resolve-Path .\.venv\Scripts\python.exe).Path
& $RuntimePython -m pip install '.[test]'
& $RuntimePython -I -m foundry_runtime --help
```

The test extra supplies pytest for repository checks. The examples use Python's
standard-library unittest. Start with the [greeting example](../examples/README.md),
[checkpoint continuation](../examples/CONTINUATION.md), or
[coding and review](../examples/REVIEW.md).

## Install a release package

Download a wheel from [GitHub Releases](https://github.com/tao19911024-gif/native-agent-orchestrator/releases).
Create a virtual environment and install the downloaded file with its interpreter:

```powershell
python -m venv .venv
$RuntimePython = (Resolve-Path .\.venv\Scripts\python.exe).Path
$WheelPath = (Resolve-Path .\foundry_runtime-0.1.0a92-py3-none-any.whl).Path
& $RuntimePython -m pip install $WheelPath
& $RuntimePython -I -m foundry_runtime --help
```

Select the filename supplied by the chosen release. Each release supplies
`SHA256SUMS.txt` for checking its distribution files. The source distribution
includes the runnable examples and documentation; extract it to use those files.

## Register a Git project

Select a workspace and a descriptor outside the source repository:

```powershell
$Workspace = 'D:/projects/example'
$ProjectConfig = 'D:/nao-local/example.json'
& $RuntimePython -I -m foundry_runtime --foundry-project register `
  --worktree $Workspace --project-config $ProjectConfig
```

Registration writes project identity, rule references, and the evidence directory.
It selects the workspace's `AGENTS.md` when present. Inspect the saved descriptor:

```powershell
& $RuntimePython -I -m foundry_runtime --foundry-project check `
  --project-config $ProjectConfig
```

## Configure execution

Before the first task, add the project's execution owner and preflight command.
For a Python project whose checks use unittest:

```powershell
$Project = Get-Content $ProjectConfig -Raw | ConvertFrom-Json
$Project | Add-Member -NotePropertyName execution -NotePropertyValue @{
    owner = 'foundry'
    preflight_argv = @($RuntimePython, '-m', 'unittest', '-v')
} -Force
$Project | ConvertTo-Json -Depth 20 | Set-Content $ProjectConfig -Encoding utf8
& $RuntimePython -I -m foundry_runtime --foundry-project check `
  --project-config $ProjectConfig
```

Use the project's own check command. The [configuration reference](CONFIGURATION.md)
explains fields, per-call inputs, launch settings, and observation hooks.
Keep the descriptor stable throughout the task and its continuations.

## Execute and inspect

Install and authenticate the selected native client. For a task order in the
project, select terminal Codex execution explicitly:

```powershell
$TaskOrder = Join-Path $Workspace 'TASK1.md'
& $RuntimePython -I -m foundry_runtime --project-config $ProjectConfig `
  --order $TaskOrder --tag TASK1 --provider codex --launcher exec
& $RuntimePython -I -m foundry_runtime --foundry-project status `
  --project-config $ProjectConfig --tag TASK1
```

Retain the printed log, report, session, and follow-up path. Use
[the command reference](CLI.md) for goals and review and
[the recovery guide](RECOVERY.md) to continue recorded work.
