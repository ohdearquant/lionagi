# ADR-0023: Dependency-aware operation-graph execution kernel

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: operations
- **Date**: 2026-07-09
- **Relations**: none

## Context

LionAGI represents a multi-step operation as a directed graph of executable `Operation` nodes.
Construction and execution are intentionally separate, but the execution kernel must turn a generic
graph into branch-aware asynchronous work. Six concrete problems shaped the current implementation.

**P1 — A graph node needs a serializable request and late-bound conversation state.** A builder can
know an operation name and parameters before it knows which branch should invoke them. `Operation`
therefore stores the request and optional branch id, while its private branch reference is assigned
immediately before invocation (`lionagi/operations/node.py`).

**P2 — Dependency failure or skipping must never strand downstream waiters.** Independent nodes
should run concurrently, but every predecessor path must settle before dependents decide whether to
run. The executor creates one `ConcurrencyEvent` per operation and releases it from terminal,
skipped, completed, failed, cancelled, and defensive exception paths
(`lionagi/operations/flow.py`).

**P3 — Conversation isolation must be explicit rather than an accident of scheduling.** An
explicitly assigned branch is reused. A dependent or context-inheriting operation without one gets
a session-owned clone allocated before execution. Some nodes also request conversation-message
inheritance from one primary predecessor.

**P4 — Data context has two sources with different semantics.** Every predecessor result is exposed
under an operation-id-derived key, while a shared flow context is copied into each operation and can
be updated by successful response mappings. Parallel writes to the same shared key have no declared
reducer, so completion order currently decides the winner. One reserved shared key,
`operator_messages`, is control input rather than model context: pending entries are rendered into
the instruction and removed from the per-operation context before dispatch.

**P5 — A running graph may grow, but growth must reuse the static kernel.** Reactive work can arrive
through a returned `SpawnRequest`, a bus emission, explicit injection, or an escalation. Accepted
nodes still need dependency waits, capacity limiting, branch allocation, context preparation, pause
behavior, and ordinary `Branch.get_operation()` dispatch.

**P6 — Restoring node results is not the same as restoring a running workflow.** The kernel can
short-circuit operations already in a terminal state and expose restored responses. It does not own
a checkpoint format, serialized spawned topology, branch-history reconstruction, or durable event
replay. Those omissions are decisive for reactive continuation, not incidental implementation
details.

| Concern | Decision |
|---------|----------|
| Graph request model | D1: `Operation` is the executable node and `OperationGraphBuilder` constructs graph shapes without executing them. |
| Static dependency execution | D2: an acyclic executor uses per-node completion events and bounded operation admission. |
| Branch allocation | D3: explicit branches are reused; dependent/context-inheriting nodes receive session-owned clones, with optional primary-message inheritance. |
| Conditions, context, and results | D4: edge conditions gate paths, predecessor results are namespaced by id, response context deep-merges into shared state, and operator messages render once into instructions. |
| Pause and restored terminals | D5: a soft boundary gate pauses new operations; pre-set terminal nodes short-circuit without kernel-owned persistence. |
| Reactive mutation and streaming | D6: reactive execution extends the same kernel with synchronized, capped, cycle-checked injection and typed completion events. |

This ADR deliberately does **not** decide:

- Checkpoint storage, retention, schema migration, or resume degradation policy; those are owned by
  the scheduling-control-plane ADR on flow checkpoint and resume semantics.
- How a CLI planner maps roles, workspaces, or persisted definitions into initial graph nodes; the
  kernel receives an already constructed `Graph[Operation]`.
- The semantics of individual branch verbs; ADR-0021 and ADR-0022 own operation dispatch and
  composed execution.
- Distributed graph scheduling, cross-process queues, or remote workers. The executor is an
  in-process async kernel.
- Deterministic conflict resolution for shared context. The current completion-timing behavior is
  recorded as shipped truth and retained as a delta, not specified as an ideal reducer.

## Decision

### D1 — `Operation` is the executable node; the builder only constructs graph state

The node contract is:

```python
BranchOperations = Literal[
    "chat",
    "operate",
    "communicate",
    "parse",
    "ReAct",
    "select",
    "interpret",
    "act",
    "ReActStream",
]

class Operation(Node, Event):
    operation: BranchOperations | str
    parameters: dict[str, Any] | BaseModel = Field(
        default_factory=dict,
        exclude=True,
    )
    _branch: Any = PrivateAttr(default=None)

    @property
    def branch_id(self) -> UUID | None: ...

    @property
    def graph_id(self) -> str | None: ...

    @property
    def request(self) -> dict: ...

    @property
    def response(self): ...

    async def _invoke(self): ...

def create_operation(
    operation: BranchOperations | str,
    parameters: dict[str, Any] | BaseModel = None,
    **kwargs,
): ...
```

`parameters` are excluded from the ordinary Pydantic field dump, but `request` returns a mapping:
it uses `model_dump()` or legacy `dict()` for a model and returns `{}` for any other non-mapping
value. `response` projects `execution.response` after event invocation.

The builder's public construction surface is:

```python
class ExpansionStrategy(Enum):
    CONCURRENT = "concurrent"
    SEQUENTIAL = "sequential"
    SEQUENTIAL_CONCURRENT_CHUNK = "sequential_concurrent_chunk"
    CONCURRENT_SEQUENTIAL_CHUNK = "concurrent_sequential_chunk"

class OperationGraphBuilder:
    def __init__(self, name: str = "DynamicGraph"): ...

    def add_operation(
        self,
        operation: str,
        node_id: str | None = None,
        depends_on: list[str] | None = None,
        inherit_context: bool = False,
        branch=None,
        **parameters,
    ) -> str: ...

    def expand_from_result(
        self,
        items: list[Any],
        source_node_id: str,
        operation: str,
        strategy: ExpansionStrategy = ExpansionStrategy.CONCURRENT,
        inherit_context: bool = False,
        chain_context: bool = False,
        **shared_params,
    ) -> list[str]: ...

    def add_aggregation(
        self,
        operation: str,
        node_id: str | None = None,
        source_node_ids: list[str] | None = None,
        inherit_context: bool = False,
        inherit_from_source: int = 0,
        branch=None,
        **parameters,
    ) -> str: ...

    def add_conditional_branch(
        self,
        condition_check_op: str,
        true_op: str,
        false_op: str | None = None,
        **check_params,
    ) -> dict[str, str]: ...
```

Exact node and builder semantics:

- `Operation._invoke()` requires `_branch` to be set, resolves the name through
  `Branch.get_operation()`, stores the actual branch id, and awaits the callable. A missing branch
  raises `ExecutionError`; a missing operation raises `OperationError`.
- `ReActStream` is the only special operation name: its async generator is fully consumed into a
  list so the event has one terminal response.
- `node_id` on builder methods is a human reference stored as
  `metadata["reference_id"]`; it does not replace the generated element UUID.
- `add_operation()` adds `depends_on` edges only for dependency ids already known to that builder.
  With no explicit dependency, it links every current head to the new node using a `"sequential"`
  label. A supplied branch is normalized through `ID.get_id()`.
- `inherit_context=True` with dependencies records one `primary_dependency`, always the first
  `depends_on` entry. This metadata controls conversation-message inheritance under D3.
- `expand_from_result()` raises `OperationError` when the source is unknown. Model items are dumped
  into parameters; other items become `item_index` and stringified `item`. Every child records the
  source and strategy and receives an expansion edge from the source.
- The expansion strategy is stored in parameters/metadata and edge labels. In this builder method,
  it does not itself install inter-child dependency edges; `chain_context` changes the primary
  message-inheritance source only for sequential strategy children after the first.
- `add_aggregation()` uses explicit sources or current heads, raises when neither exists, stores
  source ids and count in parameters, and adds an edge from every source. `inherit_from_source` is
  clamped to the final source index.
- `add_conditional_branch()` creates a check node and labeled `if_true`/`if_false` outgoing edges.
  Labels alone are descriptive; executable gating requires an `EdgeCondition` consumed by D4.
- Builder methods mutate a generic `Graph`; `get_graph()` returns it. No builder method calls
  `flow()`, assigns runtime branches beyond metadata, or invokes an operation.

