# Retained review feedback loop

- Date: 2026-10-23
- Status: complete
- Owner: existing goal delivery and amendment mechanisms

## Outcome

Goal and hosted status now connect retained review findings to their task,
candidate, artifact references and recorded-session amendment command. The
selected development scenario concerns a conflict response that omitted the
command identifier and latest state after retries.

## Architecture and scope

The delivery projection reads existing outcomes and manifests and reuses the
goal's finding deduplicator. The amendment owner selects retained execution
settings, resumes the coding session and produces the successor candidate and
reviews. Recovery and amendment commands share one formatter for the recorded
project entry, interpreter and environment. Existing predecessor relations
connect both results in the delivery baseline.

Affected surfaces are goal and hosted status, the shared invocation formatter,
installed tests, public CI and operator documentation. Execution policy, review
requirements and project acceptance retain their existing owners.

## Verification

The related installed-package regression passed 159 tests. The built wheel
passed the public CI selection of 116 tests. Wheel and source archive builds
completed successfully.

The correction regression uses real temporary Git projects, subprocess checks
and controlled native responses. It covers PASS with findings and BLOCK with
findings, deduplication, task and candidate bindings, the generated CLI command,
original-session continuation, new candidate checks, completed reviews and
baseline traversal of both candidates. Original outcome files remain unchanged.
Status tests cover automatic rounds, partial reviews, missing runtime/session
records, quoted paths and retained native environment settings.

A historical development outcome was inspected read-only. Its two findings,
candidate and original runtime/session command were resolved successfully;
the retained goal files kept their content hashes. Private business paths and
session identifiers remain in local evidence storage.

Implementation review covered association correctness, lifecycle ownership,
command quoting, read-only behavior, performance and operator documentation.

## Delivery and storage

Retain compact local reports, original failure logs, build results and source
provenance. Remove the task's disposable environment and scratch outputs after
publication. A source commit revert rolls back this change; task record formats
remain unchanged.
