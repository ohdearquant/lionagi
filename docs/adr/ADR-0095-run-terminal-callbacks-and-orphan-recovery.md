# ADR-0095: Run-terminal callbacks and orphan recovery

- **Status**: Proposed
- **Kind**: Aspirational
- **Area**: scheduling-control-plane
- **Date**: 2026-07-12
- **Relations**: supersedes ADR-0060 (run supervision — its outbox-coupled callback and
  two-stage orphan design are replaced by D1/D3/D4 here); builds on ADR-0057 (lifecycle
  transitions), ADR-0035 (terminal-status integrity floor), ADR-0059 (dispatch outbox),
  ADR-0064 (run outcome records), ADR-0071 (task-worker leases)

## Context

LionAGI has one authoritative state boundary: a successful guarded lifecycle transition
writes the entity status and its `status_transitions` audit row in a single transaction,
under compare-and-set guards, the terminal floor, controlled reason vocabularies, and audit
history. That boundary is the only place that observes terminal outcomes from sessions,
invocations, plays, and schedule runs without wiring every command teardown independently.

It is not a safe place to perform callback I/O. The transaction is open inside the lifecycle
service until the context exits; a shell command, network call, or user callback there would
lengthen the lock, couple state integrity to external code, and make callback failure
ambiguous with transition failure.

The existing seams are incomplete. `SESSION_END` is emitted only by shared session teardown;
scheduler bookkeeping, invocation-only launches, task-worker rows, and direct engine-run
bookkeeping do not all pass through it. Flow/play's `--notify` is flow-only, shell-specific,
and builds its own payload. The dispatch outbox has strong delivery machinery, but its
delivery loop is a Studio scheduler tick and its notify template is already a concrete
transport; making it the callback seam would make plain CLI behavior depend on an optional
daemon and put fleet transport policy inside LionAGI.

There is a related liveness gap. CLI sessions record PID and process-creation time; Studio
launches, scheduler fire rows, and engine-run rows do not consistently record an executor
identity. Each existing reaper implements a partial predicate. Resume is equally non-uniform:
flows have checkpoints, agents have branch continuation, task workers have lease requeue, and
the other surfaces have no replay state.

Terms used below:

- A **terminal event** is the immutable fact represented by one applied
  nonterminal-to-terminal lifecycle transition and its audit-row ID.
- A **callback** is a best-effort push of that fact to a registered in-process handler after
  the transaction commits.
- An **orphan** is a persisted nonterminal execution whose executor identity is positively
  dead, or whose ownership marker never appeared and whose startup/activity grace has
  expired. Unknown liveness is not positive evidence of death.
- A **resume** creates or claims a new attempt. It never reopens a terminal row.

## Decision

### D1 — One post-commit terminal-callback registry on the lifecycle boundary

A process-wide `TerminalCallbackRegistry` is injected into the lifecycle service. The service
constructs a terminal event only when all of the following hold: the transition outcome is
`applied`; `previous_status != current_status` (a same-status reason append is not a new
terminal event); the destination is terminal under the registered lifecycle policy; and the
entity is an execution entity (`session`, `invocation`, `schedule_run`, or `play`).

The audit append stays inside the guarded transaction. Registry emission occurs only after
the transaction has exited successfully, using the committed `status_transitions.id` as
`event_id`. The registry never runs handler code while the transaction is open.

Handlers are process-local, named, idempotently registered, and may filter by entity kind
and/or entity ID. They are invoked concurrently from an immutable envelope under one total
ten-second deadline. Handler exceptions, cancellation, timeout, and nonzero adapter exit are
logged and swallowed. Awaiting the registry may delay the transition caller's return by at
most that budget; it cannot delay, roll back, overwrite, or recategorize the terminal write.

