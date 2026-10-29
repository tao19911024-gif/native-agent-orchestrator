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
| `default_provider` | Optional `claude` or `codex` coding default for new Foundry goals |
| `default_disposable_scratch` | Optional boolean enabling registered disposable storage for new goals; omitted defaults to `false` |
| `history_roots` | Optional directory references used by historical recovery discovery |

Registration selects `AGENTS.md` when present. Repeat `--project-rule` to choose
other entry files. The format and identity rules are implemented by
[`descriptor`, `load`, and `inspect`](../src/foundry_runtime/_foundry_project.py).

Store the descriptor outside the source repository. Configure it before the
first dispatch; continuations retain its recorded identity and bytes.

### Default coding provider

Set `"default_provider": "claude"` to select Claude coding, correction and native
team backend for new goals. Explicit provider, launcher, compute-profile, coding
or correction model selections take precedence; otherwise the project default precedes the
task-kind table. The task kind continues to supply team shape and other omitted
role choices. Independent reviewer selections retain their existing rules.

For example, `--task-kind cross-module --print-staffing` with this descriptor
selects Claude coding and an adaptive Claude team, and reports `project` as the
coding/correction source and team backend source. `--provider codex` overrides
the project setting. Without `default_provider`, task-kind behavior is unchanged.

Descriptors and project adapters expose the same optional `default_provider`
and `default_disposable_scratch` fields through `ProjectExecution` and
`goal_defaults`. Adapters without those attributes retain the existing defaults.

Projects can also declare `default_staffing` on the descriptor or adapter:

```json
{
  "default_provider": "claude",
  "default_staffing": {
    "compute_profile": "claude",
    "manager_profile": "opus",
    "team_profile": "adaptive",
    "review_profile": "opus-sol"
  }
}
```

Each field supplies an omitted CLI choice. Explicit CLI profiles and model pairs
take precedence; a compute profile applies only to its coding provider. Recovery,
amendments and runtime boundary continuations keep their recorded assignments.
`--print-staffing` reports the resolved choices and their source before execution.

New Claude profiles use Sonnet for implementation and correction, including
adaptive and cross-module work. The separate Opus manager delegates edits and
tests to that worker. Claude goals default to Opus plus Codex Sol review;
`--provider claude` selects admission and coding, while `--review-profile`
selects the reviewer pair. `--review-profile claude` explicitly selects the
Claude-only pair. A known Claude coding model selects the Claude coding
transport when its provider and launcher are omitted.

`opus-sol` resolves both tiers through the existing vendor catalogue. The Sol
slot therefore follows vendor model upgrades when a new goal is staffed.
Recorded assignments keep their concrete model IDs. Automatic reviewer choices
are reselected when their models participated in coding; a separate coordinator
is not an implementation participant. Explicit reviewer models remain pinned.

Automatic Codex reviewers use the existing escalation and tier-state decisions
between correction rounds. Repeated named failures, repeated P0/P1 findings or
evidence contradictions can promote Sol to Astra; a cleared signal restores its
recorded baseline. A measured Sol provider-limit refusal with completed process
cleanup permits one Astra attempt through the existing review-transfer path.
Completed sibling verdicts and previous receipts are retained. A second provider
failure remains available for explicit recovery. Outcomes record the slot,
reason and source receipt or round in `review_tier_decisions`.
Adapter values follow the same provider and boolean contracts; invalid values
are reported during new-goal configuration. Use `--no-disposable-scratch` to
override an enabled project storage default for one new goal.
These defaults apply only to new goals. Direct dispatch, standalone review,
amendments, recovery and runtime boundary continuations retain their recorded
configuration. Preserve descriptor bytes and settings referenced by existing tasks
when changing the policy for new work.

The project execution prompt directs workers to read rule entries once, reuse
unchanged material already in context, and locate task-relevant sections in
referenced guidance and source files. Combined tool output must fit the selected
output budget. Large results remain in task evidence; after truncation, workers
retrieve the required missing ranges. Project-mandated full coverage and checks
remain part of the task. This guidance uses the existing prompt and introduces
no additional reading gate or output-limit override.

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

An explicit task deadline also applies to these hooks. A deadline reached before
admission prevents the hook from starting; a deadline reached while it runs
triggers the existing owned-process cleanup and records a `not_evaluated`
observation with the deadline error. Omitting a task deadline leaves hook duration
unbounded by a task clock.

Hooks observe produced evidence. The coding role runs tests and creates the
candidate; the project observation reports the acceptance decision.

## Native launch settings

`launch` accepts project-scoped settings such as:

```json
{
  "launch": {
    "FOUNDRY_BASH_CHARS": 12000,
    "FOUNDRY_CODEGRAPH_ARGV": []
  }
}
```

| Setting group | JSON type and purpose |
| --- | --- |
| `FOUNDRY_CONTEXT_TOKENS`, `FOUNDRY_COMPACT_TOKENS`, `FOUNDRY_COMPACT_PERCENT` | Integers configuring native context and compaction inputs |
| `FOUNDRY_TOOL_TOKENS` | Codex tool-output override in tokens; zero retains native settings; omitted defaults to 3,000 for workers/managers and native settings for Codex reviewers; a positive value selects an explicit limit |
| `FOUNDRY_BASH_CHARS` | Integer configuring the Claude Bash output size and Codex read-accounting inputs |
| `FOUNDRY_WAIT_INTERVAL_SECONDS` | Integer configuring native wait intervals |
| `FOUNDRY_CODEX_READ_SINGLE`, `FOUNDRY_CODEX_READ_TURN`, `FOUNDRY_CODEX_READ_INDEX_CHARGE` | Integer read-accounting settings for Codex |
| `FOUNDRY_CLAUDE_READ_SINGLE`, `FOUNDRY_CLAUDE_READ_TURN`, `FOUNDRY_CLAUDE_READ_INDEX_CHARGE` | Integer read-accounting settings for Claude |
| `FOUNDRY_CODEGRAPH_ARGV` | String array selecting the optional code-index command; an empty array selects direct reads |
| `FOUNDRY_CODEGRAPH_STATE_DIR` | String selecting the optional index state directory |

For a new call, descriptor `launch` values override the process environment.
Omitted values use the environment and then the runtime defaults. Continuations
use the recorded launch settings. Supported keys and default resolution are
owned by [`validate_project_launch` and `settings`](../src/foundry_runtime/_launch_context.py).

For Codex, zero omits `tool_output_token_limit` from native arguments and keeps
the client's selected configuration. A per-command `max_output_tokens` value
does not necessarily replace the client's outer tool-result limit. With several
reads in one tool call, the combined output can exceed that outer limit even
when each command completes successfully. Use focused results and retrieve only
missing sections after truncation. Existing launches retain their recorded
positive limit, including the earlier default of 3,000 tokens. Set
`FOUNDRY_TOOL_TOKENS` to `3000` to select that limit for a new project call.

## Provider selection

New goals can opt into managed disposable storage with `--disposable-scratch`
or the project's `default_disposable_scratch: true`. The runtime registers a fresh
directory under that goal's evidence directory, supplies it in worker instructions
and records the worker's temporary-directory environment in its launch context.
See the [storage contract](STORAGE_RETENTION.md#explicit-disposable-storage-for-new-goals)
for completion, recovery, report retention and cleanup accounting.

`--provider codex` or `--provider claude` selects the provider for a new task.
For a goal, it selects admission and coding; independent review has its own
profile or explicit assignment. Native clients own their
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
