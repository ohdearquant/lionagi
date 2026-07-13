# Implementation notes

- `lionagi/plugins/registry.py`: rebuilds live plugin eligibility from freshly
  scanned manifests, current settings, compatibility, trust, duplicate manifest
  names, and all tool declarations before exposing profiles or activation targets.
- `tests/plugins/test_registry.py`: covers repeated tool declarations, duplicate
  plugin names, direct activation refusal, and profile removal after live disablement
  without resetting the process snapshot.

Verification:

- `uv run pytest -n0 tests/plugins/ tests/protocols/action/test_action_manager.py tests/protocols/action/test_manager_edge_cases.py tests/test_import_laziness.py`
  — 166 passed.
- `uv run ruff format --check lionagi/plugins/registry.py tests/plugins/test_registry.py`
  — passed.
- `uv run ruff check lionagi/plugins/registry.py tests/plugins/test_registry.py`
  — passed.
