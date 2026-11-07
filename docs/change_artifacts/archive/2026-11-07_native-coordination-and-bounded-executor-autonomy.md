# Native coordination and bounded executor autonomy

Implemented for 0.1.0a113 under issue #53 and PR #54, following the operator-approved architecture review.

The change consolidates native member, command and mailbox observations in the existing activity owner. It retains resumed members and permits another quiet-wait observation after new execution evidence. AppServer owns delivery.

The managed executor may directly implement bounded assignments and make reversible decisions within the assigned scope. Delegation serves parallel deliverables, distinct capability or context separation. The manager owns scope and delivery readiness.

For new launches, efficiency heuristics use the existing advisory hook channel. Review independence, candidate identity, explicit model choice and storage ownership keep their existing admission rules. Retained launch policies preserve their recorded behavior.

The implementation is split into event ownership, executor guidance and advisory efficiency policy commits. Verification covers resumed members, stale child-turn events, completion deduplication, waiting work, recorded policies and review admission. The installed distribution passed 957 offline checks and public CI. Original event replay restored both missed quiet observations at the existing threshold. An independent delivery oracle reproduced four failures on the original baseline and passed all five cases on the implemented source.

Independent review identified an integration gap in native reviewer exclusion when the execution root and members share implementation. Policy version 7 retains both the frozen executor assignments and the observed member assignments in the exclusion set. The correction passed independent review and regression checks; retained policies preserve their previous behavior.

The paired execution also exposed a root-only mailbox wait after all members and the command had completed. The activity owner now evaluates that empty-member case with the same progress threshold and pending-work checks. A frozen replay of the observed stall produces one root observation at 300 seconds while preserving command-completion deduplication.

Runtime source, relevant tests, configuration and architecture documentation are in scope. Existing installations and active project records remain owned by their recorded tasks. Release selection uses the existing release-rotation SOP with the prior qualified version as rollback.

The release evidence records the isolated same-task comparison, tagged-package qualification, publication, new-task selection and storage disposition. Task quality, elapsed time and repeated work are measured separately. Private task logs and release inventory remain with the local operation evidence.
