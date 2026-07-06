# ADR-0095: Reactive Spawn Observability and Orchestration DX

**Status**: Proposed
**Date**: 2026-07-06
**Builds on**: ADR-0085 (flow control plane) · ADR-0072 (reactive capability bus) · ADR-0075 (domain-specific engines) · ADR-0077 (engine autonomy protections)

## Context

Reactive flow (ADR-0085, ADR-0072) lets a running operation emit a `SpawnRequest` that injects a new node into a still-executing DAG — self-expanding orchestration with no halt-and-replan. The mechanism works: a live dogfood confirmed an auditor node injecting a researcher node mid-flow, the branch cloning, executing, and the flow awaiting it before returning.

Dogfooding also surfaced that the feature's failure modes were invisible. `ReactiveExecutor` rejects a spawn for several reasons — the node builder raised, it returned no child, the injection would create a cycle, the spawn cap was hit, or the request was a duplicate — and every one of those rejections resolved to a `logger.warning` and a bare `return False`. Nothing reached the flow result. The result already reported `spawned_operations` (a success count), so a caller comparing "spawns I expected" against "spawns I got" saw a silent shortfall with no way to learn why. For an orchestration substrate whose value is that the record answers "what happened," a spawn that fails to a log line is a hole.

Three adjacent developer-experience gaps compounded it. `Role.load`/`Mode.load` raised `Unknown role: 'x'` with no list of valid names, though the catalog function sits beside them. The only shipped reactive example did not reliably trigger a spawn — a live run produced `spawned_operations=0` because the task gave the roles no reason to expand — so a first-time reader could not tell "worked, not needed" from "broken." And nothing pointed a production user from the raw reactive substrate to `lionagi/engines/`, the intended front door that wraps it with a budget cap, a deadline, and a quality gate (ADR-0075, ADR-0077).

This ADR records the observability contract and the DX conventions adopted in response. The decisions are retrospective: they are implemented in PR #1820 (observability) and PR #1819 (DX), and the observability shape was gated before implementation.

## Decision

### 1. Rejected spawns are first-class result data

The reactive flow result gains one new key, `dropped_spawns` — a list with one entry per rejected spawn or injection attempt. `spawned_operations` continues to count only fully-accepted spawns; the two are mutually exclusive per attempt. Every previously-silent `return False` in the spawn path now records before it returns.

Each entry is `{reason, assignee, emitter_id, ...}` where `reason` is drawn from a closed set:

- `builder_error` — the node builder raised; the entry also carries `error` (the exception string, truncated to 500 characters).
- `null_child` — the node builder returned no child.
- `cycle` — the injection would have created a cycle; the entry also carries `op_id` (the built child's id) for log correlation.
- `max_spawn_exceeded` — the spawn cap was reached; also carries `op_id`.
- `duplicate` — the same request was observed twice.

`builder_error`, `null_child`, and `duplicate` carry no `op_id`: they are recorded before or without a usable child object.

### 2. One recording chokepoint; a complete injection ledger

Cycle and cap rejections are recorded inside `_accept_node`, the single place that holds the built child and knows which guard failed. `_accept_node` is also the acceptance path for escalation-driven respawns and for the public `inject()` call, so `dropped_spawns` is deliberately a ledger of *all* rejected node injections under those two reasons, not only of reactive `SpawnRequest` spawns. This is the intended scope: the field answers "what injections were refused, and why," regardless of who requested them. Builder-error, null-child, and duplicate are specific to the `SpawnRequest` path and are recorded in `_inject_request`.

### 3. `duplicate` is a de-dup marker, not a failure

A `SpawnRequest` can reach the executor twice — once over the event bus and once from the post-completion result scan. The executor dedupes on request identity; the first sighting is injected normally, and the second is recorded as `duplicate`. It is not a lost spawn, and it carries no `op_id` because it is rejected before a child is built. A consumer counting genuine failures should treat `duplicate` distinctly from the four rejection reasons.

### 4. The result contract is documented at the surface

`flow()` documents its full return shape: the keys always present, and the reactive-only additions (`spawned_operations`, `escalated_operations`, `dropped_spawns`) with the `dropped_spawns` reason vocabulary. The `inject()`-while-not-running path keeps its existing `logger.warning` and is deliberately excluded from `dropped_spawns`: it fires outside a run and has no result dict to land in.

### 5. DX conventions for the reactive surface

- **Load errors name the alternatives, without changing their own failure mode.** `Role.load`/`Mode.load` now append the available names to the `Unknown role/mode` message. Because listing the catalog imports every built-in role/mode module, that listing is built through a best-effort helper that degrades to `Available: <unavailable>` if the catalog itself is broken — an error path must never let a secondary import failure replace the `ValueError` it was raising.
- **A shipped example must demonstrate the feature it names.** `examples/reactive_spawn.py` reliably triggers a spawn (single node, scope narrowed, then explicitly directed to use its granted spawn capability) so `spawned_operations >= 1` on a live run. A generic multi-role task that *might* spawn is not a demonstration of spawning.
- **The example points to the front door.** Reactive-flow examples direct production users to `lionagi/engines/` — the safe wrapper (budget cap, deadline, quality gate) over the raw substrate — rather than leaving the raw `session.flow(reactive=True)` call as the apparent recommended surface.

### 6. Compatibility

Additive only. `dropped_spawns` is a new result key; no existing key's presence, type, or value changes, and `spawned_operations` keeps its success-count meaning. The load-error change extends a message string that existing tests match by substring. No persisted schema or public signature changes.

## Consequences

**Positive**

- Spawn attrition is observable and diagnosable from the result alone: a caller sees both how many spawns landed and, for each that did not, why — with the builder's exception or the rejected child's id attached.
- The substrate-versus-front-door boundary is explicit in the shipped examples, steering production use toward the protected engine wrapper instead of the raw reactive call.
- Error-path robustness is stated as a convention (a diagnostic enrichment must not change the exception a path raises), not left as a per-site accident.

**Negative**

- `dropped_spawns` mixes reactive-spawn rejections with escalation- and manual-inject rejections under `cycle`/`max_spawn_exceeded`; a consumer that wants only `SpawnRequest` rejections must filter, and the field's exact scope has to be read from this ADR rather than inferred from its name.
- The reason set is a closed vocabulary this ADR freezes; a new rejection cause requires adding a reason here and in the recorder, not just at a call site.

## Alternatives Considered

| Alternative | Why rejected |
|-------------|--------------|
| Leave rejections as `logger.warning` only | The log is not the record; a programmatic caller cannot act on a warning, and the silent shortfall against `spawned_operations` was the reported failure. |
| Fold rejections into `spawned_operations` as a net count or failure tally | Loses the reason and the correlation id; a bare "3 dropped" is no more actionable than the log line it replaces. |
| A separate result key per reason | Five sparse keys for a rare event; a single reason-tagged list is easier to consume and to extend. |
| Record cycle/cap rejections only for `SpawnRequest` (skip escalation/manual inject) | Would require threading request-origin through `_accept_node` or duplicating the guard logic at each caller; the shared chokepoint gives a complete ledger for free, and completeness is the more useful default for an observability field. |
| Make the engine front door mandatory (hide raw reactive flow) | The raw substrate is a legitimate low-level API; the fix is to point users at the front door, not to remove the floor. |

## References

- ADR-0085 — flow control plane (pause/resume, message injection); the reactive execution surface this contract observes.
- ADR-0072 — reactive capability bus; the emission path a `SpawnRequest` travels.
- ADR-0075 / ADR-0077 — domain-specific engines and their autonomy protections; the front door this ADR points to.
- Implemented in PR #1820 (`lionagi/operations/flow.py` — `dropped_spawns`) and PR #1819 (`lionagi/casts/pattern.py` load-error hints; `examples/reactive_spawn.py`; engine front-door pointers).
