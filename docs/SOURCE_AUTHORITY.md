# Source authority and release lineage

[native-agent-orchestrator](https://github.com/tao19911024-gif/native-agent-orchestrator)
is the sole source repository for the Foundry runtime. Runtime changes, tests,
operator documentation, version declarations and release tags belong here.
The distribution remains named `foundry-runtime`; its version is declared in
`pyproject.toml`. Installed environments are immutable release artifacts.

## Historical transition

The public repository began with an export of private commit
`97a074451d3cb2c7d9b45b1d13d7adaa329be18c`. `migration-source.json` records that
initial export. It is an import record, not the source revision of later releases.
Earlier private history remains in the retained private repository.

The private a92 commit `937104abfd0cdde922473d595f668adcb17a1289` and public tag
`v0.1.0a92` have the identical `src/foundry_runtime` Git tree:
`1bfda67c953c2f7560ba7810e70439330c6b9cfd`.
The public repository carries subsequent runtime development. Release a93 is
tagged at `321032a0`; a94 is tagged at `e9e4d2f`. Resolve tags to full commit IDs
when recording or rebuilding a release.

The former private main checkout and the G4 successor worktree are superseded
archives. Their source changes are retained as archive commits, with a local
archive index identifying the original branches and revisions. Archived source
is inspected for historical provenance; new fixes are implemented here.
Private business records and archive inventories remain local.

## Ownership

| Surface | Authority |
| --- | --- |
| Runtime implementation and version | This repository and `pyproject.toml` |
| Released source | Immutable `v<version>` tag and its full commit |
| Wheel and source archive | Clean checkout of that tag, recorded SHA-256 hashes |
| New-task runtime selection | Existing shared selection command and acceptance receipt |
| Running and recovering tasks | Their recorded interpreter, launch settings and evidence |
| Project admission, checks and integration | The consuming project's source repository |
| Build logs and local inventories | Private release operation directory |

Maintenance directories store evidence and distributions. They are not source
repositories. Copying a source module into an installed environment is not a
release procedure. Follow [RELEASING.md](RELEASING.md) for publication, acceptance,
selection and rollback.
