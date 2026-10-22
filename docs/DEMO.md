# Greeting task demonstration

This demonstration was executed on October 22, 2026, using Native Agent
Orchestrator 0.1.0a92, Python 3.13, Windows, and the native Codex client. The
workspace path included spaces. Paths below use `<example-root>` for the directory
selected when running the [minimal example](../examples/README.md).

## Project setup

```powershell
$RuntimePython = (Resolve-Path .\.venv\Scripts\python.exe).Path
$ExampleRoot = Join-Path (Split-Path $PWD.Path -Parent) 'nao-example'
& $RuntimePython .\examples\quickstart.py $ExampleRoot
```

The command created the example repository, passed the baseline test, registered
the project, and inspected its configuration:

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

## Native task execution

```powershell
$ProjectConfig = Join-Path $ExampleRoot 'project.json'
$TaskOrder = Join-Path $ExampleRoot 'project\TASK1.md'
& $RuntimePython -I -m foundry_runtime --project-config $ProjectConfig `
  --order $TaskOrder --tag TASK1 --provider codex --launcher exec
```

One Codex worker implemented the task. The dispatcher returned exit code `0`
and retained the execution package, log, and final report under the project's
configured `runs` directory. The project status command reported:

```powershell
$TaskStatus = & $RuntimePython -I -m foundry_runtime --foundry-project status `
  --project-config $ProjectConfig --tag TASK1 | ConvertFrom-Json
$TaskStatus.runs | Select-Object tag, recorded_state, returncode
```

```text
tag    recorded_state    returncode
TASK1  completed         0
```

The implementation changed `greet.py` as follows:

```diff
 def greet(name: str) -> str:
+    name = name.strip() or "world"
     return f"Hello, {name}!"
```

The worker added three tests to `test_greet.py`. The two files remained
uncommitted for inspection.

## Result verification

The maintainer ran `python -m unittest -v` in the example workspace after the
dispatcher returned. All four tests passed:

```text
test_empty_name (test_greet.GreetingTests.test_empty_name) ... ok
test_name_with_surrounding_whitespace (test_greet.GreetingTests.test_name_with_surrounding_whitespace) ... ok
test_named_greeting (test_greet.GreetingTests.test_named_greeting) ... ok
test_whitespace_only_name (test_greet.GreetingTests.test_whitespace_only_name) ... ok

----------------------------------------------------------------------
Ran 4 tests in 0.000s

OK
```

| Input | Verified result |
| --- | --- |
| `"Ada"` | `"Hello, Ada!"` |
| `"  Ada  "` | `"Hello, Ada!"` |
| `""` | `"Hello, world!"` |
| `"   "` | `"Hello, world!"` |

The final diff contained the name-normalization statement and the three new
test methods. Project preflight and execution records remained under the
generated descriptor's existing lifecycle.