The push contract is **best effort**. LionAGI claims neither exactly-once nor at-least-once
callback delivery. The committed transition audit is the durable reconciliation source, and
its consumption contract is **set-based, not cursor-based**. A positional cursor over
`(created_at, id)` cannot be made safe here: the transition timestamp is captured before the
transaction begins while commit happens at context exit, so a slow transaction can commit a
row whose `created_at` sorts *behind* a cursor another consumer already acknowledged, and the
audit ID is a random UUID with no commit ordering. A strict greater-than cursor would skip
that row permanently — losing exactly the commit-then-crash window reconciliation exists to
cover.

Instead, delivery acknowledgment is durable state: a `terminal_deliveries` table with
composite primary key `(transition_id, consumer)` records that a named reconciliation
consumer has processed a terminal event, written by the consumer (never by the push path —
the in-process push stays fire-and-forget and records nothing). Acknowledgment writes are
idempotent by construction: `INSERT ... ON CONFLICT DO NOTHING` on the composite key, so
concurrent or repeated acks of the same event by the same consumer are single-row no-ops.
The reconciliation query is a read-only anti-join: terminal transitions on execution
entities with no delivery row for the requesting consumer.

Retention never expires an unacknowledged event out of an active consumer's reconciliation
set: an event remains in that set until the consumer acknowledges it, however old it gets —
a consumer offline longer than any horizon still recovers every missed terminal event on
return. What expires is the other side: delivery rows older than the retention horizon
(default ninety days) may be pruned once acknowledged. Consumer registrations end only by
explicit retirement — a recorded action taken by the registration's owner or a deployment
operator, never a side effect of inactivity. A registered consumer that merely stops
querying remains active and its unacknowledged set is retained indefinitely, regardless of
how far past any horizon its silence extends. Releasing a retired consumer's outstanding
unacked set happens atomically with the retirement itself (one transaction), so there is no
window in which the registration is gone while its set is still owed, or retained while
unowned. Registering as a reconciliation consumer is what creates the retention obligation;
an anonymous ad-hoc query gets the plain audit history with no completeness guarantee.

Because membership in the unacknowledged set does not depend on any ordering, a
late-committing older row is simply still in the set the next time the consumer queries.
Consumers deduplicate on `event_id` (push and reconciliation can both deliver the same
event; the push-then-crash-before-ack case redelivers). The audit row itself is never
mutated; acknowledgment lives entirely in the deliveries table, preserving audit
immutability.

`SESSION_END` remains supported but is not bridged into the registry; a bridge would create a
loop and a second authority. Persistent direct engine runs are covered through their
signal-session terminal transition. The `--no-persist` engine case is the sole explicit
adapter: it may publish an envelope marked `durable=false` with no reconciliation guarantee.
No other surface gets a teardown-time callback call.

### D2 — A minimal versioned envelope owned by LionAGI; transport policy stays outside

LionAGI guarantees a small, transport-neutral envelope:

```json
{
  "schema": "lionagi.run-terminal",
  "schema_version": 1,
  "event_id": "<status_transitions.id or synthetic id>",
  "durable": true,
  "entity": {"kind": "session|invocation|schedule_run|play|ephemeral_run", "id": "..."},
  "previous_status": "running",
  "terminal_status": "completed|failed|timed_out|aborted|cancelled|...",
  "reason_code": "run.failed.orphaned_parent",
  "occurred_at": 0.0,
  "correlation": {
    "invocation_id": null,
    "session_id": null,
    "schedule_run_id": null,
    "run_id": null
  },
  "artifacts": []
}
```

Guaranteed semantic fields: schema/version, event ID, durability, entity kind/ID, previous
and terminal status, canonical reason code, and transition time. Correlation keys are stable
but nullable. `artifacts` is always a list and may be empty; known entries are
`{"kind": "run_dir|artifact_root|checkpoint", "location": "..."}`. LionAGI performs no
filesystem discovery or surface-specific joins inside the transaction to populate optional
fields. Concrete notification payloads and transports (mail, chat, fleet inboxes) belong to
the external run wrapper, not to this envelope.

