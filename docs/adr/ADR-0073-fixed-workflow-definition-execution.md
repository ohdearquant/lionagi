# ADR-0073: Fixed workflow-definition execution

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: scheduling-control-plane
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0102, v0-0103

## Context

Studio persists authored graphs in `workflow_defs`. Names are unique, definitions are mutable in
place, and rows have no namespace, version, or content hash. `POST /api/workflow-defs/{def_id}/run`
loads one row, compiles it, creates a fresh `Session`, registers the engine operation, persists the
run, and calls `Session.flow()` directly. There is no planner and reactive expansion is not enabled.

The storage validator admits `input`, `chat`, `parse`, `fanout`, and `engine` node kinds. The
compiler executes only `input`, `chat`, and `engine`; `parse` and `fanout` fail with a structured
compile error. Removed `gate` nodes are rejected earlier by definition create/load validation and
are redundantly listed in the compiler's dropped set.

Chat models must be provider-prefixed when specified. Coding-engine cwd is resolved under an
operator-supplied run-level `base_dir`, with raw traversal rejection, symlink resolution,
containment, and existence checks. A definition cannot choose its own containment root.

The runner persists a session, branches, messages, authored graph metadata, and per-node lifecycle
signals. The session id is the public run id used by Studio history. There is no
`li flow run`, registry-backed name/version resolution, content-hash provenance, artifact manifest,
or fixed-lane control consumer. The separate `definitions` table versions only agent and playbook
files, so the earlier accepted workflow-registry design is not current code.

This ADR answers six problems:

- **P1 — Authored graph shape must fail early.** Duplicate ids, invalid references, removed kinds,
  unsafe conditions, and cycles must not reach the executor as ambiguous runtime failures.
- **P2 — Definition nodes need a deterministic OperationGraph mapping.** Input markers, chat turns,
  and engine definitions have different compilation rules.
- **P3 — Authored edge expressions are code-like input.** They need a closed grammar without
  `eval`, calls, imports, or hidden names.
- **P4 — Model and filesystem configuration cross safety boundaries.** Model strings need provider
  identity and engine cwd cannot escape an operator-owned root.
- **P5 — Studio runs need normal persistence.** A fixed run must appear in Session history with
  branches, messages, graph metadata, and terminal state.
- **P6 — Current storage cannot supply reproducible headless identity.** Mutable unversioned rows
  and a Studio-only route cannot pin a definition for CI/headless replay.

| Concern | Decision |
|---|---|
| Definition storage and validation | D1: Persist version-1 canvas specs in unversioned `workflow_defs` and validate graph shape on create/update/load. |
| Node compilation | D2: Treat `input` as context-only, compile `chat` to `chat_and_record`, compile `engine` through an EngineDef, and reject unsupported kinds. |
| Edge semantics | D3: Compile authored edges manually, evaluate conditions with a bounded AST grammar, and reject cycles or misleading input-node gates. |
| Model, engine, and cwd safety | D4: Require provider-prefixed chat models, validate effective engine options/budgets, and contain coding cwd under run `base_dir`. |
| Run and persistence lifecycle | D5: Use a fresh Session plus request-scoped StateDB connection; session id is run id and ordinary runtime failures return `status='failed'`. |
| Public identity boundary | D6: Describe the Studio id-only, unversioned contract honestly; headless version/hash/artifact/control behavior remains delta work. |

Out of scope:

- Planner calls and reactive `SpawnRequest` growth are forbidden by the fixed lane.
- Reactive controls and checkpoint resume from ADR-0069 do not apply.
- `parse`, `fanout`, and removed `gate` nodes are not emulated or silently dropped as executable
  behavior.
- Fixed workflow queue admission is not current; ADR-0072 owns the target worker adapter.
- A versioned workflow registry, namespace, content hash, CLI runner, artifact layout, and durable
  cancel are not shipped.
- General Python expression evaluation and graph cycles are deliberately unsupported.

## Decision

### D1 — WorkflowDefs are mutable version-1 canvas rows

The table is:

