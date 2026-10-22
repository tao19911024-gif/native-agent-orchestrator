# Greeting project with independent review

Use the Python standard library. The project owns its task requirements, tests,
and acceptance. Native Agent Orchestrator owns execution and review records.

The coding role may modify only `greet.py` and `test_greet.py`. Run
`python -m unittest -v` before and after editing. Commit the finished changes as
one candidate commit using the example identity:

```text
git -c user.name="Example Maintainer" -c user.email="maintainer@example.invalid" commit
```

Review roles inspect the candidate commit against TASK1.md and report findings.
Use the existing files and tools; leave dependencies, remotes, and releases
unchanged. Execute each assigned role directly without additional agents.

The maintainer accepts the task after the required tests and independent review
pass and the candidate diff matches the task.
