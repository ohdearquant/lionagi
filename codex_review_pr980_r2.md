Verdict: REQUEST CHANGES
Findings: 2 High, 2 Medium

Scope: Round 2 review of PR #980 on `feat/sdk-state-layer` at HEAD
`ccff506a10d00de26be53625de61948a5d9b3cf7`, compared with `main`
`30ff7b55826e91d56301d7a711a302437c8e4040`. GitHub connector metadata for
`ohdearquant/lionagi#980` shows the PR is open, mergeable, and at the requested
head.

## Findings

### [High] Matched `tool_result` messages still do not reach live SQLite persistence

Evidence: the round 2 fix changed the matched `tool_result` path to call the
synchronous manager API at `lionagi/operations/run/run.py:162`. That sync path
fires callbacks via `MessageManager._fire_on_message_added()` at
`lionagi/protocols/messages/manager.py:475`, which calls each callback directly
at `lionagi/protocols/messages/manager.py:481`. The live SQLite hooks registered
by this PR are async callbacks: orchestration defines `_on_message` at
`lionagi/cli/orchestrate/_orchestration.py:600` and appends it at
`lionagi/cli/orchestrate/_orchestration.py:611`; `li agent` appends its async
hook at `lionagi/cli/agent.py:264`.

Reproduction: a fake CLI stream with matched `tool_use` + `tool_result` yielded
`['Instruction', 'ActionRequest', 'ActionResponse']`, but an async
`on_message_added` hook observed only `['Instruction', 'ActionRequest']` and
Python warned that the hook coroutine was never awaited. This means the in-memory
branch contains the `ActionResponse`, but live `state.db` still misses it.

There is a second ordering bug on the same path: error metadata is set after
`add_message()` returns at `lionagi/operations/run/run.py:168`. A sync hook proof
observed the `ActionResponse` metadata as `{}` while the yielded message had
`{'is_error': True}`, so even a sync persistence hook would miss the error flag
needed by ADR-0012's tool-error grouping (`docs/adrs/ADR-0012-studio-execution-lineage.md:150`).

Suggested fix: use an async manager path that awaits async hooks for matched
tool results, and attach `is_error` before hook notification. Add a regression
test with an async `on_message_added` hook proving it observes `Instruction`,
`ActionRequest`, and `ActionResponse`, including error metadata.

### [High] Interrupted live sessions are still finalized as `completed`

Evidence: ADR-0017 requires interrupts to update sessions to `aborted` at
`docs/adrs/ADR-0017-session-lifecycle-status.md:72`. The agent path tracks only
`_operate_failed`, sets it in `except Exception`, and passes a boolean teardown
flag at `lionagi/cli/agent.py:132`, `lionagi/cli/agent.py:141`, and
`lionagi/cli/agent.py:146`. Flow and fanout use the same `except Exception`
pattern at `lionagi/cli/orchestrate/flow.py:472`,
`lionagi/cli/orchestrate/flow.py:482`, `lionagi/cli/orchestrate/fanout.py:97`,
and `lionagi/cli/orchestrate/fanout.py:113`. Finalization only writes
`failed` or `completed` at `lionagi/cli/orchestrate/_orchestration.py:630` and
`lionagi/cli/agent.py:287`.

Why this matters: `KeyboardInterrupt` and `asyncio.CancelledError` are not caught
by `except Exception` in this Python runtime (`CancelledError` inherits
`BaseException`). Their `finally` blocks still run with the failed flag false,
so an interrupted run is persisted as `completed`, violating the lifecycle ADR
and corrupting run status in SQLite.

Suggested fix: replace the boolean teardown API with an explicit terminal status
such as `completed`, `failed`, or `aborted`; handle `KeyboardInterrupt` and
`asyncio.CancelledError` separately in agent, flow, and fanout; add tests that
simulate cancellation and assert `sessions.status == 'aborted'`.

### [Medium] Live CLI provenance still omits `artifacts_path`