```sql
-- lionagi/state/schema.sql
CREATE TABLE workflow_defs (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  description TEXT,
  spec_json   JSON,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
);

CREATE INDEX idx_workflow_defs_name ON workflow_defs(name);
CREATE INDEX idx_workflow_defs_updated ON workflow_defs(updated_at);
```

The HTTP models are:

```python
# lionagi/studio/services/workflow_defs.py
class CreateWorkflowDefRequest(BaseModel):
    name: str
    description: str | None = None
    spec_json: dict[str, Any] | None = None

class UpdateWorkflowDefRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    spec_json: dict[str, Any] | None = None

class RunWorkflowDefRequest(BaseModel):
    inputs: dict[str, Any] | None = None
    base_dir: str | None = None
```

A valid spec has this native JSON shape:

```json
{
  "version": 1,
  "nodes": [
    {"id": "input", "kind": "input", "pos": {"x": 0, "y": 0}},
    {
      "id": "draft",
      "kind": "chat",
      "pos": {"x": 200, "y": 0},
      "config": {"prompt": "Draft the answer", "model": "openai/gpt-4.1-mini"}
    },
    {
      "id": "check",
      "kind": "engine",
      "pos": {"x": 400, "y": 0},
      "config": {"engine_def_id": "review-engine", "max_agents": 3}
    }
  ],
  "edges": [
    {"id": "e1", "from": "input", "to": "draft"},
    {"id": "e2", "from": "draft", "to": "check", "condition": "result != None"}
  ],
  "inputs": ["topic"],
  "outputs": ["check"]
}
```

Exact storage validation semantics:

- `spec_json=None` is accepted at create/update. Such a row cannot run; the runner later raises
  `WorkflowCompileError("workflow definition has no spec_json to run")`.
- Non-null spec must be an object with `version == 1`.
- A top-level `base_dir` is rejected. The containment root is a run request field.
- `nodes` and `edges` must be arrays, capped at 200 and 400 respectively. The caps bound canvas and
  compiler work; no recorded measurement explains the exact numbers.
- Every node is an object with unique non-empty string id, admitted kind, and numeric non-boolean
  `pos.x`/`pos.y`.
- Admitted storage kinds are `input | chat | parse | fanout | engine`. `gate` is not admitted.
- Chat config must be a mapping with non-empty string prompt. Optional model must be a string
  containing `/`.
- Every edge is an object with unique non-empty id; `from` and `to` must reference stored node ids.
  Optional condition must be a non-empty string.
- `inputs` and `outputs` must be arrays of strings. The current compiler does not use these lists to
  map runtime values or select returned outputs; run inputs become shared flow context.
- Definition name is trimmed and limited to 1–120 characters. Duplicate name raises
  `NameConflictError`; the create route returns HTTP 409.
- Create ids are 12 hex characters. Update mutates the same row; delete physically deletes it.
- A saved legacy `gate` node found during GET returns HTTP 422 with the offending ids rather than
  returning an unusable definition.
- Empty update fields return success without changing the row.

Why this way: a compact JSON canvas lets Studio persist and redraw authored topology without a
second file format. In-place mutation is simple, but it cannot identify the exact historical
definition used by a run.

Code anchors: `lionagi/studio/services/workflow_defs.py` (`_validate_spec`, request models, routes);
`lionagi/state/db.py` (workflow-def methods); `lionagi/state/schema.sql`.

### D2 — Three executable kinds compile to the shared graph kernel

Compiler contract:

```python
# lionagi/studio/services/workflow_compile.py
EXECUTABLE_NODE_KINDS = frozenset({"input", "chat", "engine"})
DROPPED_NODE_KINDS = frozenset({"parse", "fanout", "gate"})

async def compile_workflow_def(
    spec: dict[str, Any],
    *,
    resolve_engine_def: Callable[[str], Awaitable[dict[str, Any] | None]],
    base_dir: str | None = None,
) -> tuple[Graph, dict[str, str]]: ...

class WorkflowCompileError(Exception):
    message: str
    node_id: str | None
    edge_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.message, "node_id": self.node_id, "edge_id": self.edge_id}
```

Compilation returns the OperationGraph and a map from authored node id to internal Operation id.

Exact node semantics:

