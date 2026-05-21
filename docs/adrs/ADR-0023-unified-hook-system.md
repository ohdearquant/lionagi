# ADR-0023: Unified Hook System and Agent-Level Configuration

**Status**: Proposed
**Date**: 2026-05-21
**Extends**: ADR-0009 (SQLite state layer), ADR-0022 (run step provenance)

## Context

Model, provider, and effort information is known at the iModel layer
during API calls but never reaches the persistence layer. The reason is
structural: three disconnected hook systems exist, and none bridge the
gap between "what model am I calling?" and "write this to the DB."

### Three hook systems today

| System | Layer | What it sees | Where it lives |
|--------|-------|-------------|----------------|
| **iModel HookRegistry** | Service/API | Model, provider, payload, response, tokens, latency | `lionagi/service/hooks/` |
| **Agent hook_handlers** | Tool execution | Tool name, args, result | `lionagi/agent/hooks.py` + `AgentConfig` |
| **CLI `_on_message`** | Message persistence | Message content, role, sender | Ad-hoc closure in `lionagi/cli/agent.py` |

Problems:

1. **No data flows between them.** The iModel PostInvocation hook knows
   the model and token count, but the message persistence hook doesn't.
   To write `model` on the session (ADR-0022), the CLI persistence code
   would need to reach into the iModel's state — which it can't.

2. **Not configurable from agent definitions.** Agent profiles
   (`~/.lionagi/agents/reviewer.md`) specify model and behavior, but
   hooks are hard-coded in Python. An agent can't declare "on every API
   call, log the model and tokens" or "on tool error, notify the team
   inbox" without code changes.

3. **No lifecycle hooks.** There's no hook for "session started" or
   "session ending" — the CLI just does inline writes. Session-level
   provenance (ADR-0022) requires a session-start hook that captures
   the resolved model, provider, effort, and agent hash.

4. **`_on_message` is a closure, not a hook.** It's defined inline in
   `agent.py`, can't be reused, can't be extended, can't be disabled.

### Where model/provider info actually lives

The resolution chain:

```
agent.md → parse_model_spec() → iModel.__init__(endpoint=...) → iModel.invoke()
                                  ↑                                ↑
                              provider, model known            payload, response, tokens known
                              at Branch creation               at API call time
```

The info exists at two points:
- **Branch creation**: model spec, provider, effort are resolved and
  passed to `iModel.__init__()`. This is when session-level provenance
  should be captured.
- **API call**: the actual request payload, response, token usage, and
  latency. This is when per-call metrics should be captured.

Neither point has a hook that writes to the session DB.

## Decision

### Consolidate into a single `HookBus`

Replace three disjoint systems with one event bus that all layers emit to
and any handler can subscribe to:

```python
# lionagi/hooks/bus.py

from enum import Enum
from collections.abc import Callable, Awaitable
from typing import Any

class HookPoint(str, Enum):
    # Session lifecycle
    SESSION_START = "session.start"
    SESSION_END = "session.end"

    # Branch lifecycle
    BRANCH_CREATE = "branch.create"

    # iModel (API calls)
    API_PRE_CALL = "api.pre_call"
    API_POST_CALL = "api.post_call"
    API_STREAM_CHUNK = "api.stream_chunk"

    # Tool execution
    TOOL_PRE = "tool.pre"
    TOOL_POST = "tool.post"
    TOOL_ERROR = "tool.error"

    # Message lifecycle
    MESSAGE_ADD = "message.add"

    # Artifact production
    ARTIFACT_CREATED = "artifact.created"


HookHandler = Callable[..., Awaitable[Any]]


class HookBus:
    """Central event bus for all hook points.

    Handlers are registered by HookPoint. Multiple handlers per point.
    Handlers run sequentially in registration order. A handler raising
    StopHook aborts subsequent handlers for that point (not the operation).
    """

    def __init__(self):
        self._handlers: dict[HookPoint, list[HookHandler]] = {}

    def on(self, point: HookPoint, handler: HookHandler) -> None:
        self._handlers.setdefault(point, []).append(handler)

    def off(self, point: HookPoint, handler: HookHandler) -> None:
        handlers = self._handlers.get(point, [])
        if handler in handlers:
            handlers.remove(handler)

    async def emit(self, point: HookPoint, **kwargs) -> None:
        for handler in self._handlers.get(point, []):
            try:
                await handler(**kwargs)
            except StopHook:
                break
            except Exception:
                # Hook failures MUST NOT abort the operation.
                # Log and continue.
                import logging
                logging.getLogger("lionagi.hooks").exception(
                    "Hook failed: %s", point.value
                )


class StopHook(Exception):
    """Raised by a handler to prevent subsequent handlers from running."""
```

