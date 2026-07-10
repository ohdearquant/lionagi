# ADR-0069: Reactive flow steering and recovery

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: scheduling-control-plane
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0085, v0-0088

## Context

`li play` is command sugar for `li o flow -p <name>`, so playbook runs and planned reactive
flows use the same planning engine, dependency-aware executor, live control poller, and checkpoint
writer. The executor may accept `SpawnRequest` values when reactive expansion is enabled, subject
to a role filter and operation budget.

Live control and cross-process recovery are separate mechanisms. Live control writes
`session_controls` rows from one process and requires a poller plus in-memory executor in the
original process. Recovery writes `checkpoint.json` into the run directory and reconstructs a new
executor in a later process. Neither mechanism is a durable job queue or a universal runner handle.

This ADR answers five concrete problems:

- **P1 — A separate CLI process needs to steer a live executor.** It cannot hold an in-memory
  Python reference, so requests need a durable transport and a single consumer.
- **P2 — Pause cannot safely interrupt an arbitrary provider call.** The implemented safe boundary
  is immediately before an operation acquires execution capacity and invokes its provider.
- **P3 — Operator messages can be duplicated or missed.** Message application and DB stamping have
  different crash semantics from idempotent pause/resume, and rendering must happen before an
  operation's last provider-call boundary.
- **P4 — Process death loses the in-memory DAG.** Planned topology, results, context, and
  configuration need an independently readable checkpoint.
- **P5 — Reactive topology and branch conversation are not reconstructible from the current
  checkpoint.** Resume must refuse or explicitly degrade rather than call a partial replay exact.

| Concern | Decision |
|---|---|
| Control addressing and transport | D1: Queue ordered `pause`, `resume`, and `message` rows only for live flow/play sessions. |
| Cooperative pause semantics | D2: Gate new operation starts at the executor boundary; never preempt an in-flight provider call. |
| Context-mode steering | D3: Apply messages at most once to shared flow context and render them once into a pending operation instruction. |
| Checkpoint persistence | D4: Atomically replace a versioned full-state JSON checkpoint after operation outcomes. |
| Resume and reactive-growth bounds | D5: Replay the recorded plan without planning; refuse spawned topology and gate degraded inherited context. |

Out of scope:

- Terminal cancellation is not shipped; `stop` is reserved in the table but has no CLI producer or
  executor consumer.
- Operation-node message injection is not shipped; only context-mode `msg` exists.
- Mid-provider interruption, provider fallback, usage quotas, and model changes are not control
  verbs.
- Fixed WorkflowDef and scheduled-run controls are not implied; those paths do not run this poller.
- Checkpoints are not queue leases, worker heartbeats, or exactly-once job persistence.
- Full filesystem crash durability is not claimed: replacement is atomic to readers, but the
  current writer does not issue an explicit file or directory `fsync`.

## Decision

### D1 — Ordered session-control rows are the live transport

The public writers are:

```python
# lionagi/cli/orchestrate/_control.py
def run_ctl_pause(args: argparse.Namespace) -> int: ...
def run_ctl_resume(args: argparse.Namespace) -> int: ...
def run_ctl_msg(args: argparse.Namespace) -> int: ...

async def _enqueue_control_inner(
    *, entity_id: str, verb: str, payload: dict[str, Any] | None
) -> tuple[str, int]: ...
```

Their persisted contract is:

```sql
-- lionagi/state/schema.sql
CREATE TABLE session_controls (
  id          TEXT PRIMARY KEY,
  session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  verb        TEXT NOT NULL CHECK(verb IN ('pause', 'resume', 'message', 'stop')),
  payload     JSON,
  created_at  REAL NOT NULL,
  applied_at  REAL,
  result      TEXT
);

CREATE INDEX idx_session_controls_pending
  ON session_controls(session_id, applied_at) WHERE applied_at IS NULL;
```

Publicly accepted verbs are `pause`, `resume`, and `message`. `stop` is schema-reserved and is
rejected by the current poller as unsupported if inserted by another caller. Pause and resume use
`payload=NULL`; message uses `{"text": <string>}`. `StateDB.insert_session_control()` creates a
UUID4-hex id, a current epoch timestamp, and leaves `applied_at` and `result` null.

Exact addressing and queue semantics:

