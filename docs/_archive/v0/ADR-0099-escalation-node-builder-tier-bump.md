# ADR-0099: Escalation node_builder Tier Bump

**Status**: Proposed
**Date**: 2026-07-07

## Context

lionagi supports model-tiered roles: small, cheap models handle mechanical work by default, and a
node can emit an `EscalationRequest` when it is stuck or has low confidence, asking to be routed
somewhere better. ADR-0083 (Lifecycle Signal Contract) already defines the `escalated` terminal
state and the `NodeEscalated(op_id, name, reason, route)` signal, with `route="higher_tier"` for
re-dispatch and `route="give_up"` for no escalation path configured. ADR-0072 (Reactive Capability
Bus) is the substrate `EscalationRequest` rides on as a bus-emitted capability.

Verified in source at HEAD (`lionagi/operations/flow.py:804-834`,
`_schedule_escalation`): today, `route="higher_tier"` re-spawns the escalating operation by
calling `create_operation(emitter.operation, parameters=child_params)` — the **same** operation
definition, and therefore the **same model**, with an `[escalation] <reason>` prefix prepended to
the instruction. There is no tier bump. `route="give_up"` only marks the node `escalated` and
stops; nothing currently routes a given-up node anywhere else. This ADR is scoped to closing that
gap: mapping an escalated child to an actually higher model tier via a `node_builder`, and giving
give-up a landing place.

This is flow-**executor-internal** work — a node escalating to a different tier via
`node_builder`, consumed inside `_schedule_escalation`'s existing re-spawn path — the same layer as
ADR-0095 (Reactive Spawn Observability and DX), which already documents `dropped_spawns` covering
escalation-driven respawns alongside reactive `SpawnRequest`s and manual `inject()` calls. It is a
different layer, and a different blast radius, from ADR-0098's resident-engine host loop, which
only cares about lane-**boundary** transitions (done/failed/blocked) and treats each lane's
internal escalation behavior as unchanged. The advisor verdict that resolved this split (see
References) rejected folding this into ADR-0098 on exactly that basis: bloats the resident-engine
ADR and couples two things that ship on independent clocks.

This is also the escalation program tracked as GH #1253/#1254 (cheap-by-default routing with
escalate-when-stuck), scoped narrowly here to the tier-bump mechanism plus the queue-ceiling fence
below, not the whole program.

## Decision

`route="higher_tier"` gains an actual tier bump, bounded to a small local DAG, and
`route="give_up"` gets a defined landing place that is not an infinite retry.

**Tier bump.** A `node_builder` maps an escalating child to a higher model tier when
`_schedule_escalation` re-spawns it: instead of `create_operation(emitter.operation, ...)`
reusing the emitter's own operation definition unchanged, the builder constructs the child
operation against a higher tier in the role's model-routing pack. The tier ladder and its mapping
to concrete models are a routing-pack concern (per-role routing, consistent with the existing
model-tiered-role design), not invented in this ADR.

**Bounded to at most two local rungs.** An escalated child may itself escalate again, but only up
to two rungs within the same local DAG (emitter -> escalated child -> at most one further
escalated grandchild) before the path is exhausted. This keeps escalation a bounded, local retry
mechanism rather than an open-ended chain.

**Give-up returns to the queue.** When a node exhausts its two local rungs, or when no higher tier
is configured for `route="give_up"`, the node's underlying task returns to the queue (the `gtd`
task the lane is working, via `TaskSource.fail()` from ADR-0098) rather than dying silently inside
the DAG. The queue absorbs what the DAG could not resolve locally.

**The anti-footgun fence (load-bearing).** "Give-up returns to the queue" is safe **only if** a
**global per-task attempt ceiling lives in the queue**, not in the DAG. ADR-0098's claim/lease
contract already provides this: `gtd.claim` increments a per-task attempt counter on every claim
and refuses to re-arm the task past a caller-supplied ceiling (default 3), routing it to `inbox`
plus a consumer-emitted `lane_poisoned` comm alert instead (M3, `CLAIM_LEASE_CONTRACT.md`).
Without that ceiling, a node that always escalates and always gives up would cycle the same task
back through the queue forever — this ADR does not introduce a second, DAG-local attempt counter,
because the queue's counter already covers it and a second counter would just be two sources of
truth for the same invariant. This is the one place this ADR reaches into ADR-0098: it is a
constraint on `TaskSource.fail()`'s caller (this ADR's give-up path) and a requirement on the
queue owning the ceiling, not a new mechanism this ADR builds itself.

