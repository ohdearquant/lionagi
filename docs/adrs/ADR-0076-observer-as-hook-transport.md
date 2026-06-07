# ADR-0076: Observer as the Canonical Hook Transport

**Status**: Accepted
**Date**: 2026-06-01
**Amends**: ADR-0023 (unified hook system) · **Builds on**: ADR-0072 (reactive capability bus)

## Context

Three event-dispatch mechanisms now coexist, and two of them are
unwired buses that solve the same problem without knowing about each
other:

| Mechanism | Status | Scope | Dispatch | Wired? |
|-----------|--------|-------|----------|--------|
| `service/hooks/` `HookEvent`/`HookedEvent`/`HookRegistry` | shipped | per-**iModel** | template method, hand-orchestrated pre→core→post | **yes** — `APICalling(HookedEvent)` |
| `lionagi/hooks/` `HookBus` (ADR-0023) | built | per-**session** | sequential, ordered, `StopHook` short-circuit, `blocking_emit` | **no** — zero non-test usages |
| `session/observer.py` `SessionObserver` (ADR-0072) | built | per-**session** | concurrent fan-out (`gather`), typed-payload filters, `gate()` | partial — engines/casts; `gate()` unused |

ADR-0023 set out to consolidate the legacy systems into `HookBus`.
Before that wiring landed, the reactive arc (ADR-0072) built a *second*
per-session bus — the observer — for typed capability events. The result
is the duplication ADR-0023 was meant to remove, reintroduced one layer
over.

A tempting shortcut — "fold `service/hooks` into `observer.gate()` (pre)
and `observe(EventStatus.*)` (post)" — does not survive contact with the
code:

1. **`gate()` runs post-invoke.** `observer.gate()` executes inside
   `observer.emit()`, and the only emit seam for events
   (`branch.emit_and_log`) fires at the **log point, after** the call
   completes. The gate gates *observer dispatch*, not the operation —
   it cannot block an API call. "pre-hook = gate()" is false as built.
2. **Scope mismatch.** `service/hooks` is per-iModel; the observer is
   per-session. A standalone `imodel.invoke()` with no session would
   silently lose its hooks.
3. **Dispatch disciplines differ, by design.** `HookBus` dispatches
   handlers **sequentially, in registration order, with `StopHook`
   short-circuit and `blocking_emit`**. The observer dispatches async
   handlers **concurrently via `gather`, with no ordering and no
   short-circuit**. This is not an accident to paper over: a guard hook
   must run *before* the thing it guards, and ordered persistence
   handlers must not race. Forcing hooks onto concurrent fan-out would
   be a correctness regression.

So the two buses are not redundant implementations of one thing — they
are a **transport** and a **dispatch discipline** that got fused into two
separate stacks.

## Decision

**The observer (`SessionObserver`, ADR-0072) is the single canonical
event transport. The ADR-0023 surface — the `HookPoint` vocabulary, the
agent-YAML loader, and the ordered/blocking dispatch discipline — is
re-based to sit on top of that transport rather than owning a parallel
one.**

Concretely:

### 1. One transport, two dispatch disciplines

The observer's `Flow` is the single in-memory event record. Over it sit
two dispatch disciplines, chosen per use:

- **Reactive fan-out** (`observe`): concurrent, unordered, no
  short-circuit. For capability events and lifecycle signals where
  handlers are independent.
- **Ordered hook chain** (`HookBus`): sequential, registration-ordered,
  `StopHook` short-circuit, `blocking_emit`. For guards (must precede the
  guarded op) and ordered persistence.

Both record onto the same transport, so everything is uniformly
queryable (`session.observe(...)`, `observer.by_type(...)`) and audit-
visible in one place. The disciplines are not interchangeable; the bus
exposes the one the call site needs.

### 2. HookPoint → typed Signal

Each `HookPoint` emission is carried as a typed `HookSignal` (a `Signal`
envelope holding `point` + the loose `kwargs`). `HookBus`, when bound to
a session's observer, emits a `HookSignal` onto the observer for every
`emit`/`blocking_emit` — so hook activity is recorded on the one bus and
reactive observers may subscribe with `observe(HookSignal)`. The ordered
handler chain registered via `bus.on(point, handler)` is dispatched by
`HookBus` itself (preserving order / `StopHook` / blocking), **then** the
signal is recorded. A standalone `HookBus` (no observer) behaves exactly
as today.

### 3. Pre/post fall out of `EventStatus`

