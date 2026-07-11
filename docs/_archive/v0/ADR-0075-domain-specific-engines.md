# ADR-0075: Domain-Specific Agent Engines

**Status**: Proposed — infrastructure landed, engines pending
**Date**: 2026-06-01

## Context

The reactive capability bus (ADR-0072) and the orchestration clean break gave
lionagi a second way to run agents: instead of a controller *imperatively*
calling agents along a DAG, agents *emit typed payloads* and *reaction rules*
consume them. The `li o flow` reactive executor is one consumer of that bus — it
plans a DAG per task, then executes with live self-expansion. But planning a
fresh DAG on every invocation is the right tool only for *novel* tasks. For work
done **repeatedly in a known domain** (research, code review, incident triage),
the decomposition logic is stable and re-planning it each time is waste.

The prior-art `lionag2` codebase already proved the alternative: a generic
`Engine` base (`lionag2/engine.py`) plus two domain engines (`research/`,
`review/`). An Engine is a *standing reaction machine* — a shared event Flow, a
roster, domain emission types, and `@observer`-wired reaction rules that spawn
more work, bounded by config (`max_depth`, novelty thresholds, a concurrency
semaphore). Reviewing it against lionagi showed that **lionagi already has every
primitive it needs**, on first-class footing rather than the bolt-on `core/`
(Flow/Pile/Progression) lionag2 had to reimplement on top of ag2:

- **Emission store + typed query** — `SessionObserver.emit` records every event
  to `self.flow` (a `Flow` whose `items` is a `Pile[E]`); `observer.by_type(T)`
  and `route(cond, into=)` already provide lionag2's `flow.items[T]` and
  conditional streams.
- **Reactions** — `session.observe(EmissionType, handler)`.
- **Quiescence** — the `_spawn`/gather pattern, or the `ReactiveExecutor`.
- **Streaming output** — `session.flow_stream` (the `on_event` SSE analog).
- **Agent construction** — casts `AgentSpec.compose(role, modes=)` + pack config
  (ADR-0074), which is *richer* than lionag2's hand-rolled spec dicts and works
  across providers (incl. claude_code / codex), not just OpenAI.

The two missing pieces are small: an ergonomic query sugar, and the Engine
assembly itself.

## Decision

Introduce **domain-specific engines** as a first-class, batteries-included
layer over the reactive substrate, and the small primitives that make them
ergonomic. We are not speculating an abstraction — lionag2 validated the Engine
shape with two engines; we port that *proven* shape onto lionagi primitives
(the same "adapt, do not transliterate" discipline as the orchestration rewrite).

### 1. `pile[filter]` — query any Pile with the existing Filter primitive

`filters.py` already provides composable `Filter`s (`TypeFilter`, `SpecFilter`
via `FieldRef`, `&`/`|`/`~`). Wire them into `Pile.__getitem__` (whose current
keys — UUID / int / slice / ref-seq — never collide with a `Filter` or a bare
`type`):

```python
if isinstance(key, type):
    key = TypeFilter(key)                 # pile[FindingEmitted] sugar
if isinstance(key, Filter):
    return self.__class__(items=[v for v in self.values() if key(v)],
                          item_type=self.item_type)   # filtered Pile, like a slice
```

This gives `flow.items[FindingEmitted]`, `flow.items[spec.q >= 0.8]`,
`branch.messages[TypeFilter(ActionRequest)]` — everywhere, since everything is a
Pile. `observer.by_type(T)` collapses to `observer.flow.items[T]`. `Pile` stays
ignorant of `Signal`; the observer's Signal-unwrapping query layers on top. This
primitive stands on its own merit independent of engines.

### 2. A lionagi-native `Engine` base

A small base (~150 lines) over existing primitives — no new substrate:

- **State**: a `Session` (branches as agents) + the session's `SessionObserver`
  as the event store / reaction registry; a bounded-spawn set for quiescence.
- **Reactions**: subclasses wire `session.observe(DomainEvent, handler)` in
  `make_agent`; a handler may spawn more work (bounded by `max_spawn` / config).
- **`run_team`**: sequential roster turns with handoff (a `HandoffRequested`
  emission picks the next agent), with the `carry_instruction` variant for
  "instruction *is* the artifact" pipelines (review).
- **`run()` / `run_node()`**: subclass-defined pipeline lifecycle; streams
  events via `flow_stream`.

