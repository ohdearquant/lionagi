# ADR-0034: Hook-Based Governance Enforcement

**Status**: Proposed
**Date**: 2026-05-25
**Extends**: ADR-0023 (unified hook system), ADR-0033 (agent capabilities)
**Targets**: lionagi 0.27.0

## Context

ADR-0033 specifies the declarative grant. This document specifies the
enforcement points at which those grants are validated at runtime.

A review of the existing hook surface (2026-05-25) identifies four hook systems:

| System | File | Status | What it sees |
|---|---|---|---|
| iModel `HookRegistry` | `lionagi/service/hooks/` | live | model, payload, response, tokens |
| AgentConfig `hook_handlers` | `lionagi/agent/config.py` + `factory.py:186-196` | live | tool name, args, return value |
| CLI `_on_message` | `lionagi/cli/agent.py`, ad-hoc closure | live | message persistence only |
| `HookBus` (ADR-0023 Phase 1) | `lionagi/hooks/` | **shipped but not wired** | 11 `HookPoint` events including `TOOL_PRE`, `BRANCH_CREATE`, `SESSION_START` |

ADR-0023b/c (the wiring PRs that hook `HookBus` into CLI / iModel) are not
yet tracked as open issues. Implementation of this ADR does not depend on
those wiring PRs; the existing hook surfaces are sufficient for 0.27.0
governance.

Additional finding: the `error:` phase in `AgentConfig` is declared but never
queried by `_tool_hooks()` at `factory.py:138-143`. This is dead code that
0.27.0 should either wire or delete.

## Decision

Implement capability enforcement as **hooks layered on the existing surface**.
No new hook system. No HookBus dependency. The wiring is:

### 1. Tool-call enforcement → `security_pre:*` (AgentConfig)

When `create_agent(config)` builds an agent and registers tools, the factory
injects a generated `security_pre:{tool}` hook for every capability-gated tool:

```python
# lionagi/governance/enforcement.py (new)
def install_tool_gates(config: AgentConfig, caps: CapabilitySet) -> None:
    for tool_name in caps.tools_allowed():
        config.security_pre(tool_name, _make_tool_gate(caps, tool_name))

def _make_tool_gate(caps, tool_name):
    async def gate(tool, action, args):
        decision = caps.allow_tool(tool_name, args)
        if not decision.allowed:
            raise PolicyDenied(decision.reason)
        return decision.modified_args or args   # redaction path
    return gate
```

This rides the existing pre-tool hook chain at `function_calling.py:69-75`. The
hook can both **rewrite args** (return a modified dict — interceptor pattern)
and **block** (raise `PolicyDenied`). The agent sees a tool error and adapts.

### 2. Model-call enforcement → `HookRegistry.PreInvocation`

Cost caps, rate caps, and `model.cost_max_usd` constraints fire here:

```python
# Registers via iModel.hook_registry at branch construction
async def model_gate(api_call: APICalling, *, exit: bool, **kw):
    if caps.budget_exceeded(api_call):
        raise PolicyDenied("model.cost_max_usd exceeded")
```

### 3. Agentic-CLI-provider enforcement → `HookRegistry.PreEventCreate`

The audit confirmed that for `claude_code`, `codex`, `pi`, the **only real
blocking layer is request-construction time**. PreEventCreate fires before
APICalling is built, so a hook here projects capabilities onto provider
request kwargs:

```python
async def cli_gate(event_type, *, exit, branch_caps, **kw):
    if event_type is APICalling and isinstance(branch.chat_model.endpoint,
                                                ClaudeCodeEndpoint):
        # Project caps onto request kwargs (see ADR-0035 for the full mapping)
        kw["allowed_tools"] = _project_to_claude(branch_caps)
        kw["permission_mode"] = "plan" if "tool.editor" not in branch_caps else "default"
```

See ADR-0035 for the full projection table per provider.

### 4. Branch spawn → wire the missing emit site

`BRANCH_CREATE` is defined in `HookPoint` but never emitted. 0.27.0 wires:

```python
# lionagi/session/branch.py — at the end of __init__
if hook_bus := _resolve_hook_bus():
    hook_bus.emit(HookPoint.BRANCH_CREATE, branch=self, parent_caps=...)
```

Same for `SESSION_START` in `lionagi/cli/state.py`. Both are single-line
additions that activate the shipped HookBus governance.

### 5. Memory-write enforcement → new branch hook

Branches don't currently emit memory-write events because `branch.memory`
doesn't exist (see ADR-0036). When khive memory ships, the toolkit fires
`MEMORY_PRE` events through HookBus, which the governance layer subscribes to.

### 6. Audit trail

Every `PolicyDenied` is logged via the existing `DataLogger`. Format:

```json
{
  "event": "policy_denied",
  "branch_id": "...",
  "capability": "tool.bash",
  "reason": "exec 'rm -rf /' not in allowed list",
  "tool_args": {...},
  "timestamp": "..."
}
```

Optionally writes to khive memory when `KhiveToolkit` is attached (one-line
opt-in, see ADR-0036).

### 7. Dead code cleanup

Wire the `error:` phase in `factory.py:138-143` to query
`AgentConfig.on_error()` — capabilities can register error handlers (e.g.
"on `tool.bash` error, summarize the stderr and write to memory"). If this
proves out of scope for 0.27.0, delete the dead phase instead.

## Consequences

**Positive**

- Zero new hook infrastructure. Reuses what's there.
- Capability checks live in one module (`lionagi/governance/enforcement.py`).
  Reviewable, testable, replaceable.
- Capability denials are first-class log events, queryable via khive.
- Sub-agent capability inheritance is enforced at branch construction
  (BRANCH_CREATE hook) without dispersed guard sites.
- The fix for the unwired BRANCH_CREATE / SESSION_START emit sites is a
  side-benefit beyond governance.

**Negative**

- Enforcement is distributed across three hook layers (security_pre,
  PreInvocation, PreEventCreate). The governance module must maintain a
  consistent model across all three layers.
- Hooks that mutate state (e.g., the PreEventCreate hook adjusting
  `allowed_tools` for the claude_code provider) introduce indirection: the
  effective request payload is not fully derivable from the call site. This
  behavior is documented but represents a non-trivial source of operational
  confusion.
- Coupling governance to the existing hook surface means HookRegistry changes
  carry a risk of breaking governance — an explicit coupling cost.

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| Wait for ADR-0023b/c HookBus wiring | Delays 0.27.0 indefinitely; existing surfaces are sufficient |
| Build a new `PolicyEngine` independent of hooks | Two enforcement systems introduce drift; the existing hooks already fire at the required enforcement points |
| Enforce only at iModel layer (single point) | Misses tool-call gates, can't redact tool args, can't constrain spawn |
| Enforce only at tool-call layer | Misses model-cost caps, misses agentic-CLI permission projection |
| Use middleware decorators on tools | Less observable than hooks (no event log); duplicates the hook semantics |

## References

- ADR-0023: Unified hook system (this ADR specifies the enforcement layer; ADR-0023 specifies the hook system itself)
- ADR-0033: Agent capability declarations (this ADR enforces those)
- ADR-0035: Capability projection for agentic CLI providers (where (3) gets
  its mapping table)
- `lionagi/agent/permissions.py:190`: existing `PermissionPolicy` — same
  mechanism, scoped to allowlist/denylist (subsumed by capability constraints)
- `lionagi/agent/factory.py:138-143`: dead `error:` phase that this ADR
  proposes to wire or delete
- `lionagi/hooks/`: shipped HookBus — used for memory/session/branch events
  once their emit sites are wired