**Why this way.** Late branch binding lets one graph definition execute inside a session without
serializing a live branch into each node. Keeping construction separate makes graph topology
inspectable and testable before effects occur. The open `| str` operation name is required for
session-registered verbs and for built-ins not enumerated by the current literal.

### D2 — Static execution is acyclic, dependency-aware, and capacity-bounded

The base executor contract is:

```python
UNLIMITED_CONCURRENCY = int(os.environ.get("LIONAGI_MAX_CONCURRENCY", "10000"))

class DependencyAwareExecutor:
    def __init__(
        self,
        session: Session,
        graph: Graph,
        context: dict[str, Any] | None = None,
        max_concurrent: int = 5,
        verbose: bool = False,
        default_branch: Branch = None,
        alcall_params: AlcallParams | None = None,
        executor_ref: dict[str, Any] | None = None,
        on_branch_created: Callable[[Any], None] | None = None,
    ): ...

    async def execute(self) -> dict[str, Any]: ...
```

The session façade defaults to `max_concurrent=5`; the module-level `flow()` accepts `None`, which
the executor interprets as `UNLIMITED_CONCURRENCY`. The name means “use a high configured bound,” not
literal infinity: its inherited environment default is 10,000. `parallel=False` at the `flow()`
boundary forces `max_concurrent=1`. The code records no benchmark or dependency-capacity rationale
for 5 or 10,000; callers are expected to select the bound their operations can absorb.

Exact scheduling semantics:

- `execute()` rejects a cyclic graph with `OperationError` before branch allocation or invocation.
- Every edge carrying a condition must hold an `EdgeCondition` with an `apply` method. Invalid types
  or missing application behavior fail validation before work starts.
- Construction creates one `ConcurrencyEvent` for every `Operation`. A node already marked
  `COMPLETED` has its event set and its response copied into results immediately.
- Branches are preallocated under D3 before tasks are submitted.
- All operation coroutines are submitted through the selected `AlcallParams`; a shared
  `CapacityLimiter` encloses preparation and invocation. Capacity therefore bounds model/tool work
  and the immediately preceding context/branch materialization.
- An operation first evaluates incoming paths, then waits for all declared predecessors and
  aggregation sources, then crosses the pause gate, then acquires capacity.
- A failed `Event.invoke()` is represented by the operation's `FAILED` status and becomes
  `{"error": str(operation.execution.error)}` in `results`. An unexpected executor-level exception
  is defensively converted to the same mapping when no result exists.
- Cancellation, keyboard interrupt, and system exit set the completion event and re-raise. Every
  ordinary path also sets the event in `finally`, including skip, failure, and defensive exception.
  Downstream waiters therefore do not hang solely because an upstream node did not complete
  successfully.
- Progress callbacks receive `queued`, `started`, `completed`, or `failed`. A condition-skipped node
  currently reports `failed` to that callback even though its event/flow status is `SKIPPED`; the
  typed `FlowEvent` in D6 distinguishes this ordinary skip from an invoked operation failure. It is
  not fully authoritative for defensive executor exceptions, as D4 and D6 record.

**Why this way.** Per-node events express dependency settlement independently of task submission
order. A sequential traversal would leave independent work idle; unbounded submission without a
limiter would transfer overload to providers and tools. The limiter surrounds the full operation
boundary so capacity represents active operations, not only the final await.

### D3 — Branch reuse, cloning, and primary-message inheritance are explicit

Before execution, `_preallocate_all_branches()` scans `Operation` nodes:

```text
explicit branch_id resolves in Session.branches
    └─ reuse that exact Branch

no branch_id + (has predecessors or inherit_context)
    └─ clone Session.default_branch
       ├─ Session.include_branches(clone)
       ├─ call on_branch_created(clone), when supplied
       └─ optionally mark pending primary-message inheritance

no branch_id + root/no inheritance
    └─ resolve to executor default branch or Session.default_branch at invocation
```

Exact branch semantics:

- Explicit `branch_id` takes precedence. If lookup fails, the executor logs the miss and leaves the
  node for normal fallback rather than failing preallocation.