The base stays **generic** (roster + reactions + quiescence + team loop). The
tree-recursion in lionag2's base (`spawn_depth_node`, `max_depth`) moves to a
`TreeEngine` mixin, so flat engines (review by dimension) don't inherit depth
machinery they never use.

### 3. `make_agent` builds casts agents from a pack

An engine's roster is casts roles; `make_agent(role)` =
`create_agent(AgentSpec.compose(role, modes=…))` resolved through a **pack**
(ADR-0074). Concretely, **an engine's configuration *is*** `pack (roster +
model/effort/modes) + emission set + reaction rules + pipeline`. ADR-0074 is the
engine's agent-construction layer, not a side quest.

### 4. Two bus consumers, both kept

- `ReactiveExecutor` — one-shot DAG (plan → execute), task-group quiescence. The
  *generic planning engine* (`li o flow`), for novel tasks.
- `Engine` — standing reaction machine living across many `run_team` calls,
  active-task-set quiescence. Domain engines, for repeated domain work.

Same emission bus, two execution shapes. `li o flow` is reframed as the generic
engine; `research` / `review` are siblings (`li o <engine>` / `li engine`).

### 5. Ship two batteries first

Port **research** (recursive depth on novelty → cross-check → iterative paper
loop with gap-driven re-research) and **review** (dimensions → findings →
adversarial verify → synthesis). They double as the two concrete validations the
base is extracted against.

## Consequences

**Positive**

- Domain decomposition is amortized into a reusable engine — no per-task
  re-planning for known workflows.
- Engines are batteries-included *and* extensible: a custom engine subclasses
  the base; the base is the extension point.
- Reuses the entire reactive substrate (bus, store, `flow_stream`, casts) — the
  net-new code is assembly, not new machinery.
- `pile[filter]` is a broad ergonomic win beyond engines.
- Converges with ADR-0074: packs become engine configs.

**Negative**

- New public surface (an `Engine` base + one module per engine) to maintain and
  document.
- The observer retains *every* emitted event (`observer.flow`); long-running
  engines need bounding/compaction (lionag2 used a tail-window compaction). A
  retention/compaction policy must ship with the engine layer.
- Risk of over-generalizing the base; mitigated by extracting from two concrete
  engines (research, review), not from a speculative third.
- Two execution shapes (`ReactiveExecutor` vs `Engine`) is conceptual surface
  users must learn to choose between (novel task → flow; repeated domain →
  engine).

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Keep only the plan-then-execute DAG flow | Re-plans every invocation; no amortization of stable domain decomposition. Good for novel tasks only. |
| Transliterate lionag2's `Engine` onto ag2 inside lionagi | Drags an ag2 dependency and a bolt-on `core/`; ignores that lionagi already has Session/Pile/observer/casts natively. Same mistake the orchestration clean break corrected. |
| Add a new emission store for engines | Unnecessary — `SessionObserver.flow` already retains every emission and `by_type` already queries it. |
| Build the abstract `Engine` base first, speculatively | Premature. The shape is only justified because lionag2 proved it with *two* engines; we port that, validating against two native engines. |
| Per-engine bespoke implementations, no shared base | Duplicates quiescence, the team/handoff loop, and the store/query wiring across every engine. |
| Skip `pile[filter]`, keep `observer.by_type` | Works, but leaves the powerful Filter primitive unused at the Pile level and the query ergonomics worse than lionag2's `flow.items[T]`. |

## References

- ADR-0071 — Cognitive Mode Model (modes composed into engine agents).
- ADR-0072 — Reactive Capability Bus (`observe`/`emit`/`gate`; the substrate).
- ADR-0073 (never published; number unassigned) — Universal Agent Spec (`AgentSpec.compose`, engine agent construction).
- ADR-0074 — Role Composition & Pack-Based Per-Role Configuration (engine config).
- `lionagi/session/observer.py` — `SessionObserver` (emission store via `.flow`,
  `by_type`, `route`, `gate`).
- `lionagi/ln/types/filters.py` — `Filter`/`TypeFilter`/`SpecFilter`/`FieldRef`.
- `lionagi/protocols/generic/{pile,flow,progression}.py` — `Pile`, `Flow`.
- `lionagi/operations/flow.py` — `flow_stream`, `ReactiveExecutor`.
- Prior art: `lionag2/engine.py` (proven `Engine` base), `lionag2/research/`,
  `lionag2/review/` (two validating engines).
