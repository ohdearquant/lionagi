# ADR-0012: Branch action execution and event lifecycle

- **Status**: Accepted
- **Kind**: Retrospective
- **Area**: actions-tools
- **Date**: 2026-07-09
- **Relations**: extends ADR-0011

## Context

The descriptor and registry in ADR-0011 answer which function a branch can expose.
Execution crosses more boundaries: argument normalization, governance, hook emission,
callable invocation, event persistence, observer emission, and conversation history.
Five concrete problems define the shipped transaction.

**P1 — A failed callable must leave an inspectable execution record.** Tool bodies,
preprocessors, and postprocessors can fail after request validation. `FunctionCalling`
therefore inherits the generic `Event` lifecycle and records terminal status, response,
duration, and error. Ordinary exceptions become `FAILED` data; cancellation-class
`BaseException` values become `CANCELLED` and continue propagating
(`lionagi/protocols/action/function_calling.py`;
`lionagi/protocols/generic/event.py`).

**P2 — A model-facing call is a conversation transaction, not only a function call.**
The normal `Branch.act()` path authorizes the proposed call, emits lifecycle hooks,
invokes through the branch registry, logs and emits the event, and writes a linked
`ActionRequest`/`ActionResponse` pair. Structured `operate()` and the LNDL action bridge
route generated requests through this operation, so these model-facing paths receive the
same transaction (`lionagi/operations/act/act.py`;
`lionagi/operations/operate/operate.py`;
`lionagi/operations/lndl_middle/lndl_middle.py`).

**P3 — Captured execution failure and branch response construction disagree.**
`ActionManager.invoke()` returns a `FunctionCalling` after `Event.invoke()` even when its
status is `FAILED`. The branch transaction does not inspect that status. It emits
`TOOL_POST`, persists the failed event, and writes `func_call.response`—normally
`None`—as the attempted action output. `MessageManager.a_add_message()` removes
`None`-valued keyword arguments before dispatch, so this call re-adds the existing
`ActionRequest` rather than creating an `ActionResponse`. Conversation history therefore
cannot distinguish a failed tool body from a successful tool returning `None`: both
leave an unresponded request, even though the event record can distinguish them
(`lionagi/protocols/action/manager.py`; `lionagi/operations/act/act.py`;
`lionagi/protocols/messages/manager.py`;
`lionagi/protocols/messages/action_response.py`).

**P4 — Governance and intrinsic tool policy observe different call shapes.** The
session observer authorizes a raw `ToolInvocation` before request-model normalization.
Agent permission hooks are attached later as `Tool.preprocessor` callables and see the
normalized dictionary. When user preprocessors exist, security preprocessors run before
and after them. These are separate seams rather than one authoritative phase model
(`lionagi/session/observer.py`; `lionagi/agent/factory.py`;
`lionagi/agent/permissions.py`).

**P5 — Batch and Python callable behavior introduce concurrency edges.** The branch
operation can run request transactions concurrently or sequentially. Inside each
`FunctionCalling`, coroutine functions are awaited and declared synchronous functions
run inline on the current event-loop thread. Awaitability is classified from the
callable declaration, not the returned value (`lionagi/operations/act/act.py`;
`lionagi/ln/_async_call.py`; `lionagi/ln/concurrency/utils.py`).

| Concern | Decision |
|---|---|
| Invocation record | D1: `FunctionCalling` executes preprocessor → callable → postprocessor inside the generic failure-capturing `Event` lifecycle. |
| Conversation transaction | D2: `Branch.act()` delegates each request to `_act()`, which orders authorization, hooks, invocation, event handling, and linked messages. |
| Outcome projection | D3: branch responses currently project `FunctionCalling.response` without interpreting terminal event status. |
| Batch behavior | D4: action batches use explicit concurrent or sequential strategy, preserving one `_act()` transaction per input. |
| Policy boundary | D5: session authorization, tool processors, and direct low-level invocation remain distinct public seams. |
| Sync/async behavior | D6: coroutine declarations are awaited; synchronous declarations and processors execute inline. |

This ADR does **not** decide:

- Tool schema generation, registry identity, or MCP discovery; ADR-0011 owns those
  contracts.
