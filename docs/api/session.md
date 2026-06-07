# `Session`

```python
class Session(Node, Relational)
```

Manages multiple `Branch` instances with inter-branch messaging and DAG execution.

## Constructor

```python
session = Session(
    branches=None,
    exchange=None,
    default_branch=None,
    name="Session",
    user=None,
)
```

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `branches` | `Pile[Branch] \| None` | `None` | Pre-existing branches |
| `exchange` | `Exchange \| None` | `None` | Custom exchange instance |
| `default_branch` | `Branch \| None` | `None` | Default for delegated operations |
| `name` | `str` | `"Session"` | Human label |
| `user` | `SenderRecipient \| None` | `None` | Session owner identity |

## Branch management

### `new_branch()`

```python
branch = session.new_branch(
    system="You are a research assistant.",
    name="researcher",
    chat_model=li.iModel(model="gpt-4o"),
    tools=[search_fn],
    as_default_branch=True,
)
```

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `system` | `System \| JsonValue` | `None` | System prompt |
| `user` | `SenderRecipient` | `None` | Branch owner |
| `name` | `str \| None` | `None` | Human label |
| `chat_model` | `iModel \| None` | `None` | Chat model for the branch |
| `messages` | `Pile[RoledMessage]` | `None` | Restore prior history |
| `tools` | `list[Callable]` | `None` | Pre-registered tools |
| `as_default_branch` | `bool` | `False` | Set as session default |

Returns: `Branch`

### `get_branch()`

```python
branch = session.get_branch(branch_id)
branch = session.get_branch("researcher")  # by name
```

Returns: `Branch`. Raises `KeyError` if not found and no `default` given.

### Other branch operations

```python
session.include_branches([b1, b2])          # add existing branches
session.remove_branch(branch_id)            # remove (keeps object)
session.remove_branch(branch_id, delete=True)  # remove + delete
session.change_default_branch(branch_id)    # swap default
b2 = session.split(branch_id)              # sync clone with same history
b2 = await session.asplit(branch_id)       # async clone
```

## DAG execution

### `flow()`

```python
from lionagi import Builder

builder = Builder()
n1 = builder.add_operation("communicate", instruction="Research X")
n2 = builder.add_operation("communicate", instruction="Summarize", depends_on=[n1])
graph = builder.get_graph()

results = await session.flow(
    graph,
    context={"topic": "quantum computing"},
    parallel=True,
    max_concurrent=5,
)
# results: dict[node_id, Any]
```

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `graph` | `Graph` | required | Built via `OperationGraphBuilder` |
| `context` | `dict \| None` | `None` | Shared context injected into all nodes |
| `parallel` | `bool` | `True` | Run dependency-free nodes concurrently |
| `max_concurrent` | `int` | `5` | Concurrency cap |
| `verbose` | `bool` | `False` | Print progress |
| `default_branch` | `Branch \| ID.Ref \| None` | `None` | Branch for unassigned nodes |

Returns: `dict[str, Any]` — keyed by node ID.

For building `graph`, see [flow.md](flow.md).

## Message exchange

For inter-branch messaging, see [team.md](team.md).

Brief reference:

```python
# queue a message
session.send(sender_id, recipient_id, "analysis complete")

# route all pending messages
await session.sync()

# read messages for a branch
msgs = session.receive(branch_id)
```

## Data aggregation

```python
# combined DataFrame of all branches
df = session.to_df()

# messages from specific branches only
df = session.to_df(branches=[b1.ln_id, b2.ln_id])

# merged Pile
pile = session.concat_messages()
```

## Example: two-branch research + synthesis

```python
import asyncio
import lionagi as li

async def main():
    session = li.Session()
    researcher = session.new_branch(
        system="You are a research specialist.",
        name="researcher",
        chat_model=li.iModel(model="gpt-4o"),
    )
    writer = session.new_branch(
        system="You are a technical writer.",
        name="writer",
        chat_model=li.iModel(model="gpt-4o"),
    )

    findings = await researcher.communicate("Summarize key advances in RAG architectures.")

    session.send(researcher.ln_id, writer.ln_id, findings)
    await session.sync()

    msgs = session.receive(writer.ln_id)
    report = await writer.communicate(
        f"Write a user-friendly guide based on: {msgs[0].content}"
    )
    print(report)

asyncio.run(main())
```

Next: [DAG pipeline API](flow.md) — build typed DAG pipelines