- Clones are made from `session.default_branch` with `sender=session.id`, stored in
  `operation_branches`, and included through the full session inclusion path. That path establishes
  ownership, observer, shared registry, hooks, memory, and exchange registration.
- The optional synchronous `on_branch_created` callback runs for each preallocated clone after
  session inclusion. It is a wiring seam; its return value does not affect execution.
- `inherit_context` here means conversation-message inheritance, not D4's data mapping. The clone is
  marked with `pending_context_inheritance` and the configured `primary_dependency`.
- When the operation is prepared after dependencies settle, the executor finds the primary
  predecessor's branch, clears the clone's message pile, and copies cloned predecessor messages.
  It then clears the pending flag. Only one primary dependency supplies conversation history.
- A node that was preallocated but has no usable primary result keeps its ordinary cloned history.
- Reactive children do not use this preallocation path. D6 clones from an explicit child branch,
  the emitter branch for dependent spawns, or the session default.

**Why this way.** Reusing an explicit branch allows intentional state accumulation. Cloning dependent
work prevents parallel operations from racing on one conversation while still letting a caller
choose one primary history to inherit. Preallocation avoids acquiring the session branch lock in
the operation hot path.

### D4 — Incoming paths gate execution; context is materialized and deep-merged

For each operation, the condition input is:

```python
{
    "result": predecessor_result_as_mapping_or_scalar,
    "context": executor.context.content,
}
```

The per-operation input context is assembled as:

```python
{
    "<predecessor-uuid>_result": predecessor_result,
    # ...one entry per settled, non-skipped predecessor with a result
    # then shared flow-context keys
}
```

The reserved live-steer entry shape and rendered instruction block are:

```python
{
    "operator_messages": [
        {
            "ts": float | str,
            "text": str,
            "rendered_into_op": str | None,  # runtime breadcrumb
        }
    ]
}
```

```text
[OPERATOR STEER]
A human operator sent these live corrections while this flow is running.
Attend to them before continuing. Most recent last.
- <UTC timestamp>: <text>
[/OPERATOR STEER]

<original instruction>
```

The static result envelope is:

```python
{
    "completed_operations": list[UUID],
    "operation_results": dict[UUID, Any],
    "final_context": dict[str, Any],
    "skipped_operations": list[UUID],
}
```

Exact condition, context, and result semantics:

- A node with no incoming edges has a valid path. With incoming edges, an edge whose predecessor was
  skipped is ignored; the node runs when **at least one** remaining edge condition returns true.
  An edge with no condition returns true. If no incoming path passes, the node is marked `SKIPPED`.
- Condition checks wait on the predecessor's completion event before reading its result. A failed
  predecessor contributes its error mapping and may still satisfy an unconditional edge; failure
  does not automatically skip all dependents.
- `_wait_for_dependencies()` then waits for every graph predecessor. Aggregations also wait for each
  UUID string named in `aggregation_sources` when it matches a completion-event id.
- Predecessor results are converted recursively to mappings except for string, integer, float, and
  Boolean scalars. Skipped predecessors contribute no result key.
- If an operation's existing `context` is a mapping, predecessor entries update it. If it is another
  value, that value is preserved under `original_context` before predecessor entries are added.
- Shared flow context is applied after predecessor context. It updates a mapping in place or is
  combined with `original_context` for a non-mapping. A colliding shared key therefore overrides a
  predecessor-derived key at preparation time.
- `_render_operator_messages()` always removes `operator_messages` from the operation's context;
  the raw queue is never passed to the model as JSON. Dictionary entries without a truthy
  `rendered_into_op` are formatted in list order, prepended to the instruction, and mutated with the
  current operation id as their breadcrumb. Non-dictionary entries and already-rendered entries do
  not render. Numeric timestamps become UTC `YYYY-MM-DDTHH:MM:SSZ`; unparseable timestamps fall back
  to `str(ts)`.
- A second last-chance check runs immediately before `Operation.invoke()`. If new unrendered entries
  reached the canonical shared queue after context preparation, it reattaches the queue long enough
  to render and pop it. Breadcrumb mutation is shared across the shallow context copies, so the same
  entry is consumed once across later operations (`lionagi/operations/flow.py`).
