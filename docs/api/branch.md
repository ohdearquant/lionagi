# `Branch`

```python
class Branch(Element, Relational)
```

One stateful unit of model work: manages message history, tools, model access, logs,
optional memory, and LLM operations. Most SDK usage starts here.

| Need | Method |
|------|--------|
| Normal recorded turn, no tools | `communicate()` |
| Structured output or tool execution | `operate()` |
| Low-level unrecorded invocation | `chat()` |
| Low-level invocation that is recorded | `chat_and_record()` |
| Iterative tool use | `ReAct()` |
| Stream a CLI-backed provider | `run()` |

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
| `system_sender` | `SenderRecipient \| None` | `None` | Override the system message sender |
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
| `memory` | `MemoryStore \| None` | `None` | Explicit memory backend; otherwise created lazily on first access |

## Properties

| Property | Type | Writable | Notes |
|----------|------|----------|-------|
| `system` | `System \| None` | No | Active system message |
| `messages` | `Pile[RoledMessage]` | No | Full conversation history |
| `logs` | `Pile[Log]` | No | Activity log pile |
| `chat_model` | `iModel` | Yes | Swap chat provider at runtime |
| `parse_model` | `iModel` | Yes | Swap parse provider at runtime |
| `tools` | `dict[str, Tool]` | No | Registered tool registry |
| `memory` | `MemoryStore` | No | Explicit store, or a lazily-created private `InMemoryStore` |
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
endpoints and `run_and_collect` for CLI endpoints. Supports tool calling, structured output,
and streaming persistence. Registered tools are only exposed and invoked when `actions=True`
(or the supplied `Instruct` enables actions); passing `tools=` alone does not enable them.

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

A response that sets `action_strategy` to `"sequential"` narrows a `"concurrent"` call for that
turn only: its tool calls run one at a time, in request order, under the caller's `call_params`
except `max_concurrent`, which is 1 for that batch (the timeout, retries and throttle still
apply). The guard reads the fields the parsed response actually set, so a field the turn omits
is not read as a declaration even when the response model's own default for it is `"sequential"`,
while an explicit `"sequential"` narrows even when it equals that default. A value a validator
assigns or injects counts as a declaration, because the parsed model has set the field; the field
is read by its field name, so a turn that sends only an alias leaves it at its default and does
not narrow unless the model accepts the field name too (`populate_by_name=True`). A response
never widens a `"sequential"` call, and an `action_strategy="sequential"` argument is not
narrowed this way: `act()` awaits its calls in request order without applying `call_params`.

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

### `chat()` — low-level, unrecorded invocation

```python
text = await branch.chat("Draft an outline for a research paper on RAG.")
# does NOT add to branch.messages — caller manages history
```

Low-level building block. It does **not** add messages to history and returns the
assistant response value, usually a string, by default.

Request the generated message objects explicitly when you need them:

```python
instruction_msg, response_msg = await branch.chat(
    "Draft an outline for a research paper on RAG.",
    return_ins_res_message=True,
)
```

Returns: `str` by default, or `(Instruction, AssistantResponse)` when
`return_ins_res_message=True`.

### `chat_and_record()` — low-level invocation with history

```python
text = await branch.chat_and_record("Draft an outline for a research paper on RAG.")
```

Calls `chat(return_ins_res_message=True)`, adds both generated messages through the
hook-aware async message path, and returns the assistant response string. Use it when
you need `chat()`'s low-level parameters but still want observers and persistence to
see the turn. For ordinary stateful calls, prefer `communicate()`.

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
    action_request=[{"function": "search", "arguments": {"query": "LLM benchmarks 2025"}}],
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
| `return_analysis` | `bool` | `False` | Return every collected output instead of only the final result |
| `verbose` | `bool` | `False` | Print each iteration |

Returns: the final answer or result by default. With `return_analysis=True`, returns
the collected outputs as a `list`.

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

`Branch.from_dict` reads its input as trusted. The class names it holds are resolved as
[ADR-0001](../adr/ADR-0001-element-identity-and-polymorphic-serialization-envelope.md) D3
describes, which can import a module the data names, and its stored models choose the
provider, model, base URL and request settings. Restore only data that your own process, or a
writer you trust, produced.

A stored model never supplies a credential or the local-network opt-in. `iModel.from_dict`
attaches the provider's credential from the environment only when the stored base URL is the
provider's registered one, and takes the local-network setting from the provider's
registration. A caller that wants another endpoint, credential or setting passes it:

```python
restored = li.Branch.from_dict(data, chat_model=li.iModel(provider="openai", base_url=url))
model = li.iModel.from_dict(model_data, base_url=url, api_key="MY_GATEWAY_KEY")
```

Next: [`Session`](session.md) — manage multiple branches