- The CLI accepts a session, invocation, or play id, or a short prefix.
- Prefix search is ordered by `sessions`, then `invocations`, then `plays`. Two matches in the
  first matching table are rejected as ambiguous; a full-length id skips the prefix ambiguity
  check.
- A missing state database, unknown id, non-`running` session, or invocation kind outside
  `{"flow", "play"}` fails without inserting a row.
- The insert is bounded by `_DB_BUSY_TIMEOUT_S = 10.0`; timeout reports a busy database. The value
  mirrors the status surface and prevents an unbounded CLI hang; no empirical rationale for ten
  seconds is recorded in source.
- Success means only “queued.” The message says application is expected within roughly two
  seconds, but the writer does not wait for application.
- The poller wakes every `_CONTROL_POLL_INTERVAL = 2.0` seconds. This is the control latency floor
  when the event loop is healthy; the source records no measured rationale for the exact value.
- Pending rows are read oldest-first by `(created_at, id)`. The id tie-break makes rapid enqueues
  deterministic.
- A transient list failure is retried on the next tick and does not fail the run.
- If a successful effect cannot be terminally stamped, the poller stops that tick so a later
  control cannot overtake the unstamped row.

Terminal `result` values are `applied`, `applying`, or `rejected:<reason>`. The poller records a
compact control log in session `node_metadata`, but `session_controls` remains the authoritative
request/application row.

Why this way: a database row lets a separate CLI process address a live flow without a process-wide
handle registry. Restricting insertion to running flow/play sessions prevents controls from sitting
pending forever against runners with no consumer.

Code anchors: `lionagi/cli/orchestrate/_control.py`; `lionagi/cli/orchestrate/flow.py`
(`_control_poll_loop`, `_apply_session_control`); `lionagi/state/db.py` (session-control methods);
`lionagi/state/schema.sql`.

### D2 — Pause and resume are idempotent operation-boundary effects

The executor contract is:

```python
# lionagi/operations/flow.py
class DependencyAwareExecutor:
    def pause(self) -> None: ...
    def resume(self) -> None: ...

    async def _execute_operation(
        self, operation: Operation, limiter: CapacityLimiter
    ): ...
```

`pause()` installs an unset `ConcurrencyEvent` only when no pause event exists. `resume()` sets the
current event and clears the reference; calling resume while unpaused is a no-op. Before a ready
operation enters the concurrency limiter, `_execute_operation()` loops while a pause event exists,
emits `NodePaused`, and waits for that particular event.

Exact semantics:

- **Pause twice:** both controls apply; the second call retains the same gate. There is no nested
  pause count.
- **Resume twice:** the first releases and clears the gate; the second is a no-op.
- **Resume followed by pause:** a fresh event is installed, so operations reaching the boundary
  after the later pause wait on the new gate.
- **Operation already past the boundary:** it continues through `operation.invoke()`; pause never
  cancels or interrupts its provider/tool activity.
- **Operation waiting on dependencies:** it encounters the gate only after edge checks and
  dependency completion.
- **Skipped or already-terminal operation:** it completes its executor bookkeeping without waiting
  on the pause gate.
- **Run exits while paused:** the poller dies with the executor; queued future controls have no
  consumer. The current code has no terminal cancel-on-pause behavior.
- **Poller crash after effect but before stamp:** pause/resume are safe to reapply. `_finalize_applied`
  retries the terminal stamp twice, then leaves the row pending for a later tick.

The latency bound is cooperative, not real-time: control-poll delay plus time until an operation
reaches the boundary. A long provider call can finish after pause acknowledgement because it was
already in flight.

Why this way: an operation boundary has clear state and does not require provider-specific
cancellation guarantees. The tradeoff is latency; immediate preemption would need compensating
semantics for tools, partial provider streams, and branch persistence that the current executor
does not define.

### D3 — Operator messages use at-most-once context steering

The persisted input is:

```json
{"verb": "message", "payload": {"text": "operator correction"}}
```

The applied workspace entry is:

```python
{"ts": time.time(), "text": payload.get("text", "")}
```

Unrendered entries become one instruction prefix:

```text
[OPERATOR STEER]
A human operator sent these live corrections while this flow is running.
Attend to them before continuing. Most recent last.
- <UTC timestamp>: <text>
[/OPERATOR STEER]

<original instruction>
```

The apply sequence differs deliberately from pause/resume:

1. If the row already has `result='applying'`, leave it untouched.
2. Stamp `result='applying'` while `applied_at` remains null.
3. Confirm that at least one Operation in the current graph still has `PENDING` status.
4. Append the entry to `executor.context.content["operator_messages"]`.
5. Finalize the row as `applied`.

Exact semantics:

- **No pending operation:** finalize as `rejected:no-pending-ops`; completed/in-flight operations
  are not rewritten.
- **Unknown verb:** finalize as `rejected:unsupported-verb:<verb>`.
- **Apply exception:** finalize as `rejected:error:<message>` truncated to 500 characters. A failure
  to finalize returns the unstamped sentinel and stops later controls for the tick.
- **Crash before `applying`:** the next poll may try normally.
- **Crash after `applying`:** the row remains visibly mid-apply and is never reapplied
  automatically. This chooses at-most-once delivery over possible duplication; it can lose a
  message whose effect had not landed.
- **Rendering:** `_prepare_operation()` copies shared context and renders pending entries at
  dependency-render time. `_render_pending_operator_steers()` checks the canonical shared queue
  again immediately before `operation.invoke()` to close the window where a message arrives after
  preparation.
- **Consume once:** rendered entries receive `rendered_into_op=<operation-id>` and are skipped by
  later operations. The `operator_messages` key is removed from the per-operation context so raw
  control JSON is not separately sent to the model.
- **Several pending messages:** they are rendered together in stored order, with the most recent
  last.
- **Empty text:** the current payload accessor produces an empty list item; the CLI requires a text
  argument but the poller itself does not reject an empty string.

Why this way: context steering changes the next usable instruction without mutating graph topology.
Stamp-before-apply is the honest crash choice for a non-idempotent append. Operation-node injection
was not taken because it would require graph placement, dependencies, branch selection, and
checkpoint replay semantics that do not exist.

Code anchor: `lionagi/operations/flow.py` (`_render_operator_messages`,
`_render_pending_operator_steers`); `lionagi/cli/orchestrate/flow.py`
(`_apply_session_control`).

### D4 — Checkpoints are full JSON snapshots replaced atomically

The writer is a run-local dataclass:

```python
# lionagi/cli/orchestrate/_checkpoint.py
CHECKPOINT_VERSION = 1

@dataclass
class CheckpointWriter:
    path: Path
    session_id: str
    prompt: str
    plan: list[dict]
    config: dict[str, Any]
    flow_context: dict[str, Any] = field(default_factory=dict)
    ops: dict[str, dict[str, Any]] = field(default_factory=dict)
    spawned: list[dict] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)
    _seq: int = field(default=0, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]: ...
    async def record(
        self,
        agent_id: str,
        *,
        status: str,
        response: Any,
        flow_context: dict[str, Any] | None = None,
    ) -> None: ...
    async def record_spawned(
        self,
        node_id: str,
        *,
        status: str,
        response: Any,
        flow_context: dict[str, Any] | None = None,
    ) -> None: ...
```

Wire shape:

```json
{
  "version": 1,
  "session_id": "...",
  "prompt": "...",
  "plan": [{"agent_id": "...", "dep_indices": [], "assignee": "..."}],
  "flow_context": {},
  "ops": {
    "planned-agent-id": {
      "agent_id": "planned-agent-id",
      "status": "completed|failed",
      "response": "..."
    }
  },
  "spawned": [
    {"node_id": "operation-uuid", "status": "completed|failed", "response": "..."}
  ],
  "config": {"model_spec": "...", "reactive_spec": "...", "max_ops": 0}
}
```

Exact persistence semantics:

- A writer is created only when checkpoint configuration is provided. Fresh and resumed CLI flows
  construct one; unit seams that omit configuration do not.
- `flush()` writes an initial snapshot before execution.
- Planned nodes key `ops` by stable agent id. Spawned nodes use their operation UUID in a separate
  list so a cloned branch name cannot overwrite a planned entry.
- Re-recording a spawned node replaces the matching list entry; otherwise it appends.
- Completion observers schedule writes, and `_execute_dag()` awaits all scheduled checkpoint tasks
  before returning.
- The `asyncio.Lock` serializes full snapshots. Each write uses a unique
  `checkpoint.<sequence>.tmp`, serializes with `json.dumps(..., default=str)`, then calls
  `os.replace(tmp, checkpoint.json)`. A reader therefore sees the old or new complete JSON file,
  not a partially overwritten target.