This ADR does not change `route="higher_tier"`'s or `route="give_up"`'s signal shape
(`NodeEscalated`), which ADR-0083 already defines; it changes what happens in
`_schedule_escalation` when those routes fire.

## Consequences

**Positive**

- Escalation becomes a real cheap-by-default, escalate-when-stuck mechanism instead of a same-model
  re-run with a prefixed instruction — closing the gap the packet named directly.
- Bounded to two local rungs plus a queue-level ceiling, escalation cannot become an unbounded
  retry loop, addressed at the layer (the queue) that already owns global attempt accounting.
- Independently shippable and revertible from ADR-0098: a small, bounded surface change inside
  `_schedule_escalation`, no resident-engine-loop changes required.

**Negative**

- This ADR has a hard runtime dependency on ADR-0098's claim/lease contract (specifically M3) for
  its safety property; it cannot ship a correct give-up path against a `TaskSource` that lacks a
  global attempt ceiling. This is stated as a coupling, not resolved further here.
- The tier ladder and model-to-tier mapping are left to the role-routing pack, not specified by
  this ADR; a routing pack without a configured higher tier for a given role makes
  `route="higher_tier"` behave as `route="give_up"` by construction, which is expected but worth
  naming as a real fallback path, not a bug.

## Rejected Alternatives

| Alternative | Why Rejected |
|---|---|
| Fold into ADR-0098 (the resident-engine ADR) | Different layer (flow-executor-internal vs. host-loop boundary transitions), different reviewer, different blast radius; bloats ADR-0098 and couples two things that ship on independent clocks. |
| Unbounded escalation rungs within the DAG | No stated cap risks the same infinite-retry footgun the queue ceiling exists to prevent, just moved one layer down; two local rungs plus the queue ceiling is the smallest bound that still lets a node retry once at a higher tier before falling back to the queue. |
| A second, DAG-local attempt counter for give-up | Duplicates the queue's own per-task attempt ceiling (M3); two counters for one invariant is a consistency hazard, not a safety improvement. |

## Verify by

1. A node emitting `EscalationRequest(route="higher_tier")` re-spawns as a child running against
   a demonstrably higher tier (different model), not the same model with a prefixed instruction.
2. A node that escalates twice within one local DAG hits the two-rung bound and its third
   escalation attempt routes to give-up, not a third local rung.
3. A give-up path calls `TaskSource.fail()`, and a task that fails past the queue's configured
   ceiling lands in `inbox` with a `lane_poisoned` alert, not an infinite requeue loop — this test
   exercises the ADR-0098 dependency directly, not a mock of it.

## References

- ADR-0098: Resident Engine Work Queue (`docs/adrs/ADR-0098-resident-engine-work-queue.md`) —
  sibling ADR; owns the global per-task attempt ceiling this ADR's give-up path depends on
  (`TaskSource.fail()`, M3 of the claim/lease contract).
- ADR-0083: Lifecycle Signal Contract (`docs/adrs/ADR-0083-lifecycle-signal-contract.md`) —
  defines `escalated`, `NodeEscalated(op_id, name, reason, route)`, and the `higher_tier`/
  `give_up` route vocabulary this ADR builds on.
- ADR-0072: Reactive Capability Bus (`docs/adrs/ADR-0072-reactive-capability-bus.md`) — the bus
  substrate `EscalationRequest` rides on.
- ADR-0095: Reactive Spawn Observability and DX
  (`docs/adrs/ADR-0095-reactive-spawn-observability-and-dx.md`) — sibling at the same layer;
  documents `dropped_spawns` already covering escalation-driven respawns through the same
  `_accept_node` chokepoint this ADR's re-spawn path uses.
- `lionagi/operations/flow.py:804-834` (`_schedule_escalation`) — current same-model re-spawn
  behavior this ADR replaces.
- `lionagi/.khive/workspaces/20260707/resident-engine/ADVISOR_VERDICT.md` — Fork F, the resolved
  decision this ADR encodes, including the split-into-its-own-ADR call and the anti-footgun fence.
- GH #1253 / #1254 — the broader escalation program this ADR scopes a narrow slice of.
