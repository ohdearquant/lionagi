# ADR-0095: Run-terminal callbacks and orphan recovery

> **Proposed amendment:** The immediate named-consumer delivery changes in this document remain subject to sign-off and are not implemented by this decision. The pre-existing ADR and its separately proposed Linux correction retain their own statuses.

- **Status**: Accepted
- **Kind**: Aspirational
- **Implementation-status**: partial — the v1 callback envelope, post-commit registry,
  reconciliation store/query, settings adapters, and bootstrap wiring are shipped; D4's unified
  orphan-classification/recovery target and ADR-0123's v2 canonical-Run source cutover remain open
- **Area**: scheduling-control-plane
- **Date**: 2026-07-12
- **Relations**: supersedes ADR-0060 (run supervision — its outbox-coupled callback and
  two-stage orphan design are replaced by D1/D3/D4 here); builds on ADR-0057 (lifecycle
  transitions), ADR-0035 (terminal-status integrity floor), ADR-0059 (dispatch outbox),
  ADR-0064 (run outcome records), ADR-0071 (task-worker leases); prospectively amended by
  ADR-0123 (canonical Run identity); proposed Linux identity correction coordinated with
  ADR-0104 Amendment 5

> **Proposed correction:** The Linux identity changes to D4 and D5 in this document
> remain subject to sign-off. The rest of this accepted ADR remains standing.

## Proposed ADR-0123 boundary

The callback remains a post-commit lifecycle projection and never becomes terminal authority.
ADR-0124 freezes callback mode per Invocation before its first Run: legacy-v1 mode permits one
Run and selects the Invocation event, while canonical-v2 mode permits many Runs, suppresses the
aggregate Invocation event, and emits each Run fact. Both never emit publicly for the same Run.
Orphan recovery finalizes Run only through
ADR-0058 `LifecycleService`; callback failure cannot rewrite or reopen it.

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
  dead, or a verified spawn attempt whose identity acquisition was not recorded and whose
  spawn grace has expired. The latter describes a spawn-protocol loss, not proof of death.
  Unknown liveness is not positive evidence of death.
- A **resume** creates or claims a new attempt. It never reopens a terminal row.

## Decision

### D1 — One post-commit terminal-callback registry on the lifecycle boundary

A process-wide `TerminalCallbackRegistry` is injected into the lifecycle service. The service
constructs a terminal event only when all of the following hold: the transition outcome is
`applied`; `previous_status != current_status` (a same-status reason append is not a new
terminal event); the destination is terminal under the registered lifecycle policy; and the
entity is an execution entity (`session`, `invocation`, `schedule_run`, or `play`). During
ADR-0123 dual-write, a fifth condition applies: the immutable projection decision staged in the
owner transaction must select this exact transition. A `suppress` or `attention` decision emits
nothing.

The audit append stays inside the guarded transaction. Registry emission occurs only after
the transaction has exited successfully, using the committed `status_transitions.id` as
`event_id`. The registry never runs handler code while the transaction is open.

ADR-0058's injected `TerminalProjectionParticipant` owns that fifth decision mechanically. It
validates an exact Run or Invocation terminal-callback binding reference (ID plus expected version)
supplied by the canonical application path and stages the decision beside transition audit. It does not infer Run correlation from
`sessions.run_id`, a many-to-many RunSession traversal, recency, or filesystem state. Standalone
or unbound transitions—including a post-cutover Invocation that creates no Run—retain the
four-condition v1 behavior. Once an entity enters a
canonical dual-write binding, missing/stale/ambiguous correlation fails closed to attention/no
emit; it never falls back to the standalone default.

Handlers are process-local, named, idempotently registered, and may filter by entity kind
and/or entity ID. They are invoked concurrently from an immutable envelope under one total
ten-second deadline. The shipped compatibility behavior is intentionally precise:

- an ordinary handler exception (including an adapter's surfaced launch/exit failure) is logged
  and swallowed;
- a handler cancellation is re-raised inside the AnyIO task group and is not logged as an ordinary
  exception; it follows task-group cancellation semantics rather than an isolation guarantee;
- cancellation of the task emitting the event propagates to its caller;
- budget expiry silently requests cancellation of remaining async handler work, with no timeout
  log; a handler that does not yield can delay cancellation and return beyond the budget;
- synchronous handlers are offloaded with `abandon_on_cancel=True`, so expiry/cancellation may
  return while their worker thread continues; its later result is unobserved and cannot be claimed
  as delivered, failed, or torn down.

The shared budget bounds waiting for yielding handlers, but a non-yielding async handler can
delay the transition caller's return beyond it. Synchronous handlers abandoned at expiry may
outlive return; their later work cannot roll back, overwrite, or recategorize the terminal write.
This corrects D1's earlier hard wall-time claim. ADR-0120 names the compatibility profile;
the proposed immediate-consumer operation likewise has a cooperative deadline and cancellation
cleanup may extend beyond it.

Legacy process-local callback push is best effort and has no at-least-once guarantee. An opted-in named consumer retains eligible owed events and offers at-least-once attempts under the registered-consumer contract below, subject to a running consumer and available capacity; it does not promise exactly-once effects. The committed transition audit is the durable reconciliation source, and its consumption contract is **set-based, not cursor-based**. A positional cursor over
`(created_at, id)` cannot be made safe here: the transition timestamp is captured before the
transaction begins while commit happens at context exit, so a slow transaction can commit a
row whose `created_at` sorts *behind* a cursor another consumer already acknowledged, and the
audit ID is a random UUID with no commit ordering. A strict greater-than cursor would skip
that row permanently — losing exactly the commit-then-crash window reconciliation exists to
cover.

Delivery acknowledgement is durable state in `terminal_deliveries`, keyed by `(transition_id, consumer)`. The named consumer writes it after an observed successful or explicitly not-applicable attempt, whether triggered immediately after commit or by reconciliation. Registration may insert the baselines specified below; those rows are not evidence of delivery. Legacy callback push writes no acknowledgement. ACK insertion remains idempotent through `INSERT ... ON CONFLICT DO NOTHING`. Reconciliation selects committed eligible transitions without an acknowledgement for the active consumer. Neither ACK uniqueness nor an ACK-state check exclusively claims an external effect.

**ADR-0123 target acknowledgement amendment.** Source selection introduces a logical
`terminal_fact_id` distinct from a wire version and, for bound Runs, potentially distinct from the
selected source transition. The target delivery row is
`(terminal_fact_id, consumer, source_transition_id, acked_at)` with primary key
`(terminal_fact_id, consumer)`. Reconciliation enumerates only staged projection decisions whose
closed kind is `emit` and whose wire profile is `legacy_v1` or `canonical_run_v2`, then anti-joins
on fact ID. Suppression and attention records are never deliverable facts.

Compatibility is deterministic:

- a standalone/unbound v1 transition has implicit `terminal_fact_id=transition_id`;
- a bound v1 envelope may add optional `terminal_fact_id`; if a v1 client acknowledges only its
  `event_id`, the server resolves that source transition through the immutable staged decision and
  acknowledges its fact ID;
- v2 carries `terminal_fact_id` explicitly and acknowledgements use it directly;
- no wire schema/version component participates in deduplication identity.

Migration is dual-read/dual-write, not an in-place semantic flip. First add nullable fact/source
columns and backfill every existing delivery with `terminal_fact_id=transition_id`. During the
compatibility epoch, ack writes populate both the legacy transition lookup and the fact-keyed row;
reconciliation unions legacy terminal transitions lacking a decision (implicit legacy-v1 fact ID)
with new `TerminalProjectionEmitDecision` rows and anti-joins on
`COALESCE(fact_id, transition_id)`. After all
new terminal writes stage decisions and every existing ack is backfilled, enforce the fact-key
uniqueness/primary key and remove the legacy write. SQLite uses ADR-0118's preapproved rebuild;
PostgreSQL uses its authored migration plan. No live diff authors this change.

Retention never expires an unacknowledged event out of an active consumer's reconciliation
set: an event remains in that set until the consumer acknowledges it, however old it gets —
a consumer offline longer than any horizon still recovers every missed terminal event on
return. What expires is the other side: acknowledged delivery rows past the retention
horizon (default ninety days) — but a delivery row is prunable **only once its transition
row has left the reconciliation-queryable set** (pruned together with it, or after it).
The reconciliation query is an anti-join of transition rows against delivery rows, so
removing an ack whose transition row is still queryable would resurrect that event into
the consumer's unacknowledged set and redeliver work processed long ago, past any
consumer-side dedup state. Age alone is therefore never a sufficient pruning condition
for a delivery row. Under the target schema, pruning requires both the selected projection decision
and its source transition to have left the reconciliation-queryable set; during dual-read it uses
the stricter legacy-or-target relation. The source fact's own lifecycle bounds it. Consumer registrations end only by
explicit retirement — a recorded action taken by the registration's owner or a deployment
operator, never a side effect of inactivity. A registered consumer that merely stops
querying remains active and its unacknowledged set is retained indefinitely, regardless of
how far past any horizon its silence extends. Releasing a retired consumer's outstanding
unacked set happens atomically with the retirement itself (one transaction), so there is no
window in which the identity is retired while its set is still owed, or remains active after
its obligation is released. The retired registration row and acknowledgements remain subject
to the retention and explicit-reactivation rules below. Registering as a reconciliation consumer is what creates the retention obligation;
an anonymous ad-hoc query gets the plain audit history with no completeness guarantee.

Because membership in the unacknowledged set does not depend on any ordering, a
late-committing older row is simply still in the set the next time the consumer queries.
For standalone v1, consumers deduplicate on `event_id` because it is the implicit fact ID. Bound
v1 and v2 consumers deduplicate on `terminal_fact_id`; a legacy bound client may present only its
`event_id`, and the server resolves it through the immutable staged decision before writing the
fact-keyed acknowledgement. Independent attempts by a named consumer may deliver the same fact, including an immediate attempt followed by a crash before acknowledgement. For a committed `durable=true` eligible event owed to an opted-in identity, legacy global push is not a second delivery route. The audit row is never mutated; acknowledgement lives entirely in the deliveries table, preserving audit immutability.

`SESSION_END` remains supported but is not bridged into the registry; a bridge would create a
loop and a second authority. Persistent direct engine runs are covered through their
signal-session terminal transition. The `--no-persist` engine may publish an envelope marked
`durable=false` with no reconciliation guarantee. Existing flow invocation-finalization error
handling can also emit a synthetic non-durable envelope, and an explicitly requested `--notify`
can use its no-session direct fallback. `durable=false` envelopes dispatch through the existing
legacy emit unchanged, even when the named consumer is active: global best effort applies unless
a matching scoped override replaces it. Suppressing inherited-global wrappers for committed
`durable=true` events must not change non-durable registrations. These envelopes are not
committed transition facts and do not acknowledge or replay through the durable subscription.

### D2 — A minimal versioned envelope owned by LionAGI; transport policy stays outside

LionAGI guarantees a small, transport-neutral envelope:

```json
{
  "schema": "lionagi.run-terminal",
  "schema_version": 1,
  "event_id": "<status_transitions.id or synthetic id>",
  "terminal_fact_id": "<optional; present for a bound Run projection>",
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

`terminal_fact_id` is an additive optional v1 field under the version rules below. It is omitted
for unbound v1 pushes, whose fact ID is deterministically the event/transition ID, and present for
bound v1 projections. Its omission never causes a bound server acknowledgement to guess: the
staged decision provides the exact event-to-fact mapping.

**Version evolution rules.** Within `schema_version: 1`, the guaranteed fields' names,
types, semantics, and requiredness are immutable. New *optional* fields may be added without
a version bump; consumers MUST ignore fields they do not recognize. Any removal, type
change, semantic change, requiredness change, or change to the correlation-key set requires
incrementing `schema_version`. A consumer receiving a version it does not support MUST NOT
process the envelope as if it were v1; it logs and drops (or dead-letters) it, and can always
fall back to the reconciliation query, whose row shape is governed by the schema, not the
envelope.

ADR-0123's canonical source is therefore a v2 envelope, not a v1 optional-field trick. Version 2
sets `entity.kind="run"`/`entity.id=run_id`, uses the canonical Run transition ID as `event_id` and
the Run binding's separately minted version-independent `terminal_fact_id`, and retains typed nullable legacy correlation. No generic
v2-to-v1 adapter exists because v1 cannot represent a canonical Run without inventing a unique
legacy entity. Required consumers must advertise v2 support before a Run enters the canonical
callback cohort. Source selection, rollout, reconciliation identity, and duplicate prevention are
normative in ADR-0124.

### D3 — Direct in-process delivery; two bootstrap points; `--notify` becomes scoped sugar

For committed lifecycle transitions, legacy delivery invokes Python or external-executable handlers through the process-local registry after commit, bounded per D1. Non-durable envelopes need not have a committed transition. Executable adapters receive the v1 JSON envelope and own their transport; LionAGI observes launch, timeout, and exit outcome.

Common CLI startup and Studio startup continue to register legacy candidates without requiring an asynchronous store read at registration. After a committed `durable=true` eligible event exists, the producer reads the store's named-consumer registration asynchronously before selecting its route. For an active identity, the committing process offers the exact event to the shared consumer operation immediately after commit, outside the legacy registry's override selection, subject to mode read, admission and the shared budget. It does not require a running Studio daemon or an interprocess wake transport. An inactive or absent identity selects the legacy route. If that durable event's mode read fails, its state is unknown: do not assume inactivity or invoke the inherited-global legacy candidate; report the uncertainty, preserve independent scoped and programmatic callbacks, and leave an eligible event owed only if an active registration actually exists. Configuration for legacy handlers is:

- **Settings key**: `notify.on_terminal` (the key the current flow-only prototype already
  resolves). Accepted shapes: a string (compatibility form) or a mapping `{enabled: bool,
  adapter: {kind: "exec", argv: [...]} | {kind: "python", ref: "module:callable"},
  filter: {kinds: [...], ids: [...]}}`. The absent key and `enabled: false` are both the
  explicit disabled state; the default is disabled. The string form is converted to an
  argv array by POSIX word-splitting (`shlex.split`) with no shell interpretation of any
  kind; a string that fails to split, or whose intent requires shell features (pipes,
  redirection, `&&`, variable expansion), warns with a migration diagnostic naming the
  argv form and resolves to disabled for legacy push. A resolution producing an empty argv — an empty or whitespace-only string, an explicit empty `argv` array, or the same through a per-run override or `--notify` — is invalid by the same rule. Legacy push logs the key-naming diagnostic and resolves to disabled before launch; when enabled is true or absent, the durable global subscription records invalid configuration as failure and leaves the event owed; a valid explicit `enabled: false` wins over unused malformed fields. No
  configuration shape reaches a shell.
- **Legacy precedence**: per-run override > project `.lionagi/settings.yaml` > global `~/.lionagi/settings.yaml` > disabled. Existing `--notify` and programmatic overrides replace the legacy settings handler in their run scope. They do not replace, suppress, or acknowledge an opted-in durable global subscription.
- **Resolution and validation**: legacy handlers retain bootstrap snapshot resolution and warning/disabled behavior for invalid configuration. The durable subscription reads current global settings on every immediate or replay attempt, rather than the bootstrap handler snapshot. This changes long-running CLI behavior: a later explicit disable or filter mismatch acknowledges the event as not applicable, even if the notifier was configured when the process started; re-enabling it does not revive that ACK. A load/YAML error or malformed configured global structure is failure and leaves the event owed. Validate the raw settings root, `notify` and `on_terminal` containers before interpreting missing values: a genuinely absent or explicitly null `on_terminal` is deliberate silence. A valid boolean `enabled: false` takes precedence over malformed unused adapter/filter fields and is not applicable, with an irreversible ACK. A non-boolean `enabled` is invalid and remains owed; with `enabled: true` or absent, malformed adapter/filter fields are invalid and remain owed. A malformed present `notify` container is invalid. Current resolver behavior is not sufficient for every malformed container and must be repaired in implementation. Configuration or delivery failure never changes source lifecycle status or history.
- **Exec adapter safety**: `argv` is an array executed without a shell; the envelope is
  passed on stdin; LionAGI interprets only launch, the shared timeout budget, and exit code.

Programmatic handlers remain explicit. Flow/play `--notify` retains its scoped legacy payload, no-shell adapter, target invocation-or-session selection, `finally` unregistration, and existing explicitly requested no-session direct fallback. No new per-command notification flag is required. A legacy scoped override continues to select among legacy registry callbacks; it does not suppress the independent named-consumer call, which is not inserted into that override-filtered registry.

In durable mode, classify notification provenance from the raw global and nearest project files, not the merged settings object. The global subscription resolves only current global settings at each attempt. An explicit `--notify` or programmatic per-run notifier is a separate best-effort subscription. A project `notify.on_terminal` string or mapping containing its own `adapter` key declares a separate project subscription; resolve that project declaration on its own, without borrowing missing adapter fields from global settings. A malformed or incomplete declared adapter is a project configuration rejection, not evidence of an inherited-global subscription and not permission to send the global adapter as a second legacy push. An explicit project `enabled: false` disables that separate project subscription; it cannot silence the durable global subscription. To limit global deliveries use the global kind/ID filters, or explicitly retire the global consumer. A project declaration containing only `enabled` and/or `filter`, but no adapter, creates no independent subscription; it neither disables nor filters the global subscription, and does not cause a second inherited-global push. An absent project declaration likewise creates none. This is a deliberate change from the legacy deep-merge behavior in durable mode: partial project adapter or filter inheritance no longer modifies the global subscription. In unenrolled legacy mode the existing merge and precedence remain unchanged.

The independent project and per-run subscriptions cannot suppress or acknowledge global work. Where an explicit per-run override and a project notifier both apply to the same legacy run scope, existing per-run-over-project precedence still selects the legacy notifier; this does not affect the global subscription. For a committed `durable=true` event owed to the active consumer, an inherited-global scoped wrapper is not a second subscription and must not invoke the same global notifier through legacy push; its non-durable registration remains unchanged. Two separately configured subscriptions may notify the same destination; no cross-subscription deduplication is promised.

The dispatch outbox is not used by this v1 contract. The committed eligible transition plus active registration and acknowledgement anti-join retain the outstanding obligation. The producer offers an immediate attempt subject to mode read, admission and the shared budget; Studio performs bounded recovery passes. Recovery after the producer exits requires a subsequent consumer execution. Persisted intent, durable retry schedules, persisted or version-snapshot filters and destinations, and owner-scoped delivery remain separate decisions.

### D4 — No `orphaned` status; canonical orphan reasons, one classifier, one coordinator

No persisted `orphaned` status is added. Canonical reasons carry the fact on the sanctioned
vocabularies: execution entities ending in `failed` use `run.failed.orphaned_parent`; plays
ending in `blocked` use `play.blocked.runner_orphaned`; task-worker lease recovery keeps
`run.queued.lease_expired` and `run.failed.lease_attempts_exhausted`; a verified spawn
attempt whose identity acquisition was never durably recorded terminalizes after spawn grace
with `run.failed.spawn_identity_lost` under the handshake below. This records a protocol loss,
not positive evidence that the process died. Studio and CLI project
`display_status="orphaned"` (and health `ORPHANED`) when a row is nonterminal with positive
orphan evidence or terminal with a canonical orphan reason. Downstream state machines stop waiting on `failed`/`blocked`;
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
applicable; linked child-session status/activity. Positive-evidence rules apply: unknown or
inaccessible identity is never classified dead solely because a wall-clock threshold elapsed.
The proposed Linux correction below governs identity comparisons and legacy rows.

Identity coverage is completed without a new PID column on every table: CLI sessions keep
writing `pid`/`pid_create_time` into `sessions.node_metadata`; `spawn_and_wait()` gains an
`on_spawn` identity callback so Studio on-demand launches and scheduler fires persist child
process identity into their linked invocation's `node_metadata`; direct engine runs write
current identity into the signal session's `node_metadata`, and reconciliation folds a
terminal signal session onto the same-ID engine-run row. Task-worker executions retain lease
identity as their ownership proof.

**Spawn handshake.** Identity acquisition is a durable, ordered protocol, not a best-effort
callback:

1. *Intent*: before the child process is created, the supervisor persists a spawn-attempt
   record (attempt ID, argv summary, log path) on the linked invocation and commits it. A
   spawn with no committed intent must not proceed.
2. *Identity acquisition*: immediately after process creation returns — before any await
   point in the supervisor — the child's PID and process identity are written to the intent
   record and committed. This commit is the identity point. From the part-2 cutover, the Linux
   identity must meet the durable-identity requirements below. In phase 1 the recorded identity
   is PID plus creation time, compared under the Linux correction (epoch disagreement held
   unknown); creation time alone does not exclude PID reuse.
3. *Child registration*: on surfaces whose child runs LionAGI bootstrap code, the child
   additionally writes its own marker row carrying the attempt ID, durably linking marker to
   invocation. Marker-less surfaces (bare `command` children) rely solely on step 2.

Restart behavior is defined per incomplete phase: intent committed but no identity and the
surface is marker-capable — look up the child's marker by attempt ID and adopt its identity
only when the applicable identity requirements are met. A verified committed intent with no
identity acquisition record, and no marker where one is applicable, waits out the spawn
grace and then terminalizes with `run.failed.spawn_identity_lost`, preserving the intent
log path for manual audit. This is an attempt-specific spawn-protocol outcome, not a finding
that the child died: a child lost between process creation and the step-2 commit may still
be alive and discoverable through the intent. Absence of a complete identity on an imported
or legacy row does not establish the committed intent or the incomplete phase and cannot by
itself invoke this rule. Linux identity at adoption and liveness classification must satisfy
the correction below; creation-time comparison alone does not exclude PID reuse.

**Proposed Linux identity correction (ADR-0104 Amendment 5 part 1).** A process-creation
epoch comparison alone does not exclude Linux PID reuse, and an epoch-start or epoch-boot
disagreement alone does not establish that the recorded execution died. The classifier and
every consumer preserve an explicit unknown for a live PID with that disagreement. A session
marker, command-line shape, a live numeric PID or current boot observation alone does not
prove process birth; at the part-2 cutover none alone authorizes a recovery signal.

Phase 1 retains existing positive legacy matches, including a marker-only match when creation
time is missing, and does not rescue an epoch mismatch with a marker. Other existing behavior,
including handling of missing PID fields and non-degraded or unreadable imported manifests, is
unchanged in this phase. Each doctor and stale-sweep pass prints counts by reason for Linux
rows held unknown on epoch-start or epoch-boot disagreement, using the observation that
decided the verdict, and applies no automatic disposition to those rows. A release may claim
containment only once both doctor and stale sweep print these counts. The pending stale-sweep
change supplies its counts; doctor reporting is pending its phase-1 change and is not yet
enforced.

At the part-2 durable-identity cutover, a persisted legacy Linux row lacking a complete
identity tuple is unverifiable: it grants no recovery-signal or automatic-cancellation
authority. Each doctor and stale-sweep pass lists such rows and prints their count. Explicit
operator reconciliation and a process completing its own run remain separate actions. These
provisions do not change the macOS or Windows identity rules. The missing identity of a legacy
row is not the independent committed spawn-intent and incomplete-phase evidence required by
the spawn handshake above, and must not be treated as such.

This correction replaces D4's Linux creation-time-only claims for the classifier and spawn
handshake. The first release's bounded guarantee is containment of the demonstrated
epoch-disagreement false cancellation in updated readers, with stored epoch units unchanged.
The stricter cutover policy is not claimed until its authoritative readers and reporting
passes enforce it. This amendment does not assert that D4's unified classifier or coordinator
has shipped. ADR-0104 Amendment 5 part 2 will specify the durable identity object.

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
| Studio on-demand launch: `command` (no LionAGI child root) | `pid_liveness_only` | No marker or resume; liveness uses the platform identity requirements, and a verified incomplete spawn follows the handshake; a dead or protocol-lost action can be rerun |
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
- The shared ten-second budget bounds waiting for yielding handlers; a non-yielding async
  handler may delay the transition caller beyond it, and abandoned synchronous work may outlive
  the return.
- `display_status="orphaned"` is a projection, not a persisted status; raw-SQL consumers see
  `failed`/`blocked` plus reason codes.
- Resume remains manual for every surface except task-worker lease requeue.
- `--no-persist` engine runs get non-durable envelopes with no reconciliation guarantee.

## Current-vs-ideal delta

| Capability | Current | Target in this ADR | Size |
|---|---|---|---:|
| Terminal source | Flow direct notify plus partial `SESSION_END`; other surfaces none | Post-commit registry driven by applied lifecycle terminal transitions | M |
| Durable callback fact | Status audit exists, no terminal-event reader | Anti-join reconciliation over `status_transitions` plus `terminal_deliveries` ack table | M |
| Settings-level handler | Flow-only `notify.on_terminal` string command | Same key, generic: exec/python adapter shapes, precedence, legacy snapshot resolution, explicit disabled state; durable global attempts resolve current settings | S |
| Payload | Flow-specific environment payload | Versioned minimal terminal envelope; legacy adapter preserves old payload | S |
| Delivery | Flow shell hook, 10 s, swallowed failures | Direct bounded registry; executable/python adapter only, no shell; external retry | M |
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
1b. Immediate-attempt-then-crash: after enrollment, the committing process performs the notification effect and exits before consumer acknowledgement. A fresh consumer process redelivers the same `event_id`. A durable sink that atomically deduplicates with its effect applies one effect; a non-idempotent sink may apply two. Assert raw attempts, sink effects, and acknowledgement rows separately.
1b-i. Offline-longer-than-horizon: a registered consumer stops querying for longer than the
   retention horizon while terminal events accumulate; on return, every unacknowledged
   event is still in its reconciliation set (retention expires acks only alongside their
   transition rows, and consumers only by explicit retirement — never unacked events for
   a registered consumer; inactivity alone retires nothing).
1b-ii. Parallel acknowledgment: two workers of the same consumer ack the same event
   concurrently; exactly one delivery row exists and neither write errors.
1b-iii. Ack pruning never resurrects: acknowledge a set of terminal events, prune every
   delivery row eligible under the retention policy (transition rows still queryable),
   run reconciliation for that consumer, and assert zero redelivered events — pruning
   eligibility must have excluded every ack whose transition row remains in the
   reconciliation-queryable set.
1b-iv. Consumer retirement atomicity: retire an active consumer holding a nonempty
unacknowledged set; assert its registration changes from active to retired and its retention
obligation ends atomically, with neither intermediate state observable. The registration row
and existing acknowledgements remain for explicit reactivation and subsequent pruning policy.
1b-v. Fact-ID migration: backfill existing `(transition_id, consumer)` acknowledgements, dual-read
and dual-write one standalone v1, bound v1, and canonical v2 fact, retry each wire delivery, and
prove one `(terminal_fact_id, consumer)` row per logical fact with no resurrection after pruning.
Acking a bound v1 `event_id` must resolve through its staged decision; suppressed/attention
decisions must never appear in reconciliation.
1c. Settings contract: string form, mapping form, invalid legacy value (warns, disabled, run unaffected), per-run override replacing the legacy settings handler in its scope, and explicit `enabled: false`. Durable invalid configuration leaves the event owed; explicit disabled is acknowledged not applicable. No-shell path: string, mapping and `--notify` never construct a shell. Shell-feature strings and every empty argv form produce a key-naming diagnostic before launch; legacy push resolves them disabled and durable mode treats them as invalid when enabled is true or absent. Explicit `enabled: false` takes precedence over unused malformed fields.
1d. Spawn handshake per incomplete phase: crash before intent commit (no row, no leak
   beyond the OS process); crash between spawn and identity commit on a marker-capable
   surface (restart adopts identity from the child marker only if the applicable identity
   requirements are satisfied); same on a marker-less surface
   (row terminalizes with `run.failed.spawn_identity_lost` only after spawn grace, log path
   retained); PID reuse injected at adoption and at classification (identity requirements
   checked, with Linux epoch disagreements held unknown as specified in D4).
2. Concurrent terminal transitions plus a same-status reason append: only the winning
   transition emits; no handler runs under the transaction.
3. One hanging async handler that yields (for example, awaiting an infinite AnyIO sleep), one
   hanging sync, one failing, and one successful handler registered: the successful handler is
   not starved, waiting for yielding handlers follows the shared budget, async work is cancelled
   silently, sync work is recorded as abandoned/unknown and may continue, only the ordinary
   failure logs, and persisted status remains unchanged. A separate controlled non-yielding
   coroutine overruns the budget before yielding, demonstrating that caller return may be delayed
   beyond it. Separate fixtures cover handler cancellation and emitter cancellation propagation.
4. Every surface's terminal path (agent, flow/play, fanout, engine, Studio launch, compiled
   workflow, scheduler fire, worker lease) with Studio present and absent: expected entity
   event and correlation fields.
5. Flow `--notify` compatibility: legacy env payload unchanged; the old direct call does not
   also fire.
6. PID reuse (same PID, different create time), access denied, missing marker, live marker,
   clock skew both directions, heartbeat racing the reaper: unknown/live observations do not
   establish death or by themselves authorize terminalization; the independently evidenced
   incomplete-spawn protocol outcome in D4 remains distinct. CAS losers never stamp `ended_at`.
7. Studio killed after child spawn, before and after marker persistence: on restart, live
   linked work does not fail solely because identity is unknown; a verified incomplete spawn
   transitions only after grace with canonical protocol evidence. An incomplete legacy identity
   alone never supplies that evidence.
8. Recovery projection per surface: resume always creates a new attempt and retains the
   source terminal row; reactive-spawned checkpoints remain refused.

## Amendment: registered terminal reconciliation consumers

- **Status**: Accepted
- **Date**: 2026-10-28

This original accepted amendment adds a reconciliation consumer to standalone v1. The proposed immediate-consumer amendment below qualifies D1 and D3 while retaining ADR-0124 fact-ID source selection and v2 dual-read as separate work. For the v1 named consumer, immediate and reconciliation triggers offer at-least-once attempts for eligible owed events while a consumer runs with available capacity, not exactly-once effects. Independent processes may attempt the same fact. Standalone v1 deduplicates on `event_id=transition_id`; `(transition_id, consumer)` makes ACKs idempotent. Live settings and process-local retries do not implement ADR-0120 D5's persisted intent, durable retries and backoff, or filters and destinations persisted or resolved from a versioned snapshot.

### Stored eligibility and upgrade

State schema version 6 adds nullable `status_transitions.delivery_eligible` and
`terminal_consumers(name, registered_at, retired_at)`. An execution entity's applied write is
eligible when its target is terminal under its policy, its previous status differs from that
target, and the write is a transition rather than initial history. Initialization and
same-status writes are ineligible. Each history writer computes the same decision at write
time; native push reads the committed value, and replay selects only TRUE. An operator's
force-transition from running to terminal is eligible and emits after commit using that same
transition ID. A NULL-to-failed reaper transition is eligible, unlike an initial terminal row.
No handler runs inside the status transaction.

The v5-to-v6 migration uses the frozen v5 execution-kind and terminal-status mapping. It marks
TRUE where the predecessor is non-NULL and differs from the terminal target, including old
operator transitions; it marks FALSE for non-execution rows, nonterminal targets and equal
predecessor/target rows. Terminal execution rows with NULL predecessor remain NULL because
initialization and reaper history cannot be distinguished. Replay excludes NULL, so this
under-delivers old ambiguous reaper rows that the original push already attempted. No later
heuristic classifies them. Classification, its completion marker, migration time, and the
initial NULL count commit atomically with the v6 schema version. Reopening never reclassifies.

An old process that was already open may write another NULL after the migration; it is not
reclassified or owed. Registration and reconciliation report the initial NULL count and the
current count, and the difference reports post-migration NULL rows exactly only while no
`status_transitions` rows have been deleted. After upgrade, restart every lionagi process that
opens the store, including Studio daemons, schedulers, and long-running CLI processes.

### Registration and retention

Registration creates the retention obligation; inactivity does not retire a consumer. In one
transaction, `start="now"` registers the identity and acknowledges the eligible history in
its registration snapshot, while `start="all"` registers without acknowledging that history.
A transition not visible in the registration snapshot remains owed even if its timestamp was
captured earlier. Registering an already active identity returns the existing registration.

Enrollment must commit before execution advertised as covered begins, including Studio startup reapers and scheduler activity. A post-commit notification must never first register `start="now"` for its own event. Attaching to an active identity does not create a new cutoff. Baseline acknowledgements are not delivery evidence; no notification guarantee is implied for pre-enrollment events. Upgrade and restart every producer before enrolling the consumer: an old producer can still send legacy push and cannot participate in the single-route contract. If an old producer runs after enrollment, its legacy push plus later replay can duplicate the same notification; an already-enrolled, retained TRUE-eligible, unacknowledged event remains owed, so this mixed-version case does not lose that obligation. This claim does not cover an event committed before enrollment: `start="now"` can baseline it without delivery. Nor does it cover an old pre-v6 writer's NULL-eligibility rows, which the stored-eligibility rules above exclude.
Retirement is explicit and changes `retired_at` in one statement, atomically ending service
and its retention obligation; it does not delete acknowledgement rows. Retired-consumer
acknowledgements become eligible for pruning under the retention rule; actual pruning is a
separate decision.

Explicit reactivation resumes the same identity and retains
its acknowledgements; `start="now"` baselines currently unacknowledged eligible history and
`start="all"` leaves it owed. A late acknowledgement after retirement is accepted
idempotently because it records an already completed delivery. Immediately before calling a
handler, a reconciler re-reads the consumer's active status in its own short statement; that
check is the attempt's start. Retirement stops new attempts whose checks occur after it commits
and does not cancel attempts already started. An attempt that passed its check may finish and
acknowledge after retirement. No lock is held across the handler and no exclusive claim is
created; each reconciler has at most one batch of in-flight attempts plus the bounded abandoned
workers described below.

### Daemon delivery and bounded traversal

`LIONAGI_STUDIO_TERMINAL_RECONCILE` remains off by default. Enabling it enrolls an absent `notify.settings.on_terminal` identity before covered startup work, attaches to an existing active identity, and runs bounded startup and periodic recovery passes. A retired identity is not reactivated by the switch; explicit operator reactivation chooses its baseline. An active registration selects durable routing in every participating producer, regardless of that producer's Studio environment flag. A disabled switch does not retire an existing registration or restore legacy global push; retirement and reactivation remain explicit.

When the consumer is active, each participating CLI or Studio producer submits the exact `durable=true` committed eligible event to the shared consumer operation immediately after commit. Such events use the consumer only; `durable=false` fallback, `--no-persist`, and other non-durable envelopes use unchanged legacy emit without ACK or replay: global best effort unless a matching scoped override replaces it. This bypasses older traversal ordering, not eligibility, active-consumer, ACK or capacity checks. Immediate and replay attempts share process-local admission keyed by store, consumer and event, outcome handling, stored-eligibility and ACK checks before effects, and abandoned-worker bounds. Unavailable capacity or a failed attempt leaves an eligible event owed while the registration is active. For a `durable=true` event, no admission, preparation, store-read or ACK failure falls back to inherited-global legacy push; an unknown mode is not evidence of an active registration, so only an actually active registration creates an obligation. Post-commit read failure leaves delivery unresolved and cannot rewrite the transition. For these durable committed events owed to the active identity, legacy global settings delivery, including inherited-global scoped wrappers, is suppressed for this identity; independent project/per-run and programmatic callbacks retain their own dispatch rules. Upgrade all producers before enrollment.

In durable mode, create a run-bound recorder unconditionally at run start when a run directory exists, even if notifications are disabled at bootstrap; create no outcome file until an attempt is actually recorded. Pass the recorder to an immediate global attempt only for that run's own terminal entity. One serialized recorder owns the per-run `notify_outcome.json` and records separate `run` and `global` subscription results, including observed exec success, failure and configuration rejection, while keeping backward-compatible top-level fields for existing readers. The selected project-own-adapter subscription is recorded as `run`. Aggregate `ok` is false if any recorded subscription fails. Aggregate `exit_code`, stderr and reason come from the first failure in fixed `run`, then `global` order, independent of completion order. Global stderr uses `notify_stderr.global.log` with mode 0600. A run subscription's result cannot acknowledge the global subscription. Replay has no run handle and writes no per-run outcome; legacy-mode recorder behavior remains unchanged. Python handlers retain existing outcome-file behavior unless separately specified.

Only observed adapter success or explicit not-applicability acknowledges an attempt. Exec success requires exit zero; nonzero exit, timeout and spawn failure fail. Python normal return succeeds except explicit `False`; exception, cancellation, timeout and `False` fail, subject to the tracked synchronous late-completion rule below. Invalid configured settings fail. Explicit disabled settings and nonmatching filters are not applicable and are counted separately. Registry return is never success evidence.

Immediate dispatch shares one existing ten-second post-commit caller budget with other callback work; preparation, admission, adapter and ACK do not each receive a new serial ten-second allowance. This is a cooperative deadline, not a hard wall-time guarantee for synchronous work or cancellation cleanup. Cancellation during an ACK may occur before commit or after an ambiguous commit: never infer either outcome from the cancellation alone. Preserve the original caller cancellation, track any still-running operation under bounded ownership, and resolve the durable ledger before deciding that the event is owed or starting a retry. If the ACK did not commit, a subsequent attempt may repeat an already completed effect; if it did commit, the ACK check suppresses an ordinary retry. Notification I/O and ACK writes occur after the source transaction and cannot change source status or history. A committed, retained TRUE-eligible event without an ACK remains owed while its registration remains active; baseline or not-applicable ACKs and NULL-eligibility rows are not owed. Effect-before-ACK death may repeat an effect. There is no delivery-time guarantee while no consumer runs.

An existing flow invocation-finalization exception may produce a synthetic `durable=false` envelope without a committed transition ID. It uses unchanged legacy emit even with active enrollment: global best effort unless a matching scoped override replaces it; it cannot be ACKed or replayed. Do not manufacture a durable ID or claim that this fallback satisfies the global subscription. An exception is not itself proof the transaction did not commit: if commit outcome is ambiguous, inspect persisted state and eligibility where possible and reconcile a real committed event; otherwise report the uncertainty. The best-effort synthetic notice may duplicate a later recovered committed event.

A rotating `(created_at, transition_id)` keyset traverses the
unacknowledged set, wrapping to discover late commits; the cursor is process-local scheduling
state, never a durable acknowledgement or a watermark. A bounded process-local retry map is a
best-effort brake, evicting the least recently attempted entry when full. Retry delays and
attempt counts are lost on restart or eviction. A pass reports locally observed rows past ten
failed attempts; this is not a durable count. A traversal ends when the cursor wraps. A row
due when reached is attempted in that traversal; a cooling row is skipped and attempted in
the first traversal reaching it after backoff expires. Backoff is capped at one hour, so under
a running process and finite backlog every pending eligible row is attempted again within one
cap plus one traversal, as long as its consumer is not saturated. A consumer whose abandoned
workers stay at the bound described below attempts nothing until one of them ends, and no path
reaps a worker that never ends: it holds its slot until the process exits. There is no claim of
strict global oldest-first order, exact doubling, or progress under unbounded arrivals.

A timed-out synchronous Python hook fails without an immediate acknowledgement. While its
abandoned worker remains active, another attempt for the same consumer and transition is not
started in that process. A later normal completion other than explicit `False` is acknowledged; an exception or explicit
`False` records failure. Outstanding abandoned workers are bounded per consumer by the batch
size; at the bound, a pass starts no further attempts for that consumer and reports saturation.
Replacing the SQLite store file while the daemon runs is unsupported; late acknowledgements may
be lost or refused.
For a private in-memory SQLite store whose owning event loop has stopped, a later successful
result is retained in process and acknowledged on that store's next pass on the original loop;
the connection is not reused across event loops.

### Scope and configuration

Immediate and replay paths consider committed, retained TRUE-eligible events owed to the active global subscription regardless of whether any session, project, or unambiguous context exists; actual delivery remains subject to configuration, admission and observed handler outcome. Session and play links and invocation/schedule-run sessions may be inspected only to attach optional context and diagnostic counts. Separately count missing session, unavailable or NULL project, ambiguous project, and context-read failure; do not select an arbitrary project. None of those labels is a not-applicable outcome, an ACK, or a reason to suppress the global attempt. A context-only read failure is reported but does not by itself block global delivery; actual eligibility, registration, configuration and ACK failures retain their separate handling. Apply configured global kind and ID filters to the event itself.

The durable global subscription is independent of project-only and per-run best-effort subscriptions. Their overrides cannot suppress or satisfy it. The explicitly requested no-session `--notify` direct fallback remains best effort and cannot establish or acknowledge a durable global delivery. Two explicitly configured subscriptions may notify the same destination; there is no cross-subscription deduplication guarantee.

Notification destinations and filters are read from current global settings at each attempt, not frozen at source commit. A settings change may redirect or disable a later retry. Disabled settings ACK the owed event as not-applicable: this is an intentional drop, not deferred delivery. Re-enabling settings does not revive an already ACKed event. Persisted intent, durable retries and backoff, and filters and destinations persisted or resolved from a versioned snapshot, as specified by ADR-0120 D5, remain outside this v1 slice. All processes serving one identity must share the same logical global configuration and security domain.
Compatibility-mode stores are never migrated by this feature and durable consumer APIs refuse
them with a typed mode error. Their transition writers use stored eligibility if the foreign
schema and profile admit it, or compute the push decision in memory if the column is absent;
that push is best-effort and has no replay guarantee. Consumer APIs also refuse an unmigrated
read-only store with a typed error naming its schema version. Owner-scoped compatibility
consumers and persisted notification intent are separate follow-ups.


## Proposed amendment: immediate named-consumer delivery for standalone v1

- **Status**: Proposed — pending architectural approval; no implementation sign-off.
- **Date**: 2026-10-29

When accepted, this amendment qualifies D1/D3 and the registered-consumer text for the
standalone v1 settings subscription; all other decisions remain standing under their existing
statuses. The named consumer owns both post-commit exact-event attempts in the
committing process and bounded recovery attempts. Legacy registry push remains best effort for
other subscriptions. This proposal does not implement ADR-0124 or ADR-0120 D5.

### Planned operator interface and configuration

> Durable terminal reconciliation is off by default. Enabling the Studio switch enrolls an
> absent global subscription or attaches to an active one and runs recovery passes. It does
> not reactivate a retired subscription; use explicit reactivation below. An already active
> subscription continues to select durable producer routing after the switch is turned off until
> explicitly retired.
> Inspect consumers with `li state terminal-consumer list` (or `list --machine`); the
> listing includes name, registered_at, retired_at, and owed count. Command help names `list`
> so operators can inspect before making changes. `li state terminal-consumer retire <name>`
> retires the named consumer and ends its retention obligation; existing acknowledgements are
> retained and later producers can use legacy routing. Reactivate with
> `li state terminal-consumer reactivate <name> --start now` or `--start all`; `--start` is
> required, `now` baselines currently owed history, and `all` leaves it owed. Both mutations
> show the owed count and the changes; either accepts `--dry-run`, which performs no writes.
> These commands and their help are planned implementation, not currently shipped.
> The global subscription is independent of project or per-run notifications. Project
> `enabled: false` and `--notify` cannot suppress or satisfy the global subscription. Use
> global kind/ID filters to limit global deliveries, or retire the consumer explicitly. If two
> subscriptions target the same destination, that destination may receive both notifications.
> While global notifications are explicitly disabled, processing an owed event acknowledges it
> as not applicable even if unused adapter/filter fields are malformed; that ACK is irreversible,
> and re-enabling notifications does not replay the acknowledged event. Upgrade all producers before
> enrollment. Delivery can repeat after a crash or mixed-version rollout; use an idempotent
> receiver keyed by event ID where duplicate effects matter.

This is proposed user-facing configuration/help text for implementation, not a claim that
the help or product behavior has already changed. The off-by-default switch controls new
enrollment and daemon passes; disabling a switch alone does not retire an existing subscription.

### Verification required with implementation

The following six fixture families are required with the implementation, not a claim that any
was run for this draft. Use actual routing and composition roots and real temporary stores;
substitute only the notifier and explicit fault seams. Real-process and backend tests require
their own authorized environment. Update the comment beside
`lionagi/studio/config.py`'s `LIONAGI_STUDIO_TERMINAL_RECONCILE` switch and the user-facing
command/help surfaces when the implementation lands; the current comment describes the old
push/replay duplication and is not evidence that this amendment is already implemented.

1. **Routing and latency:** CLI and Studio, enrolled and unenrolled, actual settings bootstrap,
   inherited-global wrapper and operator force-transition; producer exact-event dispatch without
   waiting for a backlog, no second ordinary replay, and old behavior when not enrolled. Measure
   warm no-op p95 commit-to-handler entry overhead against legacy, proposed <=50 ms on local
   SQLite and PostgreSQL; report actual results.
2. **Enrollment and commit boundaries:** `now` baselines without effects, `all` leaves owed,
   active reattachment never re-baselines, and an older-timestamp late commit remains owed.
   Before-source-commit failure leaves no event; a producer crash after covered commit recovers
   through actual Studio startup. Include startup-reaper ordering, mixed old/new producers,
   and independently exercise transactional schedules on SQLite and PostgreSQL.
3. **Outcomes and policy:** Python return/False/error/cancellation, exec zero/nonzero/spawn
   failure/timeout, invalid versus disabled and filtered settings, absent/ambiguous context,
   live settings changes between immediate attempt and replay, and exact ACK/reason counts.
   Independent project/per-run subscriptions cannot suppress or satisfy durable global work;
   inherited-global wrappers must not send twice. Verify same-destination distinct subscriptions,
   intentional disabled ACK/drop and no revival on re-enable, source state integrity, and typed
   refusal for compatibility and unmigrated stores. Cover a session-less invocation, play with
   no linked session, schedule run with no invocation, a linked session with NULL project, and
   an invocation with ambiguous projects: for each eligible case, with an active consumer,
   enabled and matching valid global configuration, available capacity and a successful fake
   sink, global delivery occurs; absent/unknown/ambiguous context increments diagnostics and
   never itself ACKs. Test raw
   project declarations: absent, filter-only, disabled-only, full adapter, partial/invalid
   adapter and explicit per-run override; global, legacy precedence, destination count and
   serialized run/global outcomes must match the classifier above; a selected project-own-adapter
   subscription occupies the `run` result. For `durable=false` fallback
   before source commit, with no scoped override, assert one legacy global attempt and no ACK;
   for `--no-persist` with no override, one legacy global attempt and no ACK. Using identical
   registrations in legacy and durable modes, verify non-durable events: no scope gives one
   global attempt; `--notify` gives one scoped attempt and zero global; an inherited-global
   wrapper gives one wrapper attempt, zero bare-global attempts and its run outcome; a project
   override gives one project attempt and zero global. Contrast committed `durable=true`
   eligible events with active enrollment: global consumer delivery remains independent of
   explicit scoped/project subscriptions, and inherited-global wrappers do not send a second
   global notification. An ambiguous commit may produce up to two attempts and
   one durable ACK; no synthetic event may ACK a committed identity. Exercise the five global
   settings controls: false plus malformed adapter, false plus malformed filter, non-boolean
   enabled, true plus malformed adapter, and absent enabled plus malformed filter; the first two
   ACK not applicable, and the remaining three stay owed. Test both subscription completion
   orders and each mixed success/failure combination, deterministic run-first failure fields,
   0600 global stderr, and legacy/MCP consumers of backward-compatible top-level fields.
   Bootstrap disabled then enable exec and test success and failure; in a separate run,
   bootstrap enabled then disable before an attempt and assert not-applicable with no outcome file. Create the recorder at run start, write no file
   without an attempt, and never attach it to another run's terminal entity or a replay.
4. **Effect and ACK failure:** an actual producer exits after effect and before ACK, then actual
   recovery retries the same event ID. A durable idempotent sink applies one effect while a
   non-idempotent control may apply two. Verify ACK transaction failure leaves debt owed and
   an ambiguous response after committed ACK is resolved by rereading the ledger. Cancel the
   immediate attempt while ACK is blocked before and after database commit: re-raise caller
   cancellation, preserve source status, settle/observe any still-running operation under its
   admission owner, and use the ledger to decide whether a retry is owed. Assert deadline and
   cancellation behavior rather than claiming an unconditional hard wall-time bound.
5. **Concurrency and retirement:** race exact-event and replay admission in one process and
   in independent processes. Same-process admission rechecks ACK and prevents avoidable
   overlap; cross-process repeats are permitted. Retirement before admission blocks a new
   attempt; retirement after admission permits completion/late ACK. Switch-off does not
   silently retire or restore legacy routing; reactivation honors its explicit baseline choice.
   Exercise proposed `li state terminal-consumer list` and `list --machine`, validating name,
   registered_at, retired_at and owed count; help must point to list for inspection. Exercise
   `retire <name>` and `reactivate <name> --start now` / `--start all`, with and without
   `--dry-run`: verify both mutations show owed count and changes, dry-run performs no writes,
   and reactivation rejects missing `--start`. At bootstrap, allow synchronous legacy
   registration but select active/inactive/unknown routing for committed `durable=true` events
   only after the async store check; verify the switch enrolls absent, attaches active, and does
   not reactivate retired. Prove an override-filtered legacy callback cannot suppress the global
   consumer, and a mode read failure never invents an active enrollment or invokes inherited-global
   fallback for that durable event; non-durable emit keeps its existing routing.
6. **Bounds and fairness:** timed-out sync work has no immediate ACK or same-process duplicate;
   late success ACKs and late failure remains owed. At abandoned-worker capacity both triggers
   report saturation; release restores progress. Check cooling fairness, cursor wrap, older
   late commits, restart/backoff reset without erasing debt, and one shared post-commit budget.
