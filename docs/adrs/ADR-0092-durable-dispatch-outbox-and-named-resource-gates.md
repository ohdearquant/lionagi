# ADR-0092: Durable Dispatch Outbox and Named Resource Gates

**Status**: Accepted (spec gate signed 2026-07-04; rulings folded below)
**Date**: 2026-07-04
**Builds on**: ADR-0062 (scheduled item state machine, proposed) · ADR-0061 (universal scheduler, proposed) · ADR-0083 (lifecycle signal contract) · ADR-0085 §5 (terminal notify hook, proposed) · ADR-0027 (scheduled runs) · ADR-0030 (attention queue)

## Context

lionagi has four independent run-firing paths: the scheduler tick, the in-process
`li play` / `li o flow` / `li agent` paths, hand-fired CLI invocations, and
on_success/on_fail chain fan-out. There is no shared admission layer across them:
no durable pending-work state with retry attempts, no delivery-guaranteed outbound
notification, and no cross-process resource arbitration.

Two concrete failure modes motivate this ADR:

1. **Consumer dead at fire time.** An account-wide agent-session reset overnight
   2026-07-03/04 killed every agent seat for roughly 90 minutes. Scheduled
   notifications aimed at those seats had nowhere durable to land: nothing queued,
   nothing re-delivered. The hard requirement from the spec-gate holder: an event
   whose consumer is dead at fire time must survive and deliver on revival, never
   drop. The named use case is a post-reset revival heartbeat that pings each
   fleet seat, currently hand-rolled as a cron outside lionagi.

2. **Machine-level resource contention across firing paths.** Concurrent Metal/GPU
   work from independent processes corrupts numerics (observed roughly three times
   on one downstream consumer). The fleet convention is an advisory `flock` on a
   shared lock file (`/tmp/lion-metal-gpu-test.lock`). lionagi-fired work does not
   participate in that convention today, so a hand-fired `li play` can corrupt a
   concurrent non-lionagi run and vice versa.

### What already exists (build on, do not reinvent)

- **ADR-0061 + ADR-0062** already design the durable-queue vocabulary this problem
  needs: queued/leased/retry states, attempt counters, per-key concurrency,
  dependency edges, idempotent `transition()` writes into an append-only
  `status_transitions` log. Both are **proposed and unbuilt**: `schedule_runs`
  today carries none of it, and no lease/attempt/concurrency columns exist
  anywhere in the tree. This ADR implements a narrow slice of that design and
  references it; it does not supersede either document, and it leaves 0061's
  flow-type/webhook/template scope untouched.
- **ADR-0083 `Signal`** is the versioned event-payload envelope
  (`SIGNAL_SCHEMA_VERSION`, nullable-add is non-breaking).
- **ADR-0085 §5 terminal notify hook** (proposed, unbuilt) is the outbound
  transport seam: a shell template fired with a JSON payload. It is explicitly
  best-effort (10s timeout, failures logged and ignored). It is a transport, not
  a guarantee; the guarantee must be built on top of it.
- **`status_transitions`** (append-only, atomic with status writes, reason codes)
  and **`session_signals`** (per-session monotone `seq` cursor) exist in
  production. `session_signals` is rejected as the home for dispatch events
  because it is keyed by session: a dispatch enqueued while its consumer is dead
  has no session.

### Constraints that bind the design

- Single-writer SQLite (`_write_lock` + BEGIN IMMEDIATE); one scheduler instance
  in one daemon process, no leader election. A second daemon double-fires; do not
  design for HA.
- 30s scheduler tick is the latency floor; sub-30s delivery would need a dedicated
  wake (deferred; no concrete sub-30s consumer exists).
- `li schedule` is daemon-HTTP-only; `li monitor` reads the DB directly. The new
  CLI must pick one contract per verb and say why.
- Admission volume is machine-local tens per hour, not thousands per second, so
  single-writer serialization is a non-issue at this scope.
- v1 must revert cleanly (house slice discipline).

## Decision

**Do not build a monolithic central dispatch queue.** The net-new surface is two
narrow, independently shippable primitives:

### 1. Durable dispatch outbox with producer-driven at-least-once delivery

A new `dispatch_outbox` table in `state.db` holds outbound dispatches durably.
Delivery is **producer-driven at-least-once push to a durable sink**, not
consumer poll.

