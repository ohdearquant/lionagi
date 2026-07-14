# Concepts

lionagi has two connected surfaces:

- **Run work** with one agent, a parallel fan-out, a dependency-aware flow, or a
  reusable engine.
- **Operate work** with durable run state, monitoring, control messages, schedules,
  and Studio.

The CLI is the shortest path to both surfaces. The Python API exposes the same core
building blocks when an application needs direct control.

## Choose the shape of the work

| Shape | CLI | Python | Use it for |
|-------|-----|--------|------------|
| One stateful worker | `li agent` | `Branch` | A focused task or conversation |
| Independent workers | `li o fanout` | Separate branches | Research or review that can run in parallel |
| Dependent or reactive work | `li o flow` / `li play` | `Builder` + `Session.flow()` | Multi-step work with explicit dependencies |
| A domain pipeline | `li engine run` | `lionagi.engines` | Planning, research, review, hypothesis, or coding workflows |

Start with the smallest shape that fits. A single agent is easier to inspect and
resume than a graph; use a flow when dependencies or distinct worker contexts are
part of the problem.

## Branch

A `Branch` is one stateful unit of model work. It owns message history, registered
tools, model configuration, logs, and optional memory.

```python
import lionagi as li

branch = li.Branch(
    chat_model=li.iModel(model="openai/gpt-4.1-mini"),
    system="You are a concise technical writer.",
)
answer = await branch.communicate("Explain dependency-aware execution.")
```

Choose the operation deliberately:

| Method | Records the turn | Executes tools | Best for |
|--------|------------------|----------------|----------|
| `communicate()` | Yes | No | A normal stateful model turn |
| `operate()` | Yes | Optional | Structured output and tool-enabled work |
| `chat()` | No | No | A low-level call when you manage history yourself |
| `chat_and_record()` | Yes | No | `chat()` semantics with a recorded turn |
| `ReAct()` | Yes | Yes | Several think-act-observe rounds |
| `run()` | Yes, as streamed messages | Provider-managed | Streaming a CLI-backed model |

`chat()` returns the response value, usually a string, by default. Pass
`return_ins_res_message=True` only when you need the generated `Instruction` and
`AssistantResponse` objects.

→ [`Branch` reference](api/branch.md)

## Tools and structured output

`operate()` is the general Python entry point for a recorded turn with structured
output, tool schemas, and tool invocation.

```python
from pydantic import BaseModel

class Finding(BaseModel):
    summary: str
    severity: str

result = await branch.operate(
    instruction="Inspect the supplied change and report its highest-risk issue.",
    context={"diff": diff},
    response_format=Finding,
)
```

Registering a tool does not make `chat()` or `communicate()` execute it. Enable the
action path explicitly:

```python
branch.register_tools([search_docs])

result = await branch.operate(
    instruction="Find the current retry policy and summarize it.",
    actions=True,
    tools=["search_docs"],
)
```

For a task that may require multiple tool rounds, use `ReAct()` instead of assuming
one `operate()` call will complete an open-ended workflow.

→ [`operate()` and the Middle protocol](api/operations.md)

## Session and flow

A `Session` owns one or more branches, their in-process exchange, shared memory, and
the graph execution kernel. A default branch is created automatically.

```python
import lionagi as li

session = li.Session()
researcher = session.new_branch(name="researcher", chat_model="openai/gpt-4.1-mini")
writer = session.new_branch(name="writer", chat_model="anthropic/claude-sonnet-4")

session.send(researcher.id, writer.id, "Research is ready")
await session.sync()
messages = session.receive(writer.id)
```

For Python DAGs, `Builder` creates operations and `Session.flow()` executes them:

```python
builder = li.Builder()
research = builder.add_operation(
    "communicate",
    instruction="Research the trade-offs of the proposed design.",
)
summary = builder.add_operation(
    "communicate",
    depends_on=[research],
    instruction="Turn the research into an executive summary.",
)

result = await session.flow(builder.get_graph())
print(result["operation_results"][summary])
```

`Builder` is incremental: after the first node, omitting `depends_on` attaches the
new operation after the builder's current head or heads. It is chaining shorthand,
not a way to create a new independent root. Use explicit dependencies or
`expand_from_result(..., strategy=ExpansionStrategy.CONCURRENT)` for parallel work.

The CLI's `li o flow` uses the same execution kernel but has a model plan the graph
from a task. `li play NAME` runs a reusable, parameterized flow specification.

→ [`Session` reference](api/session.md) · [Python DAG API](api/flow.md)

## Durable runs and control

Task-producing CLI commands persist run/session state under `~/.lionagi/` and in
StateDB. This is what makes background execution, monitoring, and resume possible.
User-facing artifacts go to `--save` when supplied; durable state is not the same as
the artifact directory.

