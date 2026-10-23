# Release and runtime upgrade

The [source authority](SOURCE_AUTHORITY.md) is this repository. The release
operator publishes a candidate from a reviewed commit. Publication and default
runtime selection are separate operations.

## Build and publish

1. Complete the pull request and installed-package checks. Set the version in
   `pyproject.toml` and document the release in `CHANGELOG.md`.
2. Merge the reviewed change. Create the immutable `v<version>` tag at the
   integrated commit; never replace a published tag or its assets.
3. Add a clean detached checkout of that tag. Confirm `git status --porcelain`
   is empty and `git rev-parse HEAD` equals `git rev-list -n 1 <tag>`.
4. Run `python -m build` in that checkout, writing distributions to the release
   operation directory. Install the wheel into a new versioned environment.
5. Run installed-package checks and the three quickstart scenarios. Compare
   installed runtime files with the wheel and tagged source. Record package
   hashes in `SHA256SUMS.txt`.
6. Publish the wheel, source archive and checksums under the matching GitHub
   tag. Mark Python alpha versions as prereleases. Record the source repository,
   full commit, tag, version, build command, package hashes and test results.
7. Complete storage disposition. Retain compact evidence and remove the clean
   build checkout and disposable test outputs after verification.

If a published release needs correction, issue a new version. A maintenance
folder may hold build outputs and evidence; builds read the clean tagged checkout.

## Install a versioned runtime

Download the assets from the matching [GitHub release](https://github.com/tao19911024-gif/native-agent-orchestrator/releases).
Compare each file's `Get-FileHash -Algorithm SHA256` result with `SHA256SUMS.txt`.
Create a new environment for the new version:

```powershell
$RuntimeRoot = 'C:/AgentRuntime'
$Version = '0.1.0a95'
$ReleaseRoot = Join-Path $RuntimeRoot "releases/$Version"
python -m venv $ReleaseRoot
$RuntimePython = Join-Path $ReleaseRoot 'Scripts/python.exe'
& $RuntimePython -m pip install "./foundry_runtime-$Version-py3-none-any.whl"
& $RuntimePython -I -m foundry_runtime --help
& $RuntimePython -I -m foundry_runtime.recovery --help
```

Install into a distinct versioned directory. Retain the previous qualified
environment as the rollback runtime, including its original interpreter path.

## Select the runtime and project entry points

Installation leaves the shared selection unchanged. A second reviewer accepts
the exact release, or the operator explicitly approves that version for selection.
Record that decision in an acceptance JSON file:

```json
{
  "version": "0.1.0a95",
  "source_tag": "v0.1.0a95",
  "source_commit": "<full tagged commit ID>",
  "reviewer": "<reviewer identity>",
  "decision": "accepted",
  "evidence": "<acceptance report path>"
}
```

Use `operator-approved` for an explicit operator decision and retain its source
in `evidence`. The release operator records an existing decision; the operator's
own build checks do not constitute a second review. Keep receipts local with the
release record. The selection command requires a receipt for the target version,
records its path and digest, and atomically updates the existing selection file.

Wrappers use the selected interpreter for new work. Save the previous selection,
then run:

```powershell
$Selection = Join-Path $RuntimeRoot 'current.json'
$RollbackPython = (Get-Content -LiteralPath $Selection -Raw | ConvertFrom-Json).python
& $RuntimePython -m foundry_runtime.release_selection `
  --selection $Selection --python $RuntimePython `
  --acceptance-receipt ./acceptance-0.1.0a95.json
```

Check each project wrapper's interpreter selection. An explicit project runtime
pointer must be updated through its existing selection mechanism. Descriptor
projects can use the selected interpreter with their existing `--project-config`.
Run the project configuration check with the new interpreter before new work:

```powershell
& $RuntimePython -m foundry_runtime --foundry-project check `
  --project-config 'C:/Projects/example/foundry-project.json'
```

For new Codex calls, a95 defaults to 3,000 tool-output tokens for workers and
managers, and native output settings for independent reviewers. Omit
`FOUNDRY_TOOL_TOKENS` to use these role defaults. An explicit zero retains native
settings for every role; a positive value applies that override. Change only
new-work configuration when adopting role defaults. Recorded task settings stay
with their existing tasks.

Running tasks retain their loaded runtime. Recovery and continuation use the
interpreter and launch settings recorded in task provenance. Preserve their
versioned environments and evidence; updating the shared selection does not
migrate an existing task. Project recovery wrappers must continue to pass the
recorded interpreter where required by their recovery interface.

## Rollback and retention

Select the retained interpreter to roll back new work:

```powershell
& $RuntimePython -m foundry_runtime.release_selection `
  --selection $Selection --python $RollbackPython `
  --acceptance-receipt ./acceptance-rollback.json
```

Restore any changed project runtime pointers and new-work launch settings from
the release record. Tasks started with the new release retain that environment
for their own recovery. Apply [storage retention](STORAGE_RETENTION.md) to build
outputs and older releases after checking their recorded dependencies.
