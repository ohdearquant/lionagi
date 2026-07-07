# ADR-0098: Resident Engine Work Queue (Warm Host Loop)

**Status**: Proposed
**Date**: 2026-07-07

## Context

lambda:khive runs a ~4.5-minute cron tick that today cold-starts a `li play` subprocess per
lane. A lane is a 30-90 minute, worktree-scoped, gated Rust fix/feature cluster (gates
`fmt`/`clippy`/`test`/`doc`, one report artifact, commits-not-push), run 3-5 concurrent. Ocean's
directive (2026-07-07, verbatim intent): a krons-worker pattern — "engine 开起来，有事情就 queue
进去，需要 state transition 的按 reactive 逻辑用." Kill the cold start per lane and the shapeless
handoff between the loop and the play. lambda:khive is the anchor tenant and supplied a bounded
requirements document (queue semantics on its `gtd` pack, `comm` event signaling, a session-pack
summary layer, an atomic claim/lease verb it owns, v0 non-goals). lambda:lattice is the second
tenant. Ocean's standing norm is that seats should not hand-write orchestration code beyond a
line or two — everything runs through lionagi's own surfaces — so this pattern is a first-class
lionagi product surface, not a one-off script.

Two facts, verified in source at HEAD, make the warm-host pattern viable without a rewrite:
warm session reuse already works (`session.flow()` is re-run on the same live session in
`cli/orchestrate/flow.py:1166`, and `EngineRun` holds a caller-passed session,
`engines/engine.py:186-192`), and `cwd` is fully parametric with zero `os.chdir` anywhere in the
runtime (`AgentSpec.cwd` threads through `factory.py:209/221/259`). What does not exist is a
task-source seam: the only out-of-process channel into a live flow is the `session_controls`
poll loop, whose `stop` verb is unimplemented and whose `message` verb can only append text to an
already-pending operation, never introduce new work (`operations/flow.py:107-135`). There is also
no session-summary API; `_synthesize` is a flow-output synthesizer, not a summary primitive
(ADR-0090 confirms no summarization surface exists).

**Motivation correction (load-bearing for this ADR).** The warm host is not sold on execution
speed. Cold-starting a `li play` subprocess costs seconds of import and client init against a
30-90 minute lane — under 1% overhead. Warmth is not the deliverable. The real deliverables are
(a) a structured queue, summary, and boundary-signaling contract that replaces the current
shapeless handoff between cron tick and subprocess, and (b) `ReactiveExecutor.inject()`, which is
strictly in-process and therefore only reachable from a warm host, enabling mid-lane steering
(a director injecting "also fix the CI break" into a running lane). A reviewer should reject any
framing of this ADR that claims warmth saves meaningful wall-clock time on long lanes; that claim
does not hold and this ADR does not make it.

This ADR is sibling to ADR-0091 (khive as a pluggable MemoryStore backend), which is scoped to
`memory.*` only and explicitly excludes `gtd.*`/`comm.*` as "task-lifecycle and messaging, not
memory-shaped." The queue this ADR defines lives in that excluded space, cites ADR-0091 for MCP
transport posture and the Protocol-plus-in-tree-default pattern, and does not reopen ADR-0091's
scope.

This ADR also carries a scope extension directed separately by Ocean — the `state.db`/khive
sessions-DB consolidation (see the StateStore section under Decision). Its provenance is a two-hop
relay through lambda:khive, anchored verbatim to comm message `9cacb116` (2026-07-07) and marked
**pending Ocean's own confirmation at the gate**; the full quote and the traceability condition
under which that section may be acted on are stated where the extension is defined, not asserted as
Ocean's verified first-person words here.

## Decision

The resident engine is one warm host process that dequeues tasks from a pluggable queue, runs
each as its own fresh `Session` under a per-lane watchdog, and writes a host-plus-session
summary on completion. The design is organized as five sub-decisions (Forks A-E) plus a scope
extension (StateStore consolidation) directed separately by Ocean.

### A. Lane execution model: one warm process, one fresh Session per lane

One warm host process runs concurrent lanes in a single anyio task group. Each dequeued task
gets its **own fresh `Session`**, constructed with an explicit `cwd` from the task's properties.
Only the `Engine` config object and process-level clients (the iModel pool, `MCPConnectionPool`,
and the khive daemon connection) are shared across lanes. Warmth means process and client reuse,
not session reuse.

