# Project showcase

Native Agent Orchestrator retains native development sessions and the records
needed to inspect, continue, and recover their work. These materials present a
completed example and a maintenance case from the runtime's own development.

## Terminal demonstration

![Inspect a completed Codex continuation](assets/terminal-demo.gif)

The 24-second recording inspects the completed
[checkpoint and continuation example](../examples/CONTINUATION.md). The commands
were executed on October 22, 2026, using Windows, Python 3.13, and runtime
0.1.0a92, after the native Codex task had completed. Reading pauses are set for
presentation.

| Stage | Command result |
| --- | --- |
| Inspect task records | `dispatch` and `resume` are `completed`, both with exit code `0`; their native session IDs match. |
| Inspect the implementation | `greet.py` trims surrounding whitespace and uses `world` for an empty name. |
| Run result tests | All four greeting tests pass. |

The recording uses the example's configuration and workspace through
`$ProjectConfig` and `$Workspace`. It displays session equality without exposing
the native session identifier. All three commands exited with code `0`.

Read the [plain-text transcript](assets/terminal-demo.txt) or replay the
[asciicast recording](assets/terminal-demo.cast) with an asciinema-compatible
player. The GIF and asciicast contain the same commands and output.

### Reproduce the demonstration

Complete both stages of the [continuation workflow](../examples/CONTINUATION.md).
Keep that guide's `$RuntimePython`, `$ProjectConfig`, and `$Workspace` variables,
then run:

```powershell
$TaskStatus = & $RuntimePython -I -m foundry_runtime --foundry-project status `
  --project-config $ProjectConfig --tag TASK1 | ConvertFrom-Json
$TaskStatus.runs | Select-Object operation, recorded_state, returncode
'Same native session: ' + (@($TaskStatus.runs.session_id | Sort-Object -Unique).Count -eq 1)

git -c core.safecrlf=false -C $Workspace diff -- greet.py

Push-Location $Workspace
try { & $RuntimePython -m unittest -v } finally { Pop-Location }
```

The expected results and execution lifecycle are recorded in the
[demonstration report](DEMO.md).

## Runtime development case

[Recover an answered goal without repeating completed work](case-studies/CONTINUATION_RECOVERY.md)
documents a recovery-routing defect addressed during runtime development. It
traces the problem from an immutable question outcome to its consumed
continuation, explains the ownership of candidate verification, and provides a
reproducible nine-case regression command.

## Further workflows

- [Run a native development task](../examples/README.md).
- [Continue the same native session](../examples/CONTINUATION.md).
- [Implement, commit, and independently review a candidate](../examples/REVIEW.md).
- [Inspect lifecycle ownership and records](ARCHITECTURE.md).
