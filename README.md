
![PyPI - Version](https://img.shields.io/pypi/v/lionagi?labelColor=233476aa&color=231fc935)
![PyPI - Downloads](https://img.shields.io/pypi/dm/lionagi?color=blue)
![Python Version](https://img.shields.io/badge/python-3.10%2B-blue)
[![codecov](https://codecov.io/github/khive-ai/lionagi/graph/badge.svg?token=FAE47FY26T)](https://codecov.io/github/khive-ai/lionagi)

# lionagi

Orchestrate multi-agent AI workflows from the command line or Python.

[Docs](https://khive-ai.github.io/lionagi/) |
[Discord](https://discord.gg/JDj9ENhUE8) |
[PyPI](https://pypi.org/project/lionagi/) |
[Changelog](CHANGELOG.md)

## What's New in 0.23

- **Agent infrastructure** — `AgentConfig` presets (`.coding()`, `.research()`) with built-in permission policies, hooks, and tool registration via `create_agent()`.
- **Sandbox tool** — `SandboxSession` uses git worktrees for isolated editing: `create()` → edit → `diff()` → `commit()` → `merge()` or `discard()`.
- **New providers** — DeepSeek (`DEEPSEEK_API_KEY`) and Pi (via [Pi Code CLI](https://pi.ai)) are now supported as CLI agent backends.
- **Settings merge** — global `~/.lionagi/settings.yaml` and per-project `.lionagi/settings.yaml` are merged automatically at startup.

## Install

```bash
pip install lionagi
```

**CLI provider auth** — CLI aliases spawn subprocess tools, not REST API calls:

- `claude`: install [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) → `claude login` (subscription) or `export ANTHROPIC_API_KEY=sk-ant-...` (API key)
- `codex`: requires ChatGPT Plus/Pro → `npm install -g @openai/codex` → `codex login`
- `deepseek`: `export DEEPSEEK_API_KEY=sk-...` for DeepSeek models
- `pi`: install [Pi Code CLI](https://pi.ai) for Pi models
- Python API (`iModel`, `Branch`): `export OPENAI_API_KEY=sk-...` for gpt-4.1-mini default

## First Flow

```python
import asyncio
from lionagi import Branch

async def main():
    b = Branch()          # default: gpt-4.1-mini (requires OPENAI_API_KEY)
    reply = await b.communicate("Name 3 features of async Python, one sentence each.")
    print(reply)

asyncio.run(main())
```

```text
# output:
1. Coroutines let you write non-blocking I/O without threads.
2. asyncio.gather runs multiple coroutines concurrently under one event loop.
3. async generators stream results lazily, pausing between each yield.
```

For multi-agent orchestration without Python, see [CLI Quick Start](docs/getting-started/first-flow.md).

## Concepts

| Term | What it is |
|------|------------|
| **Branch** | Single conversation thread — message history, tools, model config. Primary API surface. |
| **Session** | Coordinates multiple Branches; runs DAG workflows across them. |
| **flow** | `li o flow` — orchestrator plans a DAG, workers execute with dependency edges resolved. |
| **team** | Persistent inbox messaging between agents via `li team send/receive`. |
| **operate** | `branch.operate(instruction=…)` — tool use + structured output + optional streaming. |
| **persist** | Every run saved to `~/.lionagi/runs/{run_id}/`. Resume with `li agent -r <branch-id>`. |
| **AgentConfig** | Preset agent configurations (coding, research) with permission policies, hooks, and tool registration. |
| **Sandbox** | Git worktree isolation for safe experimentation — `SandboxSession.create()` → edit → diff → merge or discard. |

## CLI — `li`

```bash
# Single agent
li agent claude/sonnet "Explain the observer pattern in 3 sentences"

# Fan-out: N workers in parallel, optional synthesis
li o fanout claude/sonnet "Identify code smells in this codebase" -n 3 --with-synthesis

# DAG flow: orchestrator plans agents with dependency edges
li o flow claude/sonnet "Audit the auth module for security issues" --cwd .

# Team messaging: inbox coordination between agents
li team create "review" && li team send "Start analysis" -t <id> --to analyst

# Playbook: parametric flow spec at ~/.lionagi/playbooks/audit.playbook.yaml
li play audit --mode security "the auth service"
li play NAME --help                          # Show playbook parameters and usage

# Skill: print a CC-compatible reference body to stdout (for agent context injection)
li skill commit

# Resume any run
li agent -r <branch-id> "follow up on your findings"
```

Full reference → [docs/cli-reference.md](docs/cli-reference.md) · Installable
templates → [examples/](examples/)

## Python API

**Chat**

```python
from lionagi import Branch

b = Branch(chat_model="openai/gpt-5.4", system="You are a concise assistant.")
reply = await b.communicate("What causes rainbows?")
```

**Structured output**

```python
from pydantic import BaseModel

class Summary(BaseModel):
    points: list[str]
    confidence: float

result = await b.operate(instruction="Summarize this text.", response_format=Summary)
```

**Tools + ReAct**

```python
from lionagi.tools.types import ReaderTool

branch = Branch(tools=[ReaderTool])
result = await branch.ReAct(
    instruct={"instruction": "Summarize /path/to/paper.pdf"},
)
```

Full reference → [docs/api/](docs/api/)

## Docs

| | |
|--|--|
| [Getting Started](docs/getting-started/first-flow.md) | Install, first flow, API key setup |
| [Concepts](docs/concepts.md) | Branch, Session, flow, team, operate, persist |
| [CLI Reference](docs/cli-reference.md) | `li agent`, `li o fanout`, `li o flow`, `li team` — all flags |
| [Cookbook](docs/cookbook/) | 5 runnable scenarios: codebase audit, research synthesis, multi-model pipeline, team coordination, resumable background run |
| [API Reference](docs/api/) | `branch.operate`, `branch.ReAct`, `iModel`, `Session` |
| [Migration 0.22.5 → 0.22.6](docs/migration/0.22.5-to-0.22.6.md) | Breaking changes: `branch.instruct` removed, run paths changed |
| [Contributing](docs/contributing.md) | Dev setup, PR workflow |

## Optional Extras

```bash
uv add "lionagi[reader]"    # Document reading (PDF, HTML, DOCX)
uv add "lionagi[mcp]"       # MCP server support
uv add "lionagi[ollama]"    # Local models via Ollama
uv add "lionagi[rich]"      # Rich terminal output
uv add "lionagi[graph]"     # Flow visualization
uv add "lionagi[postgres]"  # PostgreSQL persistence
uv add "lionagi[all]"       # Everything
```

## Community

- [Discord](https://discord.gg/JDj9ENhUE8) — questions, ideas, help
- [Issues](https://github.com/khive-ai/lionagi/issues) — bugs and feature requests
- [Contributing](docs/contributing.md) — PR workflow

**Citation**

```bibtex
@software{Li_LionAGI_2023,
  author = {Haiyang Li},
  year   = {2023},
  title  = {LionAGI: Towards Automated General Intelligence},
  url    = {https://github.com/khive-ai/lionagi},
}
```
