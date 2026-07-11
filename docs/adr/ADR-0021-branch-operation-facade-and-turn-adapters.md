# ADR-0021: Branch operation facade and turn-adapter contract

- **Status**: Accepted
- **Kind**: Retrospective
- **Area**: operations
- **Date**: 2026-07-09
- **Relations**: none

## Context

`Branch` is the public verb surface for one conversation, but the implementations of those verbs
live under `lionagi/operations/`. The split grew around five concrete problems.

**P1 — A caller needs one state-owning surface without making it the implementation layer.** Model
transport, structured parsing, action execution, composed operation, interpretation, and reasoning
loops all need the same branch managers. Publishing those functions only as unrelated
module calls would make callers assemble and pass that state themselves; implementing them directly
on `Branch` would make an already stateful facade own every algorithm.

**P2 — Operation implementations and `Branch` would form a runtime import cycle.** Implementations
need a branch instance, while the facade needs to expose the implementations. The shipped direction
is a type-only dependency from operation modules to `Branch` and a lazy, method-local import from
the facade to the implementation (`lionagi/session/branch.py`; `lionagi/operations/types.py`).

**P3 — Named extensions must resolve the same way from direct calls and graph nodes.** A session can
register a coroutine by name, and an `Operation` graph node stores only an operation name and
parameters. If those paths had separate registries or precedence rules, a graph invocation could
mean something different from `Session.run_operation()`.

**P4 — API calls and CLI streams do not have the same state or cleanup contract.** `chat()` is an
unrecorded one-shot request. `chat_and_record()` adds an explicit recording wrapper without adding
an operation lifecycle. `communicate()` records an instruction and assistant response before
optional parsing and does own a lifecycle. `run()` is a public async generator restricted to CLI
endpoints; it records typed stream messages, handles provider session resumption, closes the
provider stream on every exit, and owns its lifecycle signals. Treating all four as one transport
primitive would hide meaningful differences on empty output, partial output, consumer abandonment,
and provider failure.

**P5 — The adapter seam cannot truthfully promise one provider message.** `Middle` was documented as
advancing a branch by one assistant turn. Its implementations already have broader cardinality:
`run_and_collect()` can join several assistant messages, and the LNDL adapter can perform several
recorded exchanges. The stable as-built unit is one logical adapter invocation, not one provider
message (`lionagi/operations/types.py`; `lionagi/operations/run/run.py`;
`lionagi/operations/lndl_middle/lndl_middle.py`).

| Concern | Decision |
|---------|----------|
| Public operation boundary | D1: `Branch` remains the facade and lazily delegates to operation modules. |
| Named extension lookup | D2: one session-owned async registry serves direct and graph dispatch, after built-ins. |
| Model-facing transport behavior | D3: `chat`, `chat_and_record`, `communicate`, `run`, and `run_and_collect` retain distinct persistence and failure semantics. |
| Lifecycle and cleanup ownership | D4: the wrapper or stream that owns an exchange owns its lifecycle and cleanup signals. |
| Adapter substitution | D5: `Middle` means one logical adapter invocation and may contain streaming or a bounded inner loop. |

This ADR deliberately does **not** decide:

- Composed response-schema construction, validation, or outer action execution; ADR-0022 owns that
  coordinator contract.
- Dependency scheduling, graph mutation, branch cloning, or flow result envelopes; ADR-0023 owns the
  graph execution kernel.
- LNDL syntax or its specific multi-round policy; ADR-0024 records the operations adapter only.
- Provider endpoint implementation, request serialization, rate limiting, or credentials; those are
  service-provider concerns below the operation facade.
- Durable branch/session persistence; the operations layer emits and records state but does not
  choose a persistence substrate.

## Decision

### D1 — `Branch` is the public facade and operation modules are the implementation layer

The shipped dependency shape is:

```text
lionagi/
├── session/
│   ├── branch.py                 # public facade; lazy imports inside verb methods
│   └── session.py                # branch collection and shared operation manager
└── operations/
    ├── types.py                  # parameter dataclasses and Middle protocol
    ├── manager.py                # named-operation registry
    ├── node.py                   # graph-executable Operation
    ├── chat/chat.py              # unrecorded API request
    ├── communicate/communicate.py# recorded API request + optional parse
    ├── run/run.py                # recorded CLI stream + collection adapter
    ├── operate/operate.py        # composed coordinator (ADR-0022)
    ├── act/act.py
    ├── parse/parse.py
    ├── select/select.py             # module exists; no Branch.select facade is shipped
    ├── interpret/interpret.py
    └── ReAct/ReAct.py
```

The public methods construct the relevant parameter object and import their implementation inside
the method. Representative shipped signatures are:

```python
class Branch:
    async def chat(
        self,
        instruction: Instruction | JsonValue = None,
        guidance: JsonValue = None,
        context: JsonValue = None,
        sender: ID.Ref = None,
        recipient: ID.Ref = None,
        request_fields: list[str] | dict[str, JsonValue] = None,
        response_format: type[BaseModel] | BaseModel = None,
        progression: Progression | list[ID[RoledMessage].ID] = None,
        imodel: iModel = None,
        tool_schemas: list[dict] = None,
        images: list = None,
        image_detail: Literal["low", "high", "auto"] = None,
        plain_content: str = None,
        return_ins_res_message: bool = False,
        include_token_usage_to_model: bool = False,
        **kwargs,
    ) -> tuple[Instruction, AssistantResponse]: ...

    async def chat_and_record(
        self,
        instruction: Instruction | JsonValue = None,
        **kwargs,
    ) -> str: ...

    async def communicate(
        self,
        instruction: Instruction | JsonValue = None,
        *,
        guidance: JsonValue = None,
        context: JsonValue = None,
        plain_content: str = None,
        sender: SenderRecipient = None,
        recipient: SenderRecipient = None,
        progression: ID.IDSeq = None,
        response_format: type[BaseModel] = None,
        request_fields: dict | list[str] = None,
        chat_model: iModel = None,
        parse_model: iModel = None,
        skip_validation: bool = False,
        images: list = None,
        image_detail: Literal["low", "high", "auto"] = None,
        num_parse_retries: int = 3,
        clear_messages: bool = False,
        include_token_usage_to_model: bool = False,
        **kwargs,
    ) -> BaseModel | dict | str | None: ...

    async def run(
        self,
        instruction: str = "",
        *,
        chat_model: iModel | None = None,
        guidance=None,
        context=None,
        sender=None,
        recipient=None,
        images=None,
        image_detail="auto",
        stream_persist: bool = False,
        persist_dir: str | None = None,
        response_format=None,
        **kwargs,
    ) -> AsyncGenerator[RoledMessage, None]: ...
```

Exact semantics:

- A facade method receives an already constructed `Branch`; operation implementations do not create
  or attach a branch or session.
- `chat_model` and `parse_model` default through the branch's `iModelManager`; callers may override
  them on the methods that expose those parameters.
- Implementation imports remain method-local. Importing `Branch` does not eagerly import the
  transport, parsing, action, or reasoning-loop implementations.
- The public annotation on `Branch.chat()` says it returns the instruction/response tuple, but its
  default runtime path returns response text. The implementation-level `chat()` correctly declares
  the union. `return_ins_res_message` is therefore the authoritative runtime discriminator; the
  facade annotation is currently under-specified.
- `BranchOperations` and the actual facade drift in both directions. The literal lists `select`,
  although no `Branch.select()` method is shipped; it omits the public `run()` and
  `chat_and_record()` methods. `select` resolves only if a session registers that name, while the
  omitted public methods resolve through normal attribute lookup. The graph node consequently uses
  `BranchOperations | str`; the literal is a typing aid, not a closed or complete runtime registry.