- `parse`, `fanout`, or `gate` anywhere in the compiler input raises a node-addressed
  `WorkflowCompileError` before any node executes. Unknown kinds do the same.
- `input` creates no Operation. The run's `inputs` dict is passed uniformly as
  `Session.flow(context=inputs or {})`.
- `chat` requires `config.prompt`. It compiles to `chat_and_record`, not plain `chat`, because the
  latter does not append the turn through branch message persistence. The assistant response text
  becomes the operation result for downstream context/conditions.
- An explicit chat model creates `iModel(model=<provider/name>)` at compile time and is passed as the
  Branch `imodel` override.
- `engine` requires string `config.engine_def_id`. The runner resolves by id first, then by name.
  A miss raises a node-addressed compile error.
- Engine options are the stored EngineDef options overlaid by node `config.options`. Node
  `max_depth`/`max_agents` override stored values only when non-null.
- Effective options, kind-specific requirements, and integer budgets are revalidated at compile
  time so a WorkflowDef cannot bypass EngineDef write validation. Budgets are integers in `[1,100]`.
- Builder auto-chaining is disabled after every added node; authored edges alone establish
  dependencies.
- An empty graph is allowed if all nodes are `input` or there are no nodes. `Session.flow()` then
  returns an empty operation result rather than calling a planner.

The fixed topology invariant is structural: `run_workflow_def()` calls `session.flow(graph, ...)`
without `reactive=True`, `spawn_type`, or a reactive node builder. Authored topology is not expanded
at runtime.

Why this way: compilation adapts authored concepts to the existing dependency-aware kernel. Using
`chat_and_record` and the registered engine operation preserves normal messages and branches
without building a standalone workflow executor.

### D3 — Edges use a bounded safe-expression grammar

Conditions compile to:

```python
class StudioExprCondition(EdgeCondition):
    expr: str = Field(...)

    async def apply(self, context: Any = None, *args: Any, **kwargs: Any) -> bool: ...
```

They evaluate only against:

```python
{
    "result": <upstream operation result>,
    "context": <shared flow context>,
}
```

Allowed syntax is:

- comparisons `== != < <= > >= in not in`;
- boolean `and`, `or`, unary `not`;
- string, integer, float, boolean, and `None` literals;
- list and tuple literals composed from allowed values;
- names, non-private attribute access, and subscription/key access.

Calls, lambdas, comprehensions, formatted strings, assignment expressions, imports, private names,
and private attributes are rejected. The implementation walks the parsed AST and never calls
`eval`, `exec`, `compile`, or `__import__`.

Budgets are `_MAX_EXPR_LEN = 1000` characters and `_MAX_AST_DEPTH = 20`. They bound parser/evaluator
work and recursion; source contains no empirical rationale for the exact values.

Exact edge semantics:

- Bad syntax, disallowed AST node, excessive length, or excessive nesting becomes
  `WorkflowCompileError` carrying the authored edge id.
- A missing top-level name at evaluation raises `UnsafeExpressionError`; missing dict attribute,
  object attribute, key, or index resolves to `None`.
- Boolean evaluation short-circuits. Chained comparisons update the left operand conventionally.
- An edge whose target is `input` or another non-operation node is rejected because no internal
  target exists.
- An unconditional edge from `input` to an executable node is omitted: input data already enters
  through shared flow context.
- A conditional edge from `input` is rejected. Dropping it would make the target run
  unconditionally and silently ignore the authored gate.
- Labels become a zero- or one-element edge-label list.
- After edge creation, any graph cycle raises a graph-level compile error. Iteration must live
  inside an operation, not an authored graph cycle.
- At execution, a node with several incoming conditional edges runs when at least one incoming path
  remains valid; it is skipped only when all incoming paths fail. This is the underlying
  `DependencyAwareExecutor._check_edge_conditions()` contract.

Why this way: the grammar gives designers simple conditional routing without a Python code-execution
surface. Rejecting misleading input-node conditions is more honest than compiling a different
graph from the one authored.

Code anchors: `lionagi/studio/services/workflow_compile.py` (`_parse_expr`, `_validate_node`,
`_eval_node`, `StudioExprCondition`, `compile_workflow_def`); `lionagi/operations/flow.py`
(`_check_edge_conditions`).

