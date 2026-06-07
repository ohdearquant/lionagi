# `Branch`

```python
class Branch(Element, Relational)
```

Single conversation thread: manages message history, tools, model access, and LLM operations.
Most SDK usage starts here.

## Constructor

```python
branch = li.Branch(
    chat_model=li.iModel(model="gpt-4o"),
    system="You are a research assistant.",
    name="researcher",
)
```

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `user` | `SenderRecipient \| None` | `None` | Branch owner/sender identity |
| `name` | `str \| None` | `None` | Human label |
| `system` | `System \| JsonValue` | `None` | System prompt (str or dict) |
| `chat_model` | `iModel \| dict \| str` | `None` | Primary model for chat / communicate / operate |
| `parse_model` | `iModel \| dict \| str` | `None` | Model used for `parse()` retries |
| `tools` | `FuncTool \| list[FuncTool]` | `None` | Pre-register tools on construction |
| `messages` | `Pile[RoledMessage]` | `None` | Restore prior conversation history |
| `logs` | `Pile[Log]` | `None` | Restore prior activity logs |
| `log_config` | `DataLoggerConfig \| dict` | `None` | Log output configuration |
| `system_datetime` | `bool \| str` | `None` | Inject current timestamp into system prompt |
| `system_template` | `str \| None` | `None` | **Deprecated** — emits `DeprecationWarning`, has no effect; will be removed in a future release |
| `system_template_context` | `dict` | `None` | **Deprecated** — emits `DeprecationWarning`, has no effect; will be removed in a future release |
| `use_lion_system_message` | `bool` | `False` | Prepend LIONAGI system preamble |

## Properties

| Property | Type | Writable | Notes |
|----------|------|----------|-------|
| `system` | `System \| None` | No | Active system message |
| `messages` | `Pile[RoledMessage]` | No | Full conversation history |
| `logs` | `Pile[Log]` | No | Activity log pile |
| `chat_model` | `iModel` | Yes | Swap chat provider at runtime |
| `parse_model` | `iModel` | Yes | Swap parse provider at runtime |
| `tools` | `dict[str, Tool]` | No | Registered tool registry |
| `msgs` | `MessageManager` | No | Internal message manager |
| `acts` | `ActionManager` | No | Internal action manager |
| `mdls` | `iModelManager` | No | Internal model manager |

## Operations

### `operate()` — universal structured operation

```python
from pydantic import BaseModel

class Summary(BaseModel):
    title: str
    key_points: list[str]

result = await branch.operate(
    instruction="Summarize this paper: ...",
    response_format=Summary,
    actions=True,
    tools=["search"],
    action_strategy="concurrent",
)
# result: Summary instance
```