**Sharing one `Session` across concurrent lanes is forbidden.** Signals (`NodeStarted`,
`NodeCompleted`, and the rest of the lifecycle bus) are observed on `env.session`, which is
per-lane; the `Semaphore` that bounds concurrency lives per-`EngineRun`, not globally. The
grounded warm-reuse fact (`_synthesize` re-running `session.flow()` on the same live session) is
a *sequential* same-session reuse inside one lane's own lifecycle, a different claim from running
two *concurrent* lanes on one session, which would cross-talk message piles and the event bus.

A task claimed with `cwd=None` must be rejected at claim time, not defaulted to `Path.cwd()` —
that fallback is the one shared-state trap in an otherwise fully parametric `cwd` path.

Residual risk from a 30-90 minute lane hanging or growing memory inside the host process is
bounded by construction, not eliminated by it: khive's lanes run CLI coding agents (`claude_code`,
`codex`) as **child subprocesses**, so the agent's own memory and cwd live in the child, not the
host — the host holds orchestration state only. The remaining residual risk is covered by three
mechanisms, all v0: (1) a per-lane wall-clock watchdog that cancels a hung lane's anyio task
without killing the host or sibling lanes; (2) lease expiry (below) returning an abandoned task to
the queue, since khive's requirements already pre-accept in-flight compute loss on crash; and (3)
host RSS monitoring driving a graceful drain-and-restart for long-uptime memory growth. These are
mitigations, not proofs of zero host-crash blast radius — a host process crash still takes down
every in-flight lane in that process; the queue makes that recoverable, it does not make it free.

### B. OSS/commercial boundary: a narrow Protocol pair, conditional on a real in-tree default

lionagi core ships two narrow Protocols, `TaskSource` and `SummaryStore`, following the ADR-0091
template (Protocol in core, khive as an external adapter). This is **conditional**, not
unconditional: ADR-0091's own rejected-alternatives table already forbids landing a Protocol
that would only ever have one real (out-of-repo) implementor, on the grounds that "landing the
Protocol in a vacuum... doesn't prove the seam generalizes." The same reasoning applies here. B1
is correct only if lionagi ships a real, zero-khive, in-tree default that lionagi's own tests and
smoke path exercise. If that default cannot be committed as part of this work, the fallback is a
narrower slice: Protocol types only, with a trivial in-memory default seeded from a list, and the
khive-backed implementation deferred.

**Protocol surface (minimum, nothing speculative):**

```python
class TaskSource(Protocol):
    async def claim(self) -> Task | None: ...
    async def complete(self, task_id: str, result: Any) -> None: ...
    async def fail(self, task_id: str, reason: str) -> None: ...


class SummaryStore(Protocol):
    async def write(self, session_key: str, summary: Summary) -> None: ...
    async def mark_pending(self, session_key: str) -> None: ...
```

`TaskSource.claim()` is async and atomic dequeue-plus-lease in one call; `complete()`/`fail()`
carry retry-and-ceiling semantics through to the backing queue (see the claim/lease contract
below — `fail()` is the one place Fork F's escalation ADR reaches back into this Protocol; see
References). There is no `heartbeat`/lease-renewal method in v0: a fixed, generous lease TTL
(see below) replaces renewal for the 30-90 minute lane shape lionagi is designed against today.
Anything beyond this surface is speculative and out of scope.

**Required in-tree default.** `InMemoryTaskSource`, a `deque`- or `state.db`-backed default that
runs the resident engine end to end with zero khive, so lionagi's own tests exercise the Protocol
against a real first implementor (lionagi already owns a SQLite state layer, ADR-0009). The
default `SummaryStore` writes to the run directory / `state.db`. khive's `gtd`-backed
`TaskSource` and khive session-pack-backed `SummaryStore` are the second implementors, living
outside the lionagi repo, importing the public Protocols — the same posture ADR-0091 established
for `KhiveMemoryStore`.

Rejected: **B3** (an optional "khive extra" dependency inside lionagi core) is precluded by
ADR-0091's own rejected-alternatives entry — extra-gating prevents the *dependency* from
installing by default, it does not prevent the *code and naming* from being visible in the public
source tree, which violates the never-leak-commercial-in-public-repo constraint by visibility, not
just installability. Rejected: pure **B2** (the host loop calling khive MCP verbs directly, no
Protocol) either leaks khive naming into the Apache-2.0 core, or if made config-driven-generic
collapses to B1 minus the type, with no reason to skip the type once there are two implementors.

