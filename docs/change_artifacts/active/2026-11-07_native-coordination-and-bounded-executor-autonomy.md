# Native coordination and bounded executor autonomy

Status is active. This runtime change follows issue #53 and the operator-approved architecture review.

The change consolidates native member, command and mailbox observations in the existing activity owner. It retains resumed members and permits another quiet-wait observation after new execution evidence. AppServer owns delivery.

The managed executor may directly implement bounded assignments and make reversible decisions within the assigned scope. Delegation serves parallel deliverables, distinct capability or context separation. The manager owns scope and delivery readiness.

For new launches, efficiency heuristics use the existing advisory hook channel. Review independence, candidate identity, explicit model choice and storage ownership keep their existing admission rules. Retained launch policies preserve their recorded behavior.

Implementation proceeds in three commits for event ownership, executor guidance and advisory efficiency policy. Verification covers resumed members, stale events, completion deduplication, waiting work, recorded policies and review admission. Original event replay and an isolated same-task baseline/candidate comparison provide execution evidence. Public CI, installed-package checks and independent review precede release publication and new-task selection.

Runtime source, relevant tests, configuration and architecture documentation are in scope. Existing installations and active project records remain owned by their recorded tasks. Release selection uses the existing release-rotation SOP with the prior qualified version as rollback.

Public results will record task quality, elapsed time and repeated work. Private logs and release inventory remain in operation evidence. Archive this artifact after publication and storage closeout.