### Event payloads (what each hook point receives)

| HookPoint | kwargs | Source |
|-----------|--------|--------|
| `SESSION_START` | `session_id`, `model`, `provider`, `effort`, `agent_name`, `agent_hash`, `invocation_kind`, `invocation_id` | CLI session init |
| `SESSION_END` | `session_id`, `status`, `ended_at`, `error` | CLI session finalize |
| `BRANCH_CREATE` | `branch_id`, `session_id`, `model`, `provider`, `agent_name` | Branch init |
| `API_PRE_CALL` | `model`, `provider`, `payload`, `branch_id` | iModel.invoke() |
| `API_POST_CALL` | `model`, `provider`, `response`, `tokens`, `latency_ms`, `branch_id`, `status_code` | iModel.invoke() |
| `API_STREAM_CHUNK` | `chunk_type`, `chunk`, `branch_id` | iModel streaming |
| `TOOL_PRE` | `tool_name`, `action`, `args`, `branch_id` | Branch tool dispatch |
| `TOOL_POST` | `tool_name`, `action`, `args`, `result`, `branch_id` | Branch tool dispatch |
| `TOOL_ERROR` | `tool_name`, `action`, `args`, `error`, `branch_id` | Branch tool dispatch |
| `MESSAGE_ADD` | `message`, `branch_id`, `session_id`, `progression_id` | Branch.add_message() |
| `ARTIFACT_CREATED` | `artifact`, `invocation_id`, `session_id` | Skill output |

### Built-in handlers

The consolidation replaces the three existing systems with built-in
handlers registered on the bus:

```python
# lionagi/hooks/builtins.py

async def persist_session_start(*, session_id, model, provider, effort,
                                 agent_name, agent_hash, **kw):
    """Write session provenance to state.db at session start."""
    from lionagi.state.db import StateDB
    async with StateDB.open() as db:
        await db.update_session(session_id, {
            "model": model,
            "provider": provider,
            "effort": effort,
            "agent_name": agent_name,
            "agent_hash": agent_hash,
            "status": "running",
            "started_at": time.time(),
        })

async def persist_message(*, message, session_id, branch_id,
                           progression_id, **kw):
    """Write message to state.db + update last_message_at."""
    from lionagi.state.db import StateDB
    async with StateDB.open() as db:
        await db.insert_message(message)
        await db.append_to_progression(progression_id, message["id"])
        await db.update_session(session_id, {
            "last_message_at": time.time(),
        })

async def persist_branch_provenance(*, branch_id, model, provider,
                                     agent_name, **kw):
    """Write per-branch model/provider to state.db."""
    from lionagi.state.db import StateDB
    async with StateDB.open() as db:
        await db.update_branch(branch_id, {
            "model": model,
            "provider": provider,
            "agent_name": agent_name,
        })

async def guard_destructive_tool(*, tool_name, args, **kw):
    """Block destructive bash commands."""
    # Moved from lionagi/agent/hooks.py
    ...
```

### Agent-level hook configuration

Agent profiles gain a `hooks` section:

