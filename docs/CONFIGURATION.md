# Configuration reference

The project descriptor selects a Git workspace, its rule files, execution owner,
and evidence directory. Generate it with `--foundry-project register`, configure
execution before starting a task, and inspect it with `--foundry-project check`.

## Project fields

| Field | Meaning |
| --- | --- |
| `schema_version` | Descriptor format written by registration |
| `project_id` | Identity derived from the Git common directory |
| `workspace` | Absolute path to the Git workspace root |
| `git_common_dir` | Absolute path to the repository's Git common directory |
| `rule_sources` | Rule-file references; relative entries resolve against `workspace` |
| `evidence_root` | Absolute directory for task records; registration places it under the descriptor's directory |
| `execution` | Execution owner, preflight command, and optional project observation hooks |
| `launch` | Per-project native launch settings |
| `history_roots` | Optional directory references used by historical recovery discovery |

Registration selects `AGENTS.md` when present. Repeat `--project-rule` to choose
other entry files. The format and identity rules are implemented by
[`descriptor`, `load`, and `inspect`](../src/foundry_runtime/_foundry_project.py).

Store the descriptor outside the source repository. Configure it before the
first dispatch; continuations retain its recorded identity and bytes.

## Execution owner and preflight

Merge this object into the generated descriptor:

```json
{
  "execution": {
    "owner": "foundry",
    "preflight_argv": ["python", "-m", "unittest", "-v"]
  }
}
```

`owner` selects the runtime execution path. `preflight_argv` is an argument array
executed in the workspace. Use an absolute interpreter path to select a specific
environment. An empty array configures a project with no separate preflight.

Preflight receives a JSON object on stdin with `workspace`, `tag`, `operation`,
`instructions`, `preflight_input`, and `execution_intent_id`. Exit code `0`
permits execution to proceed. The runtime retains the command, exit code,
stdout, stderr, and instruction digest in a preflight receipt.

`--project-preflight-input` loads a project-owned JSON object for the invocation.
`--project-resume-preflight-input` supplies the object retained for worker
continuations within a goal. The request and receipt are defined by
[`ProjectExecution.preflight`](../src/foundry_runtime/_project_execution.py).

## Project observation hooks

| Optional field in `execution` | Purpose |
| --- | --- |
| `review_context_argv` | Read project evidence and return review criteria and findings |
| `accept_result_argv` | Read project evidence and return the project's acceptance result |

Each hook is an argument array executed in the workspace. It reads a JSON
request from stdin and writes a JSON response to stdout. The response preserves
the request's `schema_version` and exact `subject`.

`review_context` responses contain `criteria`, `findings`, and `checks` arrays;
their status is `ready` or `awaiting_project_action`. `accept_result` responses
contain `reasons` and `evidence` arrays; their status is `accepted`, `not_met`,
or `not_evaluated`. Each invocation retains its request, response, stderr,
process cleanup, and observation. The hook contract is defined by
[`observe`](../src/foundry_runtime/_project_hooks.py).

Hooks observe produced evidence. The coding role runs tests and creates the
candidate; the project observation reports the acceptance decision.

## Native launch settings

`launch` accepts project-scoped settings such as:

```json
{
  "launch": {
    "FOUNDRY_TOOL_TOKENS": 3000,
    "FOUNDRY_BASH_CHARS": 12000,
    "FOUNDRY_CODEGRAPH_ARGV": []
  }
}
```

| Setting group | JSON type and purpose |
| --- | --- |
| `FOUNDRY_CONTEXT_TOKENS`, `FOUNDRY_COMPACT_TOKENS`, `FOUNDRY_COMPACT_PERCENT` | Integers configuring native context and compaction inputs |
| `FOUNDRY_TOOL_TOKENS`, `FOUNDRY_BASH_CHARS` | Integers configuring tool-output limits |
| `FOUNDRY_WAIT_INTERVAL_SECONDS` | Integer configuring native wait intervals |
| `FOUNDRY_CODEX_READ_SINGLE`, `FOUNDRY_CODEX_READ_TURN`, `FOUNDRY_CODEX_READ_INDEX_CHARGE` | Integer read-accounting settings for Codex |
| `FOUNDRY_CLAUDE_READ_SINGLE`, `FOUNDRY_CLAUDE_READ_TURN`, `FOUNDRY_CLAUDE_READ_INDEX_CHARGE` | Integer read-accounting settings for Claude |
| `FOUNDRY_CODEGRAPH_ARGV` | String array selecting the optional code-index command; an empty array selects direct reads |
| `FOUNDRY_CODEGRAPH_STATE_DIR` | String selecting the optional index state directory |

For a new call, descriptor `launch` values override the process environment.
Omitted values use the environment and then the runtime defaults. Continuations
use the recorded launch settings. Supported keys and default resolution are
owned by [`validate_project_launch` and `settings`](../src/foundry_runtime/_launch_context.py).

## Provider selection

`--provider codex` or `--provider claude` selects the provider for a new task.
For a goal, it applies to coding and review roles. Native clients own their
authentication and account configuration.

A new Codex-executed goal starts with a persistent Claude Opus manager. Install
and authenticate both Codex CLI and Claude Code for the
[coding and review workflow](../examples/REVIEW.md#requirements). Selecting
`--team-profile none` uses a direct coding worker and retains that manager.
The initial Opus policy also applies when `--manager-profile astra` is supplied;
the goal owner transfers coordination after a measured Claude quota refusal.
Retained goals preserve their recorded manager and transfer history. The policy
is implemented by [`handle_mode`](../src/foundry_runtime/foundry_entry.py) and
[`configuration_for`](../src/foundry_runtime/_foundry_manager.py).

`--coding-model` and `--coding-effort` select an explicit coding assignment.
Codex requires both options together. Claude also accepts a model-only selection.
Omitting the pair preserves native assignment behavior. Model IDs and availability
come from the installed client; goal staffing is inspectable with `--print-staffing`.

`PDCL_MODEL_CATALOGUE=snapshot` selects the retained catalogue for offline checks.
Live workflows use normal native discovery. Package and command names are listed
in the [README](../README.md#package-and-command-names).