There is no khive import and no khive naming anywhere in lionagi core under this decision.

### C. Task-source seam: poll-per-tick behind the Protocol

v0 implements `TaskSource.claim()` as a poll loop: the host polls `gtd` via MCP, claims via
khive's lease verb (below), and builds a fresh flow per claimed task. This is the *implementation*
behind the B1 *type*; the two are orthogonal, so C1 does not foreclose anything C2 (a pluggable
push-based source, plus the reserved `stop` verb and an `inject()` bridge) might want later.

**Non-foreclosure fence:** `claim()` is declared `async` and *may* await indefinitely. A poll
source loops with a sleep-tick; a future push source (a khive-cloud WS event plane) can block on
the socket until work arrives — both satisfy the same signature, so the host loop does not change
when the transport upgrades. Poll cadence is the host's concern, not the Protocol's.

### D. Terminal-stop to summary trigger: host owns the await

The host itself calls and awaits `session.flow()` / `run_dag()` for each lane, so it knows the
terminal state directly, with no need to observe the lifecycle bus (rejected: D2) or wait on an
external control verb (rejected: D1, for this purpose).

**Contract:** the await returning normally fires the summary turn on the same session. The await
raising — crash, cancellation, or the per-lane watchdog firing — calls `SummaryStore.mark_pending`
and does **not** fire a summary turn. A wedged or degraded session must never write a
half-summary.

The reserved `stop` control verb is explicitly a **different feature** — external, operator-driven
cancellation of a running lane ("director aborts lane X") — and is not this ADR's summary trigger.
It is out of scope for v0 and must not be back-doored into the summary-trigger path.

### E. Session-summary standard: three layers, split by authorship

The three-layer summary standard khive requested stands, but the authorship is split by source of
truth, which is the actual design gap the packet under-specified. "The session writes its own
summary" is fragile exactly when it matters most: a context-exhausted or degraded session is the
one that most needs summarizing and is least able to self-report accurately.

- **(a) one-line index** and **(b) structured facts** (files touched, gates run and their results,
  commits, wall-clock, tokens) are **host-authored from ground truth**, never from the LLM. Files
  come from `git diff --name-only` on the lane's worktree; gate results come from the gates the
  host itself ran; commits come from `git log`; wall-clock and token counts come from telemetry.
  The host already holds all of layer (b) mechanically; asking a degraded session to self-report
  it invites hallucinated recall.
- **(c) prose** (decisions made, dead ends, lessons) is authored by the **one session-summary
  turn** fired per Fork D's contract, best-effort. If the turn degrades or the session cannot
  produce it, `SummaryStore.mark_pending` is called and a later salvage pass re-derives (c); layers
  (a) and (b) are never lost, since they were never dependent on the LLM in the first place.

This directly answers the "no-backfill, stop-signal-triggered, degraded sessions marked
`summary_pending`" requirement: the machine writes the machine-readable parts, the LLM writes only
the reflective part.

### StateStore consolidation (scope extension, Leo-directed)

**Provenance of this section.** This scope was directed by Ocean and relayed to lambda:lionagi
second-hand through lambda:khive, via comm message `9cacb116` (2026-07-07). The relayed intent,
quoted verbatim as received: "lionagi state.db and khive sessions.db absolutely must consolidate —
today the same sessions get mirrored TWICE (claude_mirror into state.db, khive session pack
ingesting separately), and the DB itself belongs on the khive side." This section is drafted from
that two-hop relay; the exact wording is anchored to message `9cacb116` and is **pending Ocean's
own confirmation at the gate**. It is not presented here as Ocean's verified first-person words,
and the traceability anchor above is the condition under which this section may be cited or
acted on.

**Shape of the consolidation.** The same Protocol-seam family gets a third member, an abstract
`StateStore` protocol next to `TaskSource` and `SummaryStore`. The governing split: khive is the
system of record, lionagi is the runtime. `state.db`'s `runs`/`invocations`/`plays`/`teams`/
`schedules` rows are session-and-work lifecycle records — the same shape gtd/session-pack already
own on khive's side — and today they are written twice (once by lionagi's `claude_mirror` into
`state.db`, once by khive's own session-pack ingestion), which is exactly the double-write this
consolidation kills. `li monitor` reads through the `StateStore` seam rather than reading
`state.db` directly, the same transport posture ADR-0091 established (MCP `request`, never a
direct DB connection). Only **hot execution state** stays lionagi-side: second-granularity
`run_dag` progress and in-flight operation state, which is high-write-rate and short-lived by
nature. Only boundary transitions (`started`/`gated`/`done`, mirroring the lane-boundary events
Fork A-D already emit) are persisted through the seam, so khive is never used as a write-heavy
scratchpad for execution-internal state.

