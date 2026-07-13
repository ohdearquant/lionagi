# DAG Pipeline API

Two ways to run dependency-aware work:

- **CLI** (`li o flow`): a planner emits `TaskAssignment` items whose `depends_on`
  values form the initial DAG, and workers may expand it reactively
- **Python API** (`Builder` + `Session.flow()`): you construct the DAG explicitly

This page covers the Python API path.

## Quick example

```python
import asyncio
import lionagi as li
from lionagi import Builder

async def main():
    session = li.Session()
    builder = Builder()

    n1 = builder.add_operation(
        "communicate",
        instruction="Research quantum error correction techniques.",
    )
    n2 = builder.add_operation(
        "communicate",
        instruction="Write an executive summary of the research.",
        depends_on=[n1],
    )

    results = await session.flow(builder.get_graph(), parallel=True)
    print(results["operation_results"][n2])

asyncio.run(main())
```

```text
# output:
Quantum error correction uses...
```

## `Builder` (`OperationGraphBuilder`)

```python
from lionagi import Builder

builder = Builder(name="MyGraph")
```

### `add_operation()`

```python
node_id = builder.add_operation(
    operation="communicate",
    node_id=None,              # optional reference label
    depends_on=None,           # list of node_ids this depends on
    inherit_context=False,     # inherit conversation history from dependency
    branch=None,               # assign to a specific Branch
    instruction="...",         # passed to branch.operate() or branch.communicate()
    **parameters,              # any Branch operation kwargs
)
```

Returns the node ID used in `depends_on` lists and result lookups.

`operation` must be the name of a `Branch` method: `"communicate"`, `"operate"`, `"ReAct"`, `"parse"`, etc.

`Builder` is incremental. The first operation has no predecessor. After that,
omitting `depends_on` (or passing an empty list) attaches the new operation after
every current head with sequential edges. This is convenient for building a chain,
but it does **not** create another independent root.

### `add_aggregation()`

```python
agg_id = builder.add_aggregation(
    operation="communicate",
    source_node_ids=[n1, n2, n3],  # defaults to current graph heads
    inherit_context=False,
    inherit_from_source=0,
    instruction="Synthesize the above findings.",
)
```

Adds a node that depends on multiple sources — useful for fan-in synthesis.

### `expand_from_result()`

```python
from pydantic import BaseModel
from lionagi.operations import ExpansionStrategy

class AnalysisTask(BaseModel):
    instruction: str

new_ids = builder.expand_from_result(
    items=[
        AnalysisTask(instruction="Analyze latency."),
        AnalysisTask(instruction="Analyze reliability."),
    ],
    source_node_id=n1,
    operation="communicate",
    strategy=ExpansionStrategy.CONCURRENT,
    inherit_context=False,
)
```

Expands siblings from a source — useful for iterative or fan-out pipelines. Fields
from Pydantic items become operation parameters. Non-model items are supplied as the
`item` string plus `item_index`; use an operation that accepts those parameters.

`strategy` is declarative builder metadata. Every item receives the strategy value
in its parameters and metadata, and every graph edge still runs directly from the
source node to that child. The strategy does not add child-to-child dependency
edges or implement executor scheduling or chunk boundaries.

**`ExpansionStrategy` values**:

| Value | Builder behavior |
|-------|------------------|
| `CONCURRENT` | Labels each source-to-child expansion and makes the expanded nodes the builder's current heads |
| `SEQUENTIAL` | Labels each source-to-child expansion and makes the expanded nodes the builder's current heads; it does not chain siblings |
| `SEQUENTIAL_CONCURRENT_CHUNK` | Records the label on source-to-child expansions; no chunk scheduling is created and current heads are unchanged |
| `CONCURRENT_SEQUENTIAL_CHUNK` | Records the label on source-to-child expansions; no chunk scheduling is created and current heads are unchanged |

Actual execution order comes from graph dependencies and the `Session.flow()`
options. With `inherit_context=True`, `chain_context=True` and `SEQUENTIAL`, the
builder can point each child's context inheritance metadata at the previous child;
that still does not create a graph dependency between those children.

### Other builder methods

```python
# add a conditional branch structure
ids = builder.add_conditional_branch(
    condition_check_op="communicate",
    true_op="communicate",
    false_op="communicate",
    instruction="Is this claim factual? Answer YES or NO.",
)
# ids: {"check": id, "true": id, "false": id}

# mark nodes as already executed (for incremental builds)
builder.mark_executed([n1, n2])

# get unexecuted nodes
pending = builder.get_unexecuted_nodes()

# get node by reference label
node = builder.get_node_by_reference("my_label")

# inspect graph state
state = builder.visualize_state()
# {"total_nodes": 4, "executed_nodes": 2, "unexecuted_nodes": 2, ...}

# get the Graph object for session.flow()
graph = builder.get_graph()
```