- When a successful operation response is a mapping containing `context`, `deep_update()` merges
  that value into the single shared `Note`. Nested non-conflicting keys survive.
- The response-side `context` value is not type-validated before `deep_update()`. A non-mapping
  raises after the operation response has already been stored. The defensive catch leaves that
  stored response and the operation's completed event status intact, does not apply the invalid
  context, and can omit the ordinary completed progress callback.
- Parallel operations can finish in either order. When their returned context mappings collide,
  there is no namespace or reducer; whichever deep merge executes later wins for that key.
- An exception raised while checking a condition is caught as an executor-level error mapping and
  releases the completion event, but the node's `EventStatus` is not explicitly set to `FAILED`.
  Static results therefore include the error entry as settled, and D6's status projection can label
  this defensive path `completed`. Invoked operation failures do set `FAILED` and project correctly.
- `completed_operations` is computed as every id in `results` that is not skipped. Because failed
  operations also receive error entries in `results`, the name includes failures. It means
  “settled with a result entry and not skipped,” not `EventStatus.COMPLETED`.
- `operation_results` is the authoritative per-id value/error mapping. `skipped_operations` is
  disjoint from `completed_operations`; `_validate_execution_results()` raises if the lists overlap.

**Why this way.** Namespacing predecessor results by UUID prevents ordinary fan-in collisions and
lets an aggregation inspect every source. A shared context supports flow-wide accumulation without
requiring each node to know all predecessors. The current ungoverned collision rule is simple but
nondeterministic; D4 records it honestly rather than claiming deterministic reduction. Rendering
operator corrections into the instruction gives them a dedicated, consume-once channel instead of
mixing control directives into ordinary model context.

### D5 — Pause is soft at operation boundaries; restored terminal nodes short-circuit

The control methods are synchronous and idempotent:

```python
class DependencyAwareExecutor:
    def pause(self) -> None: ...
    def resume(self) -> None: ...
```

Exact control and restoration semantics:

- `pause()` installs a new unset `ConcurrencyEvent` only when no pause exists. Repeated calls while
  paused keep the same gate.
- `resume()` sets the current gate and clears the reference. Calling it while not paused is a no-op.
  A later `pause()` creates a fresh event.
- Operations wait at the gate after condition/dependency waits and before acquiring the capacity
  limiter. Operations already past that boundary continue; pause does not cancel or interrupt them.
- A waiting operation emits a best-effort `NodePaused` signal. Signal construction/emission errors
  cannot break the pause path.
- An operation whose status is in `Event._TERMINAL_STATUSES` returns without invocation. When it has
  a response and no result entry, that response is restored into `results`; its completion event is
  set.
- Constructor-time special handling preloads only `COMPLETED` responses, but execution-time
  short-circuiting applies to all terminal statuses.
- The kernel does not load terminal state from disk, validate a checkpoint version, reconstruct a
  branch, or rebuild topology. A caller must restore the graph nodes and responses before calling
  the executor.

**Why this way.** A boundary pause has a clear safety property: admitted operations settle, new ones
wait. Interrupting active model/tool calls would require per-verb cancellation and compensation
semantics the graph kernel does not own. Terminal short-circuiting enables caller-managed replay
without coupling a reusable scheduler to one durability format.

### D6 — Reactive execution synchronizes and caps graph growth, then streams typed settlements

The default spawn payload is:

```python
class SpawnRequest(BaseModel):
    instruction: str
    assignee: str | None = None
    operation: Literal["operate", "chat", "communicate", "ReAct"] = "operate"
    independent: bool = False
    reason: str | None = None
```

The reactive and streaming contracts are:

```python
@dataclass(slots=True)
class FlowEvent:
    operation_id: str
    name: str
    status: str               # "completed" | "failed" | "skipped"
    result: Any
    spawned: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "completed"

class ReactiveExecutor(DependencyAwareExecutor):
    def __init__(
        self,
        *args: Any,
        spawn_type: type | None = None,
        node_builder: Any = None,
        max_spawn: int = 50,
        spawn_branch_setup: Callable[[Operation, Any], None] | None = None,
        **kwargs: Any,
    ): ...

    async def execute(self) -> dict[str, Any]: ...
    async def execute_stream(self): ...

    def inject(
        self,
        operation: Operation,
        *,
        after: Operation | str | None = None,
        independent: bool = False,
    ) -> bool: ...
```

