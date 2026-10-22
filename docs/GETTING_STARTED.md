# Getting started

Begin with a separate local virtual environment and an existing Git project, as
shown in the README. The selected interpreter owns this installation; it does not
replace any previously installed Foundry release.

For a complete runnable walkthrough, use the [minimal greeting example](../examples/README.md).
The [recorded demonstration](DEMO.md) shows native execution and result verification.

## Register a project

`--foundry-project register` records the workspace's Git identity, rule sources,
and an evidence directory relative to the descriptor location. Registration also
inspects that identity. `--foundry-project check` rechecks a saved descriptor.
Neither operation invokes Codex or Claude.

Keep the descriptor, evidence, native transcripts, and account files local. A
descriptor can contain absolute private paths. The default rule source is
`AGENTS.md` when that file exists in the project.

## Enable execution deliberately

The generated descriptor is initially for onboarding. To enable execution, add an
`execution` object describing the project's existing preflight command:

```json
{
  "execution": {
    "owner": "foundry",
    "preflight_argv": ["python", "scripts/preflight.py"]
  }
}
```

Merge this fragment into the generated descriptor. Use a command that actually
exists in the target project. An empty `preflight_argv` is supported for a project
that intentionally has no preflight. Project-specific input and result adapters
remain owned by that project.

Install and authenticate a native Codex or Claude client separately. Review the
selected client's permissions and the target project's execution rules. Real
model tasks consume that account's allowance or API budget.

For a task order already approved under those project rules:

```powershell
& $Python -m foundry_runtime --project-config $Config `
  --order D:/orders/TASK1.md --tag TASK1 --provider codex
```

Use `--foundry-goal --team-profile adaptive` for the existing goal/team path. Read
the CLI help and the project's requirements before selecting roles or starting
work. A project may require additional preflight or acceptance hooks.

## Retain execution ownership

Keep the returned handle, exact task identity, and emitted evidence binding.
Inspect the bound execution rather than guessing the newest run directory. A
provider turn finishing and a process exiting are separate observations from a
verified goal result and project acceptance.

Ordinary recovery retains recorded runtime and session provenance. Do not change
the interpreter, descriptor bytes, or session owner of an existing task to make a
resume appear successful. Runtime upgrade and role-transfer commands have their
own existing lifecycle requirements; consult their CLI help and recorded evidence.