- Construction and clone scope of built-in providers; ADR-0013 owns that lifecycle.
- The complete session observer or hook vocabulary. This ADR records only behavior on
  the action execution path.
- Retry policy for arbitrary tool side effects. The default action batch does not retry;
  callers can supply `AlcallParams`, but idempotency remains tool-specific.

## Decision

### D1 — `FunctionCalling` is a failure-capturing execution event

The invocation type and its generic execution state are:

```python
# lionagi/protocols/action/function_calling.py
class FunctionCalling(Event):
    func_tool: Tool = Field(..., exclude=True)
    arguments: dict[str, Any] | BaseModel

    @property
    def function(self): ...

    async def _invoke(self) -> Any: ...

    def to_dict(self, *args, **kw) -> dict[str, Any]: ...

# lionagi/protocols/generic/event.py
class EventStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    ABORTED = "aborted"

class Execution:
    __slots__ = ("status", "duration", "response", "error", "retryable")

    def __init__(
        self,
        duration: float | None | UnsetType = Unset,
        response: Any = None,
        status: EventStatus = EventStatus.PENDING,
        error: str | BaseException | None = None,
        retryable: bool | None | UnsetType = Unset,
    ) -> None: ...

class Event(Element):
    execution: Execution = Field(default_factory=Execution)
    streaming: bool = Field(False, exclude=True)

    async def invoke(self) -> None: ...
```

The successful callable pipeline is:

```text
validated arguments
  → optional preprocessor(arguments, **preprocessor_kwargs)
  → func_callable(**arguments)
  → optional postprocessor(response, **postprocessor_kwargs)
  → execution.response
```

`FunctionCalling.to_dict()` adds `function` and normalized `arguments` to the generic
event serialization. The live `func_tool` remains excluded.

**Exact semantics**

- **Construction:** an input `BaseModel` is dumped with `exclude_unset=True`. If the
  tool has `request_options`, that model is then instantiated and dumped the same way.
  Strict or minimum-key validation runs before an event reaches `invoke()` (ADR-0011
  D2); construction errors are not captured in this event.
- **First invocation:** an event in `PENDING` changes to `PROCESSING`, samples a UTC
  timestamp for duration measurement, and enters `_invoke()`. The start sample is not a
  persisted execution field; `created_at` remains the event-construction timestamp.
- **Preprocessor:** its return value replaces `self.arguments`. No second request-model
  validation follows the transform.
- **Callable:** the current arguments are expanded as keyword arguments.
- **Postprocessor:** its return value becomes the eventual response.
- **Coroutine declaration:** a callable or processor for which `is_coro_func(...)` is
  true is awaited.
- **Synchronous declaration:** a callable or processor classified as sync is called
  directly. If it returns an awaitable, that awaitable is treated as the ordinary
  response and is not awaited.
- **Successful completion:** the final result is stored in `execution.response`, status
  becomes `COMPLETED`, and duration is always recorded in seconds.
- **Ordinary exception:** any `Exception` raised by the preprocessor, callable, or
  postprocessor sets status to `FAILED`, adds the exception to `execution.error`, leaves
  the pre-existing response value unchanged (normally `None`), records duration, and is
  not re-raised.
- **Cancellation-class failure:** a `BaseException` outside `Exception` is added to the
  error, status becomes `CANCELLED`, duration is recorded, and the exception is re-raised.
- **Repeated invocation:** `Event.invoke()` returns immediately whenever status is not
  `PENDING`. A completed, failed, processing, or cancelled `FunctionCalling` is not
  executed again.
- **Terminal notification:** setting `COMPLETED`, `FAILED`, `CANCELLED`, `ABORTED`, or
  `SKIPPED` signals the lazily created `completion_event`.
- **Error accumulation cap:** `Execution.add_error()` retains at most 100 members once
  the error becomes an `ExceptionGroup`. The cap bounds serialized error growth; the
  value is inherited from the generic event layer and has no action-specific recorded
  rationale. One ordinary `FunctionCalling.invoke()` normally contributes one error.
- **Serialization:** exceptions become `{ "error": <type>, "message": <text> }`;
  unserializable responses fall back through recursive conversion and finally the
  string `"<unserializable>"`.