### D4 — Chat identity and coding cwd are validated before execution

Chat model rule:

```text
config.model omitted        → use the branch's default iModel
config.model="provider/name" → construct an explicit iModel override
config.model without "/"    → validation/compile error
```

Engine definitions are a separate store with kinds
`research | review | coding | hypothesis | planning`. Effective options admit only `test_cmd` and
`export_dir`; option values are strings that do not begin with `-` and match a conservative token
grammar. Coding requires a non-blank `test_cmd`. `max_depth` and `max_agents`, when supplied, are
integers from 1 through 100.

Engine runtime closure:

```python
def make_engine_operation(
    session: Any,
    *,
    on_branch_created: Callable[[Any], None] | None = None,
) -> Callable[..., Awaitable[Any]]: ...

async def _engine_op(
    context: dict[str, Any] | None = None,
    engine_kind: str = "",
    engine_model: str | None = None,
    engine_max_depth: int | None = None,
    engine_max_agents: int | None = None,
    engine_options: dict[str, Any] | None = None,
    engine_workspace: str | None = None,
    **_ignored: Any,
) -> Any: ...
```

The closure resolves the existing CLI engine-class registry, constructs the engine, derives a text
input from predecessor result context first and shared inputs second, and calls `engine.run()` in
the same Session. Result models are dumped to JSON; strings become `{"result": <text>}`; total
sub-agent failure becomes `{"error": ...}`.

Coding cwd contract:

```python
def _resolve_node_cwd(
    node_id: str,
    raw_cwd: Any,
    base_dir: str | None,
) -> str: ...
```

Exact cwd semantics, in order:

1. `config.cwd` must be a non-empty string.
2. Raw `..` traversal is rejected before resolution, even if a later resolution would return
   inside the root.
3. A node cwd requires run-level `base_dir`; absence is a compile error.
4. Relative cwd joins the resolved base; absolute cwd is permitted only if it resolves inside the
   base.
5. Both base and candidate resolve symlinks before `relative_to(base)` containment.
6. The resolved path must exist and be a directory.
7. Only an EngineDef whose kind is `coding` may set cwd; other kinds fail compilation.
8. The definition itself cannot contain top-level `base_dir`, so contributed content cannot choose
   its own safety root.

A coding workspace is passed as `engine.run(..., workspace=<resolved>)`. Research/review/planning
engines do not receive a cwd keyword. A node with no cwd is unaffected by whether `base_dir` was
supplied.

Why this way: provider prefixes prevent an ambiguous model string from being interpreted by a
default provider. Run-owned containment prevents a saved definition from escaping the operator's
chosen filesystem boundary, including through symlinks.

### D5 — A fixed run is a persisted Session executed in the request

The run contract is:

```python
# lionagi/studio/services/workflow_run.py
async def run_workflow_def(
    def_id: str,
    inputs: dict[str, Any] | None = None,
    *,
    base_dir: str | None = None,
    _session: Any | None = None,
) -> dict[str, Any]: ...

# return shape (the annotation is plain dict[str, Any]; "completed"/"failed" is the
# observed value set, not a Literal-typed contract)
{"run_id": str(session.id), "status": "completed|failed"}
```

The route is declared `status_code=202`, but it awaits `run_workflow_def()` to completion before
returning the body. It is not a background admission response and does not return a queued handle.

Exact sequence:

1. Load WorkflowDef by id. Missing row raises `WorkflowNotFoundError` and the route returns 404.
2. Reject missing/empty spec with structured compile error.
3. Resolve EngineDefs and compile before creating a Session persistence row. Compile failure returns
   HTTP 422 with `{error,node_id,edge_id}` and creates no run session.
4. Build an `early_graph` projection from executable authored nodes/edges for Studio rendering.
5. Create a fresh `Session` (or use the private injected test seam).
6. Open a request-scoped `StateDB`, create progression/session rows with
   `invocation_kind='flow'`, `status='running'`, WorkflowDef id/name, and early graph metadata.
