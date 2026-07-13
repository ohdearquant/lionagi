# Implementation notes

- Exchange now tracks recipient mail during the async outbox-to-inbox handoff and exposes a locked snapshot of delivered and in-transit mail. Team quiescence includes either state, so the transfer cannot disappear between reads.
- Reactive execution now preflights capacity for the complete team wakeup batch before building operations. If the batch cannot fit, no inbox is consumed and no worker is marked active.
- Added regressions for a suspended Exchange handoff and a two-worker wakeup batch with only one available operation slot.

Verification:

- `uv run --extra pandas pytest -n0 tests/cli/orchestrate/test_team_lifecycle_wiring.py tests/cli/test_team_lifecycle.py tests/operations/test_reactive_flow.py tests/state/test_teams_schema.py tests/session/`
- `uv run ruff check lionagi/session/exchange.py lionagi/cli/orchestrate/_orchestration.py lionagi/operations/flow.py lionagi/cli/orchestrate/flow.py tests/cli/orchestrate/test_team_lifecycle_wiring.py`