Routes through the [Middle protocol](operations.md#middle-protocol): `communicate` for API
endpoints, `run_and_collect` for CLI endpoints. Supports tool calling, structured output,
and streaming persistence.

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `instruction` | `Instruction \| JsonValue` | `None` | User message |
| `instruct` | `Instruct` | `None` | Alternative to `instruction` — structured instruct object |
| `guidance` | `JsonValue` | `None` | Additional guidance injected into instruction |
| `context` | `JsonValue` | `None` | Prompt context visible to the model |
| `response_format` | `type[BaseModel]` | `None` | Parse output into this Pydantic model |
| `actions` | `bool` | `False` | Enable tool calling |
| `tools` | `ToolRef` | `None` | Subset of registered tools to expose |
| `invoke_actions` | `bool` | `True` | Auto-invoke tool calls returned by model |
| `action_strategy` | `"sequential" \| "concurrent"` | `"concurrent"` | Tool execution order |
| `field_models` | `list[FieldModel]` | `None` | Dynamic field extensions |
| `stream_persist` | `bool` | `False` | Write JSONL chunks live (CLI endpoints) |
| `persist_dir` | `str \| None` | `None` | Directory for JSONL chunks |
| `middle` | `Middle \| None` | `None` | Override default routing |
| `handle_validation` | `"raise" \| "return_value" \| "return_none"` | `"return_value"` | Parse failure behavior |
| `chat_model` | `iModel` | `None` | Override branch's chat model for this call |
| `parse_model` | `iModel` | `None` | Override branch's parse model |
| `skip_validation` | `bool` | `False` | Skip response parsing entirely |
| `reason` | `bool` | `False` | Enable chain-of-thought reasoning field |
| `sender` / `recipient` | `SenderRecipient` | `None` | Override message identity |

Returns: `list | BaseModel | None | dict | str`

For cookbook usage, see [Research synthesis](../cookbook/research-synthesis.md).

---

### `communicate()` — single-turn with history accumulation

```python
result = await branch.communicate(
    "What are the main causes of climate change?",
    response_format=None,  # returns str
)
# adds both user message and assistant response to branch.messages
```

Simpler than `operate()` — no tool calling. Accumulates messages in history automatically.
Use when you need history building without tool invocation.

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `instruction` | `Instruction \| JsonValue` | `None` | User message |
| `guidance` | `JsonValue` | `None` | Additional guidance |
| `context` | `JsonValue` | `None` | Prompt context |
| `plain_content` | `str` | `None` | Bypass instruction formatting |
| `response_format` | `type[BaseModel]` | `None` | Parse output into Pydantic model |
| `request_fields` | `dict \| list[str]` | `None` | Request specific fields from model |
| `num_parse_retries` | `int` | `3` | Parse retry attempts |
| `clear_messages` | `bool` | `False` | Clear history before this turn |

Returns: `BaseModel | dict | str | None`

---

### `chat()` — raw model invocation

```python
instruction_msg, response_msg = await branch.chat(
    "Draft an outline for a research paper on RAG.",
    response_format=None,
)
# does NOT add to branch.messages — caller manages history
```

Low-level building block. Does **not** add messages to history.
Returns `(Instruction, AssistantResponse)` tuple — use when you need explicit history control.

---

### `run()` — streaming CLI endpoint

```python
async for msg in branch.run("Write a detailed analysis of..."):
    if hasattr(msg, "content"):
        print(msg.content, end="", flush=True)
```

Async generator — yields `RoledMessage` objects as chunks arrive.
Requires a CLI endpoint model (e.g., `iModel(provider="claude_code", model="sonnet")`).

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `instruction` | `str` | `""` | User message |
| `chat_model` | `iModel \| None` | `None` | Override to CLI endpoint model |
| `stream_persist` | `bool` | `False` | Write JSONL chunks to disk |
| `persist_dir` | `str \| None` | `None` | JSONL output directory |
| `response_format` | type | `None` | Parse final accumulated text |

---

### `parse()` — structured extraction from text

```python
class Verdict(BaseModel):
    score: int
    reasoning: str

verdict = await branch.parse(
    text='{"score": 8, "reasoning": "Strong methodology"}',
    response_format=Verdict,
    handle_validation="raise",
)
```

Extracts structured data from raw text without a new LLM call (unless retries are needed).
Fuzzy key matching is enabled by default — handles minor key name variations from the model.

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `text` | `str` | required | Raw text to parse |
| `response_format` | `type[BaseModel]` | `None` | Target Pydantic model |
| `handle_validation` | `"raise" \| "return_value" \| "return_none"` | `"return_value"` | Failure behavior |
| `max_retries` | `int` | `3` | LLM retry attempts on parse failure |
| `fuzzy_match` | `bool` | `True` | Enable fuzzy key matching |
| `similarity_threshold` | `float` | `0.85` | Minimum similarity for key matching |

Returns: `BaseModel | dict | str | None`

---

### `act()` — tool execution

```python
responses = await branch.act(
    action_request=[{"tool": "search", "arguments": {"query": "LLM benchmarks 2025"}}],
    strategy="concurrent",
)
```

Directly invoke tool calls. Takes `ActionRequest`, dict, or list.

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `action_request` | `list \| ActionRequest \| BaseModel \| dict` | required | Tool call(s) to execute |
| `strategy` | `"concurrent" \| "sequential"` | `"concurrent"` | Execution order |
| `suppress_errors` | `bool` | `True` | Catch tool errors instead of raising |
| `verbose_action` | `bool` | `False` | Log each invocation |

Returns: `list[ActionResponse]`

---

### `ReAct()` — think-act-observe reasoning loops

```python
result = await branch.ReAct(
    instruct={"instruction": "Find the latest papers on diffusion models and summarize."},
    tools=["search", "read_url"],
    max_extensions=5,
    response_format=ResearchReport,
)
```

Multi-round reasoning with tool use. Iterates until `max_extensions` or a terminal response.

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `instruct` | `Instruct \| dict` | required | Initial instruction |
| `tools` | `Any` | `None` | Tools to expose (defaults to all registered) |
| `response_format` | `type[BaseModel]` | `None` | Final output schema |
| `max_extensions` | `int \| None` | `3` | Max reasoning iterations |
| `reasoning_effort` | `"low" \| "medium" \| "high"` | `None` | Reasoning depth hint |
| `return_analysis` | `bool` | `False` | Also return list of intermediate steps |
| `verbose` | `bool` | `False` | Print each iteration |

Returns: `Any` or `(Any, list)` if `return_analysis=True`.

For a full working example, see [Research synthesis](../cookbook/research-synthesis.md).

---

### `interpret()` — prompt rewriting

```python
refined = await branch.interpret(
    "llm stuff for code gen",
    domain="software engineering",
    style="precise and technical",
)
# refined: "Explain LLM-based code generation techniques for production use."
```

Rewrites raw user input into a refined prompt. Does **not** add to history.

---

## Tool management

```python
# register functions
branch.register_tools([search_fn, read_file_fn])

# register with update=True to overwrite existing
branch.register_tools(new_search_fn, update=True)

# register an API endpoint as a callable tool
branch.connect(
    provider="openai",
    endpoint="chat",
    name="gpt_tool",
    description="GPT-4o as a callable tool",
)
```

## Context manager

```python
async with li.Branch(chat_model=li.iModel(model="gpt-4o")) as branch:
    result = await branch.operate(instruction="Analyze this dataset: ...")
# logs auto-dumped on exit
```

## Serialization

```python
# round-trip
data = branch.to_dict()
restored = li.Branch.from_dict(data)

# messages as DataFrame
df = branch.to_df()

# clone (sync or async)
b2 = branch.clone()
b2 = await branch.aclone(sender=new_sender_id)

# dump logs
branch.dump_logs(clear=True, persist_path="./logs/session.json")
await branch.adump_logs(clear=False)
```

Next: [`Session`](session.md) — manage multiple branches