- The latest operation completion replaces the shared `flow_context` snapshot. It is an
  accumulation, not per-operation history.
- Serialization failures leave the previous target in place; the current caller suppresses some
  checkpoint task exceptions at drain time, so checkpoint freshness is not guaranteed when a
  write fails.
- `load_checkpoint()` performs `json.loads(path.read_text())`. The current resume path does not
  reject an unsupported `version` field explicitly; version 1 is descriptive, not a complete
  migration gate.

`CHECKPOINT_VERSION = 1` marks the initial shipped shape. No rationale beyond initial format
versioning is recorded.

Why this way: full snapshots are simple to inspect and avoid a partial event-log replay protocol.
Atomic replacement protects readers from torn target files. The cost is write amplification after
every completion and the lack of fsync-level durability or schema migration enforcement.

### D5 — Resume replays the original plan and refuses unfaithful topology

Resolution and replay contracts are:

```python
async def resolve_checkpoint_target(target: str) -> tuple[RunDir, dict[str, Any]]: ...

def _apply_checkpoint_precompletion(
    env: OrchestrationEnv,
    plan_result: _PlanResult,
    dag_state: _DagState,
    checkpoint_ops: dict[str, dict],
    *,
    allow_degraded_context: bool,
    checkpoint_spawned: list[dict] | None = None,
) -> None: ...

async def _resume_flow(
    target: str,
    *,
    allow_degraded_context: bool = False,
    dry_run: bool = False,
    show_graph: bool = False,
    notify: str | None = None,
) -> tuple[str, str]: ...
```

Exact resolution semantics:

- An exact run directory is preferred. Otherwise `_find_run_dir_by_id()` chooses the most recently
  modified directory matching the supplied prefix; it does not report multiple prefix matches.
- If no run directory checkpoint is found, the target is resolved through session/invocation/play
  storage, then through the session's `node_metadata.run_id`.
- Missing target, missing backing session, missing run id, or missing `checkpoint.json` raises
  `FlowResumeError`.
- `dry_run`, `show_graph`, and an explicit notify override come from the new invocation. Model,
  prompt, playbook, operation cap, reactive policy, and other execution settings come from the
  checkpoint.

Exact replay semantics:

- The persisted `plan` is reconstructed directly; the planner is not called.
- An empty plan is rejected.
- Previously completed nodes are pre-marked `COMPLETED` with their response. Previously failed
  nodes are pre-marked `FAILED`; resume does not guess that retrying their possibly side-effecting
  operation is safe.
- Pending nodes run normally and receive restored shared result context.
- If any pending node requested `inherit_context`, resume refuses because predecessor conversation
  messages are not restored. `--allow-degraded-context` permits those named nodes to run with an
  empty branch while retaining result context.
- Any non-empty `spawned` checkpoint list refuses resume. The current writer records spawned
  outcomes, but the plan does not contain their position or branch inheritance, so replay would
  silently drop work.
- A resumed run writes its own checkpoint and records `resumed_from=<original-session-id>`.

Reactive growth itself is bounded by the live executor:

```python
async def flow(
    session: Session,
    graph: Graph,
    *,
    reactive: bool = False,
    spawn_type: type | None = None,
    node_builder: Any = None,
    max_spawn: int = 50,
    executor_ref: dict[str, Any] | None = None,
    ...,
) -> dict[str, Any]: ...
```

The CLI parses `--reactive off` as disabled, `all`/truthy spellings as all roles, and any other
comma-separated value as an allowed role set. When `--max-ops N` is positive, initial planned
nodes and spawns share the cap: `max_spawn=max(0, N-len(plan))`. Without a cap the CLI uses a
conservative default of 20 spawned nodes to bound cost; the generic executor default remains 50.
The CLI source explains the 20-node value only as a conservative guard against costly uncapped
fan-out; no measured tuning record exists. Plans above 200 assignments are truncated to 200 as a
runaway-planner defense; the numeric threshold likewise has no recorded empirical rationale.

Spawn attempts reject and record `builder_error`, `null_child`, `cycle`,
`max_spawn_exceeded`, or `duplicate`. Accepted nodes receive a branch clone, an optional dependency
edge from the emitter, and a `NodeSpawned` signal. These live semantics do not make their topology
recoverable by the version-1 checkpoint.