- The public `ReAct()` wrapper (and its keyword-preparation helper) defaults `max_extensions` to 3;
  `ReActStream()` called directly defaults it to 0 — no extension rounds unless the caller opts in
  (the internal v1 entry point shares the 0 default). The core stream treats
  `None`, zero, and negative values as no extension work; values above 100 emit a warning and clamp
  to 100. The bounds prevent an extension loop from growing without limit, but the operations code
  records no empirical rationale for exactly 3 or 100 (`lionagi/operations/ReAct/ReAct.py`).
- An exception from the delegated implementation propagates unless that implementation documents a
  more specific conversion. The facade does not turn arbitrary failures into values.

**Why this way.** `Branch` already owns the managers required by every verb, so it is the coherent
caller boundary. Method-local delegation preserves that discoverability without reversing the
dependency direction. It also permits operation modules to use `Branch` only under `TYPE_CHECKING`,
which removes the runtime cycle rather than hiding it in a package-level re-export.

### D2 — Named operations use one session-owned asynchronous registry

The registry and dispatch contracts are:

```python
class OperationManager(Manager):
    registry: dict[str, Callable]

    def register(
        self,
        operation: str,
        func: Callable,
        update: bool = False,
    ): ...

class Session:
    _operation_manager: OperationManager

    def register_operation(
        self,
        operation: str,
        func: Callable,
        *,
        update: bool = False,
    ): ...

    def operation(self, name: str = None, *, update: bool = False): ...

    async def run_operation(
        self,
        operation: str,
        *,
        branch: Branch | ID.Ref | None = None,
        **kwargs: Any,
    ) -> Any: ...

class Branch:
    def get_operation(self, operation: str) -> Callable | None:
        if hasattr(self, operation):
            return getattr(self, operation)
        return self._operation_manager.registry.get(operation)
```

When `Session.include_branches()` accepts a branch, it sets the branch's owner, session user,
observer, optional hook bus, memory store, and, load-bearing here, the exact same
`Session._operation_manager` instance. It also registers the branch in the session exchange and
selects the first included branch as the default when no default exists
(`lionagi/session/session.py`).

Exact semantics:

- Registration rejects an existing name unless `update=True`.
- Registration rejects a callable for which `is_coro_func()` is false. The registry does not wrap a
  synchronous function in a coroutine.
- `Session.operation()` is a decorator; absent an explicit `name`, it registers `func.__name__`.
- Every branch included in the same session observes later registrations because all hold the same
  manager object. A standalone branch starts with a private manager until session inclusion rewires
  it.
- Lookup checks `hasattr(branch, operation)` first. A registered name cannot shadow a built-in
  attribute, even with `update=True`; `update` only replaces a registry entry.
- `Session.run_operation()` uses the default branch when `branch` is absent, resolves a string or
  UUID branch reference through `Session.branches`, and raises `ValueError("Unknown operation:
  ...")` when lookup returns `None`.
- `Operation._invoke()` uses the same `Branch.get_operation()` path. A missing operation becomes
  `OperationError("Unsupported operation type: ...")` inside event invocation.
- `Operation._invoke()` awaits ordinary operations. The special `ReActStream` name consumes its
  async generator into a list because an `Operation` node settles to one response value.

**Why this way.** Session ownership matches the intended lifetime: named extensions coordinate a
set of branches, and graph nodes need them after branch cloning or allocation. Built-in precedence
keeps the stable facade from being silently redefined by registration. Reusing the same lookup from
direct and graph dispatch makes an operation name one contract rather than two.

### D3 — API one-shot, explicit recording, recorded API, CLI stream, and collection remain distinct

The implementation-level contracts are:

```python
async def chat(
    branch: Branch,
    instruction: JsonValue | Instruction,
    chat_param: ChatParam,
    return_ins_res_message: bool = False,
) -> tuple[Instruction, AssistantResponse] | str: ...

async def communicate(
    branch: Branch,
    instruction: JsonValue | Instruction,
    chat_param: ChatParam,
    parse_param: ParseParam | None = None,
    clear_messages: bool = False,
    skip_validation: bool = False,
    request_fields: dict | None = None,
) -> Any: ...

async def run(
    branch: Branch,
    instruction: JsonValue | Instruction,
    param: RunParam,
) -> AsyncGenerator[RoledMessage]: ...

async def run_and_collect(
    branch: Branch,
    instruction: JsonValue | Instruction,
    chat_param: ChatParam,
    parse_param: ParseParam | None = None,
    clear_messages: bool = False,
    skip_validation: bool = False,
) -> Any: ...
```

| Path | Endpoint family | Records instruction/response | Result contract |
|------|-----------------|------------------------------|-----------------|
| `chat` | API one-shot | No | response text, or `(Instruction, AssistantResponse)` when requested |
| `chat_and_record` | API one-shot through `chat` | Yes | response text |
| `communicate` | API one-shot | Yes, before optional parse | parsed value, fuzzy field mapping, or response text |
| `run` | CLI stream only | Yes, as messages arrive | async stream of instruction, assistant, action-request, action-response messages |
| `run_and_collect` | CLI stream through `run` | Already recorded by `run` | joined assistant text, parsed value, or `None` when no assistant text arrives |

Exact semantics for `chat` and `communicate`:

- Both apply registered context providers, construct one instruction/request, invoke the selected
  model, clear the temporary context-injection slot in a `finally`, and emit/log the API call.
- `chat` does not append the instruction or response to branch messages. If the API event status is
  `FAILED`, it raises `ExecutionError` before reading a null response.
- `communicate(clear_messages=True)` clears branch messages before the request. After a successful
  `chat(..., return_ins_res_message=True)`, it appends the instruction and assistant response in
  that order.
- `communicate(skip_validation=True)` returns the recorded assistant response text immediately.
- With `ParseParam` and a response format, `communicate` propagates any instruction-carried
  `Structure`, parses the text, preserves the original model response in metadata when a second
  assistant parse response exists, and wraps a parsing `ValueError` with the requested type.
- With `request_fields` and no structured parse, it fuzzy-validates the response mapping, fills
  unmatched fields with `Undefined`, and removes those `Undefined` entries before returning.
- `num_parse_retries` defaults to 3. Values above 5 emit a `UserWarning` and are clamped to 5. The
  code recommends fewer than 3 but records no empirical rationale for either the inherited default
  or cap (`lionagi/operations/communicate/communicate.py`).

Exact semantics for `chat_and_record`:

- It removes any caller-supplied `return_ins_res_message`, forces that flag to true, delegates once
  to `Branch.chat()`, then appends the returned instruction and assistant response in that order via
  the asynchronous message path.
- It returns only `AssistantResponse.response`; callers cannot request the message tuple through
  this wrapper.
- A failure from `chat()` propagates before either message is appended. If the instruction append
  succeeds and the response append fails, the wrapper does not roll the instruction back.
- It does not call `_observed_run()`. Recording and lifecycle are independent decisions: message-add
  hooks and signals see the two appends, but no `RunStart`, `RunEnd`, or `RunFailed` is introduced by
  this wrapper itself (`lionagi/session/branch.py`).

Exact semantics for `run` and `run_and_collect`:

- `run` replaces `branch.chat_model` when `RunParam.imodel` is supplied. It rejects a non-CLI model
  with a `ValueError` that directs the caller to CLI endpoint prefixes; it does not fall back to
  `communicate`.
- It records and yields the instruction before consuming provider chunks. When the model has a
  `provider_session_id`, it passes that value as `resume`.
- `system` chunks update the endpoint session id; `thinking` chunks accumulate metadata; `text`
  chunks accumulate assistant content; `tool_use` and matching `tool_result` chunks become recorded
  and yielded action messages; unmatched tool results are ignored; `result` metadata is attached to
  the final assistant response.
- Assistant text is flushed before a tool request, before a real error is raised, and at normal end.
  Consequently one stream may yield several assistant responses.
