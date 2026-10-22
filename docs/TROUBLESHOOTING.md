# Troubleshooting

Start with the exact interpreter, project descriptor, tag, and execution path
printed by the task. Project inspection and task status locate configuration and
execution evidence:

```powershell
& $RuntimePython -I -m foundry_runtime --foundry-project check `
  --project-config $ProjectConfig
& $RuntimePython -I -m foundry_runtime --foundry-project status `
  --project-config $ProjectConfig --tag TASK1
```

## Installation and client discovery

| Symptom | Resolution |
| --- | --- |
| `No module named foundry_runtime` | Install the source or downloaded wheel with the same interpreter used to run the command. From the source root, run `& $RuntimePython -m pip install .` |
| `PackageNotFoundError` in quickstart | Install the package in the selected environment before running `examples/quickstart.py` |
| `codex` or `claude` is unavailable | Follow [native client setup](GETTING_STARTED.md#prepare-a-native-client), add its executable directory to `PATH`, and confirm it in the launch shell |
| A Codex goal requests Claude Code | New Codex-executed goals use a Claude Opus manager. Prepare both clients for [coding and review](../examples/REVIEW.md#requirements); `--team-profile none` selects direct coding and retains the manager |
| Authentication or account-limit error | Open the selected native client, complete its sign-in or account procedure, and inspect the recorded task before continuing |

Show the installed package and entry points with:

```powershell
& $RuntimePython -m pip show foundry-runtime
& $RuntimePython -I -m foundry_runtime --help
& $RuntimePython -I -m foundry_runtime.recovery --help
```

## Project configuration

| Symptom | Resolution |
| --- | --- |
| `descriptor already exists with different settings` | Inspect that descriptor. Use `check` for an existing configuration; register a separate descriptor for another project |
| Repository identity mismatch or missing rule source | Restore the recorded workspace and rule file, or register the intended new workspace before starting a new task |
| `project execution owner is not configured` | Add the `execution` object described in the [configuration reference](CONFIGURATION.md#execution-owner-and-preflight), then check the descriptor before its first dispatch |
| `project preflight did not pass` | Read the printed preflight receipt's `argv`, `returncode`, `stdout`, and `stderr`; run the project check in its workspace and correct the reported project issue |
| `explicit compute requires both nonempty model and effort` | For Codex, supply `--coding-model` and `--coding-effort` together, or omit both to use native assignment |

The quickstart helper requires a new destination directory. Select another
directory to create an independent demonstration from the original baseline.
If the README quickstart already created the project, continue at
[task execution](../examples/README.md#2-execute-the-task-with-codex).

## Execution results

| Observation | Meaning and next action |
| --- | --- |
| The same command returns a prior result | The execution intent was replayed. Read the result; submit new instructions through the recorded session to continue |
| `unknown`, or an exited owner with unfinished state | Follow the exact task's reconciliation advice in the [recovery guide](RECOVERY.md#reconcile-interrupted-execution) |
| `candidate_verified` | The goal verified a committed candidate through its coding and review lifecycle. Inspect the candidate diff and project acceptance result |
| `task_acceptance: not_evaluated` | The project has no recorded acceptance observation for that result. Run its acceptance checks and record the maintainer's decision, or configure its observation hook |
| `Foundry code index unavailable` | Execution uses scoped direct reads. Configure `FOUNDRY_CODEGRAPH_ARGV` when the project requires indexed navigation |
| `automatic_delivery_unavailable` | Follow the retained command handle or read its exact task status. Hosted execution custody and chat delivery are separate host capabilities |

## Evidence for a problem report

Include the reproduction command, installed package version, Python and native
client versions, operating system, expected result, actual result, and relevant
receipt fields. Retain full logs locally and provide excerpts with synthetic
paths and identifiers. Use [GitHub Issues](https://github.com/tao19911024-gif/native-agent-orchestrator/issues)
and the [contribution guide](../CONTRIBUTING.md).
