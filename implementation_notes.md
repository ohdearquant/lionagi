# Implementation notes

- Plugin-provided endpoint registrations now retain their plugin name and declared target.
- Cached plugin endpoints re-run the plugin activation trust checks before use and are removed from the endpoint registry when revalidation fails.
- Built-in endpoint registrations remain on the existing direct lookup path.
- Added coverage for changing a provider file after its endpoint has already been activated and registered.

## Verification

- `uv run pytest -n0 tests/plugins` — 76 passed
- `uv run pytest -n0 tests/service/connections --ignore=tests/service/connections/mcp` — 361 passed
- `uv run ruff format --check lionagi/service/connections/registry.py tests/service/connections/test_plugin_provider_consumer.py` — passed
- `uv run ruff check lionagi/service/connections/registry.py tests/service/connections/test_plugin_provider_consumer.py` — passed
