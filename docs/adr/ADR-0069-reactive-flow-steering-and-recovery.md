# ADR-0069: Reactive flow steering and recovery

- **Status**: Accepted, amended 2026-10-16 (see Amendment 1)
- **Kind**: Retrospective
- **Area**: scheduling-control-plane
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0085, v0-0088; amended by ADR-0108, which widens the
  `message` verb's consumer set to the `agent` invocation kind, so this document's
  rule that an invocation kind outside `{"flow", "play"}` fails without inserting a
  row no longer holds for that verb, and widens the terminal `result` vocabulary
  below, which is no longer a closed list of three; amended by Amendment 1, which
  records that today's resume pre-marks a recorded `completed` node from its stored
  `status` alone, and files the settle check that would narrow that as delta rows 2,
  6, 7, 8, and 9

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
| Resume and reactive-growth bounds | D5: Replay the recorded plan without planning; rebuild checkpointed spawn entries that carry their reconstruction fields, refuse the whole resume on any entry that cannot, and gate degraded inherited context. |

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

The public writers that create rows are:

```python
# lionagi/cli/orchestrate/_control.py
def run_ctl_pause(args: argparse.Namespace) -> int: ...
def run_ctl_resume(args: argparse.Namespace) -> int: ...
def run_ctl_msg(args: argparse.Namespace) -> int: ...

async def _enqueue_control_inner(
    *, entity_id: str, verb: str, payload: dict[str, Any] | None
) -> tuple[str, int]: ...
```

One further public writer does not create rows but closes them, and a reader who takes the
list above as the whole set will not find it:

```python
# lionagi/cli/orchestrate/_control.py
def run_ctl_resolve(args: argparse.Namespace) -> int: ...
```

It is how a person records the outcome of a control whose claimant never came back. It is
covered in the result-vocabulary table below and in ADR-0108; it is named here because this
is the section a reader consults for the set of writers.

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
  **Amended by ADR-0108:** the `message` verb also accepts the `agent` kind, so the
  invocation-kind half of this rule no longer holds for it. `pause` and `resume` are
  unchanged. This marker is here rather than only in the header because a reader who
  reaches this rule by search or cross-reference never passes the header.
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

Terminal `result` values are `applied` or `rejected:<reason>`. `applying` is not one of them:
it is a claim, written to say a consumer has taken the row and not yet reported an outcome, and
a row still carrying it is a row whose result nobody knows. The poller records a
compact control log in session `node_metadata`, but `session_controls` remains the authoritative
request/application row.

**This list is no longer closed — ADR-0108 extends it, and a reader checking equality against
the three values above will miss rows.** The current stored forms, taken from the code rather
than from this list:

| form | written by |
|---|---|
| `applying` | a claimant that named no owner |
| `applying:<owner>` | a claimant that named one, so a second consumer can tell "someone else holds this" from "I hold this" |
| `applied` | an ordinary successful apply |
| `rejected:no-pending-ops`, `rejected:unsupported-verb:<verb>`, `rejected:error:<exc>` | the flow poller |
| `rejected: <sentence>` | run teardown, prose after the colon rather than a token |
| `<applied\|abandoned>: resolved by <who> after the claim '<prior>' was taken and never reported back` | `li o ctl resolve`, a person closing a control whose claimant never came back |

Two consequences for anyone reading this column. **Match by prefix, not equality**: every
consumer of the claim check in the tree uses `startswith("applying")` for exactly this reason,
and equality would stop matching the claims that identify who holds them. And `abandoned` is a
terminal outcome that this ADR never contemplated, reachable only through a human resolution.

Unrelated despite the name: `lionagi/state/lifecycle/` carries its own `result` with the values
`applied`, `rejected`, and `conflict`. That is the session-status transition service and it never
touches `session_controls`; the shared field name is a coincidence and the vocabularies are not
the same one.

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
CHECKPOINT_VERSION = 2

@dataclass
class CheckpointWriter:
    path: Path
    session_id: str
    prompt: str
    plan: list[dict]
    config: dict[str, Any]
    max_spawn: int | None = None
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
        operation: str | None = None,
        assignee: str | None = None,
        instruction: str | None = None,
        parent_id: str | None = None,
        spawn_id: str | None = None,
        context: Any | None = None,
    ) -> None: ...