The decomposition that makes the guarantee work: durability and delivery are
separate guarantees.

- *Durability* (survive consumer death): the outbox row persists in `state.db`
  independent of any consumer's liveness. Non-negotiable foundation.
- *Delivery* (get it to the consumer): a **surviving producer** re-attempts the
  outbound push until the transport succeeds. The producer is the Studio daemon's
  scheduler loop, a local uvicorn process rather than an agent seat, so it
  survives an agent-session reset; if the daemon itself restarts, its
  missed-fire recovery re-fires on loop start.

Consumer-poll was considered and rejected as the guarantee: the reset kills
exactly the consumer's poller, so a pure durable-until-consumed model relocates
the hand-rolled revival cron into the consumer instead of retiring it. A dead
consumer cannot be relied on to pull. Poll remains a fallback read path
(`li dispatch ls`, and later `li monitor` surfacing) for live consumers that
prefer to pull, but it is not the guarantee.

The transport is ADR-0085 §5's notify hook shape: a configured shell template
fired with the payload JSON substituted. "Delivered" means the transport command
succeeded into a **durable sink** (for fleet seats, an inbox that survives the
seat's death). Transport failure leaves the row pending and the next tick
re-attempts with backoff, bounded by `max_attempts` and `expires_at`.

Template substitution must be argv-safe: `payload` and `deliver_to` are never
string-interpolated into a shell command line. The template receives the JSON
via the argument vector or stdin (or, where a template genuinely needs inline
placement, under strict quoting that the implementation enforces, not the
template author). This ADR is §5's first real consumer carrying message-body
content, so the injection constraint binds here.

**Schema** (migration carries it):

```sql
CREATE TABLE IF NOT EXISTS dispatch_outbox (
  id                TEXT PRIMARY KEY,
  kind              TEXT NOT NULL,              -- 'revival_ping' | 'terminal_notify' | ...
  deliver_to        TEXT NOT NULL,              -- opaque routing key for the transport template
  payload           JSON NOT NULL,              -- DispatchSignal contract (below)
  dedup_key         TEXT,                       -- cross-submission idempotency
  status            TEXT NOT NULL DEFAULT 'pending',
                    -- pending|delivering|delivered|acked|dead_letter|expired
  attempt           INTEGER NOT NULL DEFAULT 0,
  max_attempts      INTEGER NOT NULL DEFAULT 8,
  next_attempt_at   REAL NOT NULL,              -- backoff schedule; drives the tick scan
  ack_required      INTEGER NOT NULL DEFAULT 0, -- opt-in retry-until-ack tier
  ack_token         TEXT,                       -- consumer presents this to `li dispatch ack`
  session_id        TEXT REFERENCES sessions(id),        -- denormalized, nullable
  schedule_run_id   TEXT REFERENCES schedule_runs(id),   -- denormalized, nullable
  last_error        TEXT,
  created_at        REAL NOT NULL,
  expires_at        REAL,
  updated_at        REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dispatch_outbox_dedup
  ON dispatch_outbox(dedup_key) WHERE dedup_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_dispatch_outbox_due
  ON dispatch_outbox(status, next_attempt_at)
  WHERE status IN ('pending', 'delivering');
```

Design notes on placement, argued against the alternatives:

- **Not a separate `dispatch.db`**: the churn premise is false at tens/hour, and
  a second DB costs `li monitor` its single-DB read plus splits the reaper,
  status, and attention machinery that all live on `state.db`.
- **Not an extension of `schedule_runs`**: its `schedule_id` is NOT NULL, so
  every ad-hoc or hand-enqueued dispatch would need a synthetic schedule row.
  The outbox instead carries nullable denormalized refs both ways.
- **Delivery bookkeeping lives in columns** (`attempt`, `last_error`,
  `next_attempt_at`), not a per-attempt append log. At this volume an events
  table is over-engineering; defer it.

**State transitions** (`pending→delivering→delivered→acked`, plus
`→dead_letter` / `→expired`) write through ADR-0062's `transition()`
compare-and-swap with `entity_type='dispatch'`, so every hop lands in
`status_transitions` with a reason code (`dispatch.delivered.transport_ok`,
`dispatch.dead_letter.max_attempts`, `dispatch.acked.consumer`). ADR-0062's
transition API is itself unbuilt; because the outbox's state set is small, this
ADR carries a minimal guarded compare-and-swap fallback
(`UPDATE ... SET status=:to WHERE id=:id AND status=:from`, plus the atomic
`status_transitions` append) so the outbox is not hard-blocked on 0062 landing.
Whether to land 0062's full API first is an open question below.

**Ack tier** (opt-in): by default a row stops retrying on first transport
success (`delivered`). A dispatch created with `ack_required=1` keeps the
stronger semantics: the producer continues re-delivering until the consumer
presents the `ack_token` via `li dispatch ack`, or the row expires. Consumer-ack
is deliberately not the default: at tens/hour, forcing an ack round-trip on
every dispatch turns a local delivery into a distributed-consensus obligation
for marginal benefit.

**Payload contract**: the versioned artifact is a `DispatchSignal` extending
ADR-0083's `Signal`, obeying its version policy (nullable-field additions are
non-breaking; renames/removals bump `SIGNAL_SCHEMA_VERSION`):

