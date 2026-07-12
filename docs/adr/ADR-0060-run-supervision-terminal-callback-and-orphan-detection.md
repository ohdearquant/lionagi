# ADR-0060: Run supervision — generic terminal callback and two-stage orphan detection

- **Status**: Superseded by ADR-0095
- **Kind**: Aspirational
- **Area**: persistence-state
- **Date**: 2026-07-10
- **Relations**: extends ADR-0058 (unified lifecycle transition service), extends
  ADR-0059 (durable dispatch outbox), extends ADR-0071/ADR-0072 (durable ad-hoc task
  queue / unified task admission — the schedule_run lease-and-reaper machinery this ADR
  hardens), touches ADR-0057 (operational lifecycle and transition audit) and ADR-0035
  (persisted run-completion contract — the "terminal is terminal" guard this ADR relies
  on). None of these are superseded; this ADR adds a phase on top of each.
  Superseded by ADR-0095, which replaces the outbox-coupled callback delivery and
  two-stage orphan design decided here with a post-commit registry, set-based
  reconciliation acknowledgment, and a single classifier/coordinator contract.

## Context

lionagi already runs three genuinely different terminal-notification and
process-supervision mechanisms, none of which talk to each other, and none of which is
durable end-to-end:

1. **`fire_terminal_notify()`** (`lionagi/cli/orchestrate/_notify.py`), wired into
   `_run_flow`'s `finally` block in `lionagi/cli/orchestrate/flow.py` (~line 1593). It
   reads `notify.on_terminal` (a bare shell-command **string**) from
   `.lionagi/settings.yaml` via `load_settings()`, or an explicit `li o flow --notify`
   CLI override, and fires it with `asyncio.create_subprocess_shell(..., start_new_session=True)`
   **after** the invocation's terminal status has already been written by a separate,
   non-transactional `db.update_status("invocation", ...)` call a few lines earlier.
   Failure is logged and swallowed. It exists **only** on the `li o flow` path — `li
   agent`, `li play`, `li o fanout`, and every schedule-fired action have no equivalent.
