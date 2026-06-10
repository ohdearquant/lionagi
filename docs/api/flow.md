# DAG Pipeline API

Two ways to run DAG pipelines:

- **CLI** (`li o flow`): the orchestrator plans the DAG automatically from a prompt
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
Run completed: 2 nodes, 1 parallel wave
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

Returns: `str` — node ID used in `depends_on` lists.

`operation` must be the name of a `Branch` method: `"communicate"`, `"operate"`, `"ReAct"`, `"parse"`, etc.

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
new_ids = builder.expand_from_result(
    items=results["operation_results"][n1],
    source_node_id=n1,
    operation="communicate",
    strategy=ExpansionStrategy.CONCURRENT,
    inherit_context=False,
    instruction="Analyze this item: {item}",
)
```

Expands the graph dynamically after partial execution — useful for iterative pipelines.

**`ExpansionStrategy` values**:

| Value | Behavior |
|-------|---------|
| `CONCURRENT` | All expanded nodes run in parallel |
| `SEQUENTIAL` | Expanded nodes chain one after another |
| `SEQUENTIAL_CONCURRENT_CHUNK` | Sequential chunks, concurrent within each chunk |
| `CONCURRENT_SEQUENTIAL_CHUNK` | Concurrent chunks, sequential within each chunk |

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
# {"total_nodes": 4, "executed": 2, "pending": 2, ...}

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

## Parallel execution semantics

Nodes without `depends_on` run concurrently (up to `max_concurrent`).
Nodes with `depends_on` wait for all dependencies to complete.
Nodes sharing a `branch=` reference run sequentially within that branch's message history —
this lets one agent accumulate context across multiple DAG nodes.

```python
branch_a = session.new_branch(name="analyst")

n1 = builder.add_operation("communicate", branch=branch_a, instruction="Step 1")
n2 = builder.add_operation("communicate", branch=branch_a, instruction="Step 2", depends_on=[n1])
n3 = builder.add_operation("communicate", branch=branch_a, instruction="Step 3", depends_on=[n2])
# n1 → n2 → n3 run sequentially on branch_a, accumulating history
```

## CLI flow vs Python builder

| Aspect | `li o flow` | Python `Builder` |
|--------|-------------|-----------------|
| DAG construction | LLM plans it | You define explicitly |
| Flexibility | High (natural language) | Total (programmatic) |
| Re-planning | Built-in (control nodes) | Manual via `expand_from_result` |
| Typing | `FlowPlan` schema | `Operation` objects |
| Best for | Ad-hoc orchestration | Application embedding |

## Full example: fan-out + synthesis

```python
import asyncio
import lionagi as li
from lionagi import Builder

async def analyze(topic: str) -> str:
    session = li.Session()
    builder = Builder()

    aspects = ["technical feasibility", "market impact", "regulatory risk"]
    worker_ids = []
    for aspect in aspects:
        nid = builder.add_operation(
            "communicate",
            instruction=f"Analyze the {aspect} of: {topic}",
        )
        worker_ids.append(nid)

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