**Version evolution rules.** Within `schema_version: 1`, the guaranteed fields' names,
types, semantics, and requiredness are immutable. New *optional* fields may be added without
a version bump; consumers MUST ignore fields they do not recognize. Any removal, type
change, semantic change, requiredness change, or change to the correlation-key set requires
incrementing `schema_version`. A consumer receiving a version it does not support MUST NOT
process the envelope as if it were v1; it logs and drops (or dead-letters) it, and can always
fall back to the reconciliation query, whose row shape is governed by the schema, not the
envelope.

### D3 — Direct in-process delivery; two bootstrap points; `--notify` becomes scoped sugar

The core mechanism is a direct in-process handler call through the registry, post-commit and
bounded per D1. A handler is a Python callable; a process bootstrap may also install an
external-executable adapter, where LionAGI passes the v1 JSON envelope and interprets only
process launch, timeout, and exit code, while the executable owns the transport.

There are two bootstrap points rather than N command flags: common CLI startup and Studio
service startup. Both resolve the handler configuration from settings, so an external
adapter is installable from user configuration alone:

- **Settings key**: `notify.on_terminal` (the key the current flow-only prototype already
  resolves). Accepted shapes: a string (compatibility form) or a mapping `{enabled: bool,
  adapter: {kind: "exec", argv: [...]} | {kind: "python", ref: "module:callable"},
  filter: {kinds: [...], ids: [...]}}`. The absent key and `enabled: false` are both the
  explicit disabled state; the default is disabled. The string form is converted to an
  argv array by POSIX word-splitting (`shlex.split`) with no shell interpretation of any
  kind; a string that fails to split, or whose intent requires shell features (pipes,
  redirection, `&&`, variable expansion), warns with a migration diagnostic naming the
  argv form and resolves to disabled. A resolution producing an empty argv — an empty or
  whitespace-only string, an explicit empty `argv` array in the mapping form, or the same
  through a per-run override or `--notify` — is invalid by the same rule: it logs the same
  key-naming diagnostic and resolves to disabled before any launch is attempted. No
  configuration shape reaches a shell.
- **Precedence**: per-run override > project `.lionagi/settings.yaml` > global
  `~/.lionagi/settings.yaml` > disabled. The per-run override surface is the existing
  `--notify` flag where it exists (flow/play) and the programmatic registration API
  everywhere; an override replaces the settings-resolved handler for that run's scope only.
- **Resolution and validation**: settings are resolved once per process at bootstrap
  (snapshot semantics; a settings edit takes effect on the next process). An invalid value
  logs a warning naming the key and falls back to disabled — configuration errors never
  fail or delay a run.
- **Exec adapter safety**: `argv` is an array executed without a shell; the envelope is
  passed on stdin; LionAGI interprets only launch, the shared timeout budget, and exit code.

Programmatic users register handlers explicitly. Flow/play `--notify` remains as scoped
compatibility sugar: after the flow session ID is known, it registers the legacy-payload
exec adapter (same no-shell argv rules as every executable adapter) for the target
invocation (if present) or session, and unregisters it in a `finally` block. The current direct teardown notify call is removed to prevent double delivery. No new
`--notify`-style flag is added to agent, fanout, engine, scheduler, or Studio APIs — those
surfaces are covered by the settings-level handler.

The dispatch outbox is not used by v1: its delivery loop is daemon-tick-driven, and a
callback enqueued by a dying CLI process would deliver only when a Studio daemon runs. A
LionAGI-native reliable-delivery tier would need a daemon-independent drainer, subscriber
identity, ack state, retention, and security policy — a separate decision if ever needed.

### D4 — No `orphaned` status; canonical orphan reasons, one classifier, one coordinator

No persisted `orphaned` status is added. Canonical reasons carry the fact on the sanctioned
vocabularies: execution entities ending in `failed` use `run.failed.orphaned_parent`; plays
ending in `blocked` use `play.blocked.runner_orphaned`; task-worker lease recovery keeps
`run.queued.lease_expired` and `run.failed.lease_attempts_exhausted`; a spawn whose executor
identity was never durably acquired terminalizes after spawn grace with
`run.failed.spawn_identity_lost`. Studio and CLI project `display_status="orphaned"` (and
health `ORPHANED`) when a row is nonterminal with positive orphan evidence or terminal with a
canonical orphan reason. Downstream state machines stop waiting on `failed`/`blocked`;
operators still see "orphaned".

