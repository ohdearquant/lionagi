# ADR-0069: Reactive flow steering and recovery

- **Status**: Accepted, amended 2026-10-16 (see Amendment 1); Amendment 2 (2026-10-26) is Proposed
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
  6, 7, 8, and 9; Amendment 2 (Proposed) specifies a version 3 checkpoint, a base
  snapshot plus an ordered journal, in place of D4's full snapshot per outcome, and
  states which version 2 durability guarantees version 3 keeps

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
  migration gate (`lionagi/cli/orchestrate/_checkpoint.py:170`).

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
  (`lionagi/cli/orchestrate/_checkpoint.py:192-193`).
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
  (`lionagi/cli/orchestrate/flow.py:1472-1480`). `--retry-failed` runs the failed nodes again
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
  `_reconstruct_spawned_nodes` (`lionagi/cli/orchestrate/flow.py:1215-1223`) runs before any spawned node is
  added by reconstruction, and each of its six checks raises `FlowResumeError` for the whole resume when any single
  entry fails: (1) the entry must carry `operation` (`CHECKPOINT_VERSION` 2+; a pre-2 entry carries
  nothing to rebuild from, `lionagi/cli/orchestrate/flow.py:1241-1249`); (2) its `status` must be
  `completed` or `failed` (`lionagi/cli/orchestrate/flow.py:1251-1259`); (3) an entry with an `assignee`
  must also carry `spawn_id` (`lionagi/cli/orchestrate/flow.py:1261-1271`); (4) under `--retry-failed`, a
  failed entry naming no assignee must not record the class of the worker that asked for it, or,
  for an escalation retry, the class it ran at (`lionagi/cli/orchestrate/flow.py:1276-1292`,
  ADR-0133); (5) under `--retry-failed`, a failed entry asked for by a worker that read no
  settings files must not be rebuilt on a branch that reads them
  (`lionagi/cli/orchestrate/flow.py:1294-1337`); (6) a `parent_id` must itself
  be checkpointed terminal or another entry in the same list
  (`lionagi/cli/orchestrate/flow.py:1339-1362`). One failing entry refuses the resume, naming that
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
# lionagi/cli/orchestrate/flow.py:1508-1511
entry = checkpoint_ops.get(agent_id)
if entry and entry.get("status") == "completed":
    node.execution.status = EventStatus.COMPLETED
    node.execution.response = entry.get("response")