**Why this way**

Failure as event data lets callers inspect and persist a complete attempt without using
exceptions as the only control channel. It also allows independent concurrent calls to
finish. Cancellation remains exceptional because swallowing task cancellation would
break structured concurrency. The design requires every caller above `Event` to inspect
status; D3 records where the branch layer currently fails to do so.

### D2 — `_act()` is the normal branch conversation transaction

The public and internal signatures are:

```python
# lionagi/session/branch.py
async def Branch.act(
    self,
    action_request: list | ActionRequest | BaseModel | dict,
    *,
    strategy: Literal["concurrent", "sequential"] = "concurrent",
    verbose_action: bool = False,
    suppress_errors: bool = True,
    call_params: AlcallParams = None,
) -> list[ActionResponse]: ...

# lionagi/operations/act/act.py
async def _act(
    branch: Branch,
    action_request: BaseModel | dict | ActionRequest,
    suppress_errors: bool = False,
    verbose_action: bool = False,
): ...
```

The accepted proposal passed to session governance is:

```python
# lionagi/session/control.py
@dataclass(frozen=True, slots=True)
class ToolInvocation:
    function: str
    arguments: dict = field(default_factory=dict)
    branch_id: str | None = None
```

The immediate return value is not the persisted message type:

```python
# lionagi/operations/fields.py
class ActionResponseModel(HashableModel):
    function: str = Field(default_factory=str)
    arguments: dict[str, Any] = Field(default_factory=dict)
    output: Any = None
```

The persisted message pair uses these content shapes:

```python
# lionagi/protocols/messages/action_request.py
@dataclass(slots=True)
class ActionRequestContent:
    function: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    action_response_id: str | None = None

# lionagi/protocols/messages/action_response.py
@dataclass(slots=True)
class ActionResponseContent:
    function: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    output: Any = None
    action_request_id: str | None = None
    error: str | None = None
```

For an allowed call that returns a `FunctionCalling`, the transaction order is:

```text
normalize envelope
  → branch.authorize(ToolInvocation from raw arguments)
  → TOOL_PRE
  → ActionManager.invoke()
  → TOOL_POST
  → branch.emit_and_log(FunctionCalling)
  → ensure ActionRequest message
  → add linked ActionResponse only when response is not None
  → return ActionResponseModel
```

The message-manager discriminator responsible for the `None` edge is:

```python
# lionagi/protocols/messages/manager.py
async def MessageManager.a_add_message(self, **kwargs):
    # None-valued entries are removed before create_message().
    _msg = self.create_message(**{k: v for k, v in kwargs.items() if v is not None})
    ...

def MessageManager.create_message(
    *,
    action_output: Any = None,
    action_request: ActionRequest | None = None,
    action_response: ActionResponse | Any = None,
    ...,
):
    if action_request and action_output is None and action_response is None:
        ...  # select ActionRequest
    elif action_response is not None or action_output is not None:
        ...  # select ActionResponse
```

**Exact semantics**

- **Accepted envelopes:** an `ActionRequest`; a `BaseModel` whose class declares at
  least `function` and `arguments`; or a dictionary containing both keys. Any other
  shape raises `ValueError` before authorization.
- **Raw gate arguments:** if the envelope's `arguments` value is not already a
  dictionary, governance receives `{}`. Pydantic request-model normalization has not yet
  run.
- **Standalone branch:** `Branch.authorize()` returns `True` when no session observer is
  bound.
- **Bound observer:** no installed gate allows. A falsy gate result or an exception from
  the gate denies and adds a `GateDenied` signal to the observer flow.
- **Denied call:** no tool hook, function event, or tool callable runs. `_act()` ensures
  an `ActionRequest`, adds an action response whose output is
  `{"error": "denied by governance gate", "function": <name>}`, and returns an
  `ActionResponseModel`. Denial is a value regardless of `suppress_errors`.
- **Correlation and summaries:** an allowed call gets a random UUID string `call_id`.
  `TOOL_PRE` receives `tool_name`, `call_id`, and the first 200 characters of
  `str(raw_arguments)`. When verbose logging is enabled, its argument preview is capped
  at 50 characters. Both are inherited observability budgets; no recorded rationale
  explains the exact lengths.
