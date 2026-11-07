# Empty native mailbox waits

Status: verified implementation for 0.1.0a114. Issue: #55. Pull request: #56.

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
The installed candidate passes 971 offline tests and public CI. Independent
review passes. Original event replay retains actual background completion and
waiting-team behavior while making empty waits eligible on the next tick.
The same isolated development task passed both independent reviews and all five
external behavior checks. Complete task time changed from 31.62 to 11.78 minutes;
the new execution waited once for a real test process and collected its original
handle. Measurements and their scope are recorded in
[the validation report](../../validation/empty-native-waits.md).

## Delivery

Release packaging uses the integrated tag. Publication and new-task selection
follow RELEASING.md with an operator-approved receipt, the prior qualified
runtime for rollback and explicit active-task holds. Private release records
retain source identity, distribution checks, selection, entry-point verification
and generated-output disposition.