## Execution via `Session.flow()`

```python
results = await session.flow(
    graph,
    context={"domain": "finance"},  # injected into all nodes
    parallel=True,
    max_concurrent=5,
    verbose=True,
)
```

See [session.md#flow](session.md#flow) for full parameter reference.

For event-at-a-time integration, iterate `session.flow_stream(...)`. For a graph that
may grow while running, set `reactive=True` and provide the spawn emission type and
node builder expected by your application. Reactive expansion is bounded by
`max_spawn`.

## Parallel execution semantics

The executor runs graph roots and newly-ready nodes concurrently, up to
`max_concurrent`, when `parallel=True`. A node waits until its incoming dependencies
are satisfied.

Do not confuse executor readiness with Builder shorthand: consecutive
`add_operation()` calls without `depends_on` are linked sequentially by the builder.
To express parallel work with this incremental API, expand concurrent children from
a source node, then aggregate them. If constructing a `Graph` directly, independent
root nodes are naturally eligible in the same wave.

Assigning the same `branch=` controls which conversation executes an operation; it
does not replace dependency edges. Add explicit edges whenever turns must be ordered
to avoid concurrent mutation of one branch's history.

```python
branch_a = session.new_branch(name="analyst")

n1 = builder.add_operation("communicate", branch=branch_a, instruction="Step 1")
n2 = builder.add_operation("communicate", branch=branch_a, instruction="Step 2", depends_on=[n1])
n3 = builder.add_operation("communicate", branch=branch_a, instruction="Step 3", depends_on=[n2])
# explicit dependencies make n1 → n2 → n3 sequential
```

## CLI flow vs Python builder

| Aspect | `li o flow` | Python `Builder` |
|--------|-------------|-----------------|
| DAG construction | LLM emits assignments and dependencies | You define explicitly |
| Flexibility | High (natural language) | Total (programmatic) |
| Live expansion | Built in through `SpawnRequest` and `--reactive` policy | Opt in with `reactive=True`, a spawn type, and a node builder |
| Typing | `list[TaskAssignment]` | `Operation` objects |
| Best for | Ad-hoc orchestration | Application embedding |

## Full example: fan-out + synthesis

```python
import asyncio
import lionagi as li
from lionagi import Builder
from lionagi.operations import ExpansionStrategy
from pydantic import BaseModel

class WorkItem(BaseModel):
    instruction: str

async def analyze(topic: str) -> str:
    session = li.Session()
    builder = Builder()

    # A source operation establishes the shared topic. expand_from_result then
    # creates three siblings that are all ready after this source completes.
    root = builder.add_operation(
        "communicate",
        instruction=f"State the key facts needed to assess: {topic}",
    )
    worker_ids = builder.expand_from_result(
        items=[
            WorkItem(instruction=f"Analyze the technical feasibility of: {topic}"),
            WorkItem(instruction=f"Analyze the market impact of: {topic}"),
            WorkItem(instruction=f"Analyze the regulatory risk of: {topic}"),
        ],
        source_node_id=root,
        operation="communicate",
        strategy=ExpansionStrategy.CONCURRENT,
    )

    synthesis_id = builder.add_aggregation(
        operation="communicate",
        source_node_ids=worker_ids,
        instruction="Synthesize these three analyses into an executive brief.",
    )

    results = await session.flow(builder.get_graph(), parallel=True, max_concurrent=3)
    return results["operation_results"][synthesis_id]

asyncio.run(analyze("open-source LLM deployment in regulated industries"))
```

## Note context accumulation

Flow operations accumulate cross-node context in a `Note` object internally.
Each operation can read from and write to this shared note during execution.

- `deep_update()` merges nested dicts across operations — keys are merged recursively.
- List values are **replaced** (last writer wins), not concatenated.
- The final merged context is available as `results["final_context"]` (a plain `dict`,
  not the `Note` wrapper) after `session.flow()` completes.

```python
results = await session.flow(builder.get_graph(), parallel=True)
accumulated = results["final_context"]  # plain dict with merged state
```

→ Note API: [note.md](note.md)

Next: [Team messaging](team.md) — inter-branch messaging
