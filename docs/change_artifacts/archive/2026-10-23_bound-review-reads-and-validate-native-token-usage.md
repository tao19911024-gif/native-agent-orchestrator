# Bound review reads and validate native token usage

- Date: 2026-10-23
- Status: complete
- Issue: [11](https://github.com/tao19911024-gif/native-agent-orchestrator/issues/11)

## Goal and scope

Reduce repeated evidence reads and tool-loop context growth in independent
review. Retained conflict-response replay transcripts supply the diagnosis;
the same complete candidate context supplies the native comparison.

The goal owner persists a read-policy version in the existing review checkpoint.
The request builder passes that policy to the existing native prompt renderer.
Recovery and runtime-upgrade history reconstruct the same request. Candidate
identity, complete requirements, evidence ownership and verdict contracts remain
with their existing owners. Historical requests retain their original bytes.

Production scope is the goal request builder, checkpoint writer, upgrade reader
and native prompt renderer. Tests cover request identity and stopped-review
recovery. No new storage, executor, model selection or reading limit is introduced.

## Verification and delivery

1. Analyze retained native counters, commands and tool-result sizes.
2. Run the same candidate and complete context with the original and selected
   request policy through installed native review owners.
3. Check full requirement coverage, independent verdicts, actual input/cache/output
   counters and installed recovery regressions.
4. Publish sanitized measurements and usage documentation; commit, push and check CI.

The validation workload and cross-module request contract require this artifact.
Private transcripts and machine paths stay in local experiment evidence.
Selected production and historical runtimes retain their recorded paths. Revert
the source change to restore the previous policy for new tasks. Retain the
experiment's primary observations and record scratch disposition at closeout.

## Results

Eight native reviews in A-B-B-A order used the same corrected candidate and
complete context. All returned PASS across eight contract requirements. Input
tokens fell from 837,305 to 666,149; uncached input fell from 165,945 to 144,805.
Model requests fell from 23 to 19. Remaining duplicate reads are documented in
[the analysis](../../REVIEW_TOKEN_ANALYSIS.md).

Installed regression passed 154 tests with zero failures or skips. The wheel's
91 runtime files match the tested installation. Legacy rendering, context
identity, interrupted recovery and upgrade-history reconstruction passed.
Public measurements contain only allowlisted counts and scenario descriptions.
Primary native records and one verified wheel are retained locally; generated
test and build output is assigned to this task for closeout.