- **Blocking pre-hook:** `_act()` calls `HookBus.emit(TOOL_PRE, ...)`; the bus routes
  that point through `blocking_emit()`, runs handlers sequentially, and propagates
  ordinary handler exceptions. Because pre-emission sits outside `_act()`'s invocation
  `try`, such an exception bypasses `TOOL_ERROR`, `suppress_errors`, event logging, and
  action history. `StopHook` stops remaining pre-handlers but does not prevent the tool
  invocation itself.
- **Returned event:** `TOOL_POST` receives the same `call_id`, `tool_name`, the first
  200 characters of `str(func_call.response)`, and the recorded duration. Non-pre hook
  handler exceptions are logged and isolated by `HookBus`.
- **Event handling:** `branch.emit_and_log()` logs the complete event and emits it to the
  session observer. The observer stores the event before applying routes and
  subscriptions; a gate denial at this emission stage suppresses dispatch but not
  storage.
- **Observer failure after invocation:** `emit_and_log()` is outside `_act()`'s
  invocation `try`, and it uses `Branch.emit()`, not the failure-isolating
  `_safe_emit()`. An observer route, predicate, or subscriber exception can therefore
  propagate after the event is durably logged but before request/response history is
  created.
- **Message linking for non-`None` output:** a dictionary or generic model becomes an
  `ActionRequest` whose recipient is the tool descriptor id. The response message copies
  function and arguments from that request and stores its id as `action_request_id`;
  message-manager linking writes the reciprocal response id. Falsy outputs other than
  `None` (`0`, `""`, `[]`, `{}`) still create and link an `ActionResponse` because the
  discriminator uses `is not None`.
- **Message linking for `None` output:** `a_add_message()` filters out the
  `action_output` entry, selects the ActionRequest construction path, and re-includes the
  same request at its existing progression position. No `ActionResponse` is created and
  `ActionRequest.action_response_id` remains `None`.
- **Message callback failure:** `a_add_message()` mutates the message pile before firing
  its `on_message_added` callbacks. It runs all callbacks, then re-raises one failure or
  an exception group. `_act()` does not roll back the already-inserted request/response,
  so the caller can receive an exception after history changed.
- **Successful return:** the immediate `ActionResponseModel` contains the request's
  function and arguments and `func_call.response` as output, including `None`; its
  existence does not imply that an `ActionResponse` message was persisted.
- **Cancellation:** cancellation re-raised by `FunctionCalling.invoke()` is not caught
  by `_act()`'s `except Exception`; it propagates before `TOOL_POST`, event logging, and
  history creation.

**Why this way**

The transaction gives model-visible calls a consistent audit and history path while
keeping denial readable by the next reasoning turn. The hook positions support guards
and observability without moving callable-specific transforms out of `Tool`. The order
also exposes two weaknesses: raw authorization and normalized processors do not see the
same payload, and a status-blind projection can contradict the event truth.

### D3 — Branch response projection is status-blind

There are two failure channels after authorization.

| Failure channel | Example | Does manager raise? | Hook path | History output |
|---|---|---:|---|---|
| Pre-invocation exception | unknown function, request-model validation, strict key mismatch | Yes | `TOOL_ERROR` | structured error dictionary when suppressed; no history when re-raised |
| Captured event failure | preprocessor, callable, or postprocessor raises ordinary `Exception` | No | `TOOL_POST` | no `ActionResponse` when `func_call.response` remains `None`; request remains unresponded |

**Exact semantics**

- **Manager exception:** `_act()` emits `TOOL_ERROR` with `call_id`, `tool_name`, the
  exception object, and `duration=None`, then logs a dictionary containing error,
  function, arguments, and branch id.
- **Suppressed manager exception:** with `suppress_errors=True`, `_act()` ensures request
  and response history. Persisted output contains `error`, `function`, and `arguments`;
  the immediate output contains `error` and a formatted `message`. The original
  exception does not propagate.
- **Unsuppressed manager exception:** with `suppress_errors=False`, the exception is
  re-raised after hook emission and logging; no request/response messages are added by
  that path.