2. **`lionagi/dispatch/outbox.py`** (ADR-0059's `dispatch_outbox` table) is a durable,
   at-least-once, argv-safe delivery mechanism with retry/backoff/dead-letter, driven by
   the Studio scheduler's 30-second tick. Its own docstring already anticipates a
   `kind='terminal_notify'` row (`schema_meta.py`'s comment: `kind ... 'revival_ping' |
   'terminal_notify' | ...`) and already carries `session_id`/`schedule_run_id`
   provenance columns, but **nothing in the codebase today calls `enqueue_dispatch()`
   with `kind='terminal_notify'`** — the anticipated kind is unused.
3. **`lionagi/studio/scheduler/worker.py`**'s lease-and-reaper pass (the ADR-0071/0072
   durable task queue) is the only process-liveness recovery mechanism that exists for
   scheduled work: a worker claims a `schedule_runs` row by writing
   `leased_by`/`lease_expires_at`/`lease_attempts` under a guarded CAS
   (`transitions.transition()`), and a reaper pass moves an expired lease straight back
   to `queued` (`RunReasons.QUEUED_LEASE_EXPIRED`) — or to `failed` once
   `lease_attempts >= MAX_LEASE_ATTEMPTS` (3) — with **no intermediate uncertain state**
   and **no fencing token checked by anything downstream**. A worker whose lease merely
   lapsed (GC pause, slow disk, not actually dead) can still be mid-execution when a
   second worker claims the same logical row after `queued` reappears; nothing at the
   sink rejects the first worker's late writes.

Separately, `lionagi/cli/kill.py` already computes `current_pid_markers()` — `{pid,
pid_create_time}` via `psutil.Process(pid).create_time()` — stored in session
`node_metadata` at session creation specifically to defend `li kill` against PID reuse
(CWE-362). This is exactly the "pidfd-equivalent birth identity" primitive the external
prior-art review cited below recommends as the minimum local process identity — it
already exists in lionagi, unused outside `li kill`.

Sessions, unlike schedule runs, already have a guarded health-sweep path:
`lionagi/state/reasons.py`'s `SessionReasons` health vocabulary
(`HEALTH_STALE_NO_HEARTBEAT`, `HEALTH_ORPHANED_NO_PROCESS`, `HEALTH_ZOMBIE_STALE_LOCKS`,
`HEALTH_PHANTOM_PROCESS_DEAD`, `HEALTH_PHANTOM_MISSING_ARTIFACTS`) is written through
the guarded `update_status()` CAS by both the Studio phantom reaper
(`studio/services/lifecycle.py`) and the doctor sweep (`cli/state.py`), each gated on a
multi-hour staleness threshold. Those sweeps are single-stage (stale evidence goes
straight to a terminal status), but the long threshold plus the process-identity check
makes their false-positive window materially smaller than the schedule-run reaper's;
extending the two-stage vocabulary to sessions is deliberately out of this ADR's Phase 2
scope.

An external prior-art review of run supervision across nine mature systems
(Kubernetes, Chubby, ZooKeeper, Temporal, systemd, Supervisor, Sidekiq, GitHub Actions,
Airflow; 2026-07-10) converged on a specific design for this exact problem, at CONFIRMED-BY-ANALYSIS confidence ≥0.90 on every load-
bearing claim: separate "evidence is stale" from "ownership is fenced" with an explicit
intermediate state; never infer death from a timeout; treat callback delivery as a
separate durable state machine, never a fire-and-forget shell hook; and never authorize
a replacement claimant before a durable compare-and-swap has committed. This ADR adopts
that direction and maps it onto lionagi's actual tables, actual transition machinery,
and actual scale (a single-host SQLite deployment by default, not the verdict's 10,000-
concurrent-run planning model).

This ADR answers six concrete problems:

**P1 — No cross-surface terminal callback.** `li agent`, `li play`, `li o fanout`, and
schedule-fired actions have no notification hook at all; only `li o flow` does, and it
is shell-based, non-durable, and fires outside any transaction.

**P2 — The one callback that exists is fire-and-forget.** `fire_terminal_notify()` runs
after the status commit, with no retry, no persisted record of the attempt, and no
distinction between "the hook wasn't configured" and "the hook failed" in any durable
store — only a log line.

**P3 — `dispatch_outbox` already has the durability primitive but no producer wired to
terminal status.** The `terminal_notify` kind is documented in a comment and unused.

**P4 — Schedule-run recovery has no uncertainty stage and no fencing.** A lapsed lease
requeues immediately; nothing prevents the pre-lapse worker from still committing after
a second worker starts.

**P5 — Local process identity already exists but is not reused for supervision.**
`current_pid_markers()` solves exactly the "beyond bare PID" problem for `li kill`; nova
supervision code duplicates or ignores it.

**P6 — lionagi has no run lineage/generation concept.** Every run (a `sessions` row) is
a single physical attempt with no `root_run_id`/`generation` pair; a resume today is
either a fresh session or, for the auto-resume handoff (`defer_terminal` in
`cli/_runs.py`), an in-place continuation of the *same* session row rather than a new
child row. This ADR's Phase 1 and Phase 2 do not require lineage; Phase 3 does, and is
deferred rather than assumed.

| Concern | Decision |
|---------|----------|
| Cross-surface callback contract | D1: One `on_terminal` shell-hook contract, generalized from `_notify.py`, callable from every spawn surface. |
| Configuration resolution | D2: `notify.on_terminal` evolves to a three-state `{unset, disabled, configured}` object; per-run CLI/schedule override wins, else project/global `settings.yaml` (lionagi's existing two-tier merge). |
| Durability | D3: Route the callback through the already-durable `dispatch_outbox` (`kind='terminal_notify'`), inserted in the same transaction as the terminal status write once ADR-0058 lands; degrade gracefully in the interim. |
| Callback identity | D4: Degenerate `event_id = "{session_id}:{terminal_status}"` (no generation yet), justified by lionagi's existing terminal-is-terminal invariant; documented upgrade path to `{root_run_id}:{generation}:{terminal_status}` under Phase 3. |
| Orphan detection | D5: Two-stage `running -> supervisor_unknown -> orphaned` for `schedule_runs`, replacing the current single-stage lease-expiry-to-`queued` reaper. |
| Process identity | D6: Reuse `current_pid_markers()` (`pid` + `pid_create_time`) as the stored identity for every locally-spawned attempt; never sweep on bare PID. |
| Rejected shortcuts | D7: Name the anti-patterns this design rules out, mapped to the concrete lines of current code that already embody two of them. |
| Rollout | D8: Gates adapted to a single-host SQLite default, not the prior-art review's 10k-run planning model. |
| Lineage and fenced resume | Phase 3 — **DEFERRED**, design recorded, not decided for implementation. |

This ADR deliberately does **not** decide:

- A universal `NormalizedState` read model spanning health, delivery, and lifecycle —
  ADR-0058 D6 already rejects that, and this ADR does not reopen it.
- Retry/backoff *values* for the callback delivery — ADR-0059 D4 already owns
  `backoff_seconds()`/`max_attempts`; this ADR reuses them unchanged.
- Extending the two-stage `supervisor_unknown` vocabulary to `sessions` — the existing
  session health sweeps already write guarded, threshold-gated transitions (see Context);
  whether they warrant an uncertainty stage is a separate, later decision.
- A sandboxing/allowlist mechanism for the configured callback executable — flagged
  NEEDS-EXPERIMENT by the source review at 0.85 confidence; lionagi's existing trust
  model (the operator who edits `.lionagi/settings.yaml` is already trusted to run
  arbitrary CLI commands) is noted as a plausible local answer but not decided here.
- Postgres-specific load numbers — ADR-0056 already establishes the SQLAlchemy Core
  dual-backend; this ADR's rollout gates are scoped to the SQLite default only.

## Decision

### D1 — One generic on-terminal callback, callable from every spawn surface

**The decision.** Generalize `fire_terminal_notify()` into a single callback contract
shared by `li agent`, `li play`, `li o flow`, `li o fanout`, and every scheduler-fired
action (`schedule_runs`). The contract is transport-agnostic: it is a configured argv
template plus a JSON payload, not a specific notification vendor.

**The contract.**

```python
# lionagi/dispatch/terminal_callback.py (target module; generalizes _notify.py)

TerminalCallbackState = Literal["unset", "disabled", "configured"]

@dataclass(frozen=True)
class TerminalCallbackConfig:
    state: TerminalCallbackState
    argv: tuple[str, ...] = ()          # e.g. ("/usr/local/bin/notify", "{payload}")
    timeout_ms: int = 30_000
    max_output_bytes: int = 65_536

class TerminalPayload(TypedDict):
    schema_version: int                 # 1
    event_id: str                       # "{session_id}:{terminal_status}" (D4)
    run_id: str                         # sessions.id
    spawn_kind: str                     # "agent" | "play" | "flow" | "fanout" | "scheduled"
    invocation_kind: str | None         # sessions.invocation_kind (ADR-0012 vocabulary)
    terminal_status: str                # sessions.status (ADR-0025/ADR-0058 vocabulary)
    reason_code: str                    # sessions.status_reason_code
    reason_summary: str
    exit_class: str                     # lionagi.cli.status._classify() output — reused, not reinvented
    save_dir: str | None
    cwd: str
    started_at: float
    ended_at: float
    duration_ms: float | None
    artifact_refs: list[dict]           # from artifact_verification_json, bounded
    metadata: dict                      # allowlisted: schedule_id, project, playbook_name

async def fire_terminal_callback(
    db: StateDB,
    *,
    entity_type: Literal["session", "invocation", "schedule_run"],
    entity_id: str,
    config: TerminalCallbackConfig,
    payload: TerminalPayload,
) -> str | None:
    """Enqueue (not execute) the callback; returns the dispatch_outbox id, or
    None when config.state != 'configured'. Never raises past the caller —
    a malformed config is a logged no-op, matching _notify.py's existing
    failure posture."""
```

**Exact semantics.**

- `spawn_kind` widens `invocation_kind`'s existing four-value vocabulary
  (`agent`/`play`/`flow`/`fanout`/`show-play`, `sessions` CHECK constraint,
  `schema_meta.py`) with a fifth value, `scheduled`, for a `schedule_runs`-originated
  terminal event that has no `sessions` row of its own (an `action_kind='agent'`
  schedule fire still creates a session; a bare shell/library action inside the
  scheduler does not). `fire_terminal_callback()` is callable with `entity_type in
  {"session", "invocation", "schedule_run"}` precisely because these are the three
  tables ADR-0058 D2 already registers policies for.
- `exit_class` is not invented fresh — it reuses `lionagi.cli.status._classify(entity_type,
  status)`, exactly as `_notify.py` already does at `flow.py`'s call site. No new
  success/failure taxonomy.
- `artifact_refs` is sourced from the existing `sessions.artifact_verification_json`
  column (ADR-0029/ADR-0064), truncated to a bounded count (default 20) — this is new
  data already computed by `verify_artifact_contract()` in `_teardown_common`, not a new
  computation.
- Every spawn surface calls `fire_terminal_callback()` from its own teardown path:
  `_teardown_common()` in `cli/_runs.py` for `li agent`/`li play`/`li o fanout` (right
  after the `db.update_status("session", ...)` call, replacing the ad-hoc call site
  `_run_flow` currently owns exclusively); `flow.py`'s existing call site for `li o
  flow` (replacing `fire_terminal_notify()` directly); and
  `SchedulerEngine`/`worker.py`'s terminal transitions for `schedule_run` (a new call
  site — today nothing fires there).
- `li o flow --notify <argv-string>` remains as a per-invocation override
  (`config.state = "configured"` with that argv, overriding whatever `settings.yaml`
  resolves to) — same precedence role it plays today, generalized to every surface via
  an equivalent `--on-terminal` flag on `li agent`/`li play`/`li o fanout`, and an
  `on_terminal_override` column on `schedules` for the scheduled case.

**Why this way.** `_notify.py` already solved the "never let a hook affect the run's own
status" and "never let shell metacharacters in payload become shell syntax" problems (via
its env-var-reference substitution trick) — this ADR keeps that posture and widens the
call sites rather than rewriting the safety model from scratch. The prior-art review's
D-decision here (an executable argv template with a versioned JSON payload) matches what
`_notify.py` already does structurally; the gap is durability (D3) and coverage (this
D1), not the shape of the hook itself.

### D2 — Configuration resolution: three-state, precedence, migration from the bare-string form

**The decision.** `notify.on_terminal` in `.lionagi/settings.yaml` moves from a bare
shell-command string to the `TerminalCallbackConfig` shape (D1). Resolution precedence
is **per-run/per-schedule override > project `.lionagi/settings.yaml` > global
`~/.lionagi/settings.yaml`** — collapsing the prior-art review's three-tier
`per-run > spawn-kind/project default > global` model onto lionagi's *actual* two-tier
`load_settings()` merge (`_deep_merge`, project wins over global, exactly as
`dispatch.notify_template` already resolves) plus one additional per-invocation layer
that `settings.yaml` itself does not have today.

**The contract.**

```yaml
# .lionagi/settings.yaml (project overrides global via existing deep_merge)
notify:
  on_terminal:
    state: configured            # unset | disabled | configured
    argv: ["/usr/local/bin/notify-slack", "{payload}"]
    timeout_ms: 30000
    max_output_bytes: 65536
```

```text
li agent <model> <prompt> --on-terminal '["cmd", "{payload}"]'   # per-run override
li o flow <playbook> --notify '<argv-json>'                      # existing flag, new shape
```

```sql
-- schedules table gains one nullable column (Phase 1 migration)
ALTER TABLE schedules ADD COLUMN on_terminal_override JSON;
```

**Exact semantics.**

- `state: unset` (or the key absent) inherits from the next layer down, exactly like
  `dispatch.notify_template`'s current `None`-means-absent behavior.
- `state: disabled` stops inheritance — a project can explicitly turn off a global
  default. This is the exact gap the prior-art review's REFUTED row "empty string means both
  unset and disabled" calls out, and the one lionagi's current `notify.on_terminal:
  <bare string or absent>` shape cannot express: there is today no way to say "I know
  global has a hook configured, and I am deliberately turning it off for this project."
- `state: configured` requires `argv` to be a non-empty list of strings. A configured-
  but-malformed value (missing `argv`, wrong types) is a **logged validation failure at
  resolution time**, treated as `disabled` for that resolution (never silently falls
  back to a lower layer that the caller did not ask for) — matching `_notify.py`'s
  existing "malformed settings must never affect the run" posture.
- **Migration**: an existing bare-string `notify.on_terminal: "some-cmd {payload}"`
  value is accepted for one deprecation window, auto-wrapped as `{state: "configured",
  argv: ["/bin/sh", "-c", <string>]}` with a one-time `warn()` on first resolution per
  process. This is a deliberate, temporary exception to D7's "no shell" anti-pattern —
  every current production `.lionagi/settings.yaml` in this shape depends on shell
  substitution (`{payload}` inside a larger shell pipeline is a realistic existing use),
  and breaking it silently on this ADR's rollout is worse than a bounded compatibility
  shim. The shim is removed on a separately tracked deprecation issue, not by this ADR.
- The resolved config is **snapshotted at run/schedule-fire creation time**, not re-read
  at teardown — matching the prior-art review's "effective configuration is immutable for that
  physical run" requirement and `sessions.artifact_contract_json`'s existing
  frozen-at-creation pattern (ADR-0029). Snapshot storage: a new
  `sessions.on_terminal_config_json` column (nullable JSON), written once in
  `setup_agent_persist()`/`create_session()`, alongside the existing
  `artifact_contract_json` write.

**Why this way.** Adding a third, purely additive settings tier (a global "org default")
was considered and rejected: lionagi has exactly two config surfaces today (global
`~/.lionagi/settings.yaml`, project `.lionagi/settings.yaml`), and `dispatch_outbox`'s
own `notify_template` already treats that as sufficient. Inventing a third tier this ADR
would be the only consumer of is new surface with no second consumer; the
CLI/schedule-column override already gives the review's top precedence layer without
adding settings-file structure.

### D3 — Atomic terminal transaction, reusing `dispatch_outbox` — no second outbox

**The decision.** The callback is enqueued as a `dispatch_outbox` row with
`kind='terminal_notify'` (the kind the schema comment already anticipates), in the
**same transaction** as the terminal status write, once ADR-0058's `LifecycleService`
ships. Until then, an interim two-write sequence with an explicit crash-window caveat is
the honest description of what Phase 1 can deliver on today's `StateDB.update_status()`.

**The contract.**

```python
# Target (post ADR-0058): inside LifecycleService.transition()'s guarded UPDATE (D4, step 10-12)
TransitionCommand(
    entity_type="session",
    entity_id=session_id,
    to_status=final_status,
    reason=ReasonRecord(code=final_reason_code, summary=final_reason_summary),
    actor=ActorRecord(type="executor", id=session_id),
    patch={"ended_at": ended_at, ...},
    # NEW field this ADR proposes adding to TransitionCommand:
    side_effect_dispatch=DispatchEnqueue(
        kind="terminal_notify",
        deliver_to=config.argv[0],
        body=payload,
        dedup_key=event_id,          # D4
        session_id=session_id,
    ) if config.state == "configured" else None,
)
```

```python
# Interim (Phase 1a, before ADR-0058 ships): _teardown_common() in cli/_runs.py,
# immediately after the existing db.update_status("session", ...) call succeeds.
if written and on_terminal_config.state == "configured":
    await enqueue_dispatch(
        db, kind="terminal_notify", deliver_to=on_terminal_config.argv[0],
        body=payload, dedup_key=event_id, session_id=session_id,
    )
```

**Exact semantics.**

- **This ADR requires an ADR-0058 extension that ADR-0058 does not currently specify**:
  `TransitionCommand` (ADR-0058 D1) has no side-effect/outbox-enqueue field, and D4's
  guarded algorithm (steps 10-12: UPDATE entity, INSERT `status_transitions`, COMMIT)
  has no step for a same-transaction outbox insert. ADR-0059 D1's own dispatch-row
  insert is already atomic with *its own* initial transition (`enqueue_dispatch()`'s
  existing two-INSERT transaction), but that is a different atomicity: a
  **caller-supplied** dispatch row being inserted atomically with a **different**
  entity's status transition is new. This is an extension ADR-0058 must accept (recorded
  as delta row 4 below) before Phase 1's "same transaction" claim can be literally true.
- **Interim posture (Phase 1a, ships now)**: two sequential writes — the existing
  `db.update_status()` call, then `enqueue_dispatch()` — inside the caller's own
  `try/except`, with the enqueue attempted **only if the status write itself
  succeeded**. This has an honest, bounded crash window (a crash between the two writes
  loses the notification, exactly the "Fire-and-forget callback after status commit"
  anti-pattern D7 names) — but it is strictly better than today's `fire_terminal_notify()`,
  which has both that window **and** no durable record of the attempt at all once the
  process exits. Phase 1a is explicitly a stepping stone, not the target; Phase 1b (the
  same-transaction form) is gated on the ADR-0058 extension landing.
- `dedup_key=event_id` (D4) means a retried/duplicate call to
  `fire_terminal_callback()` for the same terminal outcome (e.g. the `defer_terminal`
  auto-resume handoff in `_teardown_common` calling teardown twice for logically related
  legs) returns the existing dispatch row rather than double-enqueuing, reusing
  `enqueue_dispatch()`'s existing dedup-key behavior (ADR-0059 D2) unchanged.
- Delivery, retry, backoff, dead-letter, and the scheduler-tick scan are **entirely
  unchanged** from ADR-0059 D3/D4 — this ADR adds one new `kind` value and one new
  producer call site; it does not touch `deliver_due_dispatches()`.

**Why this way.** A second, purpose-built "callback outbox" table was considered and
rejected for the same reason ADR-0059's own "separate dispatch database" alternative was
rejected: `dispatch_outbox` already has the exact shape (durable row, claim CAS, lease,
retry, dead-letter, `session_id`/`schedule_run_id` provenance) this callback needs, and a
second outbox would only duplicate that machinery while adding a second place operators
must check (`li dispatch ls` already exists; a second CLI verb would fragment it).

**Conflict this decision records against ADR-0058.** `TransitionCommand` (ADR-0058 D1)
has no side-effect field today, and D4's guarded algorithm has no same-transaction
outbox-insert step. Delta row 4 proposes that extension to ADR-0058; until it lands,
Phase 1a's two-write form is the shipping behavior.

### D4 — Degenerate callback identity: no generation, one terminal event per session

**The decision.** `event_id = f"{session_id}:{terminal_status}"`. lionagi has no
`root_run_id`/`generation` pair today (P6), so the prior-art review's recommended
`run_id:generation:terminal_status` form is not literally constructible — but lionagi's
**existing** terminal-status invariant (ADR-0035/v0-ADR-0094's "terminal is terminal"
floor, enforced by `StateDB.update_status()`'s CAS-plus-terminal-rejection: a session's
status becomes one of the six terminal values **at most once**, ordinary writes past
that point raise `TransitionRejectedError`) already gives `(session_id, terminal_status)`
the exact uniqueness property the prior-art review's fuller identity exists to construct.

**Exact semantics.**

- A session can only ever reach terminal status through the guarded `update_status()`
  path (or an explicit `override=True` operational repair, which is itself audited in
  `admin_events` separately and is not an ordinary terminal transition). So under
  ordinary operation there is exactly one `(session_id, terminal_status)` pair per
  session, ever — the degenerate `event_id` is already collision-free without a
  generation counter.
- The one case this degenerate form does **not** cover: an `override=True` repair that
  moves a session from one terminal status to a *different* terminal status (a rare,
  audited operator action). That produces a second, legitimately different `event_id`
  (different `terminal_status` string) — which is the correct behavior: the operator
  changed the outcome, a second, distinguishable notification is exactly what should
  fire.
- **Upgrade path (Phase 3, deferred)**: when `root_run_id`/`generation` land,
  `event_id` becomes `f"{root_run_id}:{generation}:{terminal_status}"`, and the
  degenerate single-segment form becomes the `generation=0, root_run_id=session_id`
  special case of the general one — no field rename, no payload-shape break, because
  `run_id` in `TerminalPayload` (D1) is already `sessions.id` under both forms.

**Why this way.** Adopting the prior-art review's full `run_id:generation:terminal_status`
form today would require either fabricating a `generation` value with no backing
concept (dishonest) or blocking this entire ADR on Phase 3 landing first (unnecessary —
Phase 1/2 do not need lineage). The degenerate form is not a compromise on correctness;
it is the literal correct answer for a system where every terminal transition is already
provably unique per entity.

### D5 — Two-stage failure detector: `running -> supervisor_unknown -> orphaned`

**The decision.** `schedule_runs` gains one new status, `supervisor_unknown`, inserted
between the existing lease-expiry detection and the existing recovery/failure outcomes.
`worker.py`'s reaper pass (currently: expired lease → straight to `queued` or `failed`)
is restructured into two passes separated by a configurable grace window.

**The contract.**

```text
Current (worker.py, unchanged code paths get renamed, not rewritten from scratch):
  running --(lease_expires_at < now, lease_attempts < MAX)--> queued            [reaper pass, today]
  running --(lease_expires_at < now, lease_attempts >= MAX)--> failed           [reaper pass, today]

Target (this ADR, Phase 2):
  running            --(lease_expires_at < now)--> supervisor_unknown           [NOT terminal, NOT re-queued]
  supervisor_unknown --(same leased_by renews before grace elapses)--> running  [recovery, no new claimant admitted]
  supervisor_unknown --(grace elapses, fence CAS commits)--> orphaned           [terminal for THIS row]
  orphaned or (failed AND lease_attempts < MAX)
                     --(new worker claim)--> a NEW schedule_runs row, chain_parent_id = old row.id
```

```sql
-- schedule_runs gains two columns (Phase 2 migration)
ALTER TABLE schedule_runs ADD COLUMN unknown_since REAL;       -- epoch seconds, set on running -> supervisor_unknown
ALTER TABLE schedule_runs ADD COLUMN process_identity JSON;    -- {pid, pid_create_time} — D6
```

```python
# lionagi/state/reasons.py — new reason codes under an extended RunReasons
SUPERVISOR_UNKNOWN_LEASE_STALE = "run.supervisor_unknown.lease_stale"
ORPHANED_FENCE_COMMITTED = "run.orphaned.fence_committed"
RECOVERED_SAME_HOLDER = "run.running.recovered_same_holder"

_SUPERVISOR_UNKNOWN_GRACE_SECONDS = 20.0  # additive to the existing lease TTL (300s default)
```

**Exact semantics.**

- `supervisor_unknown` is **not terminal** (`SCHEDULE_RUN_TERMINAL_STATUSES` in
  `state/db.py` is unchanged) and is **not** `queued` — no new claimant may pick up this
  logical unit of work while a row sits in `supervisor_unknown`. This is the one-line
  change that closes P4: today `queued` reappearing is exactly the signal a second
  worker uses to claim, with nothing preventing the first (possibly-still-alive) worker
  from finishing its write after that.
- Recovery (`supervisor_unknown -> running`) is permitted **only** for the *same*
  `leased_by` value renewing its own lease — a different worker cannot claim a
  `supervisor_unknown` row directly; it must wait for the fence.
- The fence transition (`supervisor_unknown -> orphaned`) is the row this ADR's Phase 2
  actually adds work for: it is a guarded CAS (`transitions.transition()`, extending the
  existing `_TRANSITION_VOCAB["schedule_run"]` table in `state/transitions.py` with
  `"supervisor_unknown": frozenset({"running", "orphaned"})`) that, in the same
  transaction, clears `leased_by`/`lease_expires_at` **and** stamps `orphaned`. Nothing
  in this ADR requires a separate `run_lineages` table (the prior-art review's generation
  registry) for Phase 2 — a `schedule_runs` row has no logical successor identity to
  fence *against* until Phase 3's `root_run_id` exists, so Phase 2's fence is simpler
  than the prior-art review's general form: it only needs to guarantee the row itself cannot
  silently resurrect, which the existing terminal-status floor (ADR-0035) already
  provides once `orphaned` is added to `SCHEDULE_RUN_TERMINAL_STATUSES`.
- An `orphaned` row is eligible for the **existing** `max_runs`/`chain_parent_id`
  recovery path unchanged: a subsequent schedule fire (or an explicit `li dispatch
  retry`-equivalent for schedule runs, not designed here) creates a new
  `schedule_runs` row. This ADR does not add a resume/restart API distinct from what
  already exists (`chain_parent_id`) — that generalization is Phase 3.
- `MAX_LEASE_ATTEMPTS=3` stays the bound on `supervisor_unknown -> orphaned` cycles
  before the row instead goes straight to `failed` with
  `FAILED_LEASE_ATTEMPTS_EXHAUSTED` (unchanged reason code, now reached via one more hop
  through `supervisor_unknown` first rather than directly from `running`).
- `_SUPERVISOR_UNKNOWN_GRACE_SECONDS = 20.0` is the prior-art review's own planning value,
  kept as the starting default because lionagi has no measured false-orphan-rate data
  yet (this ADR's own D8 rollout gates require gathering that data before the default is
  treated as tuned, not assumed).

**Why this way.** The current single-stage reaper is not a bug in the sense of "wrong
code" — it does exactly what `worker.py`'s own docstring says it does. It is a **design
gap**: a lease timeout is evidence of staleness, not proof of death (the prior-art review's
V1/V18, CONFIRMED-BY-ANALYSIS 0.94/0.98, REFUTED respectively), and `queued` reappearing
today is observably the same signal a legitimate recovery and an unsafe double-claim both
produce. Two stages closes that ambiguity with the smallest change that preserves every
existing column and every existing reason code lionagi already has.

### D6 — Process identity beyond bare PID: reuse `current_pid_markers()`

**The decision.** Every locally-spawned attempt this ADR tracks (a `schedule_runs` row
executing via `spawn_and_wait()` in `studio/scheduler/subprocess.py`, or a `sessions`
row for `li agent`/`li play`) stores `{pid, pid_create_time}` — the exact shape
`current_pid_markers()` in `cli/kill.py` already computes for `li kill`'s CWE-362
defense — as `schedule_runs.process_identity` / a new `sessions` column of the same
shape, rather than a bare PID.

**The contract.**

```python
# Reused unchanged from lionagi/cli/kill.py — no new identity primitive invented.
def current_pid_markers() -> dict[str, Any]:
    return {"pid": os.getpid(), "pid_create_time": psutil.Process(os.getpid()).create_time()}
```

**Exact semantics.**

- `sessions.node_metadata` already carries `current_pid_markers()`'s output today (set in
  `setup_agent_persist()` via `{**session_dict.get("node_metadata"), **current_pid_markers()}`)
  — this ADR does not change that write; it adds the equivalent write for
  `schedule_runs.process_identity`, which does not exist today (the scheduler daemon
  spawns via `spawn_and_wait()`, which returns `(exit_code, stderr_tail)` and never
  records the child's PID/start-time anywhere durable).
- Any future orphan-sweep code (this ADR does not add a *new* sweeper — Phase 2's fence
  is lease-driven, not PID-driven) that wants to confirm process liveness before fencing
  MUST use `_cmdline_is_lionagi()`'s existing exact-match verification pattern from
  `cli/kill.py`, not a bare `kill(pid, 0)`/`/proc/<pid>` existence check — this is the
  literal anti-pattern D7 names first, and lionagi already has the correct pattern built
  for a different caller.
- `pid_create_time` alone (without a matching cmdline/cgroup check) is not treated as
  sufficient proof of *this specific* process on non-Linux platforms where
  `pidfd_open()`-equivalent guarantees do not exist — `_CREATE_TIME_TOLERANCE = 0.1`
  (an existing constant in `cli/kill.py`) already encodes the platform-specific fuzz this
  identity check needs; reused, not reinvented.

**Why this way.** The prior-art review's V3 (pidfd/birth-identity requirement,
CONFIRMED-BY-ANALYSIS 0.98) is already satisfied by code lionagi shipped for a different
reason. Building a second identity primitive for supervision would duplicate existing machinery
for no benefit — the CWE-362 threat model `li kill`
defends against (PID reuse racing a kill signal) is the same threat model an orphan
fence must defend against (PID reuse racing a "is the old holder still alive" check).

### D7 — Anti-patterns this design rules out

Each row below is the prior-art review's REFUTED-table finding, mapped to the specific lionagi
code this ADR either already avoids or must change to avoid:

| Anti-pattern | Where lionagi already avoids it | Where lionagi must change |
|---|---|---|
| Bare-PID sweep (`kill(pid, 0)`) | `cli/kill.py`'s `current_pid_markers()` + `_cmdline_is_lionagi()` — already correct | — |
| First missed heartbeat auto-requeues | — | **`worker.py`'s reaper today** (D5): lease expiry goes straight to `queued`. This is the one REFUTED pattern presently live in shipped code, not just a risk to avoid. |
| Fire-and-forget callback after status commit | — | **`fire_terminal_notify()` today** (D1/D3): fires after commit, non-durable. The second REFUTED pattern presently live in shipped code. |
| Callback exit code changes source terminal status | `_notify.py`'s existing "a hook failure must never affect the run" posture — already correct, kept unchanged (D1) | — |
| Shell-expand caller metadata into the command line | `_notify.py`'s env-var-reference substitution trick — already correct for the *values*; the **command itself** still runs via `create_subprocess_shell` (shell=True). D2's migration path documents this as a bounded, deprecated exception, not a silent gap. | Target `argv`-exec form (D1) drops `shell=True` entirely once the bare-string migration window closes. |
| Empty string means both unset and disabled | `dispatch.notify_template`'s existing `None`-or-absent-means-unset — already fine for that key | **`notify.on_terminal`'s current bare-string shape** cannot express "disabled" distinctly from "unset" — D2 fixes this. |
| Claim "exactly once" for an arbitrary command | ADR-0059 D3/D4 already documents at-least-once only, honestly | — |
| Mark orphaned, then later increment generation | Not applicable to Phase 2 (no generation exists yet, D5) — the fence and the terminal write are one transaction by construction, so there is no window to get this ordering wrong even without a generation counter | Applies to Phase 3 (deferred) if/when generations land — must be enforced there. |

### D8 — Rollout gates, adapted to lionagi's actual scale

The prior-art review's eight rollout gates are adopted with lionagi-specific acceptance
criteria; its quantitative load model (10,000 concurrent runs, 1,000 heartbeat
writes/sec) is **explicitly not applicable** — lionagi's default deployment is a single
Studio daemon against a single SQLite `state.db` file, and this ADR does not decide a
Postgres-scale target.

1. **Fencing gate**: an integration test that starts a fake `schedule_run` worker, lets
   its lease lapse mid-"execution" (a `time.sleep` past `lease_expires_at`), starts a
   second worker, and asserts the first worker's late `transition()` call is rejected
   (conflict, not applied) after the second worker's claim commits.
2. **Race gate**: N concurrent workers (N=8 is a reasonable local-CI bound, not the
   prior-art review's 64 — lionagi's default single-daemon deployment does not have 64
   concurrent claimants realistically) racing the same `supervisor_unknown -> orphaned`
   fence; assert exactly one commits.
3. **Crash-atomicity gate**: crash-injection around the Phase 1a two-write sequence
   (D3) proving the documented crash window is bounded to exactly "status committed,
   dispatch not enqueued" and never the reverse or a partial write of either.
4. **False-suspicion gate**: `MAX_LEASE_ATTEMPTS`/`_SUPERVISOR_UNKNOWN_GRACE_SECONDS`
   are validated against measured GC-pause/disk-latency tails from lionagi's own test
   suite runs (or Studio daemon production logs, once available) before being called
   tuned rather than borrowed defaults.
5. **Load gate**: SQLite-specific — WAL checkpoint latency and `_write_lock`
   contention (the existing `StateDB._write_lock` serializing all SQLite writes) under
   2x the daemon's actual observed concurrent-schedule-run count, not a synthetic 10k.
6. **Security gate**: callback argv fuzzing (D1) plus a targeted test that the bare-
   string shell-migration shim (D2) cannot be made to execute attacker-controlled
   arguments via a crafted `{payload}` substitution — the existing env-var-reference
   trick from `_notify.py` is the control under test, reused not re-derived.
7. **Checkpoint gate**: not applicable to Phase 1/2 (no checkpoint/resume exists yet);
   deferred to Phase 3.
8. **Operational gate**: `li dispatch ls --status pending` (existing CLI, ADR-0059 D6)
   already exposes callback backlog/dead-letters; this ADR adds the equivalent
   `supervisor_unknown` count and age to whatever operational surface Studio already
   exposes for `schedule_runs` (not designed here — a Studio UI change is out of this
   ADR's scope).

## Consequences

- Every spawn surface gets one notification contract instead of one working (`li o
  flow`) and four missing ones. A contributor adding a new spawn surface must now wire
  `fire_terminal_callback()` at its teardown point, the same way it must already wire
  `_teardown_common()`/`update_status()`.
- The `schedule_runs` status vocabulary grows by one value
  (`supervisor_unknown`) and `worker.py`'s reaper gains one more hop before reaching
  `queued`/`failed` — every place that pattern-matches `schedule_runs.status` against
  the terminal/active sets (`PLAY_ACTIVE_STATUSES`-equivalent for schedule runs,
  `SCHEDULE_RUN_TERMINAL_STATUSES`) must be checked for an implicit assumption that
  `running` is the only non-terminal, leased state.
- The Phase 1a interim (two sequential writes, not one transaction) is an honestly
  documented, temporary crash window — this ADR does not claim atomicity it cannot yet
  deliver, and ships D3's better-than-`fire_terminal_notify()` posture immediately
  without waiting on ADR-0058.
- Reversing D5 (removing `supervisor_unknown`) after schedule-run consumers depend on it
  is a schema/vocabulary change with the same cost profile as any other
  `VALID_STATUSES_BY_ENTITY_TYPE` change (ADR-0058 D2's registration-time validation
  already treats this as expensive by design).
- This ADR deliberately does not give lionagi exactly-once delivery, a generation/lineage
  primitive, or a resume/restart API distinct from the existing `chain_parent_id` field —
  callers that need those wait on Phase 3.

## Current-vs-ideal delta

| # | Delta | Size | Impact class | Issue |
|---|-------|------|---------------|-------|
| 1 | Generalize `fire_terminal_notify()` into `fire_terminal_callback()` (D1) and wire it into `_teardown_common()` (`li agent`/`li play`/`li o fanout`) and the scheduler's terminal transitions (`schedule_run`); acceptance requires all five spawn kinds firing the hook with the same payload shape. | M | behavior-visible | (filled at issue-open time) |
| 2 | Migrate `notify.on_terminal` from a bare shell string to the three-state `TerminalCallbackConfig` (D2), with the bounded shell-shim compatibility path; acceptance requires existing `.lionagi/settings.yaml` files with the old shape to keep firing unchanged, plus a `disabled` state that is newly expressible. | S | behavior-visible (config schema) | (filled at issue-open time) |
| 3 | Route the terminal callback through `dispatch_outbox` (`kind='terminal_notify'`, D3 Phase 1a); acceptance requires `li dispatch ls` to show terminal-notify rows and survive a Studio daemon restart mid-retry. | S | behavior-visible | (filled at issue-open time) |
| 4 | Propose and land the ADR-0058 `TransitionCommand.side_effect_dispatch` extension (D3 Phase 1b) so the callback enqueue is genuinely same-transaction with the status write; acceptance requires a crash-injection test proving no window exists between the two. This delta is blocked on ADR-0058's owner accepting the extension — see Conflicts. | M | internal-only (once landed, closes the Phase 1a crash window) | (filled at issue-open time) |
| 5 | Add `supervisor_unknown` to the `schedule_run` status vocabulary and restructure `worker.py`'s reaper into two passes (D5); acceptance requires the fencing and race rollout gates (D8 #1-#2) passing. | M | behavior-visible | (filled at issue-open time) |
| 6 | Add `schedule_runs.process_identity` and `sessions`'s equivalent, populated from the existing `current_pid_markers()` (D6); acceptance requires no new identity-computation code, only new call sites and columns. | S | internal-only | (filled at issue-open time) |

## Alternatives considered

- **A second, purpose-built callback outbox table**, separate from `dispatch_outbox`.
  Would avoid touching ADR-0059's schema/CHECK constraints. Lost because
  `dispatch_outbox` already has every mechanic this callback needs (claim CAS, lease,
  backoff, dead-letter, provenance columns), and ADR-0059's own "separate dispatch
  database" alternative was rejected for the identical reason — this ADR does not
  reopen a decision ADR-0059 already made.

- **Adopt the prior-art review's full three-tier settings precedence
  (`per-run > spawn-kind/project > global`) verbatim**, adding a new
  "spawn-kind default" settings layer. Lost because lionagi's `settings.yaml` has never
  had a spawn-kind-scoped layer for anything (not even `dispatch.notify_template`), and
  inventing one for this single feature would be new surface with no other consumer —
  the two-tier-plus-override model (D2) delivers the same three effective precedence
  levels (explicit override, project, global) without a schema lionagi would only use
  once.

- **Fabricate a `generation=0` value now** rather than the degenerate `event_id` form
  (D4). Would make the payload shape match Phase 3 from day one. Lost because a
  `generation` field with no corresponding `run_lineages` table, no CAS that increments
  it, and no consumer that checks it is a field that lies about having a guarantee it
  does not have — the degenerate form is honest about what lionagi's data model actually
  supports today.

- **Wait for ADR-0058 to ship before proposing any of this** (i.e., make D3's Phase 1b
  same-transaction form the only phase, with no Phase 1a). Lost because ADR-0058 is
  itself Proposed/Aspirational with a five-phase migration of its own (D5's phase gates);
  blocking every spawn surface's notification coverage and the `dispatch_outbox` wiring
  on that landing first would leave P1-P3 unaddressed for an unbounded time. Phase 1a's
  bounded, documented crash window is a better interim than doing nothing.

- **Apply the prior-art review's 10,000-run planning model and rollout-gate numbers
  unmodified.** Lost because lionagi's default deployment (single Studio daemon, single
  SQLite file, `StateDB._write_lock` serializing all writes) is not that system — using
  those numbers as gates would either be trivially passed (meaningless) or block
  shipping on infrastructure lionagi does not have and this ADR does not propose
  building (a distributed StateDB, a sharded outbox). D8 substitutes locally-meaningful
  gates instead.

- **Treat the existing session health sweeps as already covering D5, and design nothing
  new.** The Studio phantom reaper and doctor sweep do write guarded transitions with
  the `SessionReasons` health vocabulary — but they cover `sessions` only, are
  single-stage, and are gated on multi-hour staleness thresholds. The schedule-run
  reaper's minutes-scale lease expiry with immediate requeue is a categorically sharper
  double-claim hazard; extending two-stage semantics to `sessions` is deferred rather
  than bundled here.

## Notes

**In-code ADR-number drift (found while researching this ADR, not this ADR's to fix)**:
several files still cite the *pre-renumbering* v0 corpus — `state/db.py`/`cli/_runs.py`
say "ADR-0094" for the terminal-status floor (now **ADR-0035**, per
`docs/adr/dispositions.yaml`); `state/reasons.py`/`state/transitions.py`/
`dispatch/outbox.py` say "ADR-0092"/"ADR-0028" for the dispatch outbox and status-reason
model (now **ADR-0059** and **ADR-0057** respectively); `worker.py`'s docstring area
references "ADR-0101" for the task queue (now **ADR-0071**/**ADR-0072**); and
`state/transitions.py`'s module docstring says "ADR-0062's `transition()` API" when the
actual target unified-transition-service ADR is **ADR-0058** (the current ADR-0062 is
"CLI command surface ownership", unrelated). This draft cites the new numbers throughout
and does not repeat the stale ones as if current — but the source comments themselves
are stale and worth a follow-up doc-hygiene pass, separate from this ADR's content.