```yaml
# ~/.lionagi/agents/reviewer.md (frontmatter)
---
name: reviewer
model: claude/claude-sonnet-4-6
effort: high
hooks:
  session.start:
    - persist_session_start        # built-in
  api.post_call:
    - log_api_metrics              # built-in: logs model, tokens, latency
  tool.pre:
    - guard_destructive            # built-in
  tool.post:
    - log_tool_use                 # built-in
  message.add:
    - persist_message              # built-in
---
```

Hook names in agent profiles reference **registered handlers** — either
built-ins or user-defined. Custom handlers are Python callables registered
at startup:

```python
# ~/.lionagi/hooks/my_hooks.py (user-defined, auto-loaded)

from lionagi.hooks import hook

@hook("api.post_call")
async def notify_on_expensive_call(*, tokens, model, **kw):
    if tokens.get("total", 0) > 10000:
        logger.warning("Expensive call: %s tokens on %s", tokens["total"], model)
```

### Default hooks (no configuration needed)

When no `hooks` section is in the agent profile, the bus registers a
default set:

```python
DEFAULT_HOOKS = {
    HookPoint.SESSION_START: [persist_session_start],
    HookPoint.SESSION_END: [persist_session_end],
    HookPoint.MESSAGE_ADD: [persist_message],
    HookPoint.BRANCH_CREATE: [persist_branch_provenance],
}
```

These ensure ADR-0022 provenance and ADR-0019 `last_message_at` work
without any agent-level configuration. Agent profiles can **extend** or
**override** defaults:

```yaml
hooks:
  tool.pre:
    - guard_destructive      # adds to defaults (no tool.pre default)
  message.add: []            # overrides: disables message persistence
```

### Migration from existing systems

| Old system | New equivalent | Migration |
|-----------|---------------|-----------|
| `HookRegistry.pre_invoke()` | `bus.on(API_PRE_CALL, ...)` | Adapter wraps old-style handlers |
| `HookRegistry.post_invoke()` | `bus.on(API_POST_CALL, ...)` | Adapter wraps old-style handlers |
| `AgentConfig.pre(tool, fn)` | `bus.on(TOOL_PRE, fn)` with tool filter | Tool name becomes a kwarg filter |
| `AgentConfig.post(tool, fn)` | `bus.on(TOOL_POST, fn)` with tool filter | Same |
| `_on_message` closure | `bus.on(MESSAGE_ADD, persist_message)` | Delete closure, use built-in |

The old `HookRegistry` and `AgentConfig.hook_handlers` are kept as
deprecated wrappers during the transition. They delegate to the bus
internally. Removed in 0.28.0.

### Bus lifecycle

One `HookBus` per session. Created at session init, passed to all
branches in the session. Branches emit to the bus; handlers respond.

```python
class Session:
    def __init__(self, ...):
        self.hooks = HookBus()
        # Register defaults
        for point, handlers in DEFAULT_HOOKS.items():
            for h in handlers:
                self.hooks.on(point, h)
        # Register agent-specific hooks from profile
        if agent_config and agent_config.hooks:
            for point_str, handler_names in agent_config.hooks.items():
                point = HookPoint(point_str)
                for name in handler_names:
                    self.hooks.on(point, resolve_handler(name))
```

### Hook isolation

Hooks MUST NOT:
- Block the operation they're observing (async with timeout, fire-and-forget on failure)
- Modify the operation's data (hooks are observers, not interceptors — except `TOOL_PRE` which can raise to block)
- Hold references to the bus after session end (GC-safe)

The only hook that can **block** an operation is `TOOL_PRE` — raising
`PermissionError` prevents the tool call. All other hooks are
fire-and-observe.

### Package structure

```
lionagi/hooks/
  __init__.py          # exports HookBus, HookPoint, hook decorator
  bus.py               # HookBus, HookPoint enum, StopHook
  builtins.py          # persist_*, guard_*, log_* handlers
  loader.py            # load hooks from agent profile YAML
  _compat.py           # adapters for old HookRegistry + AgentConfig
```