For events that ride the total-`invoke` contract (ADR-0072: a business
failure is captured as `FAILED`, not raised), the post-hook is just
`observe(EventType, EventStatus.COMPLETED | FAILED)` — no parallel
post-invoke vocabulary required. The genuine **pre**-hook (the one that
must *block*) is the keystone the shortcut got wrong; see Follow-ups.

### 4. `service/hooks` migration is staged, not ripped out

`APICalling(HookedEvent)` is the hot path behind every provider call and
stays exactly as-is until its replacement is wired and proven. ADR-0023's
"keep via compat, remove in 0.28.0" schedule stands; this ADR only
changes the *target* of that migration from a standalone `HookBus` to the
observer-backed `HookBus`.

## Scope of the landing change

This ADR lands the **foundation** only:

- `HookSignal` typed envelope (`lionagi/hooks/bus.py`).
- `HookBus` gains an optional `observer` binding; `emit`/`blocking_emit`
  record a `HookSignal` onto the bound observer while preserving the
  ordered/blocking dispatch contract unchanged. Unbound behaviour is
  identical to before. (`HookBus` has zero non-test wiring today, so this
  is additive and zero-blast-radius.)
- Tests proving: emissions land on the observer's flow; reactive
  `observe(HookSignal)` sees them; ordered dispatch + `StopHook` +
  `blocking_emit` semantics are intact; unbound `HookBus` still works.

It deliberately does **not** touch the wired hot path. See Follow-ups.

## Follow-ups (sanctioned, not in this change)

1. **Real pre-invoke gate.** Give an operation a way to consult the
   session's gate(s) *before* invoking (the fix for "gate runs
   post-invoke"). This is what makes `TOOL_PRE`/`API_PRE_CALL` blocking
   real on the observer. Wire `blocking_emit` through it.
2. **Wire `HookBus` into the session** (ADR-0023b): `Session.hooks`,
   `build_session_bus` from the agent profile, emit at the lifecycle
   seams (`SESSION_START/END`, `BRANCH_CREATE`, `MESSAGE_ADD`).
3. **Migrate `service/hooks`** onto the observer-backed bus: `APICalling`
   pre/post become a pre-invoke gate consult + `observe(EventStatus.*)`;
   collapse `HookedEvent`'s `_core_invoke`/`_should_exit`/`_exit_cause`
   once nothing depends on them. Keep the compat shim through 0.28.0.
4. **CLI/agent `_on_message` and tool hooks** (ADR-0023c) onto the bus.

## Consequences

**Positive**

- One transport, one event record — hooks, capability events, and
  lifecycle signals are all queryable and audit-visible in the observer's
  `Flow`. The duplication ADR-0023 targeted is actually removed instead
  of forked.
- ADR-0023's product surface (named points, declarative agent-YAML
  config, `blocking_emit`) is kept, not discarded — it rides the typed
  bus rather than a parallel dict.
- The ordered/blocking discipline that guards and persistence *need* is
  preserved explicitly, rather than being silently broken by concurrent
  fan-out.
- Post-hooks collapse into `observe(EventStatus.*)` for total-invoke
  events — less bespoke vocabulary to maintain.

**Negative**

- Two dispatch disciplines over one transport is a concept a reader must
  hold; mitigated by making the bus method names carry the discipline
  (`observe` = fan-out, `HookBus.emit` = ordered chain).
- The hot-path migration (Follow-up 3) remains the hard, high-blast-
  radius part and is still ahead of us; this ADR only makes it safe to
  approach incrementally.

## Alternatives Considered

| Alternative | Why rejected |
|-------------|--------------|
| Finish ADR-0023's `HookBus` as-is; keep observer for engines only | Leaves two per-session buses permanently; the duplication persists. |
| Observer only; delete `HookBus`'s named-point/YAML layer | Loses declarative agent-hook config (a real, intended feature of ADR-0023). |
| Map `bus.on` → `observe` literally | Breaks guard ordering and `StopHook`/blocking — concurrent fan-out is the wrong discipline for guards and ordered persistence. |
| Rip out `HookedEvent._invoke` now | High blast radius on every provider call; rests on the false "gate blocks invoke" premise. Staged instead. |

## References

- [ADR-0023](ADR-0023-unified-hook-system.md) — unified hook system (amended here)
- [ADR-0072](ADR-0072-reactive-capability-bus.md) — reactive capability bus / observer
- `lionagi/hooks/bus.py` — `HookBus`, `HookPoint`, `StopHook`
- `lionagi/session/observer.py` — `SessionObserver`, `observe`, `gate`
- `lionagi/service/hooks/` — legacy iModel hook system (migration target)