- **Captured callable failure:** `ActionManager.invoke()` returns the failed event.
  `_act()` does not branch on `func_call.status` or read `func_call.execution.error`.
  It follows `TOOL_POST`, emits/logs the event, and passes the response value to the
  message manager. When the value is `None`, no response message is constructed.
- **Successful `None`:** the same immediate output (`None`) and history state (an
  unresponded `ActionRequest`, with no `ActionResponse`) is produced for a completed
  callable that intentionally returns `None`.
- **Unused message error field:** `ActionResponseContent.error` and its `success`
  property can distinguish failure, but `_act()` populates errors inside `output` only
  on the raised-and-suppressed path and never maps a failed event into that field.

**Why this way**

The mismatch arose because the low-level event deliberately captures ordinary
exceptions, while the branch code was organized around exceptions escaping manager
invocation. Both pieces are internally coherent but their composition drops the status
discriminant. The existing `ActionResponseContent.error` field provides a correction
path without changing successful output values; the shipped behavior remains recorded
here until that delta lands.

### D4 — Batches preserve one transaction per call under two strategies

Batch configuration is a frozen parameter object:

```python
# lionagi/operations/types.py
@dataclass(slots=True, frozen=True, init=False)
class ActionParam(MorphParam):
    action_call_params: AlcallParams = None
    tools: ToolRef = None
    strategy: Literal["concurrent", "sequential"] = "concurrent"
    suppress_errors: bool = True
    verbose_action: bool = False
```

`prepare_act_kw()` supplies `AlcallParams(output_dropna=True)` when callers do not pass
`call_params` (`lionagi/operations/_defaults.py`).

**Exact semantics**

- **Input normalization:** both strategies wrap a single request in a one-element list.
- **Empty input:** an empty list returns an empty list. Concurrent mode creates no task;
  sequential mode performs no iteration. No hooks, events, or messages are produced.
- **Concurrent:** the configured `AlcallParams` applies `_act()` to every request. The
  generic `alcall` implementation stores results by input index, so the returned list
  preserves input order rather than completion order.
- **Default concurrent budget:** the default action parameters set no retry, timeout,
  throttle, or `max_concurrent` value. All input calls may start concurrently, and each
  call is attempted once. `output_dropna=True` is the only action-specific override; it
  removes null-like results from the collected output. No recorded rationale explains an
  unbounded default beyond inheritance from the generic call helper.
- **Caller-supplied controls:** `AlcallParams` can add retry attempts, timeout,
  concurrency cap, throttle, and exception-return behavior. Applying retries to
  side-effecting `_act()` calls can duplicate tool effects and history; the action layer
  does not add an idempotency key or rollback.
- **Concurrent raised failure:** with `return_exceptions=False`, an unsuppressed
  exception from one `_act()` task is re-raised (a single grouped exception is unwrapped)
  and the task group cancels outstanding siblings. `suppress_errors=True` converts the
  ordinary manager-exception path to values, but cancellation-class failures still
  propagate. `return_exceptions=True` retains raised failures in their input slots.
- **Sequential:** `_act()` is awaited in list order and every result is appended. A
  raised unsuppressed exception stops the loop; an error returned because suppression is
  enabled does not.
- **Invalid strategy:** any value other than `concurrent` or `sequential` raises
  `ConfigurationError`.
- **Per-call lifecycle:** authorization, hook ids, event records, and message pairs are
  independent for every item under either strategy.

**Why this way**

The strategy switch gives callers ordering when effects depend on prior branch state and
parallelism when calls are independent. Reusing `AlcallParams` makes operational controls
available without a second batch framework. It also means callers, not the action layer,
own the safety of retries and high concurrency.

### D5 — Governance, processors, and low-level access are separate seams

The session gate accepts `ToolInvocation` before the manager constructs a
`FunctionCalling`. Agent permission policy is converted to a tool preprocessor:

```python
# lionagi/agent/permissions.py
@dataclass
class PermissionPolicy:
    mode: str = "allow_all"
    allow: dict[str, list[str]] = field(default_factory=dict)
    deny: dict[str, list[str]] = field(default_factory=dict)
    escalate: dict[str, list[str]] = field(default_factory=dict)
    on_escalate: Callable | None = None

    def check(self, tool_name: str, action: str, args: dict) -> PermissionDecision: ...
    def to_pre_hook(self) -> Callable: ...
```