```

The spawned path pre-marks the same way, from `entry["status"] == "completed"`
(`lionagi/cli/orchestrate/flow.py:1388-1390`). The executor then admits an already-completed
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
  (`lionagi/cli/orchestrate/flow.py:1079-1090`), so removing a role's `artifact_defaults` between runs
  loses that leg's contract. The persisted record exists — `artifact_contract_json` is the column
  ADR-0064 D3 has a flow write
  (`docs/adr/ADR-0064-cli-execution-outcome-and-completion-record.md:412`; declared at
  `lionagi/state/schema.sql:193`). Two stages fill it: `_build_dag` resolves planned role declarations and persists them
  before execution (`lionagi/cli/orchestrate/flow.py:1089-1090,1184-1195`). Spawned instructions use the
  cached role declarations before injection, but `_execute_dag` appends their entries to the
  session contract only after the DAG finishes (`lionagi/cli/orchestrate/flow.py:2750,2752-2764`).
  Resume does not read either persisted set. The column is also nullable — the schema records that a NULL contract means verification
  is skipped (`lionagi/state/schema.sql:192`) — so an absent or unreadable record is not evidence
  that the declared set is still present. Delta row 2 covers both the read and the check.
- **A node that declared no artifact contract.** There is nothing independent to check, so it keeps
  today's status replay like any other completed node. The settle-check delta rows cover only nodes
  that declare artifacts; giving a contract-less completion an independent recorded identity is
  delta row 6.
- **Where the declared artifacts would be looked for.** The artifact root a resume reads is not the
  original run's. `resolve_checkpoint_target` returns the original run's `RunDir` with its
  `artifact_root` (`resolve_checkpoint_target` → `_find_run_dir_by_id`,
  `lionagi/cli/orchestrate/_checkpoint.py:174`), but `_resume_flow` keeps only the checkpoint dict
  and drops the run dir (`lionagi/cli/orchestrate/flow.py:3867-3868`), and `_run_flow` allocates a run
  again through `allocate_run`, whose root is chosen from `--save`: without `--save` the resumed
  run's `artifact_root` is a fresh directory, and with `--save` it is the path `--save` names. The
  path the resume passes comes from the replayed config, which carries the original `save_dir`
  (`lionagi/cli/orchestrate/flow.py:3151`); `allocate_run` uses `save_dir` as the artifact root when
  it is given and falls back to a fresh `<state_root>/artifacts` when it is not
  (`lionagi/cli/_runs.py:617-620`). Either way the root is not read from the original `RunDir` the
  resolver returned, so without `--save` every completed node's declared artifacts are absent from
  the new root, and with a `--save` naming a directory other than the original run's they are absent
  there too. Delta row 9 covers threading the original root.
- **What the live verifier proves.** Presence and a non-zero size, not content: it reads
  `(size := os.path.getsize(full)) > 0` (`lionagi/state/artifact_verifier.py:249`), and the currency check
  beside it compares mtime and size and says itself that this "narrows, but doesn't close" the
  window a rewrite preserving both leaves (`stale_artifact_markers`,
  `lionagi/state/artifact_verifier.py:299-309`). A byte-level check is delta row 7.
- **A declared artifact above the hash size limit.** `compute_hash` refuses a payload above
  `MAX_HASH_INPUT_BYTES = 10 * 1024 * 1024` by default (`lionagi/ln/_hash.py:254,300-301`), and
  ADR-0050 records that a caller hashing larger content either opts out with `max_bytes=None` or
  streams a file through `hash_file()` rather than reading it whole
  (`docs/adr/ADR-0050-foundational-utility-and-typed-adaptation-strata.md:886-889`).

**Builder notes for the rows.** A check that re-reads the declared set would read it through the
same verifier a live run uses (`verify_artifact_contract`, `lionagi/state/artifact_verifier.py:215`),
against the contract the run persisted rather than one re-derived from the live roles. The rows
build on decisions this ADR already carries: D4 keys planned nodes by stable `agent_id`
(`CheckpointWriter.record`, `lionagi/cli/orchestrate/_checkpoint.py:72`) and spawned nodes in a
separate list keyed by node id (`record_spawned`, `lionagi/cli/orchestrate/_checkpoint.py:92`), and
a leg's declared entries are namespaced under that leg's own id — the planned `agent_id`
(`lionagi/cli/orchestrate/flow.py:1089`), and for a reactively spawned leg its `spawn_id`, which
`role_node_builder` stamps (`lionhub/src/lionhub/orchestration/patterns.py:166`), which the checkpoint stores
beside `node_id` (`CheckpointWriter.record_spawned`, `lionagi/cli/orchestrate/_checkpoint.py:127`),
and which is the key a spawned leg's artifact directory is built from
(`env.run.agent_artifact_dir(spawn_id)`, `lionagi/cli/orchestrate/flow.py:2343-2345`; the contract
entries likewise use it at `:2239` and `:2750`). A resume that looked up a spawned leg's artifacts
under its node id would find no directory at all.

The identity delta row 6 asks for is a digest recorded in the checkpoint entry when the node
completes: **field** `result_digest`, beside the entry's `status` and `response` — in a planned
entry written by `CheckpointWriter.record` (`lionagi/cli/orchestrate/_checkpoint.py:72`) and in a
spawned entry written by `record_spawned`
(`lionagi/cli/orchestrate/_checkpoint.py:92,127`). It would be taken with the
BLAKE-family hash the tree already uses for recorded content — `blake2b`
(`lionagi/cli/orchestrate/_manifest.py:333`; ADR-0110 D1 records a "BLAKE-family content hash",
`docs/adr/ADR-0110-deterministic-manifest-fanout.md:119`) — not `compute_hash`'s SHA-256 default
(`lionagi/ln/_hash.py:279-285`). A digest kept in the same file as the entry it describes lives
inside that file's trust boundary: it detects an accidental change — an artifact overwritten or
truncated with the checkpoint not following — and not a deliberate edit, which can rewrite the
digest to match.

**Spawned replay today, and the `--retry-failed` trade.** A checkpoint `spawned` list whose entries
can all be rebuilt is reconstructed into the fresh graph rather than refused as a whole:
`_reconstruct_spawned_nodes` (`lionagi/cli/orchestrate/flow.py:1215-1223`) runs before any spawned node is added by reconstruction,
and each of its six checks refuses the whole resume, not just the failing entry, when any single
entry fails — `operation` present (`lionagi/cli/orchestrate/flow.py:1241-1249`, raise at `1244`), `status`
in `completed|failed` (`:1251-1259`, raise at `1255`), `assignee` paired with a `spawn_id` (`:1261-1271`,
raise at `1265`), under `--retry-failed` no failed entry naming no assignee that records the class of
the worker that asked for it (`:1276-1292`, raise at `1285`; ADR-0133), under `--retry-failed` no
failed entry asked for by a worker that read no settings files that would run again on a branch that
reads them (`:1294-1337`, raise at `1329`), and a `parent_id` that is
itself checkpointed terminal or another entry in the same list (`:1339-1362`, raise at `1356`). One
failing entry means no entry is rebuilt and the resume
fails. One live behaviour already trades recorded spawns away: with `--retry-failed`, the resume
drops every recorded spawn descended from a re-running (formerly failed) parent
(`_drop_spawns_under_rerun_parents`, `lionagi/cli/orchestrate/flow.py:1413-1435`, called at `1490`),
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

## Amendment 2 (2026-10-26) — Journaled flow checkpoints

**Status:** Proposed. This amendment specifies version 3; it does not assert that version 3 is implemented in the current release.

**Revision (2026-10-28):** Nested context changes use a strict JSON Patch subset instead of replacing the affected top-level value. Capture compares synchronously against an immutable, structurally shared baseline; discovery can still cost O(total context), and the event-loop latency requirement remains an implementation gate.

**Scope:** Flow checkpoint storage, acknowledgement, loading, compaction, diagnostics and format migration. It supersedes D4's full-snapshot-per-outcome rule for version 3. Version 2 remains a supported single-file input and an explicitly selectable output. The separate rules for failed-operation retry, topology reconstruction, inherited conversation and output/artifact settlement remain unchanged.

**Source:** `path:line` references in this amendment, including line numbers in this record, refer to development-line commit `c325f4ef`.

### Context

Replacing the entire checkpoint on every outcome repeatedly writes the plan, configuration, previous responses and accumulated shared context. A base plus nested-delta journal is intended to reduce cumulative writes, but its actual byte count, crash correctness and event-loop latency remain subject to the verification requirements below.

On development commit `2dec6259f405bf6e1c42ffe67889d4347aac4242`, a separate real-writer fixture appending an 8,192-byte ASCII value to a nested list and recording an 8,192-byte ASCII response for each operation wrote **330,862,090 bytes** cumulatively at 200 operations. The 25/50/100-operation readings were 5,352,515, 20,991,815 and 83,131,040 bytes; the full context and outcomes reloaded at each point. This is a version-2 baseline, not a version-3 measurement.

Atomic replacement of a JSON file protects the file's visibility. Introducing a journal also requires an ordered fold, an explicit acknowledgement boundary, exact context transitions, file identity preserved from creation, coherent reading of multiple files and a recovery report. Small journal writes do not imply cheap context change detection; CPU cost and callback latency need separate treatment.

### Decision

#### 1. Versioned base and ordered journal

Version 3 consists of `checkpoint.json`, a complete base snapshot, and `checkpoint.json.journal`, an append-only UTF-8 JSON-lines journal. Both are owned by one writer for the run. Concurrent writers to the same checkpoint are unsupported and must be prevented by the owning run lifecycle.

The base retains the snapshot fields: `session_id`, `prompt`, `plan`, `config`, `max_spawn` when present, `flow_context`, `ops` and `spawned`. It adds `version: 3`, a unique `generation` of 32 lowercase hexadecimal characters, and `journal_seq: 0`. Each new base publication uses a fresh generation and incorporates all transitions compacted into it. Its journal starts at sequence 1.

Each journal record contains:

| Field | Contract |
|---|---|
| `version` | Integer 3. |
| `generation` | The base generation to which the record applies. |
| `seq` | Integer in `1..2**63-1`, contiguous in writer order within the generation; booleans and floating-point values are invalid. |
| `kind` | `op` or `spawned`. |
| `entry` | Complete terminal outcome for the corresponding stable identity. |
| `flow_context` | Optional object containing exactly `patch`, an ordered array of the operations defined below. Omission means no context change; an empty patch is a no-op. |

A planned entry retains its stable `agent_id`, terminal `status` and `response`. A spawned entry retains its `node_id`, terminal `status`, `response`, reconstruction data and all currently supported extension fields. In particular, `operation`, `assignee`, `instruction`, `parent_id`, `spawn_id`, `context`, `emitter_workload` and `emitter_contains_user_settings` retain their existing meaning and optionality. Changing storage must not erase a field used to reject an unfaithful resume.

Records use UTF-8 compact JSON with `ensure_ascii=True`, separators `(",", ":")`, `allow_nan=False`, and one trailing LF. Their top-level members are exactly those listed above; unknown members are refused. Before interpreting a v3 base or complete journal record, the loader rejects duplicate decoded JSON object member names at every depth, including escaped spellings of the same name. It must not silently retain one of the values. The loader validates the entire record before applying any of it. Supported terminal statuses are `completed` and `failed`; required identity, outcome and container fields must have their declared types. Unknown versions or record kinds are incompatibility errors, not no-ops.

`patch` is a strict subset of [RFC 6902 JSON Patch](https://www.rfc-editor.org/rfc/rfc6902) using [RFC 6901 JSON Pointers](https://www.rfc-editor.org/rfc/rfc6901). Only these exact operation objects are allowed; unknown fields and `move`, `copy`, and `test` are refused:

- `{"op":"add","path":<pointer>,"value":<normalized JSON>}` creates an absent object member, inserts at an array index at most its current length, or appends with the array token `-`. Unlike RFC 6902's general object-add rule, this subset refuses to replace an existing object member via `add`.
- `{"op":"remove","path":<pointer>}` removes an existing object member or array element.
- `{"op":"replace","path":<pointer>,"value":<normalized JSON>}` replaces an existing object member or array element. The empty pointer `""` may replace the root only with an object, including `{}` for an empty context; root `add` and root `remove` are refused because the context root already exists and must remain an object.

Each pointer is empty for the root or starts with `/`; decode `~1` to `/` and `~0` to `~`, and reject any other or incomplete `~` escape. Decode once so `~01` becomes `~1`, not `/`. Array index tokens are canonical nonnegative decimal integers (`0` or a nonzero digit followed by digits): no leading zeros, sign, or whitespace. `-` appends only for a final `add` to an array; an object member literally named `-` is addressed as an object key. Every intermediate path must exist and have the required container type; `remove` and `replace` require the final target to exist. A splice of k inserted items is k ordered `add` operations in one record; removals are ordered `remove` operations. Apply operations in order against a private candidate, so a later operation observes earlier operations in the same patch. Validate the entire outcome and patch before publishing any part: one record is one visibility unit.

For example, appending `"x"` to the existing list at `context["nested"]["entries"]` produces this complete line (the displayed JSON is followed by LF on disk):

```json
{"version":3,"generation":"0123456789abcdef0123456789abcdef","seq":1,"kind":"op","entry":{"agent_id":"op-0","status":"completed","response":"ok"},"flow_context":{"patch":[{"op":"add","path":"/nested/entries/-","value":"x"}]}}
```

For a single `add` of one nested member or array element, let `E` be the UTF-8 byte length of compact JSON encoding of the complete required `entry`, `U` that of the compact JSON encoding of the added `value`, and `P` that of the compact JSON encoding of the pointer **string**, including its quotes and JSON escapes. All three use the `ensure_ascii=True` rule above. With the fixed fields, generation and sequence limits above, the full UTF-8 line including LF must be at most `E + U + P + 256` bytes. The bound includes outcome and path overhead; it does not include the previous container, siblings, accumulated results or context. A base publication or compaction is counted separately in cumulative writes. This is an encoded-record bound, not an O(changed-bytes) discovery or CPU claim.

Planned outcomes are upserted by stable id; spawned outcomes are upserted by node id in their separate collection. A repeated identity explicitly replaces its previous state and does not add a duplicate logical entry. This is a recoverable state checkpoint, not an immutable history of every revision of an outcome.

#### 2. Ordered admission and acknowledgement

`record()` and `record_spawned()` share one total order assigned when the checkpoint writer admits their completion data, before asynchronous preparation can reorder them. That order need not be provider wall-clock finish order. No later admitted transition may overtake an earlier one in persistence. A rejected input that writes no bytes need not consume a journal sequence. Before a sequence would exceed `2**63-1`, publish a new base and start a fresh generation at sequence 1 under the same compaction and acknowledgement rules.

Successful return from either method is a **checkpoint acknowledgement**: its outcome and context transition, together with all earlier acknowledged transitions, can be reconstructed after immediate process termination using the base and valid journal prefix. Later explicitly recorded updates may supersede the same identity. A transition that reached disk before its acknowledgement was delivered may also be recovered; acknowledgement loss does not justify discarding a complete record.

Queuing a checkpoint task, receiving an executor completion signal, or printing execution progress is not a checkpoint acknowledgement. User-visible progress must distinguish execution completion from checkpoint persistence when these differ. Final run persistence status must inspect pending checkpoint results and report any error; it must not discard exceptions returned by task gathering or imply that failed persistence succeeded.

Once a filesystem operation starts, cancellation must not release writer ownership while an I/O worker can still mutate the checkpoint. The writer waits for the operation's outcome, reconciles its state, and only then releases ownership. This requirement does not introduce deadline or graceful-stop semantics.

An exception after a write or replacement may mean that bytes were committed. The writer must distinguish a known failure before publication from an uncertain outcome. On an uncertain append, flush, fsync, close, replacement or post-replacement error, it must stop accepting further writes or reload and reconcile the durable prefix under exclusive ownership. It must not blindly reuse a sequence, append behind an unrepaired torn suffix, restore an old generation after a possibly successful replacement, or acknowledge later transitions while its state disagrees with disk.

#### 3. Persistence boundary, file identity and platform claims

**Version 2 baseline.** Successful return from `record()` or `record_spawned()` means the writer has validated and serialized the entire state, written and closed its temporary file, and completed `os.replace` of the checkpoint (`lionagi/cli/orchestrate/_checkpoint.py:90`, `:150`, `:157`, `:166`, `:167`). No explicit file fsync, directory fsync or hardware flush is issued: `git show c325f4ef:lionagi/cli/orchestrate/_checkpoint.py | grep -c fsync` prints `0`. On the supported filesystem's atomic-replace boundary, a reader sees the previous or next complete JSON file, never the unfinished temporary file. After successful return, a new process can recover the acknowledged snapshot even if the writing process terminates immediately, provided the operating system and filesystem remain running and the checkpoint is not externally changed. During a subsequent incomplete temporary write or failed replacement, the preceding published snapshot remains readable. This does not promise recovery after host crash or power loss.

**Version 3 preserves that process-termination guarantee.** Recovery at each acknowledged prefix must yield the same logical outcome/context state that a successful v2 full-snapshot write of that prefix would have made recoverable. Later recorded outcome and context updates may supersede earlier values under the existing rules; immutable history is not required. Switching formats must not introduce an interval in which a later append, compaction or failed persistence attempt loses an acknowledged outcome or context update except through those explicit supersessions. The physical representation and visibility boundary change as follows:

| Property | Version 2 | Required version 3 behaviour |
|---|---|---|
| Ordinary successful commit | Complete full-file replacement. | Complete validated journal transition, including LF, buffer flush and file fsync before acknowledgement; an oversized transition uses base publication. |
| Atomic visibility | The single target contains the old or new complete JSON snapshot. | The journal-aware loader returns the logical state before or after a whole outcome/context transition. It never applies half a transition or combines an old base with a reset journal. A raw base-only read no longer means current checkpoint state. |
| Process termination after acknowledgement | The replaced snapshot is recoverable while the operating system/filesystem remains running. | The coherent base plus valid journal prefix recovers the same logical state, including across every later compaction step. A partially appended later record cannot damage the acknowledged prefix. |
| Compaction/publication | Every completion replaces the full file. | A synchronized complete new base atomically replaces the old base before the old journal is retired. Either side of that swap retains all previously acknowledged transitions. |
| Explicit synchronization | No file or directory fsync. | Mandatory file fsync and directory synchronization where supported; known unsupported directory sync is documented separately from an unexpected failure. |
| Non-finite values and serialization failure | Non-finite rejection before publication; failed serialization leaves the prior target. | Retain rejection before transition publication and preserve the last acknowledged state; validate finite values during replay as well. |
| Host crash or power loss | Not guaranteed. | Not guaranteed by this amendment. More synchronization calls alone do not establish such a guarantee. |

The required guarantee is recovery after **process termination**, on each qualified local filesystem/platform combination. Host crash, power loss, remote filesystems, media failure and external deletion/rollback of checkpoint files are outside this recovery guarantee. Additional guarantees must be documented and tested independently. This exclusion does not permit future payload writes to a substituted file: the identity requirements below apply even when external replacement has already made an older checkpoint unreachable.

For a journal acknowledgement, the implementation writes the entire encoded record including LF through the owned journal handle, handles short writes, flushes application buffers and completes file fsync before reporting success. Closing a retained journal handle after each append is not required. Any close performed as part of a transition must complete without an unreported error; a later close failure must be surfaced without silently changing the already acknowledged state. The journal's directory entry must have been established by initialization or the preceding compaction. Re-creating an unexpectedly missing journal is not an ordinary append: it requires recovery/reconciliation first.

**Identity originates at creation.** File creation must return the open handle and its handle-derived identity as one owned result. Capture identity from that handle, never by reopening or statting the newly published path to establish what was created. Temporary files must be created exclusively without following an existing link. Retain the active journal handle for appends; if the platform requires reopening, compare the safely reopened handle's identity with the original creation identity before writing any response or context bytes. A pathname check followed by an unchecked open is insufficient. Missing, substituted or unexpected file identities cause an explicit persistence error, not silent adoption or truncation of the new occupant.

Apply the same ownership rule to the base, each temporary file, each new journal generation, and the containing directory. Publication must install the object validated and synchronized through its creation handle. Use a verified directory handle and a protected staging namespace or equivalent platform mechanism that prevents temporary-source substitution during publication; directory-relative rename alone does not prevent replacement of the source leaf. Directory synchronization must refer to the directory whose entries changed. Cleanup must not unlink a different object merely because it now occupies the temporary name. Path-based metadata may verify a name-to-object binding, but must never reset the expected identity to the current occupant or create a check/use gap before payload writes.

Retaining a handle prevents redirection of its writes, but does not by itself preserve a pathname for recovery after unlink or replacement. Before acknowledgement, the owned checkpoint must remain reachable through its published names, or an identity-safe recovery protocol must have restored that reachability. Otherwise fail persistence without sending payload to the substitute. Arbitrary external destruction after acknowledgement cannot be repaired by a handle pin; recovery from such destruction remains outside the guarantee.

For a base publication:

1. Construct the complete normalized snapshot under writer ownership.
2. Create an owned temporary file in the destination directory using the identity rules above; complete all writes, flush and fsync. Retain its identity and the ownership needed for publication, whether or not the platform requires closing its write handle before replacement.
3. Atomically replace the base with that same synchronized object, without allowing temporary-source substitution during the handover. Any required close must complete or be handled as an uncertain publication error.
4. Complete parent-directory synchronization where the platform supports it.
5. Only then retire the old journal by atomically replacing it with an empty file whose creation identity, flush, fsync, required closes, replacement and directory synchronization follow the same rules. Adopt the new journal's original creation handle/identity for later appends; do not derive its identity from a post-publication reopen.
6. Establish the new generation and sequence baseline consistently before accepting the next transition.

Known lack of directory-sync support must be a documented platform capability; it must not be conflated with an unexpected I/O failure. Unexpected synchronization or cleanup failures propagate with their actual publication state. The implementation must never claim host-crash durability from a best-effort directory-sync attempt. If directory creation is required, the supported path-creation behaviour must be included in platform qualification; syncing a child directory does not establish synchronization of all its ancestors.

Before base replacement, recovery uses the previous base and journal. After base replacement, the new base already contains all compacted outcomes, so a surviving old-generation journal is redundant. The old journal must never be reset before publication of the complete new base. Temporary files are not selected as recovery bases merely because they exist.

#### 4. Immutable normalized context and exact deltas

The writer must use an immutable captured representation for each outcome, its associated context transition, and the next comparison baseline. The same captured value must drive serialization and baseline advancement. It must not serialize one version and later copy a newer live object as though that version had been persisted. At the completion boundary, the event-loop thread synchronously compares the live context against the frozen baseline and freezes the next captured representation, with no `await` between the live read and the completed freeze. Mutation from another thread during that capture is unsupported. Public `Note` and its existing alias behavior do not change.

The representation is JSON data. Preserve the existing string fallback for supported non-JSON objects, applied once during normalization; the persisted contract is that normalized string, not restoration of Python object identity. Reject non-finite numeric values and non-finite numeric keys before conversion can hide them, and reject serialization failures without publishing a partial transition. Normalize JSON-compatible object keys to strings consistently with snapshot encoding and reject collisions between distinct input keys.

Delta comparison must respect the normalized stored representation, including scalar type differences such as integer `1`, floating-point `1.0` and boolean `true`. Custom Python equality is not a serialization contract. A one-element nested append or a new nested object member produces an `add` at that path; replacement and deletion use the corresponding operations. A full context replacement may use root `replace`. It is not necessary to encode an unchanged enclosing list or object in a nested delta.

Passing no context means no context update. Passing an empty context means remove every previous key. An explicit JSON null value is a stored value, not a deletion marker. Non-copyable values use the same normalization rule as other values; there is no silent shallow-copy fallback.

Redundant identical context snapshots may be coalesced or omitted. Every distinct completed operation or spawn must retain its outcome, and every acknowledged prefix must recover its associated context state. Coalescing must not remove a transition needed to meet that rule. A physical batch may share an fsync only if all its records remain ordered and each acknowledgement waits for the durability boundary covering its record.

Context validation, normalization, comparison and freezing needed for a stable completion snapshot run synchronously on the event-loop thread at that boundary. Serialization of the frozen representation, compaction and filesystem work run off the event-loop thread. The complete admission-to-acknowledgement path, including synchronous capture and completion hooks, must meet the latency bound below. Passing live mutable references to a thread is not sufficient isolation.

**Detection cost.** The normal delta path retains a persistent normalized baseline rather than serializing it again at every completion. Normalize each current value once per admitted snapshot and reuse that immutable representation for comparison, delta encoding and the next baseline. Build the next baseline by sharing unchanged immutable subtrees, materializing changed paths and removing deleted nodes; do not deep-copy or re-encode all unchanged baseline subtrees. A persistent vector may replace its affected tail chunk and ancestor spine on append. Cached digests may accelerate comparison only with an exact equality safeguard, so collisions cannot suppress a real change.

This bounds redundant work, not the discovery cost of arbitrary mutable input. Without complete nested-mutation tracking or immutable context versions, current values must still be examined and change detection can remain O(total context) per completion. Do not claim O(changed bytes) CPU cost merely because the journal contains only a delta. Any optimization must preserve nested mutation, deletion, normalized scalar types, non-finite rejection and the immutable capture rule above. Initialization, compaction and explicitly requested full replacement may process the full state.

Synchronous discovery may increase callback lag even when journal writes are small. The p99 lag requirement is satisfied only by the measured complete workload. Record detection/normalization CPU time, acknowledgement latency, queue growth and allocations separately as total context increases with fixed changes. A separate fixture may disable append/fsync to isolate detection; acceptance measurements must use real persistence. No per-record service-time threshold is implied by a callback-lag threshold. If the full workload misses the bound, report the measurements and do not activate v3; a versioned ownership boundary is a separate decision.

#### 5. Bounded compaction

Default compaction limits are 128 appended records and 8,388,608 encoded journal bytes. They bound different costs: record count bounds replay steps, and bytes bound the journal's storage footprint. Both limits are configurable positive values.

Compact when the record count reaches its limit. If a prospective append would exceed the byte limit, compact the preceding state before that append and encode the append against the resulting generation. If one transition alone exceeds the byte limit, commit it through a new base containing that transition, using the same publication and acknowledgement rules, without first growing the journal beyond the limit. Explicit flush may also compact.

Replace, rather than accumulate, obsolete journals after the new base's publication boundary. An interrupted journal reset must be completed or reconciled before any append to the new generation. Base contents, generation changes, journal reset and in-memory state must remain consistent even when an operation reports an error after replacement.

These limits do not bound the complete base size or the size of an admitted context value. Large state can still require large snapshots and temporary memory. Full snapshots at fixed intervals can make total bytes over arbitrary run lengths non-linear even when individual nested updates are small. The measurable contract is the workload bound below.

#### 6. Coherent replay and recovery reporting

Every v3 reader must reconstruct state through the journal-aware loader. Parsing the base alone is not a complete checkpoint read. The loader must capture a coherent base/journal pair while compaction may be active: for example, verify the base generation before and after journal capture and retry if it changes. Missing/replaced-file races require the same treatment. Bounded retries must end with a typed error if a coherent view cannot be obtained; they must not return an apparently clean older base with a newer reset journal. A missing v3 journal is a recovery error after bounded race handling, not permission to assume that no deltas existed.

Within a coherent view, apply only complete, validated records matching the base generation, beginning at sequence 1 and continuing contiguously. Validate finite values on reads as well as writes. Validate an entire transition before mutating either outcome or context. Ignore and count complete stale-generation records; never apply them to a newer base. Do not sort, guess missing records, or continue after a current-generation discontinuity.

An unterminated final append is omitted and reported. A truncation exactly after a record's LF leaves a complete record and is recoverable even if the original caller never received an acknowledgement. A complete malformed record, including a duplicate object member at any depth, unsupported version/kind, invalid sequence or invalid transition produces a typed recovery refusal before graph execution, with the valid prefix available for diagnosis. A duplicate member in the v3 base likewise produces a typed refusal before graph execution; no journal prefix is available without a valid base. A malformed record does not become safe merely because it contains a newline. Sequence gaps and duplicate sequence numbers must not be silently skipped.

The loader returns recovered state plus a versioned report under the stable field name `checkpoint_recovery`, separate from checkpoint application state. Its shape is:

| Field | Meaning |
|---|---|
| `report_version` | Integer 1. |
| `source_version` | Input checkpoint format version. |
| `generation` | V3 base generation, or null for a legacy snapshot. |
| `base_sequence`, `last_applied_sequence` | Sequence watermark in the base and last journal transition applied; zero for a fresh v3 base. |
| `applied_records`, `valid_prefix_bytes` | Applied transition count and byte offset through which the journal was validated. |
| `ignored` | Counts for `torn_final_record` and `stale_generation_record`. |
| `problem` | Null, or a reason code with line/byte offset and expected/actual sequence where relevant. Reason codes include `torn_final_record`, `invalid_record`, `invalid_sequence`, `unsupported_version`, `unsupported_kind`, `missing_journal`, `identity_mismatch`, and `incoherent_view`. |
| `disposition` | `clean`, `recovered_prefix`, or `refused`. |

Unavailable values are null rather than fabricated. A discarded tail or stale-generation records yield `recovered_prefix`; a fatal validation/coherence problem yields `refused`. The report is not written back as part of the logical flow context or copied as if it were a current outcome.

Resume surfaces any non-clean report before graph execution: a concise CLI diagnostic through the standard logging surface, the same structured report in machine-readable results and resumed-run metadata, and a typed error for refusal. Studio preflight and the detached execution path use the same loader and preserve the report in their result/error or invocation metadata. Diagnostics identify positions and reasons without copying prompts, responses or context values. Keeping an unread diagnostic dictionary is insufficient.

#### 7. Compatibility and rollout

Version dispatch is explicit. Continue reading v2 single-file snapshots, and preserve already supported older snapshot inputs subject to their existing reconstruction limitations and legacy parsing semantics. Dispatch must not let duplicate version members disguise a v3 base as a legacy snapshot; an ambiguous version declaration is refused. Unknown versions fail with a typed compatibility error before graph construction. They must not be treated as legacy merely because their version is not 3.

The initial library compatibility transition retains constructor output default 2 and requires explicit `version=3` for journal output. Once all common readers, diagnostics and acceptance checks support v3, the normal CLI flow constructor explicitly selects version 3. This makes v3 the normal format for new CLI flows without silently changing an existing library caller's output format. An explicitly selected v2 writer remains available during this transition.

Resume of a v2 checkpoint requires no manual editing. Load its complete state, preserve outcome identities and all reconstruction/configuration fields, and write a v3 initial base and journal into the resumed run. Leave the source checkpoint untouched. A second resume must retain the same state. Loading or upgrading v3 must not silently downgrade it to a base-only v2 file.

All readers, including MCP run detail and Studio preflight, must be updated before v3 output is enabled for them. A reader must reconstruct journaled spawns rather than report the base alone as current; when it cannot load the unit, it must not silently present partial v3 state as complete. Existing base-only binaries cannot correctly consume v3; version stamping does not make them compatible. Documentation must identify the base and journal as one checkpoint unit for movement or backup, captured coherently. These rules change checkpoint storage, not the topology or settlement decisions of D5 and Amendment 1.

### Verification requirements

The implementation is complete only when these observable contracts hold:

1. **Bytes and fidelity.** For 200 operations, each adding an 8,192-byte ASCII response and an 8,192-byte ASCII nested context item, cumulative encoded checkpoint bytes are below 10,000,000 and the entire recovered normalized state matches the expected state. This workload bound does not apply to arbitrary values whose JSON encoding is larger. Run once without a compaction before operation 200 and once with the default limits, counting initialization, all journal bytes, every temporary snapshot and every compaction. Count actual successful bytes across all filesystem write methods, including partial writes and retries, and require a positive control showing the counter observes real writes. Do not substitute final file size or a counter that excludes retries for cumulative writes. Pin identical plan/configuration/payload fixtures for comparison; name both the version-2 full rewrite and an ideal structural delta as baselines.
2. **Event-loop latency.** With 100 concurrent completions, each carrying a 65,536-byte response and adding 65,536 context bytes, callback lag p99 during active checkpoint work is below 25 ms. Include the actual completion hooks, context capture, normalization, acknowledgements and drain. Record sampling cadence, sample count, repetitions, filesystem/platform and workload shape; a single callback that runs while I/O is paused is not this measurement.
3. **Termination and error recovery.** Inject process termination immediately before and after each append write/short write, LF completion, flush, fsync, close, in-memory advance, temporary-base write/flush/fsync/close, base replacement, directory sync, journal replacement, cleanup and acknowledgement boundary. Repeat for initialization, append and compaction. Load in a new process and recover every previously acknowledged transition. Separately inject recoverable I/O errors at those seams and attempt a subsequent write: it must either be recoverable or be refused until reconciliation.
4. **Order and exactness.** Mix concurrent planned/spawned outcomes, repeated stable identities, cancellation, compaction and concurrent loading. Verify admission order, no missing distinct outcomes, no duplicate logical entries and exact context at every acknowledged prefix. Exercise nested mutation during persistence, scalar type changes, key deletion, null/empty context, normalized non-JSON values, numeric-key collisions and non-finite values on both reads and writes.
5. **Recovery and migration surfaces.** Exercise torn JSON before LF, exact LF completion, malformed complete records, duplicate JSON object names in v3 bases and complete journal lines at the outer, operation and nested-value levels, including names equal after JSON unescaping, sequence gaps/duplicates, stale generations, interrupted reset, oversized records, missing journals and unknown versions. Check valid distinct-name controls and legacy input compatibility. Across readers, verify typed refusal and the same valid-prefix report for a duplicate-key journal line; a rejected base has no valid journal prefix. Verify CLI and Studio diagnostics/refusals. Round-trip v2 through v3 and a second resume, preserving failed/planned/spawned outcomes and every supported reconstruction field.
6. **Identity substitution.** Pause immediately after file creation returns and before the writer adopts its identity; substitute a different regular file and, where supported, a symlink. Repeat between publication and first append, between later appends, after compaction/reset, and at temporary-source publication, directory resolution and cleanup. Assert that substituted targets receive zero response/context bytes, unrelated sentinels are neither truncated nor unlinked, publication installs the synchronized created object, and every successful acknowledgement remains recoverable through the published checkpoint. Assert typed failure when identity or reachability cannot be maintained; a mismatch must not reset the pin to the substituted file.
7. **Detection scaling and durability equivalence.** With increasing total context and fixed changed data, record detection CPU, allocations, acknowledgement latency and queue growth, using both unchanged values and nested mutations. Confirm unchanged baseline values are not repeatedly deep-copied or reserialized. Keep the full p99 workload with real I/O as a separate acceptance requirement. For equivalent v2/v3 state fixtures, terminate the writer after acknowledgement and before/after the next write and every compaction boundary; the v3 loader must preserve every acknowledged logical state recoverable under the stated v2 process-termination assumptions. Neither fixture establishes power-loss durability.
8. **Nested delta and baseline sharing.** Hold the new item and outcome fixed while increasing an enclosing list or object from 1,000 to 10,000 to 100,000 entries. A one-item append or absent-member insertion must meet the encoded-line inequality above and must not include unchanged container contents. Record copied nodes/chunks and verify unchanged subtree identity is retained in the next frozen baseline. Mutating the live nested value after capture must not change an earlier frozen version. Exercise root replacement, empty and null values, object keys containing `/` and `~`, empty keys, array insertion/removal, canonical index validation, missing and existing targets, sequential operations on one record and a malformed late operation that leaves both outcome and context unapplied. Test non-ASCII and control-character escaping separately without applying the ASCII workload's 10 MB ceiling. Compare recovered state with the expected normalized state. Report full discovery and capture work separately from journal bytes and structural sharing.

### Alternatives considered

**Full snapshots in a background thread.** This simplifies recovery and removes direct filesystem blocking from the event loop, but still rewrites every previous response/context on every completion. It does not meet the cumulative-byte requirement.

**An unbounded append-only journal.** This minimizes compaction work but allows storage and replay cost to grow indefinitely and retains superseded values. Count and byte limits are required.

**Count-only compaction with permissive recovery.** This is compact to implement, but a few large records defeat its storage bound, and skipping arbitrary broken records can fabricate a state that never existed. Bounded compaction and contiguous validated replay are selected instead.

**A transactional embedded database.** This can provide mature transaction and recovery machinery, but changes checkpoint portability, dependency and inspection properties beyond this format amendment. A small versioned snapshot/journal protocol is sufficient only with the explicit publication, ownership and recovery contracts above.

### Consequences and implementation fences

The writer may batch ordered records and omit identical normalized context. Capture and comparison follow the synchronous boundary above; serialization and I/O are off-loop. It may not drop a completed outcome, compare mutable live state against a later baseline, continue after uncertain publication without reconciliation, derive creation identity from a reopened path, write payload to a substituted file, silently lose extension fields, or treat base-only parsing as v3 recovery.

The proposed format aims to reduce bytes for the specified workload and bound journal replay between compactions; the actual byte reduction remains subject to the measured acceptance gate. It preserves the v2 process-termination recovery boundary while changing atomic visibility from one file to the loader's coherent logical state and adding explicit file synchronization. It introduces handle-based file ownership, cached immutable context representations, stricter format validation, explicit error propagation and a visible recovery report. It does not promise power-loss durability, O(delta) detection for untracked mutable context, arbitrary Python-object restoration, immutable outcome history, artifact verification, or exactly-once external effects.

When integrating this amendment, mark the earlier D4 as the v2 description and update its summary row and related statements consistently. In `docs/adr/ADR-0069-reactive-flow-steering-and-recovery.md`, the full-snapshot decision at line 47, absence-of-fsync statement at line 59, D4 at line 289, single-file argument at line 651, consequences at line 663, version-dispatch delta at line 688, and rejected journal alternative at line 723 must point to this amendment for v3. The version-dispatch delta remains open until implemented; the new proposal must not mark it complete. The journal alternative becomes the selected v3 design, while Amendment 1's independent output/artifact settlement questions remain unchanged.

## Amendment 3 (2026-10-26) — resumable graceful flow deadlines

**Status:** Proposed.

This amendment addresses #2833 by making explicit flow and playbook deadlines graceful and distinct from task failure, preserving completed work for checkpoint resume, and retaining the already shipped unset `--timeout` default provided by `add_common_cli_args`.

**Relation to the existing decisions.** D4 describes version-2 full snapshots; D5 specifies
faithful graph reconstruction, explicit failed-operation retry and inherited-context refusal.
Amendment 1's independent output/artifact settlement requirements remain unchanged.
Amendment 2, *Journaled flow checkpoints*, is Proposed and specifies version 3; its presence
does not establish that journal storage has shipped. This amendment changes logical checkpoint
state and execution admission. The state fields below belong in v2 snapshots and in v3 bases
and journal entries when Amendment 2 is implemented; they do not introduce another storage
version or implement the journal.

### Decision

1. **Admission, drain and required phases.** An explicit flow or playbook deadline closes
   admission to every new assignment, including planner retries, reactive/team/escalation
   followups and requested synthesis. Assignments already invoked finish; then checkpoint
   persistence settles and the run exits. Capacity waiters recheck admission after acquiring
   capacity. No later phase may reopen expired admission or rerun still-pending worker nodes.
   Requested synthesis counts as required work: if withheld, the run is interrupted even when
   every worker finished. Resume recomputes withheld synthesis from checkpointed worker results;
   it does not record a false completed synthesis result. With no required work withheld, a
   naturally finished run remains completed. Interruption keeps `timed_out`,
   `run.timed_out.deadline`, `LionTimeoutError` and exit 124. Task failure, external cancellation
   and checkpoint-write failure retain their own outcomes. Report a resumable checkpoint only
   after the final persistence boundary succeeds.

2. **Deferred spawned intent.** A spawned entry may additionally have `status="pending"` and
   `response=null`, meaning recorded but never invoked. A withheld planned assignment remains
   absent from `ops`. Retain late followup intent rather than dropping it. Pending entries run
   without `--retry-failed`; failed entries run only with that explicit flag. Execution-sensitive
   routing, workload and settings-containment checks apply to every entry that will run:
   pending entries unconditionally, failed entries when retried. A pending entry must not fall
   back to an orchestrator branch with greater authority. Retain the recorded provenance through
   further deferral and whole-entry replacement. Refuse missing or incompatible provenance,
   response-bearing pending entries and unfaithful reconstruction before admitting graph work.

3. **Planned identity and promotion.** Optional `plan[].node_id` associates a plan entry with
   its operation UUID. Strip this metadata before constructing `TaskAssignment`; remap recorded
   planned references to the rebuilt graph and refresh the association on every later checkpoint.
   Do not infer a missing association from a display name. Explicit-deadline checkpoints record
   these witnesses from their initial snapshot. In **any** run, including fresh untimed runs and
   legacy resumes, promote the plan before the first snapshot or journal state carrying a splice
   witness. Promotion and refreshed references must be coherent at the publication boundary;
   no intermediate unguarded splice state may be published. An ordinary untimed run without
   splice/deferred recovery requirements may retain the legacy v2 shape.

4. **Every retry state preserves the splice.** Optional `spawned[].escalated_from` identifies
   the logical emitting operation; `parent_id` identifies its placement anchor. Preserve these
   remapped fields for **pending, completed and failed** retries, including pending→failed→retry
   across multiple resumes. Replacing a whole spawned entry must never erase them. Ordinary
   spawned nodes do not receive a splice witness. Reconstruct successor edges, conditions and
   labels with `Graph.splice_after`, and restore the executor's replacement chain **before any
   operation is admitted**. Further escalation must choose the same final anchor as uninterrupted
   execution. Runtime metadata read by later checkpoint callbacks must retain the remapped
   emitting/anchor references. Ordinary spawned-parent terminal checks remain; a recorded retry
   may have a pending earlier retry as its placement anchor, but its logical emitter must have a
   sound checkpointed emission and the entire replacement chain must be reconstructible.

5. **V2 refusal guards.** The old v2 spawned reader refuses pending. Its plan loader strips only
   `agent_id` and `dep_indices`; `TaskAssignment` inherits `extra="forbid"` from
   `lionagi/protocols/flow_requests.py:23–26`, so `plan[].node_id` also causes refusal. Preserve
   this extra-field policy as a v2 compatibility guard: the guarded reader strips
   `plan[].node_id` before construction (item 3), and no reader relaxes the model policy for
   checkpoint-loaded assignments, which would be a contract change. Never emit a splice witness
   in v2 without a planned UUID
   witness or pending spawned entry in the same snapshot. Promote planned UUIDs before the first
   splice publication in any run, and before the final pending entry can stop being pending.
   Writer refusal is for invalid input/state, not a substitute for promoting valid untimed work.
   A guarded-v2 reader refuses an unguarded splice rather than interpreting it as an ordinary edge.

6. **V3 interoperability follows Amendment 2.** A reader supporting only guarded v2 must reject
   v3 before graph construction; it must not parse its base as a complete checkpoint. A
   v3-capable reader uses Amendment 2's coherent base-plus-journal loader, preserving these fields
   through complete-entry upserts, compaction and v2→v3→second-resume migration. Its `spawned`
   entry validation admits deferred pending state in addition to completed/failed outcomes;
   pending is recorded intent, not a terminal outcome. Planned UUID promotion belongs in the
   complete base state and must be published coherently before a journal transition needing it;
   no new journal record kind is introduced here. The explicit version dispatch, supported-reader
   rollout, acknowledgement, identity and recovery-report requirements remain Amendment 2's.

The shipped old v2 `json.loads` loader does not enforce `version`. Therefore a numeric v3 stamp
does not protect that old binary, and a pending record found only in a journal cannot guard a
base-only read. V2 refusal guards do **not** make old v2 readers compatible with v3. All common
readers must be updated before enabling v3 output as Amendment 2 requires; an incidental extra
field rejection is not a supported v3 reader. Until that implementation exists, guarded-v2
readers fail closed on v3 rather than inventing journal replay.

### Verification requirements

Use event-gated A/B/C admission and actual CLI/Session/Invocation results. Cover late spawn/team
intent; failed retry R still gating B under `--retry-failed`; pending→failed→retry across
generations; multiple restored retries followed by another escalation with the same final anchor
as uninterrupted execution; and callback metadata retained through a second resume. Verify
restricted pending routing/workload/settings without a retry flag and across two deferrals.
Inspect every intermediate checkpoint from fresh untimed and legacy-resumed retry execution,
not just the final file. None may contain an unguarded or lost splice witness. Exercise synthesis
after all workers finish and with workers still pending; neither may reopen expired admission.
Test old-v2 refusal, guarded-v2 rejection of v3, and, when available, journal-aware v3 roundtrips
preserving pending/completed/failed retry metadata through replacement and compaction. Retain
whole-resume refusal and failure reporting; neither storage format settles Amendment 1's separate
artifact/output questions by recorded status alone.

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
  `lionagi/cli/orchestrate/flow.py:1472-1480`) unless `--retry-failed` re-runs those entries as
  pending (`lionagi/cli/orchestrate/flow.py:1508-1519` for a planned node, `:1391-1397` for a
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
| 2 | Verify a replayed node's declared artifact set from the contract the run persisted, before pre-marking it `COMPLETED`, and refuse rather than trust when it cannot be proven; acceptance: a resumed run never marks a node that declared artifacts completed from a record whose declared set is missing or empty (presence and non-empty size, not content), it reads the persisted `artifact_contract_json` column rather than re-deriving the contract from the live roles, and a reactively spawned node's contract is persisted when that node completes rather than only after `run_dag`, where the spawned-entry extension is written today (`lionagi/cli/orchestrate/flow.py:2750,2752-2764`). Depends on delta 9 — without the original run's artifact root every declared path resolves against a root that never held the artifact, so presence cannot be checked until row 9 lands. | M | #2015 |
| 3 | Persist predecessor conversation state needed by `inherit_context`; acceptance: eligible resumed operations receive the same inherited conversation without requiring degraded mode. | L | #2015 |
| 4 | Enforce checkpoint-version compatibility and report unsupported versions before graph construction; acceptance: a checkpoint with an unknown version fails with a typed migration/compatibility error. | S | (filled at issue-open time) |
| 5 | Add operator recovery for controls stranded in `applying`; acceptance: status distinguishes definitely-applied, definitely-not-applied, and indeterminate messages without automatic duplicate injection. | M | (filled at issue-open time) |
| 6 | Give a completed node's settlement an independent recorded result identity, for the nodes delta 2's artifact check cannot cover; acceptance: a checkpoint records a `result_digest` beside the entry's `status` and `response` when the node completes, so a completed node that declared no artifact contract is re-checked from that digest instead of being trusted from its recorded `status`. The bytes hashed are the node's own recorded result — the `<id>.md` file `_record_result` writes into the node's artifact directory once the DAG finishes (`lionagi/cli/orchestrate/flow.py:2654-2656`), which today lands only after `run_dag`, not at node completion. | L | (filled at issue-open time) |
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
