# Greeting project

This standalone example uses Python's standard library. The project owns its
requirements, tests, and acceptance; Native Agent Orchestrator owns dispatch and
execution records.

Run `python -m unittest -v` before and after changes. Change only the files named
in the task. Do not install dependencies, access external services, create
additional agents, or commit changes. Return the changed files and test results.

Acceptance requires all task-specific assertions and the existing test to pass.