Factory hook composition is:

```text
security_pre:* and security_pre:<tool>
  → pre:* and pre:<tool>
  → security hooks again, but only when user pre-hooks exist
  → callable
  → post hooks
```

**Exact semantics**

- **Session authorization payload:** function, raw dictionary arguments (or `{}` for a
  non-dictionary), and branch id.
- **Request-model validation:** runs only after authorization, when the manager builds
  `FunctionCalling`.
- **Tool processor payload:** the Pydantic-normalized argument dictionary. A processor
  may replace it with another dictionary before invocation.
- **Security revalidation:** `_chain_pre_hooks()` appends the same security hooks after
  user hooks only when at least one user hook exists. With no user pre-hook, security
  runs once.
- **Permission errors:** a permission preprocessor raises `PermissionError`; because it
  occurs inside `Event.invoke()`, it becomes a captured `FAILED` event rather than the
  session gate's denial-shaped value. D3 then projects it as a post-call `None` output.
- **Public low-level manager:** `branch.acts` returns the manager. A caller can invoke it
  directly, receiving request-model validation and event capture but bypassing session
  authorization, `HookBus`, branch logging/emission, and messages.
- **Public callable:** `Tool.func_callable` can be called directly, bypassing manager
  validation, tool processors, event lifecycle, governance, hooks, logging, and history.
- **Normal model paths:** `operate()` and LNDL call the branch action operation, so their
  model-generated requests use the governed transaction.

**Why this way**

The seams were added for different scopes: session gates govern a conversation,
processors adapt or protect one tool, and manager/callable access supports testing and
low-level composition. Their coexistence is useful but not substitutable. A future
authoritative executor must name bypass APIs explicitly and present every policy phase
with one normalized call context.

### D6 — Sync execution is inline and awaitability follows declarations

`FunctionCalling._invoke()` calls `is_coro_func()` separately for the preprocessor,
tool callable, and postprocessor.

**Exact semantics**

- An `async def` function is awaited on the current event loop.
- A declared synchronous function is executed directly on the event-loop thread; it is
  not sent through `run_sync()` by the generic action executor.
- A sync pre- or postprocessor has the same inline behavior.
- A callable object or decorator whose declaration is classified as sync but whose
  return value is awaitable produces that awaitable as data; the executor does not call
  `inspect.isawaitable()` on results.
- Blocking sync CPU or I/O stalls other calls sharing the event loop, including a
  nominally concurrent action batch.
- Built-in tools that know they perform blocking work explicitly use `run_sync()` inside
  their async wrappers; this is a provider implementation convention, not enforcement by
  `FunctionCalling`.

**Why this way**

Direct sync support makes ordinary Python functions easy to register and avoids imposing
thread handoff on tiny pure functions. The absence of an explicit blocking policy leaves
latency and decorator edge cases to individual tool authors, which is retained as a
delta rather than presented as an ideal contract.

## Consequences

- Ordinary callable failures survive as queryable event records and do not inherently
  abort sibling calls in a concurrent batch.
- Session denial is model-visible and occurs before hook and callable side effects.
- The normal branch transaction provides separate integration points for pre-guards,
  post/error observation, durable logs, reactive observer handlers, and conversation
  messages.
- Captured tool failures are truthful in the event but absent from action-response
  history when the response remains `None`; a successful `None` and a failed call are
  conversation-equivalent unresponded requests today.
- A blocking `TOOL_PRE` exception bypasses suppression and history, while a permission
  preprocessor exception is captured and then misprojected by D3. Policy location changes
  user-visible semantics.
- An observer exception after invocation can leave a durable function event without the
  linked action response in conversation history. Recovery and debugging must compare
  both records rather than assuming they advance atomically.
- Direct manager and callable entry points are useful escape hatches but are not governed
  equivalents of `Branch.act()`.
- Concurrent batches have no default cap or retry; caller-supplied retries can repeat
  non-idempotent effects, while an unsuppressed failure can cancel outstanding siblings.