```bash
li agent claude "Review the authentication module"
li agent -r BRANCH_ID "Now propose the smallest fix"

li o flow claude "Audit, fix, and verify the package" --save ./out --background
li monitor --watch
li wait RUN_ID
li o ctl status RUN_ID
```

`li o flow --resume ID` restarts a checkpointed flow after its process ended.
`li o ctl resume ID` is different: it unpauses a flow that is still running.

Python users control serialization themselves with `to_dict()`, `from_dict()`,
`to_df()`, and log persistence. The CLI/Studio run lifecycle is not automatically
created merely by constructing a `Branch`.

→ [CLI reference](cli-reference.md)

## Providers and endpoints

A **provider** selects a backend family. An **endpoint** selects one capability from
that family. `iModel` resolves the pair through the endpoint registry.

```python
api_model = li.iModel(provider="openai", endpoint="chat", model="gpt-4.1-mini")
cli_model = li.iModel(provider="claude_code", model="sonnet")
```

Keep API and CLI providers distinct:

- `openai`, `anthropic`, and `gemini` call hosted APIs and use API keys.
- `codex`, `claude_code`, `gemini_code`, and `pi` launch installed command-line
  agents and use those tools' authentication.
- Model strings with a slash, such as `anthropic/claude-sonnet-4`, infer the provider
  from the prefix.

Provider packages are organized by implementation owner, not always by public
alias: Gemini endpoints live in `lionagi/providers/google/`, while users select
`provider="gemini"` or `provider="gemini_code"`.

→ [`iModel` reference](api/imodel.md) · [Provider matrix](reference/providers.md)

## Reusable definitions

The CLI supports four different kinds of reusable material:

| Definition | Typical location | Purpose |
|------------|------------------|---------|
| Agent profile | `.lionagi/agents/<name>/<name>.md` | Model defaults and system prompt |
| Skill | `.lionagi/skills/<name>/SKILL.md` | Static instructions loaded on demand |
| Playbook | `.lionagi/playbooks/<name>.playbook.yaml` | Parameterized flow invocation |
| Plugin | `.lionagi/plugins/<name>/plugin.yaml` | A trusted bundle of profiles, playbooks, providers, or other extensions |

Project-local definitions take precedence where supported. Plugins are inert until
their declared contents are explicitly trusted and enabled.

```bash
li agent -a reviewer "Review this patch"
li skill show commit
li play audit --mode security "the auth package"
li plugin info my-plugin
```

## AgentSpec and permissions

`AgentSpec` is the Python equivalent of a repeatable agent configuration. It combines
a role/profile with model, tools, permissions, policy pack, context management, and
tool hooks, then `create_agent()` wires a ready-to-use `Branch`.

```python
from lionagi.agent import AgentSpec, create_agent
from lionagi.agent.hooks import log_tool_call

spec = AgentSpec.coding(
    model="openai/gpt-4.1",
    cwd="/path/to/project",
    secure=True,
)
spec.post("*", log_tool_call)

branch = await create_agent(spec)
result = await branch.operate(
    instruction="Inspect the import cycle and make the smallest safe edit.",
    actions=True,
)
```

The secure coding preset installs destructive-command and workspace-containment
guards in the `security_pre` hook phase. Permissions and guards are complementary:
permissions decide which calls are allowed, while guards enforce non-negotiable
safety checks and re-check rewritten arguments.

→ [`AgentSpec`, permissions, and hooks](api/agent-config.md)

## Team

A CLI team is a durable, named inbox shared across separate processes. It is useful
when coordination outlives one flow invocation.

```bash
li team create docs-team -m researcher,writer,reviewer
li team send "Draft ready" --team docs-team --to writer --from researcher
li team receive --team docs-team --as writer
```

Within one Python process, use the `Session` exchange. Within a single dependency
graph, prefer graph edges unless workers genuinely need asynchronous messages.

## Sandbox

`SandboxSession` creates an isolated git worktree for reversible code changes.

```python
from lionagi.tools.sandbox import create_sandbox, sandbox_diff, sandbox_discard

sandbox = await create_sandbox(repo_root="/path/to/project")
agent = await create_agent(AgentSpec.coding(cwd=sandbox.worktree_path))
await agent.operate(instruction="Refactor the auth module.", actions=True)

print((await sandbox_diff(sandbox))["stat"])
await sandbox_discard(sandbox)  # or commit and merge after review
```

Use a sandbox for speculative writes that need an explicit review boundary. A
read-only analysis does not need the worktree overhead.

→ [`SandboxSession` reference](api/sandbox.md)

Next: [Choose a surface](choosing-a-surface.md), [CLI reference](cli-reference.md),
or [Python API reference](api/index.md).
