# TASK1: Normalize greeting names and review the candidate

Update `greet.py` and `test_greet.py` to meet these requirements:

1. Preserve `greet("Ada") == "Hello, Ada!"`.
2. Remove surrounding whitespace: `greet("  Ada  ") == "Hello, Ada!"`.
3. Use `world` for empty and whitespace-only names:
   `greet("") == "Hello, world!"` and `greet("   ") == "Hello, world!"`.
4. Keep the function signature and use the Python standard library.
5. Add the three missing input tests, run `python -m unittest -v`, and commit the
   two changed files as one candidate commit following AGENTS.md.

Independent review checks the committed diff, all four required behaviors, test
coverage, and the project rules. Return the candidate commit and test results.
