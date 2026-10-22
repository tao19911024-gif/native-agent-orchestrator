# TASK1: Continue a greeting task from a recorded checkpoint

The task has two stages in the same native session.

## First stage

Read `AGENTS.md`, `greet.py`, and `test_greet.py`. Run the baseline test. Summarize
the implementation needed for the second stage, then end this turn with the text
`CHECKPOINT_READY`. Keep the source and tests unchanged during this stage.

## Second stage

When the maintainer continues this session with `CONTINUE.md`, update `greet.py`
and `test_greet.py` to satisfy these requirements:

- `greet("Ada") == "Hello, Ada!"`.
- `greet("  Ada  ") == "Hello, Ada!"`.
- `greet("") == "Hello, world!"`.
- `greet("   ") == "Hello, world!"`.

Add tests for all four cases, run `python -m unittest -v`, and leave the changes
uncommitted. Return the changed files and test results.
