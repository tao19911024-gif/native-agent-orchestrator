# TASK1: Normalize greeting names

Update `greet.py` and `test_greet.py` to meet these requirements:

1. Preserve `greet("Ada") == "Hello, Ada!"`.
2. Remove surrounding whitespace: `greet("  Ada  ") == "Hello, Ada!"`.
3. Use `world` for an empty or whitespace-only name:
   `greet("") == "Hello, world!"` and `greet("   ") == "Hello, world!"`.
4. Add tests for the three new input cases and run `python -m unittest -v`.

Keep the function signature unchanged and use the Python standard library.
Return the modified files and the test result. Leave the changes uncommitted.