`lionagi/service/hooks/` is NOT moved or deleted during transition — it
continues to work via `_compat.py` adapter. After 0.28.0 deprecation
window, it moves to `lionagi/hooks/_legacy/`.

## Consequences

**Positive**
- Model/provider/tokens flow from iModel layer to DB persistence via
  a single bus — no more data gap.
- Agent profiles can configure hooks declaratively — no code changes for
  "log token usage on this agent" or "guard destructive commands on that agent."
- Session lifecycle hooks enable clean provenance writes (ADR-0022)
  without inline CLI code.
- `last_message_at` (ADR-0019) becomes a built-in handler, not a
  custom persistence path.
- User-defined hooks are auto-loaded from `~/.lionagi/hooks/` — extensible
  without modifying lionagi source.
- One bus per session = clean isolation. No global state, no cross-session
  leaks.

**Negative**
- Deprecation period for `HookRegistry` and `AgentConfig.hook_handlers`
  means two code paths temporarily coexist.
- Hook handler names in agent YAML must be resolvable — typos fail silently
  unless validated at load time. Mitigation: validate at session start,
  warn on unresolvable handler names.
- Async handlers add latency to hot paths (message add, API call).
  Mitigation: handlers that do I/O (DB writes) are fire-and-forget with
  timeout; the operation never waits.
- Moving from "hooks can modify" (old `AgentConfig.pre` returns modified
  args) to "hooks observe" (new bus) changes the contract. `TOOL_PRE` is
  the exception — it can block but not modify.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Keep three separate hook systems, bridge with adapters | Complexity grows with each new hook point; unified bus is simpler long-term |
| Global singleton bus | Cross-session leaks; per-session bus is cleaner |
| Hooks modify operation data (interceptor pattern) | Debugging nightmares when hooks silently alter payloads; observer pattern is safer |
| Configuration in Python only (no YAML) | Agent profiles are YAML/markdown; hooks should be configurable at the same level |
| Event sourcing (persist all events, derive state) | Over-engineered for our scale; targeted hooks on specific points is sufficient |
| Middleware chain (Express/Koa style) | Implies ordering dependencies between middleware; flat handler list is simpler |

## References

- [ADR-0009](ADR-0009-sqlite-state-layer.md) — SQLite state layer
- [ADR-0019](ADR-0019-teams-db-and-run-lifecycle.md) — `last_message_at` column
- [ADR-0022](ADR-0022-run-step-provenance.md) — Session/branch provenance columns
- `lionagi/service/hooks/` — Current iModel hook system
- `lionagi/agent/hooks.py` — Current tool-call hooks
- `lionagi/agent/config.py` — Current AgentConfig hook registration
- `lionagi/cli/agent.py` — Current `_on_message` closure

### Prior art

- **Claude Code Hook Registry** (`_references/claude-code/src/utils/hooks.ts`)
  — Merges three config sources, uses `getMatchingHooks()` with regex pattern
  matching and `deny>ask>allow` permission precedence. The HookBus design is
  convergent with this architecture.
- **autogen Stream** (`autogen/beta/stream.py::MemoryStream`) — Typed event
  bus with publish/subscribe and condition filters. Per-stream turn lock
  serializes concurrent calls. Validates the pub/sub event bus approach.

### Open question: dual Middle Protocol

The CLI subprocess path (`run_and_collect`) and the API path (`communicate`)
are both hook emission sites. CLI agents spawned via `li agent -a reviewer`
use `run_and_collect` (subprocess streaming); Python API agents use
`communicate` (one-shot). The HookBus must emit on both paths — the current
design specifies hook points for the API path but should explicitly address
the CLI subprocess path where `API_PRE_CALL`/`API_POST_CALL` semantics differ
(the "call" is a subprocess spawn, not an HTTP request).