`display_status` for a **nonterminal** row is a live, non-replayable advisory projection and
is never a recovery source: it consumes the OS process table, marker presence, and elapsed
grace at read time, and the same stored row may legitimately project differently as those
observations change. No decision may be made from it. Recovery decisions are made only by
the coordinator through guarded transitions, and every terminalizing transition persists the
classification evidence it acted on — observed PID and create-time (or their absence),
marker state, lease state, elapsed grace against the configured threshold, and the
classifier policy version — in the transition row's evidence payload. Terminal decisions are
therefore replayable from persisted bytes even though the live advisory is not. Time-source
rules: persisted stamps use the coordinator's wall clock; grace elapse is measured against
persisted `updated_at`/activity stamps, never against a remembered in-process instant, so a
coordinator restart cannot shrink a grace window.

One pure ownership classifier and one `reconcile_orphans()` coordinator replace the current
per-reaper predicates. Existing startup and periodic Studio reaper triggers call the
coordinator; `li kill --all-stale` and monitor/status read paths reuse the classifier.
Read paths derive health without mutating; mutation happens only in the coordinator or the
manual sweep, always through guarded transitions with `expected_statuses` and, where the
schema supplies it, `expected_updated_at`.

The classifier consumes a normalized descriptor: entity kind/ID; `started_at` and
`updated_at`/last-activity; executor PID and process-creation time; expected
invocation/session marker when available; owner/lease identity and lease expiry when
applicable; linked child-session status/activity. Positive-evidence rules apply: PID reuse is
excluded by create-time comparison; unknown or inaccessible identity is never classified dead
solely because a wall-clock threshold elapsed.

Identity coverage is completed without a new PID column on every table: CLI sessions keep
writing `pid`/`pid_create_time` into `sessions.node_metadata`; `spawn_and_wait()` gains an
`on_spawn` identity callback so Studio on-demand launches and scheduler fires persist child
PID/create-time into their linked invocation's `node_metadata`; direct engine runs write
current identity into the signal session's `node_metadata`, and reconciliation folds a
terminal signal session onto the same-ID engine-run row. Task-worker executions retain lease
identity as their ownership proof.

**Spawn handshake.** Identity acquisition is a durable, ordered protocol, not a best-effort
callback:

1. *Intent*: before the child process is created, the supervisor persists a spawn-attempt
   record (attempt ID, argv summary, log path) on the linked invocation and commits it. A
   spawn with no committed intent must not proceed.
2. *Identity acquisition*: immediately after process creation returns — before any await
   point in the supervisor — the child's PID and process-creation time are written to the
   intent record and committed. This commit is the identity point.
3. *Child registration*: on surfaces whose child runs LionAGI bootstrap code, the child
   additionally writes its own marker row carrying the attempt ID, durably linking marker to
   invocation. Marker-less surfaces (bare `command` children) rely solely on step 2.

Restart behavior is defined per incomplete phase: intent committed but no identity and the
surface is marker-capable — look up the child's marker by attempt ID and adopt its identity;
intent committed, no identity, no marker — the row waits out the spawn grace and then
terminalizes with `run.failed.spawn_identity_lost`, with the intent's log path preserved for
manual audit. The residual window is the gap between process creation and the step-2 commit;
it is milliseconds wide by construction (no intervening awaits), and a child lost in it on a
marker-less surface is an accepted, documented loss: its row terminalizes honestly rather
than hanging, and the OS process, if alive, is discoverable through the logged intent. PID
reuse is excluded at every use of a recorded identity by comparing process-creation time,
both when adopting a marker and when classifying liveness.

### D5 — Orphan detection ships now; resume stays explicit and per-surface

This slice ships detection and an explicit recovery contract, not universal or automatic
resume. The status/monitor response for an orphan names one recovery capability:

| Surface | Capability | V1 action |
|---|---|---|
| Flow / play-backed flow with a valid checkpoint | `checkpoint_resume` | Existing flow resume path; new attempt linked by `resumed_from`; the failed source row is never reopened; reactive-spawned checkpoints remain refused |
| Agent with persisted branch snapshot/stream | `branch_continue` | Explicit agent resume; labeled continuation, not replay; new execution attempt |
| Task-worker leased schedule run | `lease_requeue` | Existing automatic bounded requeue; fail after the attempt limit |
| Fanout | `rerun_only` | No resume frontier; new fanout from original inputs |
| Direct engine run | `rerun_only` | No checkpoint contract; new engine run |
| Studio compiled workflow | `rerun_only` | StateDB persistence but no checkpoint/replay contract |
| Direct schedule fire / schedule-run wrapper | `child_dependent` | If the child resolves to a supported flow checkpoint, expose the child recovery; otherwise rerun as a new schedule attempt |
| Studio on-demand launch: `agent`, `flow`, `fanout`, `play`, `flow_yaml`, `engine` | Same as the equivalent CLI surface above | The launch's invocation carries the child identity from the spawn handshake; recovery is the child surface's row (flow checkpoint, agent branch continuation, rerun) |
| Studio on-demand launch: `command` (no LionAGI child root) | `pid_liveness_only` | Liveness from the recorded PID/create-time only; no marker, no resume; dead or identity-lost rows terminalize and the action is rerun |
| Invocation umbrella | `children_only` | Recover eligible child roots; fold the new attempt separately |

The launch kinds named here are the closed set accepted by the Studio launches service;
verification item 4 iterates exactly this table's surface inventory, so the promised
capability and the tested coverage cannot drift apart.

Automatic flow re-arm is deferred until replay can prove idempotency boundaries, attempt
limits, budget inheritance, and behavior for side-effecting completed nodes. The known
failure this avoids: a checkpoint records an operation as incomplete while its external side
effect committed; automatic resume would execute it twice. An operator or external fleet
wrapper makes the resume decision against the recovery projection.

## Consequences

### Positive

- Every execution surface gains wake-on-terminal semantics from one seam — the guarded
  lifecycle transition — with zero per-command flag proliferation.
- Callback failure is structurally incapable of corrupting or blocking the terminal write.
- External wrappers get a versioned envelope plus a durable reconciliation query, so fleet
  transport policy (retry, dedup, routing) lives outside LionAGI.
- Orphan handling collapses five partial predicates into one classifier with
  positive-evidence rules, and every surface gets honest, named recovery semantics.
- The Studio daemon remains optional for plain CLI use.

### Costs and accepted limits

- Best-effort push: a process that dies between commit and emission delivers no callback;
  consumers that need certainty must reconcile from the audit projection.
- A transition caller can be delayed up to the shared ten-second handler budget.
- `display_status="orphaned"` is a projection, not a persisted status; raw-SQL consumers see
  `failed`/`blocked` plus reason codes.
- Resume remains manual for every surface except task-worker lease requeue.
- `--no-persist` engine runs get non-durable envelopes with no reconciliation guarantee.

## Current-vs-ideal delta