Reactive execution returns the D4 envelope plus:

```python
{
    "spawned_operations": int,
    "escalated_operations": list[UUID],
    "dropped_spawns": [
        {
            "reason": "builder_error" | "null_child" | "cycle"
                      | "max_spawn_exceeded" | "duplicate",
            "assignee": str | None,
            "emitter_id": UUID | None,
            # optional: "op_id", "error"
        }
    ],
}
```

Exact reactive semantics:

- The executor reuses D2's acyclicity, edge validation, preallocation, capacity limiter, operation
  method, pause gate, context logic, and result storage. Initial and accepted nodes run in one task
  group.
- While running, it subscribes through the session's public observer to the configured spawn type
  and `EscalationRequest`. It unsubscribes in `finally`.
- Spawn requests are discovered both from bus delivery and by recursively scanning settled results
  through lists, tuples, Pydantic model fields, and mapping values to depth 4. The bound prevents an
  unbounded walk through arbitrary returned structures; the code records no rationale for exactly
  four nested levels.
- The same request object is de-duplicated by Python object identity. Its second sighting is recorded
  as a `duplicate` dropped spawn; semantically identical but distinct objects are not de-duplicated.
- The default node builder creates the requested operation (or `operate`) with only the request
  instruction. A custom builder exception becomes `builder_error`; a `None` child becomes
  `null_child`. Neither aborts the rest of the graph.
- A recorded `builder_error` keeps at most 500 characters of the exception string. The best-effort
  `NodeSpawned` signal keeps at most 512 characters of the child instruction. These caps bound
  observability payloads only; they do not alter the exception used for logging or the instruction
  stored on the operation. No rationale for the exact 500/512 split is recorded.
- Acceptance is guarded by a threading lock. The default cap is 50 accepted injected nodes; initial
  nodes do not count. At the cap, the child is dropped with `max_spawn_exceeded`. The code records no
  workload measurement for exactly 50; it is an inherited runaway guard and is caller-configurable.
- A new child is added to the graph and receives a completion event. A dependent spawn adds an edge
  from emitter to child; an independent spawn does not. If that mutation creates a cycle, the edge
  and newly added node/event are removed before returning false.
- An injected `Operation` whose UUID is already present is not rejected as an operation duplicate.
  It can receive another spawn edge, increments the spawn count, is marked spawned, and is scheduled
  again. Because it is not `newly_added`, it receives neither a new completion event nor branch
  assignment nor `parent_id` metadata. Request-identity de-duplication therefore does not make
  explicit operation reinjection idempotent.
- Accepted children increment the spawn count, are marked as spawned, and are scheduled through the
  same `_run_tracked()` path. Newly added dependent children store `parent_id` metadata.
- A newly added reactive child branch is always a clone. The base is the child's explicit branch
  when valid, otherwise the emitter's branch for a dependent spawn, otherwise the session default.
  The clone is session-included before `spawn_branch_setup(child, clone)` runs.
- `on_branch_created` from D3 is not called for reactive clones. The separate
  `spawn_branch_setup` seam is invoked, which leaves general persistence-hook parity unresolved.
- `inject()` outside a running executor returns false. Accepted explicit injections follow the same
  cap, edge, cycle, branch, and scheduling rules.
- An escalation with route `"higher_tier"` and an emitter creates an independent child of the same
  operation, copies parameters, and replaces the instruction with an escalation notice. Other
  routes record/emit the escalation without scheduling a child.

Exact streaming semantics:

- `_run_tracked()` emits one `FlowEvent` after each node settles. Status is `skipped` for the skip
  set, `failed` for `EventStatus.FAILED`, and `completed` otherwise. It discriminates ordinary
  invoked failures, but an executor-level exception that stored an error without setting the event
  status still projects as `completed`.
- `execute_stream()` uses `anyio.create_memory_object_stream(math.inf)`: the completion-event
  channel is unbounded and provides no backpressure. No recorded rationale establishes that unbound;
  it follows the assumption that completion events are small and short-lived.
