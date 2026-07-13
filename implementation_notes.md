# Implementation Notes

- Changed `lionagi/protocols/action/tool_hooks.py` so snapshot failures never expose live call state to post hooks.
- Created one detached canonical snapshot of the final arguments, result, and error, then copied that evidence independently for every post hook.
- Added regressions covering uncopyable results, cross-hook mutation isolation, and failed-call exception isolation in `tests/protocols/action/test_tool_invoke_hooks.py`.

## Verification

- `uv run pytest -n0 tests/hooks/ tests/protocols/action/` — 228 passed.
- `uv run ruff check lionagi/protocols/action/tool_hooks.py tests/protocols/action/test_tool_invoke_hooks.py` — passed.
- `uv run ruff format --check lionagi/protocols/action/tool_hooks.py tests/protocols/action/test_tool_invoke_hooks.py` — passed.

## Domain Utility

Low. Retrieved material focused on storage snapshots rather than in-process object isolation, so the explicit hook contract and existing implementation patterns drove the fix.