7. Bind observer persistence and per-branch message hooks.
8. Register the `engine` operation closure, including branch-persistence callbacks for engine
   sub-agents.
9. Execute `Session.flow(graph, context=inputs or {}, on_progress=..., on_branch_created=...)`.
10. Terminalize and close only this request-scoped DB connection.

The request-scoped connection is deliberate. CLI orchestration helpers use a process-wide shared
DB registry suitable for one-shot processes; closing that registry from one Studio request could
tear down concurrent runs.

Exact outcome semantics:

- Per-node queued/started/completed/failed signals persist through `flow_progress_signals`.
- Flow-created branch clones and engine-created sub-agent branches receive persistence hooks after
  creation; setup-time branches are hooked initially.
- If `operation_results` contains any dict with an `error` key, overall status becomes `failed`.
- An ordinary runtime exception is caught, status becomes `failed`, teardown records the exception,
  and the function returns `{run_id,status}` rather than re-raising a bare server error.
- `asyncio.CancelledError` sets status `cancelled`, tears down persistence, then re-raises; the
  annotated return type does not list cancelled because no normal body is returned on that path.
- Session id is the run id read by `/api/sessions/{id}` and Studio history; no parallel workflow-run
  identity is created.
- The result payload does not include operation outputs or an artifact path. Those remain in normal
  session/branch persistence and operation result handling.
- No control poller, checkpoint writer, queue lease, planner, or reactive node builder is started.

Why this way: the fixed lane gets standard graph execution, branches, messages, and Studio history
without another runtime. Awaiting inline keeps implementation simple but makes HTTP 202 semantically
closer to a completed execution response than deferred admission.

### D6 — Current identity is id-only and unversioned

The current source of truth is `workflow_defs`. It supports lookup by id and by name internally,
but the run route accepts only `def_id`. Session metadata records:

```json
{
  "workflow_def_id": "12hexid",
  "workflow_def_name": "daily-review",
  "early_graph": {"nodes": [], "edges": []}
}
```

It does not record definition version, content hash, namespace, original `spec_json`, or artifact
manifest. A later edit to the same row changes what future runs compile; old run metadata cannot by
itself reconstruct the exact prior definition.

The other versioned store is not a current workflow registry:

```sql
CREATE TABLE definitions (
  id         TEXT PRIMARY KEY,
  kind       TEXT NOT NULL CHECK(kind IN ('agent', 'playbook')),
  name       TEXT NOT NULL,
  path       TEXT NOT NULL,
  content    TEXT NOT NULL,
  version    INTEGER NOT NULL,
  created_at REAL NOT NULL,
  message    TEXT
);

CREATE UNIQUE INDEX idx_def_unique_version
  ON definitions(kind, name, version);
```

Exact public-boundary semantics:

- There is no `li flow run <name-or-file>` parser or binding.
- There is no stable name/version resolution for fixed runs.
- There is no content-hash comparison at run time.
- There is no standard per-run artifact directory/manifest owned by this runner.
- There is no fixed-run cancellation endpoint or `session_controls` consumer.
- None of these missing features may be inferred from the existence of the shared Session kernel.

Why this way: this section records the honest current boundary. Choosing whether to version
`workflow_defs` in place or migrate into a widened registry is a future architectural decision; the
current implementation provides evidence for neither choice as already settled.

## Consequences

- Fixed authored graphs reuse dependency execution, edge conditions, branch isolation, lifecycle
  signals, and Studio history without a second kernel.
- Unsupported nodes, unsafe expressions, cycles, ambiguous chat models, invalid engine budgets, and
  unsafe coding cwd fail before provider execution.
- `chat_and_record` and dynamic branch hooks preserve transcripts for authored and engine-created
  branches.
- The route is coupled to Studio and awaits the whole run despite HTTP 202.
- Mutable unversioned definitions prevent exact historical replay and stable headless identity.
- Input/output declarations are validated canvas metadata but not a runtime mapping/output-selection
  contract.
- Runtime failure normally becomes a returned failed status, while compile failure is an HTTP 422
  before a run id exists.
