# Empty native mailbox waits

Status: implementation and verification. Issue: #55.

## Scope and ownership

CodexMemberActivity remains the owner of observed native members, commands and
waits. AppServer retains input ownership and scoped delivery. Empty root waits
become eligible on the next observation tick. Waiting teams retain the existing
quiet-progress interval. Command completion and empty-wait delivery share the
existing observed-event set to avoid two notifications for the same completion.

Native wait configuration, frozen prompts, process custody, pending input,
review admission, historical recovery and active installations are unchanged.
The prior qualified runtime supplies rollback for new-task selection.

## Verification

The installed a113 baseline reproduced five regression failures. The changed
source passes all 51 native-wait policy tests, including immediate empty waits,
unchanged-result suppression, pending-input ownership and background completion.
Remaining verification covers original event replay, installed-package checks,
independent review and the same isolated development task used for the preceding
coordination change. Quality, elapsed time, use of original handles, repeated
reads and actual token usage are compared against retained task evidence.

## Delivery

Commit and publish through the repository review process. Build the integrated
release tag, verify distributions, publish the release and select it for new
tasks using an operator-approved receipt. Retain the prior qualified runtime,
active-task dependencies and private evidence. Close generated outputs through
the existing storage lifecycle.