```python
class DispatchSignal(Signal):
    """Outbound dispatch payload contract. schema_version rides Signal."""
    dispatch_id: str = ""
    kind: str = ""                 # revival_ping | terminal_notify | ...
    deliver_to: str = ""
    attempt: int = 0
    ack_token: str | None = None
    body: dict = {}                # kind-specific payload
```

The `{payload}` the notify template substitutes is
`DispatchSignal.to_dict(mode="json")`: one stable envelope shared by every
dispatch kind, so the transport template never churns per-kind.

**CLI contract**: enqueue-from-schedule-action goes through the daemon (the
scheduler already runs there). The read/ack verbs
(`li dispatch ls|show|ack|retry|purge`) follow `li monitor`'s direct-DB-read
discipline, NOT `li schedule`'s daemon-HTTP-only discipline. Rationale: the
whole point is post-reset survival; if `li dispatch ack` required the daemon to
be up, a daemon restart window would strand acks.

Because the ack/retry/purge verbs write to `state.db` from outside the daemon,
the outbox introduces a second writer process. Every direct-DB write is a
single-row guarded compare-and-swap inside `BEGIN IMMEDIATE` with a
`busy_timeout` set before any other pragma or statement. No CLI write path may
hold a transaction across user interaction or transport execution.

### 2. Named resource gates, queue-independent, composing with the OS flock

A `--gate <name>` flag on `li agent`, `li play`, `li o flow`, and `li schedule`
actions. Before the run starts, lionagi resolves the gate name through a
`gates:` name→lock-path table in settings (example:
`metal-gpu → /tmp/lion-metal-gpu-test.lock`) and acquires a **cross-process
advisory `fcntl.flock`** on that exact file (`LOCK_EX`, or `LOCK_EX|LOCK_NB`
with a timeout), releasing it when the run ends.

Hard constraint, per the spec-gate holder: **the gate composes with the
OS-level convention, it never replaces it.** Non-lionagi processes (cargo test
suites, one-off scripts, other harnesses) keep acquiring the same flock
directly, so:

- lionagi acquires the **same OS flock on the same shared path**. It does not
  substitute an internal semaphore, a private lock directory, or a queue-table
  row. The OS flock is the ground truth for acquisition.
- The dispatch queue may ORDER and OBSERVE gate contention (record "waiting on
  gate metal-gpu" on a dispatch row, surface it in `li monitor`), but queue
  state is never the sole arbiter of a machine-level resource. Any design where
  the queue row is the lock is ruled out: lionagi-managed and non-lionagi work
  would then race through the side door and the numerics corruption returns.
- lionagi-internal fairness (ordering among lionagi waiters, priority) may layer
  on top later; it sits above the flock, never in place of it.
- A lionagi-private lock directory is acceptable only for purely internal gate
  names that no external process contends.

The gate is deliberately **independent of the durable queue** so it ships
without the invasive all-paths admission rewrite: it is the one piece that the
direct in-process `li play` path genuinely needs, and it works there today as a
flag plus a lock helper. ADR-0061's `global_limit_key` does not cover this: it
is a scheduler-internal count bucket that a hand-fired `li play` (which never
enters the scheduler) would not respect. When the durable queue later routes a
path, a dispatch row declares its required gates and the queue serializes on
them; v1 ships only the flag and the lock helper.

### Distinction preserved: revival-push vs condition-wait

The fleet's dominant hand-rolled polling pattern (bounded sleep-loops on
external state, for example PR merge-state watches) is a *condition-wait*,
which is genuinely poll-shaped and distinct from revival *delivery*. This ADR
serves the delivery case with push-to-durable-sink; condition-wait stays poll
and belongs to the existing `li monitor run` terminal-wait primitive. Do not
collapse the two.