This is a design-shape statement, not an implementation slice for this ADR's v0 (see Minimum v0
below, which does not include the `StateStore` protocol). It is included here because it extends
the same Protocol-seam family this ADR already establishes for `TaskSource`/`SummaryStore`, and a
future implementation slice against it should not need a fourth sibling ADR to justify the shape.

**Open / to resolve at gate:** the exact `StateStore` method surface (mirroring `TaskSource`'s
`claim`/`complete`/`fail` shape, or a different verb set suited to boundary-transition writes) is
not specified by the relayed directive and is not invented here. It is deferred to the
implementation slice, once Ocean confirms the directive at the gate.

### Crash safety: the claim/lease dependency

The resident engine's entire crash-safety story — "host dies, in-flight lane's task returns to the
queue, nothing lost" — rests on khive's `gtd` claim/lease verb. That verb is pinned in a signed
joint contract (`CLAIM_LEASE_CONTRACT.md`, v1, khive signed implementable 2026-07-07, comm msg
`4351afcc`), reproduced here as the invariants this ADR depends on:

- **M1 — Atomic exactly-one-winner claim.** A claim transitions a task `next -> active`, stamping
  worker id and lease deadline in one atomic operation. When N workers race for the same task,
  exactly one wins; losers observe it as already-claimed. No two workers may ever both believe
  they hold the same task.