- An asyncio task created with `asyncio.ensure_future()` owns the task group while the async
  generator drains the channel. This avoids spanning an AnyIO task-group context across `yield` and
  makes the current streaming surface asyncio-specific.
- Normal channel close is followed by awaiting the driver so driver exceptions propagate. If the
  consumer breaks early, the generator cancels and awaits the driver so remaining work does not
  keep running invisibly.

**Why this way.** A second scheduler would duplicate the most failure-sensitive parts of static
execution. Synchronized mutation plus an immediate acyclicity check preserves the graph invariant
while a hard cap prevents unbounded self-expansion. The typed event makes completion-order streaming
explicit and improves on the legacy aggregate result name, while retaining the defensive-error gap
documented above.

## Consequences

- Fixed and reactive graphs share dependency ordering, capacity, pause, branch, context, and dispatch
  semantics.
- Every upstream path releases its event, so a failed or skipped predecessor cannot deadlock a
  dependent solely by failing to signal settlement.
- Explicit branch reuse supports intentional shared history; default cloning isolates dependent
  work but increases session branch count and copying cost.
- Incoming edge conditions are OR paths, while dependency waiting is all-predecessor settlement.
  Contributors must not confuse “one valid path” with “wait for one predecessor.”
- The shared flow context is convenient but not deterministic under colliding parallel writes.
  Consumers needing reproducibility must avoid collisions today.
- Live operator messages are rendered once as an instruction prefix and withheld from raw model
  context. The shared queue is mutated with consumption breadcrumbs, so callers treating their
  input context as immutable will observe otherwise.
- `completed_operations` is a compatibility hazard because failures appear in it. Consumers needing
  truth must inspect operation status and results together. `FlowEvent.status` improves ordinary
  failure reporting but still labels defensive executor errors completed when no event failure was
  set.
- Reactive mutation is bounded and cycle-safe, but object-identity de-duplication does not provide
  durable or semantic idempotency across reconstruction, and reinjecting an existing operation UUID
  can schedule that node again without a fresh branch or completion event.
- Streaming offers immediate settlement events but is currently tied to asyncio and an unbounded
  in-memory channel.
- Pre-marked nodes enable caller-owned static resume, but reactive continuation remains unsafe
  without persisted spawned topology, edges, branches, messages, requests, and shared context.
- Reversing D1 or D2 is high-cost because graph definitions and all flow callers depend on them.
  Changing D4 collision policy is medium-cost and requires migration guidance for consumers.
  Replacing D6 internals is feasible if the result/event contracts and static-kernel reuse remain.
- Focused static, reactive, stream, parallelism, pause, and node tests support `τ ≈ 0.9`; they do not
  establish crash consistency or portability to every async backend.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Replace `completed_operations` with explicit completed, failed, and skipped collections, retaining a deprecated compatibility alias whose settled-not-skipped meaning is documented; classify defensive executor exceptions consistently in both the aggregate and `FlowEvent`. | M | (filled at issue-open time) |
| 2 | Define deterministic shared-context semantics by namespacing node outputs by default and requiring an explicit reducer for colliding shared keys; add parallel collision tests. | M | (filled at issue-open time) |
| 3 | Pass `on_branch_created` into reactive execution and invoke it for every injected clone; add identical persistence-hook coverage for preallocated and spawned branches. | S | (filled at issue-open time) |
| 4 | Extract reactive graph mutation and streaming-driver concerns behind internal collaborators while retaining one public executor contract; document the required async backend. | M | (filled at issue-open time) |
| 5 | Specify durable reactive continuation with persisted spawned topology, parent edges, operation requests, branch reconstruction, context, and conversation-history policy; keep the current fail-closed refusal until all fields can be restored. | L | (filled at issue-open time) |
| 6 | Validate that an operation response's `context` is a mapping before deep merge; on mismatch, produce one explicit failed status/result and add a regression test for progress and streaming status. | S | (filled at issue-open time) |
| 7 | Define existing-operation UUID injection as reject, no-op, or intentional rerun; enforce that choice before graph mutation and test branch, completion-event, and spawn-count behavior. | S | (filled at issue-open time) |

