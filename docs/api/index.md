# Python API Reference

lionagi 0.28 is an async-first Python toolkit for model operations, tool-enabled
agents, and dependency-aware multi-agent execution. The CLI is the fastest way to
run and operate durable tasks; the Python API is for embedding the same primitives
inside an application.

## Start with `Branch`

```bash
uv add lionagi
```

```python
import lionagi as li

branch = li.Branch(chat_model="openai/gpt-4.1-mini")
answer = await branch.communicate("Explain what this package does in three bullets.")
print(answer)
```

Use `operate()` when a turn needs a schema or registered tools:

```python
from pydantic import BaseModel

class Review(BaseModel):
    summary: str
    risks: list[str]

review = await branch.operate(
    instruction="Review this change.",
    context={"diff": diff},
    response_format=Review,
)
```

## Pick the right layer

| Need | Start here | Important behavior |
|------|------------|--------------------|
| A recorded model turn | `Branch.communicate()` | Adds the user and assistant messages; does not execute tools |
| Structured or tool-enabled work | `Branch.operate()` | Set `actions=True` to expose and invoke registered tools |
| Low-level model invocation | `Branch.chat()` | Does not record; returns the response value by default |
| Several tool rounds | `Branch.ReAct()` | Iterative think-act-observe execution |
| Stream a CLI-backed model | `Branch.run()` | Async iterator over streamed messages |
| Multiple branches | `Session` | Owns branches, exchange, shared memory, and lifecycle hooks |
| An explicit DAG | `Builder` + `Session.flow()` | You construct dependencies; the session executes them |
| A reusable configured agent | `AgentSpec` + `create_agent()` | Wires role, model, tools, permissions, hooks, and MCP |
| Provider configuration | `iModel` | Resolves a provider/endpoint pair through the endpoint registry |

## CLI or Python?

| Goal | CLI | Python API |
|------|-----|------------|
| Run and resume one worker | `li agent`, `li agent -r` | `Branch` plus application-managed persistence |
| Parallel independent work | `li o fanout` | Multiple branches or an explicit graph |
| Planned/reactive workflow | `li o flow`, `li play` | `Builder` + `Session.flow()` |
| Durable monitoring and control | `li monitor`, `li wait`, `li o ctl` | Integrate the session observer/callbacks yourself |
| Application-specific tools and schemas | Possible through profiles/presets | `Branch.operate()` |
| Deterministic tests | — | `lionagi.testing` |

The CLI owns run directories, StateDB records, checkpoint resume, background
processes, and Studio integration. Constructing a Python `Branch` does not implicitly
create that durable CLI lifecycle.

## Reference pages

| Page | Contract |
|------|----------|
| [`Branch`](branch.md) | Model turns, structured output, tools, ReAct, streaming, serialization |
| [`Session`](session.md) | Branch ownership, exchange, shared memory, DAG execution |
| [DAG pipeline API](flow.md) | `Builder`, dependency semantics, expansion, aggregation |
| [Team messaging](team.md) | Session exchange patterns |
| [`iModel`](imodel.md) | Provider/endpoint resolution, rate limits, invocation hooks |
| [Operations and extension](operations.md) | Middle protocol and custom operation parameters |
| [`AgentSpec` and `create_agent()`](agent-config.md) | Reusable agents, permissions, secure hooks, MCP |
| [`SandboxSession`](sandbox.md) | Isolated git-worktree execution |

For lower-level protocol and storage types, continue through the Reference section
rather than treating every internal module as an application entry point.

## Public imports

The curated top-level surface is defined by `lionagi.__all__` and tested for exact
importability. Common application imports are:

```python
from lionagi import Branch, Session, Builder, Operation, iModel
from lionagi import Graph, Node, Edge, Element, Pile, Progression
from lionagi import FieldModel, OperableModel, load_mcp_tools
```

Feature-specific APIs intentionally live in their subpackages:

```python
from lionagi.agent import AgentSpec, PermissionPolicy, create_agent
from lionagi.engines import ResearchEngine, ReviewEngine, CodingEngine
from lionagi.hooks import HookBus, HookPoint, hook
from lionagi.testing import ScriptModel, ScriptedEndpoint, TestBranch
```

Deprecated convenience names may remain importable for compatibility. New code
should use the replacement named in the changelog rather than assuming every
top-level export is a recommended starting point.

Next: [`Branch`](branch.md)
