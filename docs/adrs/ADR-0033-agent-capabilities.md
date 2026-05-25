# ADR-0033: Agent Capability Declarations

**Status**: Proposed
**Date**: 2026-05-25
**Extends**: ADR-0023 (unified hook system), ADR-0028 (status reason model)
**Targets**: lionagi 0.27.0

## Context

Agents in lionagi can do anything their tools allow: shell out, edit files,
write to memory, spawn branches, hit external APIs. Today, the safety surface
is a mix of:

- Hard-coded `guard_paths` / `guard_destructive` hooks attached by the `coding`
  preset (`lionagi/agent/hooks.py`)
- A `PermissionPolicy` with `allowlist` / `denylist` / `confirm` modes
  (`lionagi/agent/permissions.py`) — wired only when explicitly attached
- Per-agent profiles in `~/.lionagi/agents/{name}.md` that declare model and
  prompt but not what the agent is *allowed* to do
- For agentic CLI providers (claude_code / codex / gemini_code / pi), safety
  flags (`permission_mode`, `allowed_tools`, `sandbox`, `yolo`) are passed
  ad-hoc via `PROVIDER_YOLO_KWARGS` and CLI flags — not declared per agent

This is fine when humans write the agent profiles by hand. It is brittle when
agents spawn agents (subagent tool, FlowAgent, fanout) — the parent has no
canonical way to declare "this child can read but not write" or "this child
gets the memory tools but not bash."

### Triggering observation

PR #1151 added `lionagi.testing` so the CLI can be tested without real API
calls. But the test stories that justify the new infrastructure all reduce to
"is this agent allowed to do X?" — and that question has no first-class
representation in the codebase today.

## Decision

Introduce a **capability** as a typed, declarative grant a parent makes to an
agent. Capabilities are the unit of governance; every gate in the system —
tool execution, model invocation, memory write, branch spawn, file edit —
reads from the same capability set.

### Capability shape

```python
@dataclass(frozen=True, slots=True)
class Capability:
    name: str                          # canonical: "tool.bash", "memory.write"
    constraints: dict[str, Any] = {}   # optional bounds (paths, exec list, ...)
    scope: Literal["branch", "session", "global"] = "branch"
```

### Canonical vocabulary (closed set, v0)

| Family | Capabilities |
|---|---|
| `tool.*` | `tool.bash`, `tool.reader`, `tool.editor`, `tool.search`, `tool.sandbox`, `tool.subagent`, `tool.<custom-name>` |
| `memory.*` | `memory.recall`, `memory.write`, `memory.delete` |
| `kg.*` | `kg.search`, `kg.create`, `kg.link` |
| `branch.*` | `branch.spawn`, `branch.clone` |
| `model.*` | `model.invoke`, `model.cost_max_usd:N` |
| `network.*` | `network.http`, `network.mcp` |

Closed set — agents cannot invent new capability names. New capabilities require
an ADR amendment.

### Declaration sites

```yaml
# ~/.lionagi/agents/researcher.md frontmatter
---
model: gpt-5.4-mini
capabilities:
  - tool.reader
  - tool.search
  - memory.recall
  - memory.write
  - kg.search
constraints:
  tool.editor: { paths: ["./drafts/**"] }   # editor explicitly NOT granted
  memory.write: { importance_max: 0.8 }
  model.cost_max_usd: 2.00
---
```

Plus a Python builder for programmatic use:

```python
from lionagi.governance import Capability, CapabilitySet

caps = CapabilitySet.coding() | {
    Capability("memory.recall"),
    Capability("memory.write", constraints={"importance_max": 0.7}),
}
branch = Branch(name="coder", capabilities=caps)
```

### Default sets (presets)

```python
CapabilitySet.minimal()    # model.invoke only
CapabilitySet.research()   # + tool.reader, tool.search, memory.{recall,write}, kg.*
CapabilitySet.coding()     # + tool.bash, tool.editor, tool.search, tool.sandbox
CapabilitySet.full()       # everything (escape hatch — discouraged)
```

### Inheritance for spawned branches

When agent A spawns agent B via `tool.subagent` or `branch.spawn`, B's
capabilities default to **A's capabilities minus `branch.*`** (subagents can't
spawn by default). Explicit narrowing is supported; broadening requires the
parent to hold `tool.subagent.elevate` (separate cap, not in v0).

## Consequences

**Positive**

- One declarative surface for "what can this agent do," replacing 4+ ad-hoc
  mechanisms.
- Capabilities are inspectable, serializable, auditable. `branch.capabilities`
  surfaces in run logs and the Studio UI for free.
- Spawned branches inherit a narrowed set by default — no implicit privilege
  escalation through nested agents.
- Agent profiles become portable: a `researcher.md` from one host runs with the
  same guardrails on another.

**Negative**

- Adds a layer agents have to declare. Existing agent profiles need migration
  (default to `CapabilitySet.minimal()` plus whatever tools they already
  register).
- A closed capability vocabulary needs maintenance as new tool families ship.

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| Keep ad-hoc hooks (`guard_paths`, etc.) | No single surface; "what can this agent do" is unanswerable without reading Python |
| Use `PermissionPolicy` as the only mechanism | It's tool-args focused; doesn't cover memory writes, branch spawn, model cost |
| Capability strings only (no constraints) | Loses path scoping, exec allowlists, cost caps — every governance system needs constraints |
| Open vocabulary (any string) | Drift across projects; impossible to audit; security teams can't enumerate |
| Bind capabilities to roles instead of agents | Roles (researcher/coder/etc.) are useful presets but agents are the unit that holds capabilities; ADR conflates the two if it picks roles |

## References

- ADR-0023: Unified hook system (governance plugs into the hooks defined there)
- ADR-0028: Status reason model (capability denials use the reason taxonomy)
- ADR-0034: Hook-based governance enforcement (how capabilities are checked)
- ADR-0035: Capability projection for agentic CLI providers
- `lionagi/agent/permissions.py`: existing `PermissionPolicy` — subsumed
- `lionagi/agent/hooks.py`: existing `guard_*` hooks — re-expressed as
  capability constraints