- Reversing D2-D5 into a standalone executor would be costly and would duplicate kernel semantics.
  Replacing D1/D6 with versioned identity requires schema and historical-run migration but can
  retain the compiler/runner behind a resolution adapter.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|---|---|---|
| 1 | Implement `li flow run <name-or-file>` over the existing compiler-runner; acceptance: fixed workflows run without a Studio daemon and never call the planner. | M | (filled at issue-open time) |
| 2 | Choose and migrate to one versioned workflow registry with namespace and content hash; acceptance: name-mode runs pin the exact version and hash while existing WorkflowDefs remain loadable or fail with an explicit migration error. | L | (filled at issue-open time) |
| 3 | Materialize a stable fixed-run artifact manifest and per-node result files; acceptance: completed and ordinary failed runs expose the same documented layout and record its path on the session. | M | (filled at issue-open time) |
| 4 | Define fixed-run cancellation and terminalization; acceptance: queued or running fixed work reaches one durable cancelled state without inheriting unsupported reactive controls. | M | (filled at issue-open time) |
| 5 | Make the run HTTP contract honest about admission versus completion; acceptance: either return immediately with a durable queued/running handle or use a completion response status and document its blocking behavior. | S | (filled at issue-open time) |
| 6 | Define runtime semantics for declared `inputs` and `outputs`; acceptance: input validation/mapping and selected output payloads are deterministic or the fields are removed from the executable spec. | M | (filled at issue-open time) |

## Alternatives considered

### A new standalone graph executor

A fixed-specific executor could directly understand WorkflowDef nodes and avoid compilation. It
would buy a closer domain model, but would duplicate dependency waits, conditional edges, branch
allocation, result assembly, and persistence integration already in `Session.flow()`. It lost
because a compiler adapter is a narrower seam.

### Send WorkflowDefs through the planner

A planner could translate unsupported nodes and repair incomplete graphs. It lost because fixed
topology and reproducibility are the lane's defining guarantees. Planner output could change the
authored graph and turn compile errors into nondeterministic behavior.

### Silently ignore unsupported nodes

Dropping `parse`, `fanout`, or legacy `gate` would allow more old definitions to run. It lost
because downstream edges and outputs would no longer mean what the canvas shows. Structured
node-addressed failure is safer.

### Python `eval` for edge conditions

`eval` would buy richer expressions with very little code. It lost because authored definitions
are external input and calls/imports/dunder access create code execution. The bounded AST grammar
covers comparisons and boolean routing without that surface.

### Conditions as dedicated gate nodes

Gate nodes could make routing visible on the canvas. They lost in the current model because
conditions already belong to edges and a gate node introduces unclear data/result semantics. Saved
legacy gates are rejected with migration guidance.

### Let a definition carry `base_dir`

This would make definitions self-contained and portable between callers. It lost because the
definition would choose its own containment root, defeating the security boundary. The operator
who invokes the run owns the root.

### Allow cwd on all engine kinds

A uniform config field would simplify the schema. It lost because only the coding engine's current
`run()` signature accepts `workspace`; forwarding it to other engines would fail at runtime. The
compiler rejects unsupported combinations early.

### Reuse the process-wide CLI DB registry

This would reduce persistence code. It lost because Studio is long-lived and concurrent; one
request's teardown could close connections used by another run. Request-scoped StateDB ownership
matches request lifetime.

### Keep Studio as the only entry permanently

One entry avoids CLI/schema migration. It lost as the target because headless and CI consumers need
daemon-free fixed execution and stable source provenance. The current route remains the honest
retrospective implementation until Delta 1 lands.

### Promote `workflow_defs` versus widen `definitions`

Both remain viable. Versioning `workflow_defs` in place preserves designer identity and JSON shape;
widening `definitions` reuses version, name, path/content history, and registry tooling. Neither is
selected because current source implements neither and migration consequences have not been
decided. Delta 2 is intentionally a decision gate, not a fabricated conclusion.

## Notes

`gate` rejection is not normally a compiler error: create/load validation excludes it before the
compiler. `parse` and `fanout` are the stored kinds that reach the compiler and receive structured
dropped-kind errors. Coding cwd is engine-kind-specific, not a universal per-node field.