## Revival heartbeat, end-to-end (the hard case: consumer dead at fire time)

1. An interval schedule `fleet-revival-ping` fires on the daemon tick. The
   producer survives the failure mode: the incident was an agent-session reset;
   the daemon is not an agent seat. If the daemon itself restarts, missed-fire
   recovery re-fires at loop start.
2. The schedule action enqueues one `dispatch_outbox` row per seat:
   `kind=revival_ping`, `deliver_to=<seat routing key>`,
   `dedup_key=revival:<seat>:<reset_epoch>` (a re-fire cannot double-queue),
   `status=pending`, `next_attempt_at=now`, `expires_at=now+T`.
3. The row is durable in `state.db`. It exists independent of any consumer's
   liveness. Nothing to drop.
4. The delivery loop scans due pending rows each tick and fires the notify
   template per row: transition `pending→delivering` as an exclusive claim —
   `attempt++` and `next_attempt_at = now + claim_lease` in the same guarded
   UPDATE. Backoff is written only when the row returns to `pending`
   (transport retry or ack-required redelivery).
5. Consumer dead right now: the transport still lands the message in the seat's
   durable inbox (the inbox waits for the seat regardless of process liveness).
   On transport success, `delivering→delivered`. If the transport itself failed,
   the row stays due and the next tick re-attempts: at-least-once, bounded.
6. Revival: the seat comes back and its wake-up path reads the ping from its
   durable inbox. The guarantee is closed by two durable sinks (outbox, inbox)
   plus producer retry.
7. Optional ack tier: with `ack_required=1` the seat calls
   `li dispatch ack <token>` (direct-DB write) and the producer stops
   re-delivering. `max_attempts` bounds every send while awaiting ack:
   transport-failure exhaustion → `dead_letter` (max-attempts reason),
   successful-but-unacked exhaustion → `dead_letter` (ack-timeout reason).
   Un-acked past `expires_at` → `expired` as an additional, optional bound.
   Dead-lettered rows surface to the attention queue and `li monitor` for
   the operator.

No step depends on the consumer being alive at fire time.

## Slice-1 boundary

Ships first (v1):

1. `dispatch_outbox` table (migration); `dispatch` as a `status_transitions`
   entity type; guarded compare-and-swap transitions.
2. Producer delivery loop on the scheduler tick: scan due pending rows, fire
   the notify template, transition with backoff, `dead_letter` on
   `max_attempts`.
3. Named resource gate: `--gate <name>` flag + `gates:` settings table +
   shared-OS-flock acquisition, honored by `li agent` / `li play` /
   `li o flow` / `li schedule`.
4. `DispatchSignal` versioned payload contract.
5. Revival-heartbeat reference implementation: a schedule whose action enqueues
   one dispatch per seat.
6. `li dispatch ls|show|ack|retry|purge` (direct-DB read/ack).

Must NOT contain (v1):

- Routing the direct `li play` / `li o flow` / `li agent` paths through the
  durable queue (ADR-0061 universal-admission scope; deferred).
- Re-authoring 0061/0062's queue vocabulary or building the universal scheduler
  (flow types, webhook receiver, dependency-edge DAG, template renderer).
- A separate `dispatch.db`.
- A per-attempt `dispatch_events` append-only table.
- Global priority ordering / cross-schedule ranking (0061 scope; deferred).
- Sub-second or sub-30s push (a dedicated wake is a flagged follow-up only if a
  concrete sub-30s consumer appears).
- Leader election / multi-instance support.
- Baking any specific messaging CLI into lionagi code (the §5 shell-template
  boundary is deliberate; the transport command is configuration).
- Consumer-ack as a mandatory step (opt-in `ack_required` only).

## Consequences

- The revival heartbeat's hand-rolled external cron retires into a schedule +
  outbox rows with an auditable delivery trail (`status_transitions` reason
  codes, `li dispatch ls`).
- ADR-0085 §5's notify hook gains a real consumer and a durability layer above
  it, without changing its best-effort transport semantics.
