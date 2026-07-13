# Agent Hooks Reference

Reference for `lionagi.hooks.HookPoint` — the closed vocabulary of session
lifecycle hook points — and the built-in handlers registered via
`lionagi.hooks.loader.DEFAULT_HOOKS`.

## HookPoint catalog

### Dispatched (active emit callsites)

| Point | Value | Callsite |
|-------|-------|---------|
| `MESSAGE_ADD` | `message.add` | `branch.py _persist_via_bus` — every inbound message |
| `BRANCH_END` | `branch.end` | `cli/_runs.py teardown_persist` — once per branch the teardown owns, only when the run reached a genuine terminal outcome (never for the "running" reconciliation-suppression case) |

### Registered in DEFAULT_HOOKS (handlers wired; emit callsite deferred to ADR-0023b)

| Point | Value | Default handler |
|-------|-------|----------------|
| `SESSION_START` | `session.start` | `persist_session_start` |
| `SESSION_END` | `session.end` | `persist_session_end` |
| `BRANCH_CREATE` | `branch.create` | `persist_branch_provenance` |

### Reserved (vocabulary only; no handler and no emit callsite yet, per ADR-0023)

| Point | Value | Planned surface |
|-------|-------|----------------|
| `API_PRE_CALL` | `api.pre_call` | Before each iModel API call |
| `API_POST_CALL` | `api.post_call` | After each iModel API call (tokens / latency) |
| `API_STREAM_CHUNK` | `api.stream_chunk` | Per-chunk during streaming responses |
| `TOOL_PRE` | `tool.pre` | Before tool invocation (blocking via `blocking_emit`) |
| `TOOL_POST` | `tool.post` | After successful tool invocation |
| `TOOL_ERROR` | `tool.error` | On tool invocation failure |
| `ARTIFACT_CREATED` | `artifact.created` | When an artifact is persisted to disk |

## Bus dispatch semantics

`HookBus.emit` fires handlers sequentially and logs exceptions without
propagating them (isolation invariant). Exception: `TOOL_PRE` routes through
`blocking_emit`, which propagates exceptions so guards can raise `PermissionError`
to abort the tool call.

`StopHook` may be raised by any handler to skip remaining handlers on the same
point without propagating as an error.

## Override semantics (build_session_bus)

When an agent profile's `hooks` section mentions a point, the profile list
**replaces** the default for that point. An empty list disables the default
(e.g. `message.add: []` turns off built-in persistence). Points not mentioned
keep their defaults.

## Built-in handlers

| Handler | Registered for |
|---------|---------------|
| `persist_session_start` | `SESSION_START` |
| `persist_session_end` | `SESSION_END` |
| `persist_branch_provenance` | `BRANCH_CREATE` |
| `persist_branch_end` | `BRANCH_END` |
| `persist_message` | `MESSAGE_ADD` |
| `log_api_metrics` | (name-addressable; not in DEFAULT_HOOKS) |
| `log_tool_call` | (name-addressable; not in DEFAULT_HOOKS) |
| `log_tool_use` | (name-addressable; not in DEFAULT_HOOKS; deprecated — use `log_tool_call`) |

All handlers are name-addressable via the loader registry and can be referenced
as strings in agent YAML profiles.

## AgentSpec coding() guards

`AgentSpec.coding(secure=True)` (the default) wires two security guards via
`_wire_secure_guards`:

- `guard_destructive` as a pre-hook on `bash` — blocks destructive shell
  commands (`rm -rf`, `git push --force`, `git reset --hard`, `git clean -fd`,
  `drop table`, `drop database`, `truncate table`, `mkfs`, `dd if=`,
  `> /dev/sd*`).
- `guard_paths(allowed_paths=[workspace_root])` as a pre-hook on `reader` and
  `editor` — restricts file access to the workspace root (`cwd` if provided,
  else `Path.cwd()` at call time). Relative paths are resolved against the
  workspace root, not the process cwd.

Set `secure=False` to disable these defaults and manage guards manually.
