# Operating environment

## Installation and native clients

| Area | Operating details |
| --- | --- |
| Platform | Release verification uses Windows and Python 3.13. Requirements and dependencies are declared in `pyproject.toml` |
| Native clients | Follow [native client setup](GETTING_STARTED.md#prepare-a-native-client) for Codex or Claude Code. The selected account supplies model access and usage allowance |
| Distribution | GitHub Releases supplies a wheel, source distribution, and SHA-256 checksums. The source distribution includes documentation and examples |
| Package identity | Distribution and command names are listed in the [package table](../README.md#package-and-command-names) |
| Model catalogue | New goal assignments resolve through the installed native clients. `PDCL_MODEL_CATALOGUE=snapshot` selects the retained offline catalogue for tests |
| Code index | CodeGraph is optional. Configure its command through `FOUNDRY_CODEGRAPH_ARGV`; execution uses scoped direct reads when the index is unavailable |

See [installation](GETTING_STARTED.md) and [configuration](CONFIGURATION.md) for
commands and settings. Select platform-specific integration tests through the
[contribution guide](../CONTRIBUTING.md).

## Execution permissions

The Codex profile uses `danger-full-access` with approval policy `never`. The
Claude profile uses `bypassPermissions`. Native executors run under the current
user account. Project rules, preflight, and acceptance criteria come from the
selected project. Profiles are defined by
[`coding_policy` and `claude_policy`](../src/foundry_runtime/_execution_profile.py).

## Host paths and lifecycle

Some temporary-output guidance and candidate-closure paths refer to Windows D:
or H: layouts. Runtime upgrade aliases such as `current` and a bare version
resolve under `D:/Foundry`; the command also accepts an explicit target
interpreter. Historical release retention searches local installation roots.

A task records its interpreter, project context, and native session. Continue
through those records. Select a new installation for new work, or use explicit
runtime upgrade at a settled goal boundary. See [recovery](RECOVERY.md).

## Hosted execution and delivery

Windows hosted execution uses the host's Task Scheduler integration and retains
one launch directory. Its custody covers desktop exit while the Windows user
remains logged in. User logoff and machine reboot end that custody scope.

Chat delivery depends on an originating-host receiver. Read `host_follow_up` and
`delivery_status` in the launch binding. When it reports
`automatic_delivery_unavailable`, retain the active command handle or inspect
its exact status path. Use `--hosted-status` for hosted launches and the retained
goal or dispatch status command for direct launches.
