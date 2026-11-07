# Empty native mailbox waits

Version 0.1.0a114 separates empty root waits from observed background work and
pending input through the existing member observer and AppServer transport.

| Observed state | Delivery behavior |
| --- | --- |
| Native command running | Retain the wait; completion wakes its recorded owner |
| Active member working | Retain the member's existing ownership |
| Operator or manager input pending | Continue the existing input workflow |
| Root waiting with no active command, member or pending input | Deliver an observation on the next transport tick |
| Root and active members all waiting | Retain the existing quiet-progress interval |
| Completion already issued for this wait | Suppress an additional empty-wait observation |

The native wait timeout configuration, process cleanup and frozen launch
instructions retain their existing behavior. Repeated waits and identical command
results do not re-arm the observation; a successful edit or changed result does.

## Verification

Five installed-baseline regression failures pass after the repair. The candidate
installation passes 971 offline tests. Independent code review passed, including
a separate 66-test run. Recorded process replay produces one actual background
completion and zero previously returned command notifications. The retained
waiting-team replay continues to use the 300-second progress threshold.

Two original empty waits lasted 300.018 and 300.017 seconds. Replaying the same
events against the new observer makes them eligible after 0.010 and 0.365 seconds.
The replay checks at recorded event timestamps and on a 0.5-second clock grid.
Both versions emit two observations and zero
replayed command completions. These are replay eligibility times; delivery and
model response are measured separately in the native task.

## Same-task execution

Both executions repair the same resumed-member and quiet-observation defects
from an identical source tree and order. Coding uses Sol at high effort, Opus
coordinates, and independent Opus and Sonnet reviewers evaluate the candidate.
The retained preceding coordination candidate supplies the baseline. Its frozen
team configurations match the measured runtime; its two empty-wait observations
also reproduce on the final a113 installation. Neither execution received
operator steering.

| Measurement | Retained baseline | New execution |
| --- | ---: | ---: |
| Complete task elapsed time | 31.62 min | 11.78 min |
| Coding elapsed time | 22.11 min | 7.77 min |
| Empty mailbox waits | 2 | 0 |
| Observed mailbox waiting | 600.047 s | 17.706 s |
| Coding tokens, including cached input | 3,041,209 | 1,864,755 |
| Uncached coding input | 93,246 | 76,709 |
| Observed coding, manager and review tokens | 5,139,694 | 3,749,527 |
| Read outputs | 27 | 21 |
| Truncated read outputs | 5 | 2 |
| Repeated initial read ranges within the same session | 9 | 9 |
| Initial overlapping reads after truncation | 7 | 0 |
| Independent candidate reviews | PASS / PASS | PASS / PASS |
| Shared external behavior checks | 5 / 5 | 5 / 5 |

The new execution's only mailbox wait belonged to an unfinished test process.
Its completion produced one wake-up, and the worker collected the original
handle once. No duplicate command or empty-wait notification was emitted.

Original-event replay isolates removal of the empty-wait delay. Whole-task time
and tokens also reflect the model's execution choices and provider timing.
Token counts include observed cached input and output, and measure usage rather
than account charges. Initial read-range counts cover literal file reads before
the first edit; post-edit verification is counted separately from repeated reads.
The change removes idle latency without changing the reading policy.