- **M2 — Lease TTL >= max lane wall-clock.** The lease deadline stamped at claim is caller-supplied
  per claim and must be at or above the maximum wall-clock a lane can run (30-90 minutes for
  khive's lanes; the pack enforces a floor of >=600s so a typo cannot reintroduce a double-pull).
  Expiry is **lazy, on-claim** — `claim()`'s pick predicate is `status='next' OR (status='active'
  AND lease_deadline < now)` — not a daemon sweep, since no daemon periodic timer exists to hang
  one on. A dead holder's task returns to the pool exactly when the next worker asks for work,
  never merely because a holder is slow. There is no lease-renewal/heartbeat verb in v0; a lane
  that outlives its lease is a spec error, addressed by setting `lease_seconds` high enough, not
  by building renewal machinery.
- **M3 — Attempt-increment-on-claim, ceiling to inbox plus alert.** Each claim increments a
  per-task attempt counter. Past a caller-supplied ceiling (default 3), the task does not return
  to `next` on the next failure; the pack-side transition moves it to `inbox` (out of the worker's
  pull path) atomically, and the **consumer** (the host loop, not the pack) emits a `lane_poisoned`
  comm alert on seeing the `{rejected: "attempt_ceiling"}` response. The pack stays decoupled from
  `comm` by design (the consumer owns alert emission, the same principle as `brain.auto_feedback`
  staying decoupled from `memory`).

The host loop calls `gtd.claim(assignee, lease_seconds, worker_id, max_attempts?)`,
`gtd.complete(id, worker_id, result)`, and `gtd.fail(id, worker_id, note)`. `worker_id` is
**optional** on `complete`/`fail`: the holder-check applies only when the task carries a claim
stamp (`worker_id` + lease in its properties), so the entire existing fleet `gtd` flow — tasks
completed by humans, Leo, or khive without ever being claimed — keeps completing without a
`worker_id`, zero breakage. A *claimed* task completed without a `worker_id`, or with the wrong
one, is rejected (`missing_holder`). When the stamp is present, both `complete` and `fail` are
holder-checked: a stale holder (lease already expired and reclaimed by another worker) gets a typed
`stale_lease` rejection rather than a silent double-complete over the new holder's work. khive owns and ships these three verbs in the `gtd` pack; lionagi's host loop consumes them
via MCP `request` only, never touching khive's database directly — the same transport posture
ADR-0091 established for `memory.*`.

## Minimum v0

A single warm host process, one anyio loop:

1. `task = await task_source.claim()` — v0 implementation polls `gtd` via MCP using khive's lease
   verb with a fixed, generous TTL. `None` means nothing claimable; sleep-tick and retry.
2. Up to concurrency N, for each claimed task start an anyio task: build a fresh `Session` with
   explicit `cwd` from task properties (reject the claim if `cwd` is `None`), and run one flow to
   completion under a per-lane wall-clock deadline.
3. Normal completion: the host writes summary layers (a) and (b) from git and telemetry, fires one
   session turn for layer (c), and calls `SummaryStore.write` (v0 implementation: khive
   session-pack via MCP). Then `task_source.complete()`. Comm events fire at boundaries
   (`lane_claimed`, `lane_gated`, `lane_blocked`, `lane_done`).
4. Crash, cancellation, or watchdog deadline: `task_source.fail()` (increments the attempt count;
   a ceiling hit routes to `inbox` plus the consumer-emitted `lane_poisoned` alert) and
   `SummaryStore.mark_pending`.

Excluded from v0: the `stop` verb, the `inject()` bridge, tier escalation (ADR-0099), push
transport, reactive checkpoint/resume, and lease-renewal/heartbeat. Protocol types and a
zero-khive in-tree default ship so lionagi's own tests run without khive; khive's `gtd`/
session-pack adapters live outside the repo. This proves the full loop — warm client reuse,
queue-driven dequeue, worktree isolation, boundary transitions, summary write-back — and
lambda:lattice becomes tenant #2 by writing adapters against the same Protocol.

## Consequences

**Positive**

- Replaces a cold-start-per-lane, shapeless-handoff loop with a structured queue, boundary-event
  vocabulary, and a summary contract that survives degraded sessions.
- `inject()` mid-lane steering becomes reachable as a fast-follow, which is impossible under the
  current subprocess-per-lane model.
- The Protocol pair follows a proven pattern (ADR-0091) rather than inventing a new one, and is
  gated on the same "real in-tree default" bar that pattern established.
- The crash-safety story is pinned against a signed contract (M1-M3) rather than an assumed verb,
  closing the single biggest risk the advisor flagged for this gate.
- lionagi core gains zero khive dependency and zero khive naming; khive remains an external,
  opt-in adapter, consistent with ADR-0091's posture.

**Negative**

- A1's v0 advantage over a subprocess-per-lane model is modest on its own (process and client
  warmth only); the real differentiator, `inject()`, is a fast-follow, not v0 — the ADR is
  explicit about this so the gate is not sold on an overstated speed claim.
- A host-process crash still takes down every in-flight lane running in that process; the queue
  makes the lost work recoverable (M2 lease expiry), it does not make the crash free of cost.
- No lease-renewal/heartbeat in v0 means a lane that genuinely needs longer than its lease TTL is
  a spec error today, not a handled case; the mitigation is sizing `lease_seconds` generously
  (>=600s floor, caller-supplied above that), not a renewal loop.
- The `StateStore` consolidation section is drafted from a second-hand relay and is explicitly
  gated on Ocean's confirmation before it is acted on; its method surface is unresolved.
- `B1`'s Protocol pair is only justified if the in-tree `InMemoryTaskSource` default is actually
  committed and exercised by lionagi's own tests; if that slips, the fallback (Protocol-types-only,
  trivial in-memory default) is a narrower ADR-0098 than described here and should be re-scoped
  rather than silently shipped as the full B1.

## Rejected Alternatives

| Alternative | Why Rejected |
|---|---|
| A2: warm supervisor, subprocess-per-lane | Forecloses `inject()` mid-lane steering, an explicit Ocean want; does not deliver the cold-start-eliminated outcome Ocean asked for; "defeats the purpose" framing is weak on its own (cold start is cheap for long lanes) but A2 still loses on the inject point. |
| A3: hybrid, warm for short lanes, subprocess for long/risky ones | Adds a branch and two code paths for a problem the child-subprocess execution model (CLI coding agents as children of the host) already solves for the long-lane case. |
| B2: host loop calls khive MCP verbs directly, no Protocol | Either leaks khive naming into the Apache-2.0 core, or if made generic collapses to B1 minus the type with no reason to skip it once there are two implementors. |
| B3: optional "khive extra" dependency inside lionagi core | Precluded by ADR-0091's own rejected-alternatives entry: extra-gating hides installability, not visibility, in the public source tree. |
| C2 now: pluggable push `TaskSource` plus `stop` verb plus `inject()` bridge, built for v0 | Speculative ahead of the first tenant; the non-foreclosure fence (`claim()` is async and may await) means C1 does not block C2 later. |
| D1: implement the reserved `stop` verb as the summary trigger | `stop` is a different feature (external cancellation); the host already knows its own await's terminal state with no new machinery. |
| D2: host observes the `RunEnd` bus signal | Adds an observation indirection the host does not need, since it already awaits the flow call directly. |
| E: session self-authors all three summary layers (the original packet's design) | Fragile exactly when it matters — a degraded, context-exhausted session is the one least able to self-report structured facts accurately; splitting authorship by source of truth removes that failure mode for layers (a)/(b). |
| F folded into this ADR | Different layer (flow-executor-internal escalation vs. host-loop boundary transitions), different reviewer, different blast radius, independently shippable; folding it bloats this ADR and couples two things that ship on separate clocks. Split into ADR-0099. |

## Implementation fences

- **MAY**: run concurrent lanes in one warm host process, each with its own fresh `Session` and
  explicit `cwd`; share only the `Engine` config and process-level clients (iModel pool,
  `MCPConnectionPool`, khive daemon connection); implement `claim()` as poll-per-tick behind the
  `TaskSource` Protocol; author summary layers (a) and (b) mechanically from git and telemetry.
- **MAY NOT**: share one `Session` across concurrent lanes; let a lane run with `cwd=None` (reject
  at claim); build the `stop` verb, the `inject()` bridge, tier escalation (ADR-0099), or
  lease-renewal in v0; put any khive import or khive naming in lionagi core; ship the `TaskSource`
  Protocol without a real zero-khive in-tree default; ask a degraded session to self-report layer
  (b); requeue a failed task without the global attempt ceiling khive's `gtd.claim`/`gtd.fail`
  already enforce; fire a summary turn on a lane whose flow await raised (`mark_pending` instead).

## Verify by

1. A zero-khive smoke test drives the resident engine against the in-tree default `TaskSource`
   end to end: claim, run a fresh-Session lane in a temp worktree, write a summary, complete.
2. A concurrency test proves two workers claiming the same task yield exactly one winner; the
   loser gets `None`.
3. A hung-lane test proves the watchdog cancels one lane without killing sibling lanes or the
   host, and the task returns to the queue.
4. A degraded-session test proves the flow-await-raises path writes `summary_pending` and does not
   fire a summary turn.
5. `grep` across the lionagi package tree for any khive symbol returns zero hits.

## References

- ADR-0091: khive as a Pluggable lionagi MemoryStore Backend
  (`docs/adrs/ADR-0091-khive-pluggable-memorystore-backend.md`) — Protocol-plus-in-tree-default
  template; MCP transport posture; rejected-alternatives precedent for B3.
- ADR-0090: Minimal Memory Contract and Pluggable Backend Seam
  (`docs/adrs/ADR-0090-minimal-memory-contract-and-backend-seam.md`) — in-tree default precedent,
  `state.db`/ADR-0009 state-seam context.
- ADR-0009: SQLite State Layer (`docs/adrs/ADR-0009-sqlite-state-layer.md`).
- ADR-0099: Escalation node_builder Tier Bump (`docs/adrs/ADR-0099-escalation-node-builder-tier-bump.md`)
  — out-of-scope sibling dependency: the global attempt ceiling that makes `TaskSource.fail()`
  safe against poison work also bounds ADR-0099's DAG-local escalation give-up path.
- `lionagi/.khive/workspaces/20260707/resident-engine/ADVISOR_VERDICT.md` — the resolved design
  decision this ADR encodes (Forks A-F).
- `lionagi/.khive/workspaces/20260707/resident-engine/CLAIM_LEASE_CONTRACT.md` — the signed joint
  contract (v1) for the `gtd` claim/lease verb, M1-M3.
- `lionagi/.khive/workspaces/20260707/resident-engine/PACKET.md` — the original advisor packet.
- khive `.khive/workspaces/20260707/resident-engine/REQUIREMENTS.md` — the anchor tenant's
  consumer requirements this ADR is grounded against.
- comm message `9cacb116` (2026-07-07, lambda:khive to lambda:lionagi) — the relayed StateStore
  consolidation directive; see the StateStore section above for the traceability condition.
- comm message `4351afcc` (2026-07-07, lambda:khive) — khive's signature on the claim/lease
  contract v1.