- Reversing D1 from captured failures to raised failures would change manager and batch
  control flow. Reversing D2 requires migrating every model-facing operation that relies
  on action history and hooks.
- Contributors must inspect `FunctionCalling.status` whenever invoking through the
  manager and must explicitly offload blocking work inside async tool adapters.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Make the branch action transaction inspect `FunctionCalling.status`, emit `TOOL_ERROR` for captured failures, persist an error-bearing `ActionResponse`, and reserve `TOOL_POST` for completed calls; acceptance must define and persist a linked response for successful `None` and distinguish it from failure in sequential and concurrent regression tests. | M | #2014 |
| 2 | Publish one authoritative branch action executor and reduce `ActionManager` to registry and resolution responsibilities; acceptance requires Branch, operate, iterative reasoning, and LNDL paths to use the executor, any no-history invocation to be explicitly named, and observer/message-callback failures to have a tested policy that cannot silently split event logging from action history. | M | (filled at issue-open time) |
| 3 | Define one normalized call context and explicit authorization, intrinsic-policy, agent-policy, transform, and revalidation phases; acceptance requires every phase to receive the same normalized arguments and security revalidation not to depend on whether a user preprocessor exists. | M | (filled at issue-open time) |
| 4 | Define and enforce a sync-tool execution policy; acceptance requires blocking sync work either to be offloaded by the executor or to require an explicit inline opt-in, with returned awaitables handled consistently. | S | (filled at issue-open time) |

## Alternatives considered

### A. Let callable exceptions propagate instead of recording failed events

This would make failure impossible to overlook and align with conventional Python call
semantics. It lost because event status, duration, and error are durable execution data,
and independent action batches benefit from a returned attempt record. Cancellation
still propagates because it belongs to task control rather than business failure.

### B. Return only values from `ActionManager.invoke()`

This would simplify callers and hide the event abstraction below the manager. It lost
because callers need status, duration, and error for logging and reactive observation;
the branch transaction persists the full `FunctionCalling`, not only its response.

### C. Treat any returned `FunctionCalling` as success

This is the organic shape currently shipped: exceptions escaping the manager enter the
error path, while a returned object enters the post path. It bought simple exception-led
control flow. It loses the event's status discriminant, creating the successful-`None`
ambiguity. Delta 1 retains the status-aware alternative as the required correction.

### D. Raise session denial as `PermissionError`

This would make authorization failure match ordinary Python access-control behavior and
stop the current operation immediately. It lost because reasoning loops need to see the
denial as a tool result and adapt, and denial is expected policy output rather than an
executor malfunction.

### E. Put all policy in the session gate

One gate would give authorization a single location and avoid processor-dependent
failure semantics. It lost because intrinsic tool policies and transforms travel with a
tool even outside one session, while session governance needs branch and conversation
scope. The unresolved requirement is a shared normalized call context, not deletion of
either scope.

### F. Put all policy in tool preprocessors

This would keep tools self-contained and make direct manager invocation policy-aware. It
lost because preprocessors run after request construction and cannot represent the
session observer's pre-invocation audit/denial contract. Direct callable access would
still bypass them.

### G. Execute every sync callable in a worker thread

This would protect the event loop from blocking I/O and CPU work. It lost in the shipped
generic executor because it imposes thread handoff and thread-safety constraints on
small pure functions and bound objects. Built-ins offload known blocking operations
manually; Delta 4 requires an explicit policy instead of relying on convention.

### H. Detect and await any returned awaitable

This would make decorated sync wrappers and callable objects behave like coroutine
functions. It lost because the implementation uses declaration classification uniformly
for processors and callables. The current behavior is simple but sharp; changing it
requires tests for nested awaitables and postprocessor ordering.

### I. Make a batch transactional

An all-or-nothing batch could roll back earlier tool effects when one call fails. It lost
because arbitrary tools cross files, processes, networks, and conversation state with no
shared transaction manager. Sequential ordering and per-call results are honest; atomic
multi-step behavior belongs inside a purpose-built tool.

## Notes

The existing `ActionResponseContent.error` field can carry a structured failure without
changing successful output values. Whether branch execution returns failures as values
or raises them is separable from whether history represents the failure truthfully.
