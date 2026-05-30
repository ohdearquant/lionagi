# ADR-0072: Reactive Capability Bus

**Status**: accepted
**Date**: 2026-05-29

## Context

An agent's turn used to be opaque and terminal. You called
`response = await branch.operate(response_format=Finding)`, waited for the whole
run to finish, and got one `Finding` back. While the model worked — reading
files, reasoning, calling tools — the orchestrator sat idle. The only way to get
structured signals *during* a run was to hand the model a dedicated tool to
call, which is heavyweight and bends the model toward tool-calling instead of
its natural response.

We want the opposite: **an agent's cognition as an observable, typed event
stream that can be reacted to and steered in real time.** As the model works it
should be able to emit typed signals — `Finding`, `Question`, `Hypothesis`,
`Correction`, `Citation` — inline in its ordinary text, and an orchestrator
should be able to react the instant each one appears, without interrupting the
agentic loop.

This crystallized a standing thesis: **"a capability = a structured-output
event."** A capability is a named, typed field an agent may produce; exercising
it is emitting a value of that type; an observer reacting to that type is the
capability being *honored*. The whole thing should be built on lionagi's own
primitives (`Element`, `Pile`, `Progression`, `Flow`, `Observer`), not an
external event engine.

A prior prototype (lionag2, on AG2/autogen.beta) validated the shape — `Flow` as
a shared pile plus named condition-streams, `@agent.observer(EventType)`
reactions, `ctx.send(TypedEvent)` emission — by hacking an `id` onto AG2 events
so they could live in a `Pile`. The lesson kept: we do **not** need a heavy
event model, only an identifiable envelope.

## Decision

Build a reactive capability bus on the `Session`, in three layers.

### 1. Signals — the envelope

`Signal(Element)` is a lightweight Observable envelope carrying an arbitrary
`data` payload; its `id` comes for free from `Element`, so it lives in a
`Pile`/`Flow` like anything else. `StructuredOutput(Signal)` is the typed case
(`data` is a structured model). `ActionRequestSignal` / `ActionResponseSignal`
wrap tool-use / tool-result messages.

Observers key off the **payload**, not the envelope: an emitted `Signal` is
unwrapped (`_payload`) before filtering, so `session.observe(MyModel)` fires for
`emit(StructuredOutput(data=MyModel(...)))`. Bare Observables dispatch by their
own type. The full envelope is always stored in the `Flow` for audit, even when
a gate denies dispatch.

### 2. The observer and the Filter DSL

`SessionObserver` runs one chain on `emit`: **gate → store → route → dispatch.**
The gate is the single governance seam (a callable, sync or async; falsy
suppresses dispatch but not storage). Routes append matching events to named
condition-streams (lazy `Progression`s over the one shared `Flow`). Dispatch
fires subscribed handlers.

Subscriptions are **Filters** (`lionagi/ln/types/filters.py`):

- `TypeFilter(T)` — matches when the payload *is* a `T`, or carries a field
  whose value is a `T` (it scans `model_fields`). A type subscription is just a
  filter.
- `SpecFilter` — a predicate, built by `FieldRef`, matching a *named field by
  value*: `flower.q == "rose"`.
- Filters compose with `& | ~`; `observe(type | Filter | predicate)` coerces via
  `as_filter`. The handler receives the matched value (the instance for a type,
  the payload for a value/predicate filter).

Two naming/structure choices, both forced by existing code:

- **Filter, not Condition.** `Condition` is already a protocols concept
  (`await condition.apply`).
- **Operators on `FieldRef` (via `Spec.q`), not `Spec`.** `Spec` is a frozen
  dataclass used in sets, dedup, and the annotation cache — `Spec.__eq__` is
  load-bearing. Overloading it to return a predicate would silently break the
  Operable system, so `spec.q` hands out a fresh `FieldRef` whose operators
  build `SpecFilter`s.

### 3. Capabilities — grant, emission, prompt

A capability is a named typed `Spec`; an agent's **grant** is an `Operable` of
those Specs, held on `branch.capabilities` (the runtime carrier). The streaming
`run` loop — the single chokepoint for both `branch.run` and operate-CLI — parses
every assistant message:

- Pull every fenced ```json block out of the text (fuzzy-tolerant; the
  injected prompt instructs the model to fence its emissions). A single
  response may carry several blocks. A response that is *itself* one JSON
  object is also parsed, but un-fenced JSON **embedded in surrounding prose**
  is not extracted — fencing is the contract, which avoids false positives on
  incidental JSON.
- Per block, apply the legality rule **`set(keys) ⊆ grant`**:
  - **disjoint** (no granted keys) → ordinary prose/JSON, ignored;
  - **subset** → validate via `operable.create_model(include=keys)
    .model_validate(block)` into a *bundle* (a dynamic model, one field per
    present capability) and emit one `StructuredOutput(data=bundle)`;
  - **mixed** (a granted key plus an ungranted one) → an illegal over-grant:
    the block is not honored and a `CapabilityViolation` is raised onto the bus.

Tool-use and tool-result messages always emit `ActionRequestSignal` /
`ActionResponseSignal`, making per-tool stats and reactive manipulation trivial.

**Opt-in is the presence of a grant.** `branch.grant_capabilities(operable)`
sets the runtime grant *and* injects an idempotent instruction block into the
system message (markered so a re-grant replaces rather than stacks;
`revoke_capabilities()` removes it). The prompt is rendered *from* the Operable —
instructions plus the exact JSON schema (`create_model().model_json_schema()`)
the extractor validates against, plus the prose mirror of the `keys ⊆ grant`
rule — so the prose can never drift from what is actually extractable.

`response_format` (the final-output strict parse) and `capabilities`
(per-message emission) are **independent, orthogonal knobs**.

### Run lifecycle signals

`branch.operate` emits a lifecycle triple onto the bus: `RunStart` →
`RunEnd(data=result)` on success, or `RunFailed(data=exc)` on error. These are
orthogonal to capabilities — they report the *run*, not an exercised
capability, so they require **no grant**. Observe them by their own envelope
type (`session.observe(RunEnd)`); because `RunEnd.data` unwraps, a plain
`session.observe(ResultType)` also fires on the final result. The per-message
capability bundles are distinct dynamic-model payloads, so there is no
double-emit between the two channels.

Lifecycle emission is gated only on *having a session observer attached* — a
**standalone branch (no observer) emits nothing, so its behavior is exactly as
before.** Within a session, the final result is intentionally surfaced on the
bus (it is the most important event of the run); this replaces the earlier,
ambiguous "emit any `BaseModel` result as `StructuredOutput`" path, which
conflated lifecycle with capability emission and could double-fire on CLI.

### Governing principle: the observer registry *is* the capability registry

There is no central capability enum or taxonomy. A capability is *live* iff
something observes it; `session.observe(Finding)` is what makes `Finding` real
for that session. Extraction only ever materializes what the grant allows, and
an un-observed value is just recorded data that costs nothing. This is the
"field earns its place" discipline applied to capabilities — absence beats
enforcement.

## Consequences

**Positive**

- Real-time steering: react the instant a typed signal streams in, mid-run,
  without a dedicated emit tool and without interrupting the agentic loop. A
  `claude_code/sonnet` agent reading the codebase emitted five `Finding`s inline
  and the observer fired on each as they arrived (`examples/capability_bus_demo.py`).
- Observability and audit: every signal (and every denied one) lands in the
  session `Flow`; tool-use stats fall out of `ActionRequestSignal`s.
- Governance seam: the `gate` mediates dispatch; `CapabilityViolation` turns an
  over-grant into an observable event rather than a silent drop. (Charter/ocap
  enforcement plugs in at the gate — see the governance projection.)
- Backward compatible: bare-type observe and `response_format` are untouched.

**Negative / limits**

- Dispatch is O(subscriptions) per emit (filters are evaluated linearly). Fine
  at expected handler counts; revisit if a session registers very many.
- The session `Flow` accumulates every signal (the audit trail). Long runs grow
  it unbounded; a sliding-window/eviction policy is a future concern.
- Extraction is best-effort and depends on model compliance — the model must
  emit valid JSON for the granted keys. The injected prompt elicits this but
  does not guarantee it.
- A *pure* ungranted JSON block is ignored, not flagged, to avoid false
  violations on incidental JSON; only *mixed* over-reach is caught. A model
  emitting only an ungranted "capability" is indistinguishable from ordinary
  data and passes silently.

## Alternatives considered

- **Dedicated emit tool (lionag2's `ctx.send`).** Reliable and natively typed,
  but heavyweight and steers the model toward tool-calling. Rejected as the
  default; tools stay tools. Field-in-structured-output is the chosen channel.
- **Parse only the final output.** The original strict mode — kept for
  `response_format`, but it cannot express a *stream* of mid-run emissions.
- **Predicate operators on `Spec`.** Ergonomic (`spec == "rose"`) but breaks
  `Spec`'s hash/eq contract and the Operable system. Resolved via `Spec.q`.
- **Per-field emission** (one `StructuredOutput` per capability value). Simpler
  dispatch, but throws away the field *name*, so scalar named capabilities
  (`Spec(str, name="flower_name")`) and `SpecFilter` value-matching become
  impossible. Resolved by emitting the named **bundle**.
- **A heavy `Event` model for emissions.** Unnecessary; a `Signal` envelope with
  an `id` is enough, exactly as the lionag2 prototype showed.

## Implementation

- `lionagi/ln/types/filters.py` — `Filter`, `TypeFilter`, `SpecFilter`,
  `FieldRef`, `as_filter`; `Spec.q` in `spec.py`.
- `lionagi/session/signal.py` — `Signal`, `StructuredOutput`,
  `ActionRequestSignal`, `ActionResponseSignal`, and the run-lifecycle
  `RunStart` / `RunEnd` / `RunFailed`.
- `lionagi/session/observer.py` — `SessionObserver` (gate → store → route →
  dispatch; Filter-based).
- `lionagi/session/capabilities.py` — `render_capabilities_prompt`,
  `CapabilityViolation`.
- `lionagi/session/branch.py` — `branch.capabilities`, `grant_capabilities`,
  `revoke_capabilities`, `emit`.
- `lionagi/operations/run/run.py` — `_attempt_extract`, `_emit_message_signal`,
  wired into the streaming yield points.
- `examples/capability_bus_demo.py` — live demo.

## Deferred

- Wiring capabilities into `AgentConfig`/`Role` (the "agent = role + modes +
  tools + models + governance + capabilities" home) — held until the casts
  rework settles.
- A real reaction in the demo's `dig_deeper` (spawn a sub-branch / enqueue a
  follow-up on high-confidence findings).
- `Flow` eviction policy for long-running sessions.