```

Wire shape:

```json
{
  "version": 2,
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
    {
      "node_id": "operation-uuid",
      "status": "completed|failed",
      "response": "...",
      "operation": "...",
      "assignee": "...",
      "instruction": "...",
      "parent_id": "...",
      "spawn_id": "...",
      "context": null
    }
  ],
  "config": {"model_spec": "...", "reactive_spec": "...", "max_ops": 0},
  "max_spawn": 20
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
  reject an unsupported `version` field explicitly; version 2 is descriptive, not a complete
  migration gate (`lionagi/cli/orchestrate/_checkpoint.py:165`).

`CHECKPOINT_VERSION = 2` (`lionagi/cli/orchestrate/_checkpoint.py:29`) is the current shape: 2 adds
the spawn-reconstruction fields resume reads (`operation`, `assignee`, `instruction`, `parent_id`,
`spawn_id`, and the later `context`) that `record_spawned` writes
(`lionagi/cli/orchestrate/_checkpoint.py:92`). No rationale beyond initial format versioning is
recorded.

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
    retry_failed: bool = False,
    checkpoint_spawned: list[dict] | None = None,
) -> None: ...

async def _resume_flow(
    target: str,
    *,
    allow_degraded_context: bool = False,
    retry_failed: bool = False,
    dry_run: bool = False,
    show_graph: bool = False,
    notify: str | None = None,
) -> tuple[str, str]: ...
```

Exact resolution semantics:

- An exact run directory is preferred. Otherwise `_find_run_dir_by_id()` resolves an unambiguous
  prefix of the run id; more than one directory matching the prefix raises `AmbiguousIdError`
  (`lionagi/cli/orchestrate/_checkpoint.py:187-188`).
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
  nodes are not: a checkpoint that records one refuses the resume, because replaying a failed node
  as terminal skips it and everything downstream, so resuming would finish nothing
  (`lionagi/cli/orchestrate/flow.py:1418-1426`). `--retry-failed` runs the failed nodes again
  instead, which resume does not do silently because it re-executes whatever side effects the first
  attempt already had.
  **Amended by Amendment 1:** this pre-marking is from the recorded `status` alone — the settled
  node is not checked against what it produced — and Amendment 1 records that as today's behaviour
  while filing the settle check as delta row 2.
- Pending nodes run normally and receive restored shared result context.
- If any pending node requested `inherit_context`, resume refuses because predecessor conversation
  messages are not restored. `--allow-degraded-context` permits those named nodes to run with an
  empty branch while retaining result context.
- A checkpoint `spawned` list whose entries can all be rebuilt is reconstructed into the fresh
  graph rather than refused as a whole. Refusal is whole-resume, not per-entry:
  `_reconstruct_spawned_nodes` (`lionagi/cli/orchestrate/flow.py:1206-1214`) runs before any spawned node is
  added by reconstruction, and each of its five checks raises `FlowResumeError` for the whole resume when any single
  entry fails: (1) the entry must carry `operation` (`CHECKPOINT_VERSION` 2+; a pre-2 entry carries
  nothing to rebuild from, `lionagi/cli/orchestrate/flow.py:1232-1240`); (2) its `status` must be
  `completed` or `failed` (`lionagi/cli/orchestrate/flow.py:1242-1250`); (3) an entry with an `assignee`
  must also carry `spawn_id` (`lionagi/cli/orchestrate/flow.py:1252-1262`); (4) under `--retry-failed`, a
  failed entry naming no assignee must not record the class of the worker that asked for it, or,
  for an escalation retry, the class it ran at (`lionagi/cli/orchestrate/flow.py:1267-1283`,
  ADR-0133); (5) a `parent_id` must itself
  be checkpointed terminal or another entry in the same list
  (`lionagi/cli/orchestrate/flow.py:1285-1308`). One failing entry refuses the resume, naming that
  entry's node, and no entry is rebuilt. The rule is therefore "no spawned entry is replayed unless
  every spawned entry is soundly rebuilt".
  **Amended by Amendment 1:** that is what the code does today, and Amendment 1 records it as such
  rather than as a target; a resume does not yet check a replayed node against the result it
  recorded.
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
edge from the emitter, and a `NodeSpawned` signal. These live semantics are recoverable only to the
extent the version-2 checkpoint records them: `record_spawned` stamps the reconstruction fields
above (`lionagi/cli/orchestrate/_checkpoint.py:92`), and a spawned entry that lacks them refuses the
resume (see Amendment 1).

Why this way: replaying a stable planner output avoids paying for or drifting through a second
plan. Refusal is preferable to silently dropping spawned work or conversational inheritance. The
explicit degraded flag makes the one supported fidelity loss visible to the caller.

## Amendment 1 (2026-10-16) — what a recorded `completed` node is worth to a resume

**What the code does today.** A resume trusts a recorded `completed` status by itself. The planned
path reads the checkpoint entry and pre-marks on the stored status with no other check:

```python
# lionagi/cli/orchestrate/flow.py:1454-1457
entry = checkpoint_ops.get(agent_id)
if entry and entry.get("status") == "completed":
    node.execution.status = EventStatus.COMPLETED
    node.execution.response = entry.get("response")
