# Contributing

Open an issue with the expected behavior, actual result, Foundry version, Python
version, OS, and a small reproduction. Use synthetic project names and credentials.
Include the relevant error or receipt fields after removing private paths,
business content, account identifiers, transcripts, and secrets.

Keep runtime execution, provider session custody, and project acceptance under
their existing owners. Prefer extending the authoritative path over introducing
a second launcher, scheduler, or state store.

## Offline installed-package checks

Install the project into a dedicated virtual environment with the test extra:

```powershell
& .\.venv\Scripts\python.exe -m pip install '.[test]'
$env:PDCL_MODEL_CATALOGUE = 'snapshot'
& .\.venv\Scripts\python.exe -m pytest tests/test_installed_runtime.py tests/test_project_launch.py
```

These tests check installed import independence, CLI help, and descriptor-based
lifecycle behavior with synthetic execution. Installation matters: several tests
intentionally require imports from `site-packages` rather than the source tree.
Restore the previous environment setting after the check in a shared shell.

Select further tests for the contracts affected by a change. Tests that need a
native provider, Windows service facility, external project or other live
integration must run only when that environment is intentionally selected.
Report passes, failures and existing skips separately, together with their scope.

## Contributions and license

Contributions must be code and documentation you can license under this
repository's MIT license. Preserve third-party notices. Do not commit private
task history, live descriptors, credentials, account files, or runtime evidence.
Runtime tests and sanitized regression fixtures belong in the source repository;
business acceptance remains with each target project.
