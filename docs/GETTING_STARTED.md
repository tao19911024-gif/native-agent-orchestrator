# Installation and project setup

## Install from source

Use Windows, Python 3.13 or newer, Git, and PowerShell. Run these commands in the
directory selected for the source checkout:

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
In the download directory, create a virtual environment and install the file
with its interpreter:

```powershell
python -m venv .venv
$RuntimePython = (Resolve-Path .\.venv\Scripts\python.exe).Path
$WheelPath = (Resolve-Path .\foundry_runtime-0.1.0a92-py3-none-any.whl).Path
& $RuntimePython -m pip install $WheelPath
& $RuntimePython -I -m foundry_runtime --help
```

Select the filename supplied by the chosen release. Download its matching source
distribution and `SHA256SUMS.txt` from the same release. Compare each downloaded
file's `Get-FileHash -Algorithm SHA256` result with the checksum file.

To use the release's examples, extract its source distribution, enter the
extracted directory, and select the environment created above:

```powershell
$SourceArchive = (Resolve-Path .\foundry_runtime-0.1.0a92.tar.gz).Path
tar -xf $SourceArchive
cd foundry_runtime-0.1.0a92
$ExampleRoot = Join-Path (Split-Path $PWD.Path -Parent) 'nao-release-example'
& $RuntimePython .\examples\quickstart.py $ExampleRoot
```

Use the archive name and directory supplied by the chosen release. Keep
`$RuntimePython` and `$ExampleRoot`, then continue at
[task execution](../examples/README.md#2-execute-the-task-with-codex). The extracted
documentation describes that release's included workflows. Source checkout
instructions refer to the files on the selected repository branch.

## Prepare a native client

Project initialization runs locally. To execute a development task, install and
sign in to the selected native client using its official instructions:

- [Codex CLI installation and sign-in](https://learn.chatgpt.com/docs/codex/cli).
- [Claude Code installation and authentication](https://code.claude.com/docs/en/setup).

Confirm the selected client is available in the same PowerShell session. For the
Codex examples:

```powershell
Get-Command codex
codex --version
```

Complete the client's sign-in before launching a runtime task. For Claude Code,
check `Get-Command claude` and `claude --version`. The selected client owns
authentication and account access. See [execution permissions](LIMITATIONS.md#execution-permissions)
for the runtime's native execution profiles.

## Register a Git project

Select an existing Git workspace with a baseline commit and a descriptor outside
the source repository. For the generated greeting example, these are the
`project` directory and `project.json` created by quickstart. To register a
separate project:

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
