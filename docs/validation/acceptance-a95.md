# a93/a94 acceptance follow-up

The a95 follow-up addresses the runtime findings from external acceptance of
a93 and a94. Tests execute the installed distribution with synthetic task data.

| Finding | Resolution | Verification |
| --- | --- | --- |
| Ambiguous source authority | One source repository; historical migration and tagged-build procedure documented | Private a92 and public a92 runtime Git trees match exactly |
| Default changes before acceptance | Existing selector requires a target-version acceptance receipt | Missing/mismatched receipt preserves current selection; switch and rollback retain resolved tasks |
| Referenced review input lacks retrieval receipt | New long-field contexts require pointer/value digests | Missing and incorrect order digests rejected; correct digest accepted; legacy context accepted |
| Worker defaults lack token evidence | Workers/managers use 3,000; Codex reviewers use native settings | Role defaults, explicit overrides and historical settings verified |
| Fixed Git observation timeout | Existing task deadline controls subprocess timeout | Simulated 60-second observation receives sufficient time; expiry becomes a named goal block |
| Legacy assignment query reads growing or malformed logs | Read at most 1 MiB; malformed/unmatched input retains the existing refusal | Malformed and 200 MiB fixtures complete with a bounded read |
| Baseline projection can break usage | Use the existing unknown-result projection | A missing delivery field produces an uncovered baseline while the usage report returns |
| Capacity retry exception masks the cause | Small resume wrapper retains the previous package only for non-timeout failure | Original exceptions survive absent log paths; deadline stops receive no predecessor package |
| Recovered-assignment test uses a private fixture name | Use the existing public fixture | Assignment-refusal regression included in CI |

The installed public CI test selection passed 200 tests locally. Public CI repeats
the same installed-package checks, command entry checks and three example setups.
The capacity change is 26 changed lines when whitespace-only unindentation is
excluded (`git diff -w v0.1.0a94 -- src/foundry_runtime/_dispatch_capacity.py`).

The existing native-output experiment measured reviewer calls only. No worker
token-saving result is reported. A future worker-default change requires paired
coding-worker measurements; a95 retains the established worker limit meanwhile.

Project-owned predispatch retries remain with the consuming project. The related
adapter follow-up retains timeout output, names exhausted timeout attempts and
updates steering fixtures against the installed runtime. Project integration
follows that repository's branch and acceptance procedure.