- ADR-0061/0062 gain a shipped, narrow instantiation of their queue vocabulary
  instead of a competing one; their remaining scope stays theirs.
- One more run-shaped table on `state.db` widens the monitor join story;
  mitigated by denormalized refs and a status-scoped index, and the outbox is
  queried by its own index rather than joined on the hot path.
- Gate acquisition adds a blocking (or timeout-bounded) step in front of gated
  runs; ungated runs are unaffected.

## Spec-gate rulings (signed 2026-07-04)

1. **Transition machinery ordering**: ship the ~30-line guarded compare-and-swap
   fallback now; do not block on ADR-0062. Condition: the fallback mirrors
   0062's `transition()` signature and reason-code discipline exactly, so 0062
   absorbs it later as a refactor, not a migration.
2. **Gate table ownership**: the canonical `gates:` name→lock-path table is
   fleet-shared configuration owned by the fleet's global resource manager, not
   by this harness — machine-level resource conventions span non-lionagi
   processes. lionagi reads the fleet table and may add lionagi-private internal
   gate names in its own settings only. The concrete fleet file path is proposed
   in the implementation PR and placed by the fleet resource owner.
   Wedged-live-holder handling: surfacing the holder pid in
   `li dispatch` / `li monitor` is sufficient for v1; a live holder is never
   auto-broken.
3. **Backoff shape**: `min(30 * 2**attempt, 1800)` seconds, confirmed as
   proposed. No jitter: immaterial at tens of dispatches per hour.

## Post-signing erratum (2026-07-04, slice 1 review round 2)

Slice-1 implementation review (PR #1705) surfaced two places where this ADR's
text under-specified the guarantee it was actually describing. Both are
tightened here rather than left to contradict the shipped code.

1. **Ack tier boundedness is `max_attempts`-first, `expires_at` additional.**
   §"Ack tier" above says re-delivery continues "until the consumer presents
   the `ack_token` ... or the row expires," which reads as unbounded when no
   `expires_at` is set. That is not the intended contract: `max_attempts`
   bounds **every** send while awaiting ack, not only transport failures. A
   dispatch with `ack_required=1` that keeps transporting successfully but is
   never acked still exhausts at `max_attempts` sends and moves to
   `dead_letter` with the distinct reason `dispatch.dead_letter.ack_timeout`
   (as opposed to `dispatch.dead_letter.max_attempts` for the
   transport-failure exhaustion path). `expires_at` remains a valid,
   *additional* bound on top of `max_attempts` — it is honored when set, but
   is not required for an `ack_required=1` row to terminate. This was picked
   over the alternative of requiring a finite `expires_at` at enqueue time
   for `ack_required=1` rows: a mandatory expiry couples an orthogonal
   deadline concept to the ack tier for no correctness gain, since
   `max_attempts` already bounds it.
2. **A `delivering` recovery claim must be exclusive, not a same-state
   match.** The compare-and-swap description in §1 ("guarded compare-and-swap
   fallback... `UPDATE ... SET status=:to WHERE id=:id AND status=:from`")
   is CAS-correct for state changes but under-specifies the recovery case:
   the due-scan intentionally re-selects `delivering` rows for crash
   recovery, and a `delivering -> delivering` claim is a same-state match
   that the status guard alone lets two overlapping scans both win. The
   guarded fallback's claim step additionally guards on the row's pre-claim
   `attempt` value and atomically bumps `attempt` (plus advances
   `next_attempt_at` by a short claim lease) as part of the same guarded
   UPDATE, so only one overlapping claimant's guard still matches at write
   time. A later scan only revisits a `delivering` row once its claim lease
   has lapsed.

## Verify by

- Kill the consumer process entirely; enqueue a dispatch; confirm the row
  persists, delivery retries, the message lands in the durable inbox, and the
  consumer reads it on revival. Then repeat with the daemon also restarted
  between enqueue and delivery.
- Two concurrent gated `li play` runs on the same gate name serialize; a
  non-lionagi process holding the same flock blocks a gated lionagi run and
  vice versa (compose-with-convention proof).
- `dedup_key` uniqueness: re-firing the enqueue action does not double-queue.
- `max_attempts` transport failures produce `dead_letter` with the reason code
  in `status_transitions` and an attention-queue surface.
- v1 reverts cleanly: dropping the migration and the flag leaves no dangling
  references.