Evidence: ADR-0012 defines `artifacts_path` as a session provenance field at
`docs/adrs/ADR-0012-studio-execution-lineage.md:48` and requires future CLI
invocations to write provenance at session creation time at
`docs/adrs/ADR-0012-studio-execution-lineage.md:52`. The run directory is
allocated before live persistence in the agent path at `lionagi/cli/agent.py:126`,
but `_setup_live_persist()` is called with only `agent_name` at
`lionagi/cli/agent.py:130`, and `create_session()` omits `artifacts_path` at
`lionagi/cli/agent.py:204`. Orchestration does the same at
`lionagi/cli/orchestrate/_orchestration.py:521`.

Why this matters: the SQLite sessions row is supposed to be the canonical query
record for Studio. Leaving `artifacts_path` null for live agent, flow, and fanout
runs makes SQLite less useful than the legacy filesystem manifest for opening
run artifacts.

Suggested fix: pass the allocated run artifact path into live-persist setup and
write it when creating the session row for agent, flow, fanout, and play paths.
Add a test around live session creation that asserts `artifacts_path` is present.

### [Medium] Changed-file Ruff gate is not clean

Evidence: the changed-file Ruff gate fails with 25 diagnostics. Round 2 adds at
least two new lint failures in the touched run files: unused
`ActionResponse` import at `lionagi/operations/run/run.py:17` and unused
`Instruction` import at `tests/operations/run/test_run.py:21`. Existing changed
files also still include unused imports, silent `except Exception: pass`, and
review-required SQL-construction suppressions, including
`lionagi/cli/agent.py:25`, `lionagi/cli/orchestrate/_orchestration.py:608`,
`lionagi/state/db.py:242`, `lionagi/state/db.py:319`,
`lionagi/state/db.py:386`, and `lionagi/state/db.py:447`.

Suggested fix: remove unused imports, either log/narrow the silent exception
handlers or document why they are intentionally ignored, and add explicit
reviewed suppressions or refactors for the allowlisted dynamic SQL sites.

## Round 2 Fix Verification

- Concurrent progression append: fixed. `StateDB.append_to_progression()` now
  uses SQLite `json_insert(collection, '$[#]', ?)` at `lionagi/state/db.py:187`.
- List message content: fixed. `StateDB.insert_message()` now serializes both
  dict and list content at `lionagi/state/db.py:128`.
- NULL metadata from DB rows: fixed. `Element.from_dict()` treats
  `node_metadata=None` / `metadata=None` as `{}` at
  `lionagi/protocols/generic/element.py:200`.
- Tool result persistence: not fixed. The direct `Pile.include()` bypass is gone,
  but the replacement still does not execute async live-persist hooks and sets
  error metadata after hook notification.

## Gates

- `python -m pytest -o addopts='' tests/state/test_db.py tests/operations/run/test_run.py tests/operations/test_act.py tests/operations/test_communicate.py`: 73 passed.
- `python -m pytest -o addopts='' tests/cli/orchestrate/test_flow_spec_file.py tests/providers/openai/codex/test_fast_mode.py tests/session/test_branch.py tests/session/test_branch_actionmanager_coverage.py tests/test_init.py tests/docs/test_for_ai_agents.py tests/providers/ag2/agent/test_endpoint.py`: 300 passed.
- `git diff --check origin/main...HEAD`: passed.
- `python -m ruff check lionagi/state lionagi/cli/state.py lionagi/cli/agent.py lionagi/cli/orchestrate/_orchestration.py lionagi/cli/orchestrate/flow.py lionagi/cli/orchestrate/fanout.py lionagi/operations/run/run.py lionagi/protocols/generic/element.py tests/state/test_db.py tests/operations/run/test_run.py`: failed with 25 diagnostics.
- Custom hook proof for matched `tool_use` + `tool_result`: async hook observed only `Instruction` and `ActionRequest`, not `ActionResponse`.
- Custom hook proof for matched error `tool_result`: sync hook observed `ActionResponse` metadata before `is_error` was attached.

Domain utility: SKIPPED - the prompt requested lore `suggest`/`compose`, but no
lore MCP tools are available in this session; I used local PR/code review,
GitHub connector metadata, and targeted execution instead.