| Capability | Current | Target in this ADR | Size |
|---|---|---|---:|
| Terminal source | Flow direct notify plus partial `SESSION_END`; other surfaces none | Post-commit registry driven by applied lifecycle terminal transitions | M |
| Durable callback fact | Status audit exists, no terminal-event reader | Anti-join reconciliation over `status_transitions` plus `terminal_deliveries` ack table | M |
| Settings-level handler | Flow-only `notify.on_terminal` string command | Same key, generic: exec/python adapter shapes, precedence, snapshot resolution, explicit disabled state | S |
| Payload | Flow-specific environment payload | Versioned minimal terminal envelope; legacy adapter preserves old payload | S |
| Delivery | Flow shell hook, 10 s, swallowed failures | Direct bounded registry; shell/executable only as adapter; external retry | M |
| Flow `--notify` | Direct teardown call | Scoped sugar over the registry, same legacy payload/timeout | S |
| Session liveness | PID/create-time on CLI sessions, several predicates | Shared conservative identity classifier used by all reaper/read paths | M |
| Studio/scheduler child identity | Child PID not persisted on invocation/schedule run | `spawn_and_wait(on_spawn=...)` persists identity on the linked invocation | M |
| Engine identity | Engine-run rows and signal session omit PID | Signal session records identity; reconciler folds the engine row | S |
| Persisted orphan state | No status; mixed failed/blocked reasons | Canonical orphan reasons plus derived display status | S |
| Reaper coverage | Sessions, invocations, plays, leases, outbox separate; gaps | One coordinator/classifier, surface-specific recovery/folding | M |
| Flow/agent recovery | Manual paths not tied to orphan projection | Capability and exact command/target exposed in status response | S |
| Universal auto-resume | None | Explicitly deferred pending idempotency and checkpoint contracts | L (deferred) |

## Verification plan

1. Fault-inject process death after commit and before handler emission: the terminal row and
   audit row exist; reconciliation returns the same `event_id`.
1a. Late-older-commit interleaving: transaction A captures its timestamp, stalls; B captures
   a later timestamp, commits, and is reconciled and acknowledged; A then commits. A's event
   MUST appear in the consumer's next unacknowledged set (this is the case a positional
   cursor loses).
1b. Push-then-crash duplicate: a handler receives the push, the consumer crashes before
   writing its delivery acknowledgment; reconciliation redelivers the same `event_id` and
   consumer-side dedup makes processing idempotent.
1b-i. Offline-longer-than-horizon: a registered consumer stops querying for longer than the
   retention horizon while terminal events accumulate; on return, every unacknowledged
   event is still in its reconciliation set (retention expires acks and explicitly retired
   consumers, never unacked events for a registered consumer; inactivity alone retires
   nothing).
1b-ii. Parallel acknowledgment: two workers of the same consumer ack the same event
   concurrently; exactly one delivery row exists and neither write errors.
1c. Settings contract: string form, mapping form, invalid value (warns, disabled, run
   unaffected), per-run override replacing the settings handler for its scope only, and the
   explicit `enabled: false` state. No-shell path: assert no executable adapter invocation
   (string form, mapping form, or `--notify`) ever constructs a shell; a shell-feature
   string (pipe, redirection, conjunction) resolves to disabled with the migration
   diagnostic, and every empty-argv resolution (empty string, whitespace-only string,
   empty argv array, and the same via per-run override and `--notify`) resolves to
   disabled with the diagnostic before any launch.
1d. Spawn handshake per incomplete phase: crash before intent commit (no row, no leak
   beyond the OS process); crash between spawn and identity commit on a marker-capable
   surface (restart adopts identity from the child marker); same on a marker-less surface
   (row terminalizes with `run.failed.spawn_identity_lost` only after spawn grace, log path
   retained); PID reuse injected at adoption and at classification (create-time mismatch
   rejects).
2. Concurrent terminal transitions plus a same-status reason append: only the winning
   transition emits; no handler runs under the transaction.
3. One hanging, one failing, one successful handler registered: the successful handler is not
   starved, total delay is bounded, persisted status unchanged.
4. Every surface's terminal path (agent, flow/play, fanout, engine, Studio launch, compiled
   workflow, scheduler fire, worker lease) with Studio present and absent: expected entity
   event and correlation fields.
5. Flow `--notify` compatibility: legacy env payload unchanged; the old direct call does not
   also fire.
6. PID reuse (same PID, different create time), access denied, missing marker, live marker,
   clock skew both directions, heartbeat racing the reaper: unknown/live rows are never
   terminalized; CAS losers never stamp `ended_at`.
7. Studio killed after child spawn, before and after marker persistence: on restart, live
   linked work does not fail; dead/no-marker work transitions only after grace with canonical
   evidence.
8. Recovery projection per surface: resume always creates a new attempt and retains the
   source terminal row; reactive-spawned checkpoints remain refused.
