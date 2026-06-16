# Python API Reference

lionagi 0.22.6 — provider-agnostic LLM orchestration SDK.

## Quick start

```bash
uv add lionagi
```

```python
import lionagi as li

branch = li.Branch(chat_model=li.iModel(model="gpt-4o-mini"))
result = await branch.communicate("hello")
print(result)
```

```text
# output:
Hello! How can I help you today?
```

## When to use API vs CLI

| Need | CLI (`li`) | Python API |
|------|-----------|-----------|
| Run a one-off task | `li agent claude "…"` | — |
| Multi-step DAG pipelines | `li o flow claude "…"` | `Session.flow()` + `Builder` |
| Embed LLM calls in application code | — | `Branch.operate()` |
| Structured output extraction | — | `Branch.parse()` |
| Custom tool registration | — | `branch.register_tools()` |
| Background + resumable runs | `li o flow … --background` | — |
| Multi-provider benchmarking | — | `Session` + multiple `iModel` instances |
| Stream live chunks | `li agent … -v` | `Branch.run()` |
| Programmatic message history | — | `Branch.to_df()` |
| MCP tool integration | — | `load_mcp_tools()` |

CLI is the default path for most users. The Python API is for application embedding,
custom orchestration, and programmatic control the CLI doesn't expose.

## Pages

| Page | Topic | When you need it |
|------|-------|-----------------|
| [branch.md](branch.md) | `Branch` | Single conversation thread — chat, operate, tools |
| [session.md](session.md) | `Session` | Manage multiple branches, run DAGs |
| [flow.md](flow.md) | `OperationGraphBuilder` | Build typed DAG pipelines in Python |
| [team.md](team.md) | `Session` exchange | Inter-branch messaging |
| [imodel.md](imodel.md) | `iModel` | Configure providers, rate limits, hooks |
| [operations.md](operations.md) | `Middle`, param types | Extend `operate()`, build custom middles |
| [agent-config.md](agent-config.md) | `AgentSpec`, `create_agent()`, `PermissionPolicy` | Preset agent configurations with hooks and permissions |
| [sandbox.md](sandbox.md) | `SandboxSession` | Isolated git worktree execution for safe agent edits |

## Import surface

```python
from lionagi import Branch, Session, iModel, Builder, Operation
from lionagi import Element, Pile, Progression, Node, Graph
from lionagi import FieldModel, OperableModel, load_mcp_tools
```

Next: [`Branch`](branch.md) — start here for most SDK usage