- An error chunk marked `benign_eos` ends normally. Any other error chunk flushes already received
  text and raises a classified provider error; an empty error becomes the literal diagnostic
  `"(empty error)"`.
- A positive numeric `timeout` from model kwargs becomes one absolute stream deadline. `None`, zero,
  a negative value, or a non-number disables enforcement. No operations-layer default timeout is
  imposed; the value is inherited from caller/provider options.
- On normal completion, failure, stop control, generator close, or consumer abandonment, `run`
  explicitly closes the stream generator and restores the prior `streaming_process_func`. A
  secondary close failure is logged and cannot replace an exception already propagating.
- With `stream_persist=True`, `run` writes a branch snapshot, appends stream chunks to a JSONL buffer,
  writes a final snapshot during cleanup, and removes the live buffer if it exists. `snapshot_dir`
  falls back to `persist_dir`.
- `run_and_collect(clear_messages=True)` clears first, promotes `ChatParam` to `RunParam`, consumes
  `run`, joins non-empty assistant responses with two newlines, and returns `None` if none arrived.
  It returns raw joined text when validation is skipped or no parse response format exists;
  otherwise it parses once after collection.

**Why this way.** The distinct functions make persistence, lifecycle, and cleanup observable in the
call shape. `chat_and_record` is the smallest opt-in stateful wrapper around the unrecorded
primitive; `communicate` adds parsing and a top-level lifecycle. A CLI stream must preserve partial
output and deterministically close an underlying process when the consumer stops early.
`run_and_collect` adapts that richer stream to the one-result `Middle` shape without weakening the
stream contract itself.

### D4 — The wrapper that owns the exchange owns lifecycle and cleanup

`Branch._observed_run()` emits a branch-level `RunStart`, awaits a coroutine, drains pending message
signals, then emits `RunEnd` with duration and result or `RunFailed` with the exception. Observer
exceptions are logged and swallowed so they do not change the operation result
(`lionagi/session/branch.py`).

The ownership matrix is:

| Public path | Lifecycle owner | Reason |
|-------------|-----------------|--------|
| `chat()` | none at branch wrapper | low-level, unrecorded API primitive |
| `chat_and_record()` | none at branch wrapper | explicit message recording without an operation-lifecycle wrapper |
| `communicate()` | `Branch._observed_run` | one recorded API exchange |
| `operate()` | `Branch._observed_run` | one logical composed operation, regardless of adapter internals |
| `run()` | `run` async generator | lifecycle must span iteration and generator close |
| `ReAct()` | explicit `ReAct` wrapper | one outer lifecycle while nested CLI turns suppress their own signals |

Exact semantics:

- `operate()` and `communicate()` pass their coroutine to `_observed_run`; their implementations do
  not emit an additional outer lifecycle.
- `chat_and_record()` deliberately does not use `_observed_run`; it emits the normal message-add
  effects of `a_add_message()` but no run lifecycle of its own.
- `run()` cannot use a coroutine wrapper because work happens during async iteration. It emits at
  most one terminal signal per emitted start. Consumer abandonment is a clean `RunEnd`; provider or
  operation failure is `RunFailed`.
- `run()` checks a task-scoped `ContextVar` before lifecycle emission. `ReAct()` sets that variable
  while its nested turns run, so concurrent tasks using the same branch do not suppress one another.
- Message-add signals are drained before terminal emission. Lifecycle observer failures are
  non-authoritative and do not replace the transport result or primary exception.
- Cleanup belongs to the stream even when lifecycle is suppressed: nested execution still closes
  generators, restores callbacks, writes final snapshots, and removes temporary buffers.

**Why this way.** Lifecycle scope follows the actual resource lifetime. Wrapping `run()` only around
generator construction would end the lifecycle before iteration; letting every nested turn emit
would turn one reasoning loop into multiple apparent top-level runs. The split is more complex than
one universal wrapper but preserves truthful start/terminal pairs.

### D5 — `Middle` is a logical exchange adapter, not a one-provider-message promise

The shipped structural protocol is:

```python
class Middle(Protocol):
    async def __call__(
        self,
        branch: Branch,
        instruction: JsonValue | Instruction,
        chat_param: ChatParam,
        parse_param: ParseParam | None = None,
        clear_messages: bool = False,
        skip_validation: bool = False,
    ) -> Any: ...
```

The argument dataclasses passed through the seam are frozen, slotted parameter objects:

```python
@dataclass(slots=True, frozen=True, init=False)
class ChatParam(MorphParam):
    guidance: JsonValue = None
    context: JsonValue = None
    sender: SenderRecipient = None
    recipient: SenderRecipient = None
    response_format: type[BaseModel] | dict = None
    structure: type[Structure] | str | None = None
    progression: ID.RefSeq = None
    tool_schemas: list[dict] = None
    images: list = None
    image_detail: Literal["low", "high", "auto"] = None
    plain_content: str = None
    include_token_usage_to_model: bool = False
    imodel: iModel = None
    imodel_kw: dict = None

@dataclass(slots=True, frozen=True, init=False)
class RunParam(ChatParam):
    stream_persist: bool = False
    persist_dir: str | Path = LIONAGI_HOME / "logs" / "runs"
    snapshot_dir: str | Path | None = None
```

Exact semantics:

- `operate()` calls the selected `Middle` exactly once. The adapter may call a provider once, consume
  a stream containing several assistant messages, or execute a bounded series of recorded inner
  exchanges.
- The adapter owns message clearing and persistence for the exchange it performs. `operate()` does
  not append a duplicate instruction or response around it.
- `communicate` and `run_and_collect` are the canonical API and CLI implementations. A caller may
  inject any async callable satisfying the protocol.
- `clear_messages` is a pre-exchange instruction to the adapter. `skip_validation` is forwarded into
  the adapter; ADR-0022 records its additional outer-coordinator effect.
- The protocol does not require a response model, guarantee one provider response, expose streaming
  chunks, or convert exceptions to values. Those properties belong to the chosen adapter.
- `RunParam.persist_dir` inherits the library run-log location. No rationale for that specific path
  is recorded in the operations code; callers can override it.

**Why this way.** The seam substitutes transport-plus-exchange behavior while keeping outer schema
and action policy in `operate()`. Defining it by logical invocation matches every shipped adapter.
Narrowing it to one provider message would make the CLI collector and LNDL adapter non-conforming;
widening it to every operation verb would confuse persisted exchanges with pure parsing or action
execution.

## Consequences

- Callers get one discoverable branch surface while operation implementations remain independently
  testable modules.
- Session extensions and graph nodes share lookup and precedence. A custom operation registered once
  is visible to every branch included in that session.
- Choosing `chat`, `chat_and_record`, or `communicate` is a state and lifecycle decision, not merely
  a convenience alias: the first leaves conversation messages unchanged, the second records without
  a run lifecycle, and the third records under one observed operation lifecycle.
- CLI streams preserve partial messages, native tool messages, cancellation cleanup, and provider
  resumption. Adapting them to a single result requires explicit collection.
- Custom `Middle` implementations can replace a full logical exchange without reimplementing the
  composed validation and action phase.
- Contributors must understand that lifecycle ownership is intentionally split. Adding a wrapper at
  the wrong layer can duplicate start/terminal signals or end a run before a generator is consumed.
- Reversing D1 or D2 is high-cost because public callers and graph dispatch both depend on the
  facade/lookup contract. Replacing a `Middle` is low-cost when it honors D5. Unifying D3 would be
  high-risk because it changes persistence and cancellation behavior.
