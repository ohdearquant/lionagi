# ADR-0033: Agent Capability Declarations

**Status**: Proposed
**Date**: 2026-05-25
**Extends**: ADR-0023 (unified hook system), ADR-0028 (status reason model)
**Targets**: lionagi 0.27.0

## Context

Agents in lionagi can perform any action their registered tools permit: shell
execution, file editing, memory writes, branch spawning, and external API
calls. The current safety surface is a mix of:

- Hard-coded `guard_paths` / `guard_destructive` hooks attached by the `coding`
  preset (`lionagi/agent/hooks.py`)
- A `PermissionPolicy` with `allowlist` / `denylist` / `confirm` modes
  (`lionagi/agent/permissions.py`) — wired only when explicitly attached
- Per-agent profiles in `~/.lionagi/agents/{name}.md` that declare model and
  prompt but not what the agent is *permitted* to do
- For agentic CLI providers (claude_code / codex / gemini_code / pi), safety
  flags (`permission_mode`, `allowed_tools`, `sandbox`, `yolo`) are passed
  ad-hoc via `PROVIDER_YOLO_KWARGS` and CLI flags — not declared per agent

The current approach is workable when agent profiles are authored manually.
It becomes brittle when agents spawn agents (subagent tool, FlowAgent, fanout)
— the parent has no canonical mechanism to declare "this child may read but not
write" or "this child receives the memory tools but not bash."

### Triggering observation

The introduction of a test infrastructure layer for the CLI (enabling
end-to-end testing without live API calls) surfaced a recurring question in
every test scenario: "is this agent permitted to perform action X?" That
question has no first-class representation in the codebase. Each test story
is forced to reverse-engineer permission boundaries from ad-hoc hook
configurations rather than reading a single declared capability set.

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

The vocabulary is a closed set; agents may not introduce capability names
outside this list. Extending the vocabulary requires an ADR amendment.

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

A Python builder is also provided for programmatic construction:

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

- Introduces an additional declaration surface for agent authors. Existing
  agent profiles require migration (defaulting to `CapabilitySet.minimal()`
  plus whatever tools they already register).
- A closed capability vocabulary requires ongoing maintenance as new tool
  families ship.

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| Keep ad-hoc hooks (`guard_paths`, etc.) | No single surface; the question "what is this agent permitted to do?" is unanswerable without reading Python source |
| Use `PermissionPolicy` as the only mechanism | Focused on tool-argument filtering; does not cover memory writes, branch spawning, or model cost |
| Capability strings only (no constraints) | Loses path scoping, exec allowlists, and cost caps; any practical governance system requires constraint parameters |
| Open vocabulary (any string) | Produces naming drift across projects; the granted surface is impossible to enumerate and audit |
| Bind capabilities to roles instead of agents | Roles (researcher, coder, etc.) are useful presets but the agent is the runtime unit that holds capabilities; conflating the two overloads role semantics |

## References

- [ADR-0023](ADR-0023-unified-hook-system.md) — Unified hook system; capability enforcement gates plug into the hooks defined there.
- [ADR-0028](ADR-0028-status-reason-model.md) — Status reason model; capability denials emit reason codes drawn from that taxonomy.
- [ADR-0034](ADR-0034-hook-based-governance-enforcement.md) — Hook-based governance enforcement; specifies how capability checks are evaluated at runtime.
- [ADR-0035](ADR-0035-cli-provider-capability-projection.md) — Capability projection for agentic CLI providers (claude_code, codex, gemini_code, pi).
- `lionagi/agent/permissions.py` — Existing `PermissionPolicy`; subsumed by the capability model introduced here.
- `lionagi/agent/hooks.py` — Existing `guard_*` hooks; re-expressed as capability constraints under this ADR.
