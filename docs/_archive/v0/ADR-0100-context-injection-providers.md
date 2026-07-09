# ADR-0100: Pre-Turn Context Injection Providers

**Status**: Proposed
**Date**: 2026-07-08

## Context

Agents benefit from organizational knowledge — prior lessons, curated corpus material, graph
context from a memory store — but today lionagi only surfaces such knowledge if the model
itself decides to call a tool for it. That makes knowledge delivery unreliable (models skip
retrieval under pressure), unauditable (no record of what was or wasn't available), and
impossible to toggle for measurement (there is no switch to run the same agent with and
without a knowledge substrate).

The turn loop already has exactly three context-shaping surfaces, each with a distinct owner:

| Plane | Surface | Owner |
|---|---|---|
| Pre-turn knowledge in | the rendered first-message guidance fold in chat preparation (`operations/chat/_prepare.py`): the first message's rendered content = `system.rendered + guidance`, recomputed every turn | **this ADR** |
| View selection | `branch.progression` (`session/branch.py`) — which recorded messages are in view | context tool (model-operated) |
| Post-tool steering | toolkit `"*"` post-hook suffix on tool results | nudge engine |

The pre-turn plane is the natural home for programmatic knowledge injection: it is rebuilt
every turn from the durable record, so injected text is ephemeral by construction — never a
message in the record, refreshed or dropped each turn, and a policy change takes effect on the
next turn.

Constraint discovered at source: `_prepare_run_kwargs` is synchronous. Any injection that
performs I/O (memory recall, corpus retrieval) must run earlier, in the async operation path
(the Middle), and hand its text to the render.

## Decision

Introduce an ordered **ContextProvider** registry on `Branch`:

```python
class ContextProvider(Protocol):
    async def provide(self, branch: Branch, instruction: Instruction) -> str | None: ...
```

- Providers run in the Middle (communicate / run / operate) before chat preparation. Their
  outputs are stored in a per-turn slot; the guidance fold renders
  `system.rendered + "\n".join(provider_blocks) + guidance`, and the slot is cleared after
  render. Injected text never enters the durable message record.
- Each provider declares `max_tokens`; the registry enforces a total injection budget
  (default ~2k, configurable), dropping lowest-priority providers first.
- Provider failure is contained: warn + skip, never block the turn.
- A reference `MemoryInjectionProvider` ships behind an optional extra: policy-configured
  recall against a pluggable memory backend (the ADR-0090 seam), with query text derived
  programmatically from the current instruction, an optional post-turn write-back hook
  (off by default), and per-run effectiveness recorded so a degraded backend is visible in
  run metadata rather than silently changing what the agent knew.

Division of responsibility with the sibling planes: providers inject **knowledge**; the nudge
engine injects **behavioral steering**; the context tool selects the **view**. A capability
that seems to need two planes composes them (a nudge can tell the model help exists; the next
turn's provider surfaces it) — no fourth mechanism.

## Consequences

- Knowledge delivery becomes a runtime policy, not a model choice: enable, disable, or re-scope
  injection per agent spec with zero prompt changes, which also makes with/without comparisons
  measurable.
- Zero record pollution and no context creep: the injection budget is a hard cap, and stale
  policy never lingers because nothing persists between turns.
- Core import path stays clean: the reference provider lives behind an extra; the registry
  itself has no third-party dependency.
- The seam adds one responsibility to the Middle (run providers, fill the slot); chat
  preparation itself stays synchronous and unchanged in shape.
- Write-back, when enabled, is bounded: low-salience, tagged provenance, and rule-based
  extraction first — richer extraction only lands with evidence that it pays.
