# Preserve native tool output sizing

- Date: 2026-10-23
- Status: complete
- Issue: [13](https://github.com/tao19911024-gif/native-agent-orchestrator/issues/13)

## Objective and authority

Reduce repeated reads following oversized native tool results. The existing
launch-context owner selects and freezes output settings; the native client
owns output presentation. The selected change removes the default Codex
3,000-token override by using zero for native settings. Explicit positive
values and retained launch records continue through the same argument builder.
Claude output controls and review request content retain their existing behavior.

The source scope is `_launch_context.py`, its installed tests and configuration
reference. No reader, log store, scheduler or prompt-policy version is added.
The cross-transport setting and native comparison require this change artifact.

## Verification

Compare the same corrected conflict-response candidate and complete context
using the existing review owner and read policy. Run A-B-B-A groups with two
independent reviewers each; record native input, cached input, output, request
counts and repeated reads. Require complete acceptance coverage. Verify default,
explicit and historical launch settings across exec, app-server and review;
run installed regressions and public CI. Review and simplify before publishing.

## Delivery and retention

Publish sanitized measurements and formal operator documentation. Preserve raw
native evidence locally and retain one tested wheel. Assign disposable outputs
to this experiment and record cleanup disposition. Existing runtimes remain at
their recorded paths. Source rollback restores the previous new-launch default;
an explicit project value of 3000 selects the prior output limit.

## Results

Eight native reviews in A-B-B-A order retained byte-identical review inputs and
the same candidate/context. All passed. Total input fell from 693,621 to 486,159;
output fell from 6,768 to 4,031. Uncached input increased from 125,301 to 127,631.
Requirements were extracted eight times with the prior override and four times
with native settings; guidance reads fell from nine to four. Model requests fell
from 20 to 14. The change affects one production module.

Installed regression passed 221 tests with zero failures or skips; all 91 package
files match the built wheel. Source review confirmed ownership, frozen historical
settings, explicit project overrides and unchanged native review contracts.
[The report](../../REVIEW_TOKEN_ANALYSIS.md#native-tool-output-follow-up) retains
the full comparison. Public data contains only allowlisted measurements.