```

The spawned path pre-marks the same way, from `entry["status"] == "completed"`
(`lionagi/cli/orchestrate/flow.py:1334-1336`). The executor then admits an already-completed
operation and stores its response as that node's result (`lionagi/operations/flow.py:241-244`), and
dependents read the stored value into their own context (`lionagi/operations/flow.py:939-956`).
Nothing on the replay path reads, re-checks, or hashes the result a dependent consumes.

**Why that is a gap.** A `completed` string in a JSON file is a claim about work rather than the
work itself. A checkpoint can be edited, can be left behind by a run whose artifacts were cleaned
up, or can name a node whose result never landed, and each of those replays as finished. The
recorded `response` is no better a settlement: a value that only round-tripped through serialization
proves nothing about whether the node's output still exists, and a resume must not derive a node's
state by reading the thing the check was supposed to be about. The one check a resume could make
against the artifact set a node declared, it does not make.

**The cases a resume does not answer today.**

- **A node that declared an artifact contract.** Its recorded `status` is replayed with no check of
  the declared set, and resume does not even use the contract the first run persisted: `_build_dag`
  re-folds each leg's profile or role `artifact_defaults` from the live roles at resume
  (`lionagi/cli/orchestrate/flow.py:1070-1081`), so removing a role's `artifact_defaults` between runs
  loses that leg's contract. The persisted record exists — `artifact_contract_json` is the column
  ADR-0064 D3 has a flow write
  (`docs/adr/ADR-0064-cli-execution-outcome-and-completion-record.md:412`; declared at
  `lionagi/state/schema.sql:192`). Two stages fill it: `_build_dag` resolves planned role declarations and persists them
  before execution (`lionagi/cli/orchestrate/flow.py:1080-1081,1173-1184`). Spawned instructions use the
  cached role declarations before injection, but `_execute_dag` appends their entries to the
  session contract only after the DAG finishes (`lionagi/cli/orchestrate/flow.py:2650,2652-2664`).
  Resume does not read either persisted set. The column is also nullable — the schema records that a NULL contract means verification
  is skipped (`lionagi/state/schema.sql:191`) — so an absent or unreadable record is not evidence
  that the declared set is still present. Delta row 2 covers both the read and the check.
- **A node that declared no artifact contract.** There is nothing independent to check, so it keeps
  today's status replay like any other completed node. The settle-check delta rows cover only nodes
  that declare artifacts; giving a contract-less completion an independent recorded identity is
  delta row 6.
- **Where the declared artifacts would be looked for.** The artifact root a resume reads is not the
  original run's. `resolve_checkpoint_target` returns the original run's `RunDir` with its
  `artifact_root` (`resolve_checkpoint_target` → `_find_run_dir_by_id`,
  `lionagi/cli/orchestrate/_checkpoint.py:169`), but `_resume_flow` keeps only the checkpoint dict
  and drops the run dir (`lionagi/cli/orchestrate/flow.py:3731-3732`), and `_run_flow` allocates a run
  again through `allocate_run`, whose root is chosen from `--save`: without `--save` the resumed
  run's `artifact_root` is a fresh directory, and with `--save` it is the path `--save` names. The
  path the resume passes comes from the replayed config, which carries the original `save_dir`
  (`lionagi/cli/orchestrate/flow.py:3046`); `allocate_run` uses `save_dir` as the artifact root when
  it is given and falls back to a fresh `<state_root>/artifacts` when it is not
  (`lionagi/cli/_runs.py:616-619`). Either way the root is not read from the original `RunDir` the
  resolver returned, so without `--save` every completed node's declared artifacts are absent from
  the new root, and with a `--save` naming a directory other than the original run's they are absent
  there too. Delta row 9 covers threading the original root.
- **What the live verifier proves.** Presence and a non-zero size, not content: it reads
  `os.path.getsize(full) > 0` (`lionagi/state/artifact_verifier.py:249`), and the currency check
  beside it compares mtime and size and says itself that this "narrows, but doesn't close" the
  window a rewrite preserving both leaves (`stale_artifact_markers`,
  `lionagi/state/artifact_verifier.py:299-309`). A byte-level check is delta row 7.
- **A declared artifact above the hash size limit.** `compute_hash` refuses a payload above
  `MAX_HASH_INPUT_BYTES = 10 * 1024 * 1024` by default (`lionagi/ln/_hash.py:254,300-301`), and
  ADR-0050 records that a caller hashing larger content either opts out with `max_bytes=None` or
  streams a file through `hash_file()` rather than reading it whole
  (`docs/adr/ADR-0050-foundational-utility-and-typed-adaptation-strata.md:882-885`).

**Builder notes for the rows.** A check that re-reads the declared set would read it through the
same verifier a live run uses (`verify_artifact_contract`, `lionagi/state/artifact_verifier.py:215`),
against the contract the run persisted rather than one re-derived from the live roles. The rows
build on decisions this ADR already carries: D4 keys planned nodes by stable `agent_id`
(`CheckpointWriter.record`, `lionagi/cli/orchestrate/_checkpoint.py:72`) and spawned nodes in a
separate list keyed by node id (`record_spawned`, `lionagi/cli/orchestrate/_checkpoint.py:92`), and
a leg's declared entries are namespaced under that leg's own id — the planned `agent_id`
(`lionagi/cli/orchestrate/flow.py:1080`), and for a reactively spawned leg its `spawn_id`, which
`role_node_builder` stamps (`lionagi/orchestration/patterns.py:164`), which the checkpoint stores
beside `node_id` (`CheckpointWriter.record_spawned`, `lionagi/cli/orchestrate/_checkpoint.py:124`),
and which is the key a spawned leg's artifact directory is built from
(`env.run.agent_artifact_dir(spawn_id)`, `lionagi/cli/orchestrate/flow.py:2249-2251`; the contract
entries likewise use it at `:2019` and `:2178`). A resume that looked up a spawned leg's artifacts
under its node id would find no directory at all.

The identity delta row 6 asks for is a digest recorded in the checkpoint entry when the node
completes: **field** `result_digest`, beside the entry's `status` and `response` — in a planned
entry written by `CheckpointWriter.record` (`lionagi/cli/orchestrate/_checkpoint.py:72`) and in a
spawned entry written by `record_spawned`
(`lionagi/cli/orchestrate/_checkpoint.py:92,124`). It would be taken with the
BLAKE-family hash the tree already uses for recorded content — `blake2b`
(`lionagi/cli/orchestrate/_manifest.py:333`; ADR-0110 D1 records a "BLAKE-family content hash",
`docs/adr/ADR-0110-deterministic-manifest-fanout.md:119`) — not `compute_hash`'s SHA-256 default
(`lionagi/ln/_hash.py:279-285`). A digest kept in the same file as the entry it describes lives
inside that file's trust boundary: it detects an accidental change — an artifact overwritten or
truncated with the checkpoint not following — and not a deliberate edit, which can rewrite the
digest to match.

**Spawned replay today, and the `--retry-failed` trade.** A checkpoint `spawned` list whose entries
can all be rebuilt is reconstructed into the fresh graph rather than refused as a whole:
`_reconstruct_spawned_nodes` (`lionagi/cli/orchestrate/flow.py:1206-1214`) runs before any spawned node is added by reconstruction,
and each of its five checks refuses the whole resume, not just the failing entry, when any single
entry fails — `operation` present (`lionagi/cli/orchestrate/flow.py:1232-1240`, raise at `1235`), `status`
in `completed|failed` (`:1242-1250`, raise at `1246`), `assignee` paired with a `spawn_id` (`:1252-1262`,
raise at `1256`), under `--retry-failed` no failed entry naming no assignee that records the class of
the worker that asked for it (`:1267-1283`, raise at `1276`; ADR-0133), and a `parent_id` that is
itself checkpointed terminal or another entry in the same list (`:1285-1308`, raise at `1302`). One
failing entry means no entry is rebuilt and the resume
fails. One live behaviour already trades recorded spawns away: with `--retry-failed`, the resume
drops every recorded spawn descended from a re-running (formerly failed) parent
(`_drop_spawns_under_rerun_parents`, `lionagi/cli/orchestrate/flow.py:1359-1381`, called at `1436`),
which D5's rule that refusal is preferable to silently dropping spawned work disfavours.

**Rejected paths, kept with their reasons.** Three ways of settling a node were weighed, and each
losing one is what a shorter version of this amendment would have decided.

**Trust a done node by its recorded status.** This is the cheapest replay and what the code does
today. It lost because a `completed` string in a JSON file is a claim about work rather than the
work itself: a checkpoint can be edited, be left behind by a writer whose artifacts were cleaned
up, or name a node whose result never landed, and every one of those replays as finished. A record
that cannot be shown to be wrong is not a record a recovery can stand on.

**Trust a done node by its stored response.** Reading the recorded response and calling it the
result is the natural middle ground, and it lost because a value that only round-tripped through
serialization proves nothing about whether the node's output still exists. The response is evidence
of what the node returned, and a resume that settles a node from it has decided from the thing the
decision was supposed to check.

**Infer a node's state from whatever the checkpoint does contain.** A checkpoint is one atomically
replaced file rather than a log, so there is no gap across which to infer; an entry that cannot be
checked is named rather than completed by presumption, which is what delta rows 2 and 6 exist to
provide.

## Consequences

- A separate CLI process can steer a live flow through the database without an in-process handle
  registry.
- Pause is safe at operation boundaries but can lag behind long in-flight calls.
- Context steering is at-most-once; a crash in the `applying` window may lose a message and leaves a
  visible row requiring diagnosis.
- Full checkpoint snapshots are inspectable and atomically visible, but are not a transactional log
  and do not guarantee fsync-level persistence.
- Amendment 1 records that a recorded `completed` node is replayed from its stored `status` alone:
  a checkpoint whose declared artifacts were lost or changed still replays as finished. The check
  that would narrow this is delta rows 2, 6, 7, 8, and 9, and it is not shipped.
- Resume is planner-free and preserves completed outcomes, but a checkpoint that records a failed
  node refuses the resume (`_apply_checkpoint_precompletion` raises `FlowResumeError`,
  `lionagi/cli/orchestrate/flow.py:1418-1426`) unless `--retry-failed` re-runs those entries as
  pending (`lionagi/cli/orchestrate/flow.py:1454-1465` for a planned node, `:1337-1343` for a
  spawned one). It rebuilds a checkpoint's spawn entries when all of them can be soundly rebuilt
  and refuses the whole resume on any entry that cannot, and requires an explicit flag to lose
  inherited conversation state.
- Contributors extending control must understand verb-specific apply/stamp ordering; treating a
  message like idempotent pause/resume would introduce duplicate steering.
- Reversing D1-D3 requires a new control transport and compatibility for pending rows. D4's JSON
  format can be versioned, while D5's refusal rules may be relaxed only when the missing state is
  actually persisted and replayed.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|---|---|---|
| 1 | Add a terminal reactive-flow cancel control; acceptance: after acknowledgement, no new operation starts and the run persists a cancelled terminal state. | M | (filled at issue-open time) |
| 2 | Verify a replayed node's declared artifact set from the contract the run persisted, before pre-marking it `COMPLETED`, and refuse rather than trust when it cannot be proven; acceptance: a resumed run never marks a node that declared artifacts completed from a record whose declared set is missing or empty (presence and non-empty size, not content), it reads the persisted `artifact_contract_json` column rather than re-deriving the contract from the live roles, and a reactively spawned node's contract is persisted when that node completes rather than only after `run_dag`, where the spawned-entry extension is written today (`lionagi/cli/orchestrate/flow.py:2650,2652-2664`). Depends on delta 9 — without the original run's artifact root every declared path resolves against a root that never held the artifact, so presence cannot be checked until row 9 lands. | M | #2015 |
| 3 | Persist predecessor conversation state needed by `inherit_context`; acceptance: eligible resumed operations receive the same inherited conversation without requiring degraded mode. | L | #2015 |
| 4 | Enforce checkpoint-version compatibility and report unsupported versions before graph construction; acceptance: a checkpoint with an unknown version fails with a typed migration/compatibility error. | S | (filled at issue-open time) |
| 5 | Add operator recovery for controls stranded in `applying`; acceptance: status distinguishes definitely-applied, definitely-not-applied, and indeterminate messages without automatic duplicate injection. | M | (filled at issue-open time) |
| 6 | Give a completed node's settlement an independent recorded result identity, for the nodes delta 2's artifact check cannot cover; acceptance: a checkpoint records a `result_digest` beside the entry's `status` and `response` when the node completes, so a completed node that declared no artifact contract is re-checked from that digest instead of being trusted from its recorded `status`. The bytes hashed are the node's own recorded result — the `<id>.md` file `_record_result` writes into the node's artifact directory once the DAG finishes (`lionagi/cli/orchestrate/flow.py:2554-2556`), which today lands only after `run_dag`, not at node completion. | L | (filled at issue-open time) |
| 7 | Make the artifact check byte-level with a per-artifact completion digest, and recompute the dependent closure when a value a consumer already read must be re-produced; acceptance: a checkpoint records a completion digest per declared artifact, so a result whose bytes changed under a preserved path, size, and mtime is detected, and a dependent of a re-run producer is never left reading a stale value. Depends on delta 2 — the digest is compared against artifacts row 2 has already shown are present; the size-and-mtime check alone narrows the window rather than closing it. | L | (filled at issue-open time) |
| 8 | Route a producer result whose recorded value is missing or changed to exactly one outcome by whether a dependent already consumed it, describing the behaviour before delta 7's byte-level check lands; acceptance: when a dependent consumed the producer's result, resume refuses and names the producer and the consumer that read it, and never re-runs that producer, while a producer no dependent consumed may run again — a resume over a consumed-and-changed producer result raises a typed refusal naming both, and no run re-executes a producer a dependent has read. The recomputation that would let such a producer re-run is delta 7. | M | (filled at issue-open time) |
| 9 | Thread the original run's `artifact_root` through a resume so a settled node's declared artifacts are read from the run that produced them, not from the fresh root a resumed run is given; acceptance: resuming a run without `--save` still resolves every completed node's declared artifacts against the original run's artifact root. | M | (filled at issue-open time) |

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
run is not faithful. The code rebuilds a checkpoint's spawn entries when every entry passes the four
soundness checks, refuses the whole resume on any entry that does not, and requires an explicit
degraded flag for inherited conversation.

### Use the durable task queue as the live-control channel

Controls could be tiny queue jobs, buying one generic transport. It lost because task admission
and executor-local steering have different ownership and terminal semantics. A worker claiming a
pause job would still need the live executor reference this design already exposes directly to the
poller.

## Notes

The schema reserves `stop`, but reservation is not implementation. Current public vocabulary is
only `pause | resume | message`. Checkpoint recovery is a reactive-flow feature and must not be
described as durable task persistence.