## Alternatives considered

### Execute the graph as a sequential traversal

This would make result order, context writes, and error propagation easy to reason about. It lost
because independent nodes would never overlap, discarding the principal value of a DAG. It also
would not remove the need for condition semantics or branch isolation.

### Schedule dependents by recursively awaiting predecessor tasks

This would avoid an explicit event table. It lost because terminal/restored/skipped nodes and
reactively injected nodes do not all originate from one stable task tree. Per-node completion events
provide a uniform settlement primitive and can be pre-set for restored work.

### Use one shared branch for every node

This would avoid cloning and make all prior messages visible. It lost because independent nodes
would mutate one conversation concurrently, producing nondeterministic history and provider-session
state. The explicit `branch_id` path remains available when shared accumulation is intentional.

### Clone a branch for every node, including independent roots with no metadata

This would maximize isolation and make allocation uniform. It lost because root nodes commonly use
the caller's intended default conversation; cloning every root changes history semantics and creates
unnecessary branches. The current rule clones only when dependency/history isolation requires it.

### Replace shared context with immutable per-edge payloads only

This would eliminate completion-order collisions and make dataflow fully explicit. It lost in the
shipped architecture because callers rely on a flow-wide workspace and operations can contribute
context without knowing every downstream edge. Deterministic namespacing/reducers remain the
smaller corrective path.

### Pass `operator_messages` through as ordinary context

This would avoid special-key handling and mutation of the caller's queue. It lost because live
corrections are control directives, not background data: leaving the raw list in context makes
consumption ambiguous and asks each operation to notice the convention. The shipped renderer turns
pending entries into a visible instruction prefix, removes the raw key, and records one consumption
breadcrumb.

### Treat any predecessor failure as automatic downstream skip

This would make failure propagation simple. It lost because some operations can consume error
results, unconditional alternate paths may remain valid, and edge conditions are the explicit place
to decide viability. Failed predecessors therefore settle and expose an error mapping.

### Build a separate reactive scheduler

This would isolate mutation and streaming complexity from the static executor. It lost because the
new scheduler would need to duplicate dependency events, branch allocation, capacity, pause,
conditions, context, and operation dispatch. Subclassing preserves one execution kernel while the
delta acknowledges that internal collaborators would improve maintainability.

### Permit unlimited reactive spawning

This would avoid dropping legitimate recursive work. It lost because a self-returning
`SpawnRequest` can grow without bound. The configurable cap provides an explicit failure record
instead of exhausting memory or provider capacity.

### Treat an existing operation UUID as an idempotent injected no-op

This would make explicit reinjection safe for callers retrying delivery. It did not ship: the
acceptance path tests only whether a node must be added, then still counts and schedules an existing
node. The current behavior is recorded rather than called idempotent; delta 7 requires an explicit
choice before consumers rely on conflict handling.

### Persist checkpoints inside `flow.py`

This would make resume appear self-contained. It lost because storage format, topology versioning,
branch serialization, partial-write policy, and degradation behavior are cross-layer concerns. The
kernel supports restored terminal nodes but does not pretend that is a complete durable replay.

### Use a task group directly across async-generator yields

This would avoid the asyncio driver task in streaming mode. It lost because AnyIO task-group context
ownership cannot safely span generator yields. The current detached driver is explicit, though it
creates the asyncio-specific portability delta.

## Notes

Primary implementation anchors are `lionagi/operations/node.py`,
`lionagi/operations/builder.py`, `lionagi/operations/flow.py`,
`lionagi/protocols/graph/edge.py`, `lionagi/casts/emission.py`, and
`lionagi/session/session.py`. Focused behavioral anchors live in
`tests/operations/test_operation_node.py`, `tests/operations/test_builder_extended.py`,
`tests/operations/test_flow.py`, `tests/operations/test_flow_parallelism.py`,
`tests/operations/test_flow_pause.py`, `tests/operations/test_flow_stream.py`, and
`tests/operations/test_reactive_flow.py`, plus operator-message coverage in
`tests/operations/test_flow_operator_steer.py`.
