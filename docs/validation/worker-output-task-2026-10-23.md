# Correct reviewer launch settings and malformed review handling

Fix two confirmed defects in this Foundry source checkout.

1. Independent goal review-group members currently inherit the worker tool-output
   default. Codex reviewers must use native output settings (tool_tokens=0) when
   no explicit setting is supplied. Coding workers retain 3000. The launch role,
   native invocation and backend identity must agree with captured settings.
   Explicit positive/zero values and retained historical settings take precedence.
2. When review_field_reads is enabled, null, list, string or numeric review
   responses raise AttributeError before schema validation. Return the existing
   schema-validation error for invalid response shapes. Preserve field-read
   receipt checks, complete-context identity checks and historical contracts.

Reuse the existing launch-context, review-group, native transport and response
validation owners. No new dispatcher, policy layer, compatibility shim or dependency.
Add meaningful regression coverage and keep existing behavioral assertions.

Allowed implementation files: src/foundry_runtime/_foundry_review_group.py,
src/foundry_runtime/_foundry_native_transport.py, src/foundry_runtime/foundry_native.py,
src/foundry_runtime/foundry_goal.py and src/foundry_runtime/_launch_context.py.
Tests may change under tests/. Do not change the package version, public docs,
release configuration, project rules or task instructions.

This is an authorized isolated replay of a real repository defect. Work only in
this checkout. Do not inspect other checkouts, prior solutions, remote history,
maintenance records or installed production source. Do not change shared runtime
selection, installed production environments, external project entries or services.
Use one coding worker; do not delegate this bounded repair. Commit the completed
candidate in this checkout and report changes and actual verification.

The local .venv already has the original package and test dependencies. After
source edits, install the candidate into this local environment with:
`.venv/Scripts/python.exe -m pip install --no-index --no-deps --no-build-isolation .`
Verify installed imports and run the affected checks, including:
`tests/test_acceptance_feedback.py tests/test_launch_context.py tests/test_review_context.py`
and your new regression cases. Preserve original failures. Write compact test
evidence under a local ignored directory rather than committing machine logs.