- The facade and `flow.py` depend on the operation model, but the measured eight-component
  operations architecture remains `κ = 12 / (8 × 7) = 0.214`, below the 0.3 target. The decision
  boundaries remain testable through injected models, registered coroutine operations, and custom
  `Middle` callables (`τ ≈ 0.9` for the area; static tests do not establish provider reliability).

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Replace the `Middle` one-turn wording with a logical-exchange contract that states message-recording, streaming, bounded-loop, native-tool, and validation responsibilities; add conformance tests for `communicate`, `run_and_collect`, and LNDL. | M | (filled at issue-open time) |
| 2 | Make the branch-operation vocabulary explicitly extensible or align it with the facade by adding or removing `select` and including `run` and `chat_and_record`; publish the intended registry and adapter exports from one canonical namespace. | S | #2021 |
| 3 | Document the session ownership of the named-operation registry in its type and public API, preserving built-in precedence and asynchronous registration checks. | S | (filled at issue-open time) |
| 4 | Represent lifecycle ownership for API turns, CLI streams, and nested operations in an explicit internal contract and add tests that prevent duplicate start or terminal signals. | M | (filled at issue-open time) |
| 5 | Correct the public `Branch.chat()` return annotation so it expresses response text by default and the instruction/response tuple when `return_ins_res_message=True`; add typing coverage for both call forms. | S | #2022 |

## Alternatives considered

### Publish operation functions instead of a `Branch` facade

This would make dependency direction obvious and keep `Branch` smaller. It lost because every call
would still require the caller to select and pass the branch's managers, models, observer, hooks,
and persistence state. It would also create a second public calling convention while graph nodes
already dispatch by branch operation name.

### Eagerly import all implementations from `branch.py`

This would make navigation and static symbol discovery simpler. It lost because implementations
type-reference `Branch` and some import branch-owned types; eager imports reintroduce the runtime
cycle that the method-local imports avoid. It would also load provider- and operation-specific code
when a caller only constructs a branch.

### Give every branch an independent named-operation registry

This would permit branch-local overrides and narrower visibility. It lost because session-created
clones and graph-assigned branches would not reliably see the same custom operation. Coordinated
flows would need registry-copy rules and could drift after registration. The shipped session-owned
manager gives one lifetime and one lookup truth.

### Allow registered operations to shadow built-in methods

This would make the extension mechanism maximally flexible. It lost because a registration could
silently redefine a stable public verb for all branches in a session. `update=True` intentionally
means replace a registry entry, not replace `Branch.operate` or another facade method.

### Collapse API and CLI calls into one transport primitive

This would reduce the number of public verbs and adapters. It lost because an API invocation is a
single awaited event while a CLI invocation is an async-generator resource whose cleanup must span
iteration and consumer abandonment. A single primitive would either hide the stream or force every
API call through streaming lifecycle machinery.

### Make the facade record every model call automatically

This would make history behavior superficially uniform. It lost because `chat()` is intentionally a
non-recording primitive used when a caller needs a request without conversation mutation.
`chat_and_record()` already supplies the explicit recording wrapper. Automatic recording would also
duplicate messages for `communicate`, `run`, and custom `Middle` implementations that already own
persistence.

### Define `Middle` as exactly one provider turn

This would be a narrower, easier-to-test protocol. It lost against shipped behavior:
`run_and_collect` may combine multiple assistant messages and LNDL owns several inner exchanges.
The logical-invocation definition preserves a useful substitution seam without falsifying adapter
cardinality.

### Make every branch verb a `Middle`

This would provide one universal callable shape. It lost because `parse`, `act`, and `interpret` do
not share the persisted model-exchange contract, and `run` must remain an async generator rather
than an awaited one-result callable. Forcing them through `Middle` would erase meaningful types and
lifecycle boundaries.

## Notes

Primary implementation anchors are `lionagi/session/branch.py`, `lionagi/session/session.py`,
`lionagi/operations/types.py`, `lionagi/operations/manager.py`, `lionagi/operations/node.py`,
`lionagi/operations/chat/chat.py`, `lionagi/operations/communicate/communicate.py`, and
`lionagi/operations/run/run.py`. Focused behavioral anchors live under `tests/operations/test_chat.py`,
`tests/operations/test_communicate.py`, `tests/operations/run/`, and
`tests/session/test_run_lifecycle.py`.
