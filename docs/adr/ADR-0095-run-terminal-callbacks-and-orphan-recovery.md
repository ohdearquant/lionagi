# ADR-0095: Run-terminal callbacks and orphan recovery

- **Status**: Proposed
- **Kind**: Aspirational
- **Area**: scheduling-control-plane
- **Date**: 2026-07-12
- **Relations**: builds on ADR-0057 (lifecycle transitions), ADR-0035 (terminal-status
  integrity floor), ADR-0059 (dispatch outbox), ADR-0064 (run outcome records),
  ADR-0071 (task-worker leases)

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
callback delivery. The committed transition audit is the durable reconciliation source: a
read-only terminal-event query, cursor-ordered by `(created_at, id)`, lets an external
wrapper recover a missed push by target entity or correlation ID. Consumers deduplicate on
`event_id`.

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

### D3 — Direct in-process delivery; two bootstrap points; `--notify` becomes scoped sugar

The core mechanism is a direct in-process handler call through the registry, post-commit and
bounded per D1. A handler is a Python callable; a process bootstrap may also install an
external-executable adapter, where LionAGI passes the v1 JSON envelope and interprets only
process launch, timeout, and exit code, while the executable owns the transport.

There are two bootstrap points rather than N command flags: common CLI startup and Studio
service startup. Programmatic users register handlers explicitly. Flow/play `--notify`
remains as scoped compatibility sugar: after the flow session ID is known, it registers the
legacy shell adapter for the target invocation (if present) or session, and unregisters it in
a `finally` block. The current direct teardown notify call is removed to prevent double
delivery. No `--notify`-style flag is added to agent, fanout, engine, scheduler, or Studio
APIs.

The dispatch outbox is not used by v1: its delivery loop is daemon-tick-driven, and a
callback enqueued by a dying CLI process would deliver only when a Studio daemon runs. A
LionAGI-native reliable-delivery tier would need a daemon-independent drainer, subscriber
identity, ack state, retention, and security policy — a separate decision if ever needed.

### D4 — No `orphaned` status; canonical orphan reasons, one classifier, one coordinator

No persisted `orphaned` status is added. Canonical reasons carry the fact on the sanctioned
vocabularies: execution entities ending in `failed` use `run.failed.orphaned_parent`; plays
ending in `blocked` use `play.blocked.runner_orphaned`; task-worker lease recovery keeps
`run.queued.lease_expired` and `run.failed.lease_attempts_exhausted`. Studio and CLI project
`display_status="orphaned"` (and health `ORPHANED`) when a row is nonterminal with positive
orphan evidence or terminal with a canonical orphan reason. Downstream state machines stop
waiting on `failed`/`blocked`; operators still see "orphaned".

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
| Invocation umbrella | `children_only` | Recover eligible child roots; fold the new attempt separately |

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
| Durable callback fact | Status audit exists, no terminal-event reader | Read-only projection/query over `status_transitions` | S |
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
