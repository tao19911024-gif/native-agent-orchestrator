# Greeting task demonstration

The [project showcase](SHOWCASE.md) includes a short terminal recording of the
completed continuation workflow and a runtime development case.

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

## Checkpoint continuation

The [continuation workflow](../examples/CONTINUATION.md) was executed on October
22, 2026, on Windows with Python 3.13 and runtime 0.1.0a92.

The first turn read the project, passed its baseline test, returned
`CHECKPOINT_READY`, and left the working tree clean. The second invocation used
`--resume` with the recorded session and `CONTINUE.md`.

| Observation | Result |
| --- | --- |
| Initial operation | `dispatch`, `completed`, exit code `0` |
| Continuation operation | `resume`, `completed`, exit code `0` |
| Native session | Identical in both execution records |
| Candidate tests | Four passed |
| Changed files | `greet.py`, `test_greet.py`; uncommitted |

The implementation trimmed the name and selected `world` for empty input.
The maintainer ran the complete unittest suite after continuation and inspected
the diff.

## Coding and independent review

The [review workflow](../examples/REVIEW.md) was executed on the same date and
environment. The goal used `--provider codex --launcher exec --foundry-goal
--team-profile none` and an explicitly named goal directory.

| Observation | Result |
| --- | --- |
| Controller outcome | `candidate_verified` |
| Persistent manager | Claude Opus through Claude Code |
| Coding and review | Codex |
| Candidate | One commit changing the greeting function and its tests |
| Review results | Two `PASS` verdicts, each bound to the candidate commit |
| Review findings | Empty in both results |
| Model independence | `recorded_coding_assignment_excluded` |
| Candidate tests | Four passed in the maintainer's post-run verification |
| Working tree | Clean |
| Goal recovery preflight | Exit code `0` |

The goal retained its manifest, outcome, coding session, review sessions, and
usage records. The project acceptance observation was `not_evaluated`; the
maintainer's post-run check confirmed the four required behaviors, committed
diff, and review verdicts.

## First-use verification

The source and release installation paths were checked again on October 22,
2026, using Python 3.13.3 and Codex CLI 0.159.3 on Windows. Source installation
started from a new public Git clone in a path containing spaces. Release
installation used the wheel, source archive, and checksum file downloaded from
the published `v0.1.0a92` release.

| Check | Result |
| --- | --- |
| Source installation in a new virtual environment | Package installation and both module and console entry points passed. |
| Release download and installation | Distribution SHA-256 values matched `SHA256SUMS.txt`; the installed wheel's CLI ran successfully. |
| Release archive example | The documented extraction and quickstart commands passed the baseline test, registration, and inspection. |
| Current source archive | All three scenario initializations passed with the release wheel's interpreter. |
| Installed-package and recovery tests | 29 tests passed: 20 installation/project-launch tests and the nine recovery cases from the development case study. |
| Direct native task | One completed Codex turn; four result tests passed; only the two task files changed. |
| Same-session continuation | Checkpoint left the baseline clean; both turns completed in the same session; four result tests passed. |
| Coding and independent review | `candidate_verified`; two `PASS` verdicts on one candidate commit; four result tests passed; working tree clean. |

Use the [installation guide](GETTING_STARTED.md) for environment selection and
the individual [example guides](../examples/README.md) for executable commands.
The [terminal recording](SHOWCASE.md) presents the same continuation lifecycle
and greeting behaviors.
