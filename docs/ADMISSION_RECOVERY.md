# Admission judgement recovery

A native judgement may end without a verdict because the provider is at
capacity, a connection fails or the call times out. A project using a native
preflight judge can call `foundry_runtime._admission_call.run` in place of its
single `subprocess.run` invocation. The project still constructs the command,
validates the structured result and decides whether the worker may proceed.
Descriptor preflight commands retain their project-owned behavior.

```python
from foundry_runtime._admission_call import run

process = run(
    argv,
    evidence=evidence_directory,
    output_path=temporary_result_file,
    input=prompt,
    cwd=str(workspace),
    text=True,
    capture_output=True,
    encoding="utf-8",
    errors="replace",
    env=environment,
    timeout=900,
)
```

The evidence directory must already exist and belong to this judgement. The
output path is the temporary structured-output file specified in the native
command. Every retry removes the previous temporary result before calling the
same command with the same instructions. Failed output remains in its attempt
directory. Native calls returning a completed response pass back to the project
for validation; PASS and REFUSE are never interpreted by this transport.

## Retry and evidence contract

Capacity, timeout and recognized transport failures allow at most two retries,
after 60 and 300 seconds. A 900-second call timeout permits at most 3,060 seconds
of calls and scheduled waiting, excluding process cleanup and local I/O.
Quota exhaustion, authentication errors, missing executables and unclassified
failures stop immediately. The provider and model stay selected by the project;
this entry does not introduce a separate model fallback policy.

`attempt-1/` through `attempt-3/` retain native stdout, stderr and any structured
output. `admission-attempts.json` records return codes and failure classes.
The root `native.jsonl` and `native.stderr.txt` contain the final call's output
for existing receipt readers. Exhaustion raises `AdmissionUnavailable` and writes
`admission-failure.json`, including the reason and attempt references. Timeout
output is retained even when the native process exits before producing a verdict.

The existing task deadline limits each call and prevents waiting beyond that
deadline. An adapter spawning a separate judgement process must forward the
retained absolute deadline and enter `_task_deadline.scope` there. A completed
native success after intermediate error events is returned without another call.

## Deployment and recovery

Install the new runtime in its own versioned environment. An external adapter
that supports older installed runtimes selects this call only when that runtime
provides it; older runtimes retain their original invocation. The runtime upgrade
and adapter integration are both needed to enable judgement retries.

Updating the shared selection affects new tasks. Existing controllers and
recoveries keep their recorded interpreter and worker session. A stopped task
continues through its existing recovery command; publishing this release does
not restart or migrate it. Retain the previous qualified runtime for rollback.
