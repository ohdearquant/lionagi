# Testing, State, and Session — Reference

## Testing: `lionagi.testing`

### Quick start — in-process

```python
from lionagi.testing import TestBranch

branch = TestBranch.from_text("hello back")
assert await branch.chat("hello") == "hello back"
assert TestBranch.calls(branch)[0].last_user_message == "hello"
```

`TestBranch.from_responses`, `from_yaml`, `from_json`, and `from_script` all
return real `Branch` objects with a `ScriptedEndpoint` underneath — the full
production code path (rate limiter, payload builder, `AssistantResponse` parser)
runs without any network call.

### Quick start — subprocess

```python
from lionagi.testing import scripted_env
import subprocess

with scripted_env("tests/fixtures/scripts/foo.yaml"):
    r = subprocess.run(["li", "agent", "hi"], capture_output=True, text=True)
```

`subprocess_env` returns a dict suitable for `subprocess.run(env=...)`.

### Scripted endpoint activation paths

In-process:

```python
iModel(provider="scripted", script="path.yaml", model="any")
iModel(provider="scripted", script=ScriptModel.from_responses([...]))
```

Subprocess — set env vars before launching `li`:

```text
LIONAGI_CHAT_PROVIDER=scripted
LIONAGI_CHAT_MODEL=scripted-test
LIONAGI_TEST_SCRIPT=<path to .yaml or .json>
```

The endpoint registers as `provider="scripted"` with endpoint
`chat/completions` plus aliases `chat`, `query_cli`, and `cli`. It inherits
`is_cli = True` from `AgenticEndpoint` so `li agent` routes through
`branch.run` → `endpoint.stream` exactly like real CLI providers.

### ScriptModel matching strategy

1. `when:` matchers are checked first (in declaration order). Any entry whose
   matcher succeeds and which has not been exhausted is served.
2. Otherwise the positional cursor advances to the next entry without a `when:`
   predicate.

Mix ordered positional entries with content matchers — useful when an agent's
mid-loop call depends on a prior tool result.

### IModelKwargCaptor — usage pattern

```python
import lionagi.cli._providers as pmod
from lionagi.testing import IModelKwargCaptor

captor = IModelKwargCaptor.fresh()  # pristine subclass, isolated captures list
monkeypatch.setattr(pmod, "iModel", captor)
build_imodel_from_spec("codex/gpt-5.5", fast=True)
assert captor.captures[0]["fast_mode"] is True
```

`IModelKwargCaptor.fresh()` returns a subclass with its own `captures` list so
multiple tests in the same session do not interfere.

### MockClaudeCode — response shapes

Returns different dict shapes based on the last user message:

- `"generate tasks ..."` → `{"content": ..., "instruct_model": [...]}`
- `"research ..."` → `{"content": ..., "findings": [...]}`
- otherwise → `{"content": "Processed: ..."}`

### pytest plugin

```python
# conftest.py
pytest_plugins = ["lionagi.testing.pytest_plugin"]
```

Fixtures: `scripted_branch_factory`, `scripted_branch`, `scripted_endpoint_for`,
`make_mocked_branch`, `mocked_branch`, `mock_factory`, `async_helpers`,
`validation_helpers`, `test_data_loader`, `performance_benchmark`.

---

## State: session health and staleness

### Session health ([ADR-0057](../adr/ADR-0057-operational-lifecycle-and-transition-audit.md))

Six-level health model replacing the binary "phantom / not":

| Level | Meaning | Action |
|-------|---------|--------|
| `HEALTHY` | Terminal or active with recent activity | None |
| `IDLE` | Alive, quiet (> 1h, < kind threshold) | Monitor |
| `UNRESPONSIVE` | Alive but past kind threshold | Investigate |
| `STALE` | Process dead, had work | Transition to failed |
| `ORPHANED` | Process dead, no work ever produced | Safe to delete |
| `ZOMBIE` | Terminal but left stale locks behind | Cleanup |

Health is orthogonal to status: a `status='running'` session can be
`health=stale`. Derived at read time from three signals:

1. `status` — what the CLI last wrote.
2. `last_message_at` vs the kind-aware threshold — activity.
3. Process liveness — does the OS still own the PID recorded?

### Staleness thresholds ([ADR-0057](../adr/ADR-0057-operational-lifecycle-and-transition-audit.md))

Kind-aware thresholds distinguish "stuck" from "still working":

| `invocation_kind` | Threshold |
|-------------------|-----------|
| `agent` | 6 hours |
| `play` | 6 hours |
| `flow` | 12 hours |
| `fanout` | 12 hours |
| `show-play` | 12 hours |
| (unknown/missing) | 6 hours |

### Lifecycle suppression (`suppress_lifecycle_var`)

`suppress_lifecycle_var` is a `ContextVar[bool]` (default `False`). Using a
`ContextVar` instead of a Branch-level flag means suppression is scoped to the
asyncio task that set it. `asyncio` copies the context when spawning a new task,
so nested coroutines in the same task inherit the flag while concurrent tasks on
the same Branch each get their own copy.

Usage:

```python
token = suppress_lifecycle_var.set(True)
try:
    ...
finally:
    suppress_lifecycle_var.reset(token)
```

---

## Session: capabilities and control

### CapabilityViolation and EmissionRejected

Both are emitted onto the session bus rather than silently dropped or raised:

- `CapabilityViolation` — agent emitted a key outside its grant. The offending
  block is not validated or honored. Observe via `session.observe(CapabilityViolation)`.
- `EmissionRejected` — in-grant capability block failed schema validation. The
  `error` field carries the verbatim validation error so a repair loop can
  re-prompt the agent.

### Exchange — lifecycle

```text
register(owner_id)            # create entity mailbox
send(sender, recipient, ...)  # queue message in sender's outbox
collect(owner_id)             # route outbox → recipient inboxes (two-phase, releases lock before delivery)
receive(owner_id)             # peek at inbound messages (non-destructive)
pop_message(owner_id, sender) # FIFO pop from a specific sender's inbox
sync() / run(interval)        # collect_all() / continuous loop
```

### NodeEscalated — `route` semantics

`route` is `"higher_tier"` when a re-dispatch is scheduled, `"give_up"` when no
escalation path is configured. The `escalation_request` field stores the original
request payload for the audit trail. It is a named field (not `Signal.data`) so
the observer's payload-matching does not re-fire the escalation handler when this
signal is emitted.