Why this way: replaying a stable planner output avoids paying for or drifting through a second
plan. Refusal is preferable to silently dropping spawned work or conversational inheritance. The
explicit degraded flag makes the one supported fidelity loss visible to the caller.

## Consequences

- A separate CLI process can steer a live flow through the database without an in-process handle
  registry.
- Pause is safe at operation boundaries but can lag behind long in-flight calls.
- Context steering is at-most-once; a crash in the `applying` window may lose a message and leaves a
  visible row requiring diagnosis.
- Full checkpoint snapshots are inspectable and atomically visible, but are not a transactional log
  and do not guarantee fsync-level persistence.
- Resume is planner-free and preserves completed/failed outcomes, but refuses checkpoints containing
  reactive spawn outcomes and requires an explicit flag to lose inherited conversation state.
- Contributors extending control must understand verb-specific apply/stamp ordering; treating a
  message like idempotent pause/resume would introduce duplicate steering.
- Reversing D1-D3 requires a new control transport and compatibility for pending rows. D4's JSON
  format can be versioned, while D5's refusal rules may be relaxed only when the missing state is
  actually persisted and replayed.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|---|---|---|
| 1 | Add a terminal reactive-flow cancel control; acceptance: after acknowledgement, no new operation starts and the run persists a cancelled terminal state. | M | (filled at issue-open time) |
| 2 | Replay reactively spawned nodes from checkpoints or reject affected checkpoints before execution; acceptance: resume never returns a partially replayed spawned DAG as successful recovery. | M | (filled at issue-open time) |
| 3 | Persist predecessor conversation state needed by `inherit_context`; acceptance: eligible resumed operations receive the same inherited conversation without requiring degraded mode. | L | (filled at issue-open time) |
| 4 | Enforce checkpoint-version compatibility and report unsupported versions before graph construction; acceptance: a checkpoint with an unknown version fails with a typed migration/compatibility error. | S | (filled at issue-open time) |
| 5 | Add operator recovery for controls stranded in `applying`; acceptance: status distinguishes definitely-applied, definitely-not-applied, and indeterminate messages without automatic duplicate injection. | M | (filled at issue-open time) |

## Alternatives considered

### Process-local runner handle registry

A registry could map run ids to executor objects and make pause/resume direct method calls. It
would buy lower latency and simpler stamping, but a separate `li` process could not reach it
without another IPC service, and a restart would erase every handle. It lost because the shipped
control surface is explicitly cross-process.

### Mid-provider cancellation

Cancelling the coroutine immediately would buy faster stop/pause response. It lost because provider
calls and tool actions may have external side effects and no uniform compensation contract. The
operation boundary is the implemented point where “has not started” is unambiguous.

### Operation-node message injection

A message could become a first-class graph node with its own branch and dependencies. That would
buy topology-level observability and targeted placement. It lost because the current design has no
contract for where to attach the node, how to replay it, or how it should inherit context. Context
mode meets the steering need without pretending those questions are solved.

### Apply message then stamp

This ordering would avoid messages stranded in `applying`. It lost because a crash after append but
before stamp would cause the next poll to append the same instruction again. The chosen
stamp-then-apply order accepts a visible at-most-once loss window instead of silent duplication.

### Incremental checkpoint event log

Appending one event per completion would reduce full-file write amplification and could support
more exact replay. It lost for the current implementation because it needs log framing,
compaction, corruption recovery, schema migration, and a deterministic fold. A full JSON snapshot
is much smaller operational machinery for current flow sizes.

### Replan on resume

Calling the planner again could reconstruct a complete graph without storing as much topology. It
lost because planner output is not deterministic: dependencies, roles, and node count could change,
invalidating completed results and provenance.

### Silently skip unsupported spawned nodes or inherited conversation

This would maximize the number of checkpoints that appear resumable. It lost because the resulting
run is not faithful. The code now refuses spawned checkpoints and requires an explicit degraded
flag for inherited conversation.

### Use the durable task queue as the live-control channel

Controls could be tiny queue jobs, buying one generic transport. It lost because task admission
and executor-local steering have different ownership and terminal semantics. A worker claiming a
pause job would still need the live executor reference this design already exposes directly to the
poller.

## Notes

The schema reserves `stop`, but reservation is not implementation. Current public vocabulary is
only `pause | resume | message`. Checkpoint recovery is a reactive-flow feature and must not be
described as durable task persistence.
