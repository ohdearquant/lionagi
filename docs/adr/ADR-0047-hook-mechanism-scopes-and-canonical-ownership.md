# ADR-0047: Hook mechanism scopes and canonical ownership

- **Status**: Accepted
- **Kind**: Retrospective
- **Area**: hooks
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0023, v0-0072, v0-0076

## Context

LionAGI has three live mechanisms called hooks. They intercept different operations, have different
lifetimes, and deliberately provide different failure semantics. Treating them as alternative
implementations of one handler interface would remove behavior required by at least one caller.

This ADR answers six concrete problems.

**P1 — “hook” names three different timing contracts.** A Session hook may be an ordered observer
or a guard before the action manager. A service hook surrounds one `Event` before and after an API
call and also handles streaming chunks. An agent Tool hook can replace invocation arguments and
results. A common name does not make their handler arguments or control effects substitutable.

**P2 — Session hooks need both ordered execution and reactive recording.** Persistence and guards
need registration order, sequential awaits, and `StopHook` short-circuiting. Session-wide reactive
subscribers use typed filters and concurrent async fan-out. The shipped `HookBus` therefore owns
the ordered chain and records `HookSignal` envelopes through `SessionObserver`; neither discipline
can replace the other.

**P3 — service hooks must work without a Session.** `iModel` can be constructed and invoked on its
own. Its `HookRegistry` is attached to that model, and `APICalling(HookedEvent)` applies pre-create,
pre-invocation, post-invocation, timeout, exit, and stream rules around the provider event. Moving
that control path into Session state would silently remove hooks from standalone model use.

**P4 — Tool interception needs invocation data, not telemetry summaries.** A Tool preprocessor
receives the argument mapping and may replace it before the callable runs. The Session
`HookPoint.TOOL_PRE` payload contains `tool_name`, `call_id`, and a 200-character summary. It can
block by raising but cannot transform the Tool's argument mapping. An observation adapter cannot
manufacture the stronger mutation contract.

**P5 — the public Session vocabulary is wider than the wired implementation.** `HookPoint` has
eleven values. Seven have production emit sites: session start/end, branch creation, tool
pre/post/error, and message addition. `API_PRE_CALL`, `API_POST_CALL`, `API_STREAM_CHUNK`, and
`ARTIFACT_CREATED` have no production `HookBus` emit site. A declared enum member is not evidence
that its integration exists.

**P6 — lazy ownership and compatibility surfaces expose real maintenance traps.** Creating
`Session.hooks` after branches were included does not backfill the bus onto those branches.
`build_session_bus()` accepts declarative overrides, but the shipped `Session.hooks` property calls
it with defaults only. Service stream-handler typing and `iModel.create_event()` return annotation
also differ from their runtime calls. These facts must be visible instead of hidden behind a claim
of a completed universal hook system.

The shipped ownership and invocation relationships are:

```text
Session
├── HookBus ── ordered handlers ──► HookSignal ──best effort──► SessionObserver
│      └── TOOL_PRE may block                              └── Flow / reactive subscribers
├── Branch message callback ──► MESSAGE_ADD handler chain + MessageAdded signal
└── Branch action
       └── governance gate ──► HookBus TOOL_PRE ──► ActionManager
                                                    └── Tool preprocessor
                                                        ──► callable
                                                        ──► Tool postprocessor
                                                    ──► HookBus TOOL_POST/ERROR

iModel (standalone or Branch-owned)
└── HookRegistry
      ├── pre-event-create
      ├── HookedEvent pre-invocation ──► APICalling core ──► post-invocation
      └── per-chunk stream handlers
```

| Concern | Decision |
|---|---|
| Scope ownership | D1: Session, service-event, and Tool-invocation hooks remain three canonical mechanisms with explicit boundaries. |
| Ordered Session dispatch | D2: `HookBus` owns the closed point vocabulary, sequential chains, `StopHook`, and blocking `TOOL_PRE`. |
| Session recording and lifetime | D3: `SessionObserver` is the recording/reactive transport; Session owns bus attachment and persistence routing. |
| Service event lifecycle | D4: `HookRegistry` and `HookedEvent` remain per-`iModel` control with their shipped status, timeout, exit, and stream semantics. |
| Tool interception | D5: `Tool.preprocessor` and `Tool.postprocessor` remain the mutable per-invocation interception surface. |
| Cross-scope integration | D6: adapters may add observations, but must preserve the source scope, ordering, payload, and failure behavior. |

This ADR does not decide:

- Executable Tool permission policy or the target universal interceptor plan. ADR-0044 owns those
  changes; this ADR records the currently shipped mechanism that target must replace.
- Session governance policy. `SessionObserver.authorize()` is mentioned only to locate hooks in the
  invocation order.
- Capability grants and structured emissions. They share `SessionObserver` transport but are not
  hook execution.
- The persistence schema for session signals or messages. This ADR records the hook-side payload and
  routing contracts only.
- A new API-call telemetry bridge. The current absence is recorded; implementing the bridge is a
  separate change with its own typed payload.
- Artifact production ownership. No canonical artifact emit site exists in the hook system today.
- User code discovery or import trust for hook modules. The loader resolves already registered
  names; it is not a general plugin loader.

## Decision

### D1 — canonical ownership is per scope, not one universal module

There is no single canonical handler ABI for all hooks. Canonical ownership is:

| Canonical mechanism | Lifetime and scope | Handler sees | Control it can exert |
|---|---|---|---|
| `lionagi/hooks/HookBus` | one Session bus shared by attached Branches | point-specific keyword payload | sequential observation; `TOOL_PRE` may block by raising |
| `lionagi/service/hooks/HookRegistry` + `HookedEvent` | one `iModel` and its API events | event type or event instance; hook params; stream chunk tuple | replace event at pre-create; abort before core; observe/fail after core; process chunks |
| `AgentSpec`/`CodingToolkit` Tool hooks | one registered Tool invocation | validated invocation arguments or returned result | replace arguments, block callable, replace dictionary results |

The module boundary is:

```text
lionagi/
├── hooks/
│   ├── bus.py             HookPoint, HookBus, HookSignal, StopHook, @hook
│   ├── loader.py          named handler registry and per-point default replacement
│   ├── builtins.py        Session persistence and logging handlers
│   └── persist.py         CLI message-persistence routing through a Session bus
├── session/
│   ├── observer.py        SessionObserver recording, filters, routes, governance gate
│   ├── signal.py          Signal and MessageAdded envelopes
│   ├── session.py         lazy Session ownership of observer and bus
│   └── branch.py          observer/bus attachment and message callbacks
├── service/hooks/
│   ├── _types.py          HookEventTypes, HookDict, StreamHandlers
│   ├── hook_registry.py   one handler per service phase and per stream key
│   ├── hook_event.py      timeout/status/exit wrapper
│   └── hooked_event.py    pre/core/post template method
├── agent/
│   ├── spec.py            HooksMixin declarations
│   └── factory.py         per-Tool hook-chain attachment
└── protocols/action/
    ├── tool.py             Tool preprocessor/postprocessor fields
    └── function_calling.py invocation order
```

`SessionObserver` is the canonical recording and reactive fan-out transport for session signals;
it is not the execution API for service or Tool pre-invocation hooks. `HookBus` remains the
canonical ordered Session hook dispatcher; being bound to an observer does not delegate its
handler chain to reactive subscribers.

**Exact semantics.**

- A standalone `Branch` has neither Session observer nor Session bus. `Branch.emit()` returns an
  empty list and `Branch.authorize()` allows.
- A standalone `iModel` still owns a fresh `HookRegistry` by default and may be given a configured
  registry. No Session is required.
- A Tool preprocessor is stored on the Tool object and runs inside `FunctionCalling`; it is not
  registered on `HookBus` or `SessionObserver`.
- The same callable can be adapted into more than one scope only when its signature and failure
  behavior are explicitly adapted. Registering the same function object does not unify lifetimes.

**Why this way.** The narrowest shared abstraction is observation, not control. All three
mechanisms can report that something occurred, but only the mechanism surrounding the operation
has the data and timing needed to change or stop it. Keeping canonical ownership per scope avoids
claiming that a post-facto signal can enforce a precondition.

### D2 — `HookBus` owns ordered Session hook dispatch

**The contract** (`lionagi/hooks/bus.py`):

```python
class HookPoint(str, Enum):
    SESSION_START = "session.start"
    SESSION_END = "session.end"
    BRANCH_CREATE = "branch.create"
    API_PRE_CALL = "api.pre_call"
    API_POST_CALL = "api.post_call"
    API_STREAM_CHUNK = "api.stream_chunk"
    TOOL_PRE = "tool.pre"
    TOOL_POST = "tool.post"
    TOOL_ERROR = "tool.error"
    MESSAGE_ADD = "message.add"
    ARTIFACT_CREATED = "artifact.created"

HookHandler = Callable[..., Awaitable[Any] | Any]

class StopHook(Exception): ...

class HookSignal(Signal):
    point: HookPoint | None = None
    kwargs: dict[str, Any] = Field(default_factory=dict)

class HookBus:
    def __init__(self, observer: SessionObserver | None = None) -> None: ...
    def bind(self, observer: SessionObserver | None) -> HookBus: ...
    def on(self, point: HookPoint | str, handler: HookHandler) -> None: ...
    def off(self, point: HookPoint | str, handler: HookHandler) -> None: ...
    def handlers_for(self, point: HookPoint | str) -> list[HookHandler]: ...
    async def blocking_emit(
        self, point: HookPoint | str, /, **kwargs: Any
    ) -> None: ...
    async def emit(
        self, point: HookPoint | str, /, **kwargs: Any
    ) -> None: ...

def hook(point: HookPoint | str) -> Callable[[HookHandler], HookHandler]: ...
```

**Registration and miss semantics.**

- Enum members and exact value strings are accepted. An unknown string raises `ValueError` before
  registration, removal, lookup, or emission.
- `on()` appends. Duplicate registrations are allowed and run more than once.
- `off()` removes the first equal registered handler. A missing handler or point is a no-op.
- `handlers_for()` returns a copy; mutating it does not mutate the bus.
- Emission snapshots the current handler list. Registration changes made by a running handler
  affect later emissions, not the current chain.
- No handlers is a valid emission. A bound bus can still record the point.
- `@hook(point)` sets `__lionagi_hook_point__` on the callable. It does not register or discover the
  function by itself.

**Dispatch and error semantics.**

- Non-`TOOL_PRE` `emit()` awaits handlers sequentially in registration order. Sync return values
  are accepted through `maybe_await`.
- `StopHook` stops the remaining handlers for that point and is swallowed. It does not stop the
  observed operation.
- Any other handler exception at a non-blocking point is logged, swallowed, and the next handler
  runs.
- `emit(TOOL_PRE, ...)` delegates to `blocking_emit()`. A non-`StopHook` exception propagates,
  later handlers do not run, and the Tool invocation is prevented by the caller in
  `operations/act/act.py`.
- A `StopHook` at `TOOL_PRE` only stops sibling handlers; the Tool invocation continues.
- After a successful or `StopHook`-short-circuited chain, the bus records a `HookSignal` through its
  bound observer. A raised blocking exception exits before recording, so denied `TOOL_PRE`
  attempts currently have no `HookSignal` audit record.
- Observer recording is best effort. Observer exceptions are logged and swallowed after the
  ordered handler chain has completed.
- `MESSAGE_ADD` deliberately skips `HookSignal` recording because the Branch separately emits a
  typed `MessageAdded` signal. Its `HookBus` handlers still run.

The eleven-point vocabulary is closed by the enum and pinned by tests. It is not a promise that all
points emit. The shipped production matrix is:

| Point | Production source | Payload supplied at that source | State |
|---|---|---|---|
| `SESSION_START` | CLI persistence setup | `session_id`, `model`, `provider`, `effort`, `agent_name`, `agent_hash`, `invocation_id` | wired |
| `SESSION_END` | CLI persistence teardown | `session_id`, `status`, `error`, plus available usage fields | wired |
| `BRANCH_CREATE` | CLI persistence setup | `branch_id`, `model`, `provider`, `agent_name` | wired |
| `TOOL_PRE` | `_act()` before `ActionManager.invoke()` | `tool_name`, `call_id`, `args_summary` | wired, blocking |
| `TOOL_POST` | `_act()` after successful invoke | `call_id`, `tool_name`, `result_summary`, `duration` | wired |
| `TOOL_ERROR` | `_act()` when invoke raises | `call_id`, `tool_name`, `error`, `duration=None` | wired |
| `MESSAGE_ADD` | routed Branch message callback | `branch_id`, `message` | conditionally wired by persistence routing |
| `API_PRE_CALL` | none | none | dormant |
| `API_POST_CALL` | none | none | dormant |
| `API_STREAM_CHUNK` | none | none | dormant |
| `ARTIFACT_CREATED` | none | none | dormant |

`call_id` is a fresh UUID4 string shared by a Tool's pre and post/error emissions. Argument and
result summaries are truncated to 200 characters. The truncation bounds incidental telemetry size;
no recorded rationale explains why exactly 200 was selected.

**Why this way.** Sequential execution makes persistence and guard ordering inspectable. A special
blocking path is necessary because ordinary hooks are intentionally failure-isolated. Recording
after dispatch preserves the handler discipline while exposing successful hook activity to the
typed Session transport.

### D3 — Session owns bus attachment; `SessionObserver` owns recording and fan-out

**The Session and observer contract** (`lionagi/session/session.py`,
`lionagi/session/observer.py`, and `lionagi/session/signal.py`):

```python
class Signal(Element):
    data: Any = None
    emitter_role: str | None = None
    schema_version: int = 1

class Session:
    @property
    def hooks(self) -> HookBus: ...

    @property
    def observer(self) -> SessionObserver: ...

class SessionObserver:
    def observe(
        self,
        *keys: type | Filter | Predicate | Any,
        handler: Handler | None = None,
        role: str | None = None,
    ) -> Any: ...

    def unobserve(self, handler: Handler) -> int: ...
    def route(self, condition: Predicate, *, into: str) -> SessionObserver: ...
    def gate(self, check: Gate) -> SessionObserver: ...
    async def authorize(self, action: Any) -> bool: ...
    async def emit(self, event: Any) -> list[Any]: ...
    def stream(self, name: str) -> list[Any]: ...
    def by_type(self, event_type: type) -> list[Any]: ...
    def bind_db_persistence(self, session_id: str, db: Any = None) -> None: ...
    def unbind_db_persistence(self) -> None: ...
```

`Session.observer` is lazy and stores one `SessionObserver(session=self)`. `Session.hooks` is also
lazy and stores one `build_session_bus(observer=self.observer)`. Repeated property access returns
the same objects for that Session.

`SessionObserver.emit()` follows this as-built order:

```text
coerce a non-Observable value to Signal(data=value)
→ evaluate the observer gate against the payload
→ store the event in the Session Flow even when denied
→ if allowed, append matching named routes
→ invoke matching synchronous subscribers
→ gather matching async subscribers
→ return synchronous results followed by async results
```

This reactive dispatch is not registration-ordered for async completion and has no `StopHook`
contract. A `HookBus` bound to it still runs its own chain first. If the observer gate denies a
`HookSignal`, the signal remains stored but reactive subscribers and routes do not receive it.

Observer database persistence serializes Signal fields into a JSON-safe payload. Payloads over
16,384 bytes are replaced by a bounded object containing `truncated`, `original_bytes`, and a
clipped `data` string. The 16 KiB value bounds the database payload column; it is not a hard cap on
the whole stream frame because row metadata adds overhead.

**The loader contract** (`lionagi/hooks/loader.py`):

```python
DEFAULT_HOOKS: dict[HookPoint, list[HookHandler]] = {
    HookPoint.SESSION_START: [persist_session_start],
    HookPoint.SESSION_END: [persist_session_end],
    HookPoint.MESSAGE_ADD: [persist_message],
    HookPoint.BRANCH_CREATE: [persist_branch_provenance],
}

def register_handler(
    name: str,
    handler: Callable[..., Awaitable[Any]],
) -> None: ...

def resolve_handler(name: str) -> HookHandler: ...

def load_hooks_for_agent(
    agent_hooks: dict[str, list[str]] | None,
) -> dict[HookPoint, list[HookHandler]]: ...

def build_session_bus(
    agent_hooks: dict[str, list[str]] | None = None,
    *,
    observer: Any = None,
) -> HookBus: ...
```

**Loader and attachment semantics.**

- `register_handler()` is a process-global name registry and last writer wins.
- `resolve_handler()` raises `KeyError` for a miss and includes the registered names.
- `load_hooks_for_agent(None)` and an empty mapping return no overrides. Unknown points raise
  `ValueError`; a per-point value that is not a list raises `ValueError`; unknown handler names
  raise `KeyError`.
- An override for a default point replaces the whole default list. An explicit empty list disables
  that default. Overrides for non-default points are appended to the fresh bus.
- Each `build_session_bus()` call returns a new bus. The global handler-name registry is shared;
  registered handler chains are not.
- The shipped `Session.hooks` property does not pass `agent_hooks`. Declarative overrides work when
  a caller invokes `build_session_bus()` explicitly, but no production AgentSpec/profile path feeds
  them into Session construction.
- `include_branches()` assigns `branch._observer` immediately. It assigns `branch._hooks` only if
  the bus already exists. Creating the bus later does not backfill branches already included.
- Branches included after bus creation receive the existing bus. Removing a Branch clears its
  Session observer and bus references.

Message persistence has an explicit routing adapter (`lionagi/hooks/persist.py`):

```python
def route_message_persistence(
    session: Any,
    branch: Any,
    on_message: Callable[[Any], Awaitable[None]],
) -> HookHandler: ...

def unroute_message_persistence(
    holder: Any,
    handler: HookHandler,
) -> None: ...
```

Routing creates/accesses the Session bus, removes the default `persist_message` handler from the
shared bus, assigns the bus to that Branch, registers `branch._persist_via_bus` once on the Branch's
message callbacks, and adds a Branch-id-filtered async handler. Teardown removes both registrations.
The adapter is why the CLI persistence path supplies its own callback rather than using two
competing persistence writes.

The default `persist_message` signature requires `session_id` and accepts Branch/session
progression ids, but the Branch's routed `MESSAGE_ADD` emission supplies only `branch_id` and
`message`. Standard CLI routing removes that default before live messages flow. Directly attaching
the default bus without the routing adapter does not create the missing persistence identifiers;
its handler error is isolated by non-blocking `HookBus.emit()`.

**Why this way.** Session signals need one queryable Flow, while persistence and guards need a
different dispatch discipline over that transport. Lazy objects avoid Session hook allocation when
unused. The current access-order and loader gaps are organic limitations, not intended guarantees.

### D4 — service hooks remain a per-`iModel` event lifecycle

The service mechanism owns one handler per lifecycle phase and one handler per streaming chunk key.

**The type and registry contract** (`lionagi/service/hooks/_types.py` and
`lionagi/service/hooks/hook_registry.py`):

```python
class HookEventTypes(str, Enum):
    PreEventCreate = "pre_event_create"
    PreInvocation = "pre_invocation"
    PostInvocation = "post_invocation"

class HookDict(TypedDict):
    pre_event_create: Callable | None
    pre_invocation: Callable | None
    post_invocation: Callable | None

StreamHandlers = dict[str, Callable[[SC], Awaitable[None]]]

class AssociatedEventInfo(TypedDict, total=False):
    lion_class: str
    event_id: str
    event_created_at: float

class HookRegistry:
    def __init__(
        self,
        hooks: HookDict = None,
        stream_handlers: StreamHandlers = None,
    ): ...

    def pre_event_create_hook(self, fn: F) -> F: ...
    def pre_invoke(self, fn: F) -> F: ...
    def post_invoke(self, fn: F) -> F: ...

    async def call(
        self,
        event_like: Event | type[Event],
        /,
        *,
        hook_type: HookEventTypes = None,
        chunk_type=None,
        chunk=None,
        should_exit: bool = False,
        **kw,
    ): ...
```

String hook keys `pre_event_create`, `pre_event_create_hook`, `pre_invoke`, `pre_invocation`,
`post_invoke`, and `post_invocation` normalize to enum keys. Other keys fail validation. Sync
handlers are wrapped for asynchronous execution. Decorators overwrite an existing phase handler
and emit a warning; the registry is not a multi-handler chain.

At runtime, a stream handler is called as:

```python
await handler(None, chunk_type, chunk, **hook_params)
```

The shipped `StreamHandlers` alias documents a one-chunk callable even though dispatch passes three
positional arguments plus keywords. Tests and runtime use the three-argument form; the annotation is
not a faithful callable contract today.

**The event wrapper contract** (`lionagi/service/hooks/hook_event.py`):

```python
class HookEvent(Event):
    registry: HookRegistry = Field(..., exclude=True)
    hook_type: HookEventTypes
    exit: bool = Field(False, exclude=True)
    timeout: int | float = Field(30, exclude=True)
    params: dict[str, Any] = Field(default_factory=dict, exclude=True)
    event_like: Event | type[Event] = Field(..., exclude=True)
    associated_event_info: AssociatedEventInfo | None = None

    async def invoke(self): ...
```

`exit=None` normalizes to `False`. Each service phase injects the resolved `exit` value into the
handler's keyword arguments. The registry returns `(result, should_exit, EventStatus)` plus
associated-event metadata.

| Path | Result | `should_exit` | Status |
|---|---|---|---|
| any phase succeeds | handler return | `False` | `COMPLETED` |
| pre-create or pre-invoke raises cancellation | `(Undefined, error)` | `True` | `CANCELLED` |
| pre-create or pre-invoke raises another exception | exception object | configured `exit` | `CANCELLED` |
| post-invoke raises cancellation | `(Undefined, error)` | `True` | `CANCELLED` |
| post-invoke raises another exception | exception object | configured `exit` | `ABORTED` |
| stream handler succeeds | handler return | `False` | `None` |
| stream handler raises cancellation | `(Undefined, error)` | `True` | `CANCELLED` |
| stream handler raises another exception | exception object | configured `exit` | `ABORTED` |

`HookEvent.invoke()` starts at `PROCESSING`, applies an AnyIO timeout, copies metadata, and records
duration. Cancellation propagates. Other registry/wiring failures become `FAILED`, clear the
response, record the error, and set `_should_exit` from the configured `exit` policy. A handler must
raise to signal phase failure; merely returning an exception object records it as the response-side
error while retaining the registry's success status.

**The `iModel` and template-method contract** (`lionagi/service/imodel.py` and
`lionagi/service/hooks/hooked_event.py`):

```python
class iModel:
    def __init__(
        self,
        ...,
        streaming_process_func: Callable = None,
        provider_metadata: dict | None = None,
        hook_registry: HookRegistry | dict | None = None,
        exit_hook: bool = False,
        ...,
    ) -> None: ...

    async def create_event(
        self,
        create_event_type: type[Event] = APICalling,
        create_event_exit_hook: bool = None,
        create_event_hook_timeout: float = 10.0,
        create_event_hook_params: dict = None,
        pre_invoke_event_exit_hook: bool = None,
        pre_invoke_event_hook_timeout: float = 30.0,
        pre_invoke_event_hook_params: dict = None,
        post_invoke_event_exit_hook: bool = None,
        post_invoke_event_hook_timeout: float = 30.0,
        post_invoke_event_hook_params: dict = None,
        **kwargs,
    ) -> tuple[HookEvent | None, APICalling]: ...

class HookedEvent(Event):
    async def _core_invoke(self): ...
    async def _core_stream(self): ...
    async def _invoke(self): ...
    async def _stream(self): ...
    def create_pre_invoke_hook(
        self,
        hook_registry,
        exit_hook: bool = None,
        hook_timeout: float = 30.0,
        hook_params: dict = None,
    ): ...
    def create_post_invoke_hook(
        self,
        hook_registry,
        exit_hook: bool = None,
        hook_timeout: float = 30.0,
        hook_params: dict = None,
    ): ...
```

Despite its annotation, `iModel.create_event()` returns the created `APICalling` object, not a
`(HookEvent, APICalling)` tuple. The pre-create HookEvent remains available only as internal local
state and through logging.

**Creation and invocation semantics.**

- With no registered handler, no HookEvent is attached and the core path is unchanged.
- A pre-create hook receives the event class. Its response value is currently unused beyond the
  exit/cancellation decision: construction of the event runs unconditionally after the hook, and
  no code path substitutes the hook's return value for the constructed event
  (`lionagi/service/imodel.py`, `create_event()`).
- A pre-create failure with `exit=False` does not prevent default construction. A cancellation or
  failure with `_should_exit=True` raises before construction.
- Pre-invocation and post-invocation HookEvents attach only when their phase is present in the
  registry.
- A failed or cancelled pre-invocation HookEvent prevents `_core_invoke()` and `_core_stream()`.
  This status check blocks even when the non-cancellation `exit` setting was false.
- Non-stream `_core_invoke()` errors are held until the post hook runs. If both core and post fail,
  the original core error wins. A regular post-handler exception becomes `ABORTED`: with
  `exit=False` it is logged and the successful core result survives; with `exit=True` its cause
  propagates when the core succeeded. Cancellation and wrapper-level `FAILED`/`CANCELLED` states
  propagate only when there is no earlier core error.
- Streaming runs its pre hook, yields core chunks, and runs its post hook only after normal stream
  completion. A core-stream error bypasses the post section. A non-cancellation `Exception` raised
  by the post-stream wrapper is logged because chunks have already been delivered; cancellation
  keeps the runtime's cancellation behavior. HookEvent status is not used to retract data.
- Per-chunk registry handlers take precedence over `streaming_process_func`. A non-exception return
  replaces the processed chunk; `None` causes the caller to yield the original chunk. A handler
  error with `exit=False` is ignored for delivery and the original chunk continues; with
  `should_exit=True` the cause is raised.

Pre-create hooks default to 10 seconds; pre/post HookEvents default to 30 seconds. `HookEvent`
itself also defaults to 30 seconds. These are inherited implementation values; no recorded
rationale explains the exact thresholds. The global hook logger buffers 100 entries before its
logger-specific persistence behavior; 100 is likewise inherited without a recorded sizing study.

**Why this way.** The template method surrounds the actual provider event and retains standalone
`iModel` behavior. Phase-specific status lets callers distinguish cancellation from aborted
postprocessing. Streaming necessarily gives post-delivery failures weaker control because already
yielded chunks cannot be rolled back.

### D5 — Tool hooks are per-Tool mutable interceptors

The shipped agent declaration surface is (`lionagi/agent/spec.py`):

```python
class HooksMixin:
    hook_handlers: dict[str, list[Callable]]

    def pre(self, tool_name: str, handler: Callable) -> HooksMixin: ...
    def post(self, tool_name: str, handler: Callable) -> HooksMixin: ...
    def on_error(self, tool_name: str, handler: Callable) -> HooksMixin: ...
```

Those methods append handlers under `pre:<name>`, `post:<name>`, and `error:<name>` and return the
same mutable spec. `CodingToolkit` has parallel `security_pre()`, `pre()`, `post()`, and
`on_error()` builders.

The execution carrier is (`lionagi/protocols/action/tool.py` and
`lionagi/protocols/action/function_calling.py`):

```python
class Tool(Element):
    func_callable: Callable[..., Any]
    request_options: type | None = None
    preprocessor: Callable[[Any], Any] | None = None
    preprocessor_kwargs: dict[str, Any] = Field(default_factory=dict)
    postprocessor: Callable[[Any], Any] | None = None
    postprocessor_kwargs: dict[str, Any] = Field(default_factory=dict)
    strict_func_call: bool = False

class FunctionCalling(Event):
    func_tool: Tool
    arguments: dict[str, Any] | BaseModel

    async def _invoke(self) -> Any: ...
```

FunctionCalling validates request-model and required-field shape during model construction, then
invokes in this order:

```text
Tool.preprocessor(arguments)
→ Tool.func_callable(**arguments)
→ Tool.postprocessor(response)
```

A preprocessor return becomes the new argument mapping. A postprocessor return becomes the response.
The shipped factory builds a narrower convention over those fields:

```python
async def pre_hook(
    tool_name: str,
    action: str,
    args: dict,
) -> dict | None: ...

async def post_hook(
    tool_name: str,
    action: str,
    args: dict,
    result: Any,
) -> Any | None: ...
```

**Exact factory semantics.**

- Handler lookup order on the standalone reader/editor/bash/search path is wildcard, canonical
  tool name, then `<canonical_name>_tool` compatibility alias. `CodingToolkit`'s own hook lookup
  (`lionagi/tools/coding.py`) — the path exercised by the default `AgentSpec.coding()`
  construction — consults only `"*"` and the exact tool name, with no alias fallback.
- Security pre-hooks run first. When at least one user pre-hook exists, the complete security list
  runs again after all user pre-hooks.
- Each factory pre-hook receives the canonical tool name, `args.get("action", "")`, and the
  current argument mapping. A dictionary return replaces that mapping; other returns are ignored.
  A raised exception prevents the callable.
- The current `FunctionCalling` path does not re-run request-model validation after a preprocessor
  replaces arguments.
- The factory post chain runs only when the Tool result is a dictionary. It calls each handler with
  the canonical tool name, empty action, empty argument mapping, and current result. Each dictionary
  return replaces the result; non-dictionary returns are ignored. A non-dictionary Tool result
  bypasses all declared post handlers.
- `error:<name>` handlers are copied into CodingToolkit maps, but `Tool` has no error-processor
  field and `FunctionCalling` never invokes them. Registration is not execution.
- Ordinary built-ins receive their chained preprocessors/postprocessors before registration.
  MCP-discovered Tools are registered later and currently receive none of these agent hooks.
- Tool-level preprocessing is inside `ActionManager.invoke()`. Session `TOOL_PRE` fires earlier with
  a summary payload; Session `TOOL_POST` or `TOOL_ERROR` fires after the manager returns or raises.
- The built-in `auto_format_python` post-hook runs `ruff format` as an argv subprocess with shell
  disabled and a 10-second timeout. Ten seconds is inherited; no recorded rationale establishes the
  exact formatter deadline.

The effective shipped action order is:

```text
validate action-request envelope
→ SessionObserver.authorize(ToolInvocation)
   └── deny: record GateDenied and return a Tool-shaped denial; no HookBus point fires
→ HookBus TOOL_PRE(summary)
   └── raised guard: propagate; no TOOL_ERROR and no HookSignal record
→ ActionManager.invoke()
   └── FunctionCalling Tool.preprocessor(arguments)
       └── callable
       └── Tool.postprocessor(result)
→ HookBus TOOL_POST(summary) on success
  or HookBus TOOL_ERROR(exception) on manager/invocation error
```

**Why this way.** Tool preprocessors sit at the only layer that owns the callable's argument mapping
and result. The Session bus provides lifecycle visibility and an earlier coarse guard, but its
summary payload is intentionally unsuitable for silent mutation. ADR-0044 specifies the target
interceptor repair; this retrospective ADR does not pretend that target is already shipped.

### D6 — cross-scope adapters are observational and semantics-preserving

The shipped adapter is `HookBus` to `SessionObserver`: after the ordered chain succeeds, it emits a
typed `HookSignal`. This establishes the rule for future bridges.

An adapter is valid only when it preserves all of these properties:

1. **Scope.** A per-`iModel` hook continues to function without a Session. Session binding adds
   telemetry; it does not become the sole execution path.
2. **Timing.** A pre-invocation guard still runs before the core operation. Emitting an observation
   after completion cannot satisfy that contract.
3. **Ordering.** An ordered chain is not translated into unordered reactive subscribers when
   handler order or `StopHook` matters.
4. **Payload.** Summary telemetry is not promoted into mutable invocation arguments. An adapter may
   redact or summarize a stronger payload, but may not claim the reverse transformation.
5. **Failure behavior.** Best-effort observation failure does not change the source operation.
   Blocking source failure remains blocking and must be audited separately if an observation is
   still required.
6. **No duplicate canonical event.** `MESSAGE_ADD` uses `MessageAdded` on the observer and suppresses
   a redundant `HookSignal`.

The three dormant API HookPoints may acquire meaning only through a typed optional
service-to-session adapter that states when it emits, what it redacts, and whether it observes a
stream chunk before or after the service handler. `ARTIFACT_CREATED` remains dormant until the
artifact owner supplies a payload and emit site. Merely calling `bus.emit()` in tests is not a
production integration.

**Why this way.** Adapters can unify telemetry without erasing control boundaries. That is the
maximum safe consolidation supported by the shipped code. A stronger universal hook API would
need a new operation wrapper, payload algebra, and compatibility plan rather than aliases between
unlike callables.

## Consequences

- Session hooks remain ordered and observable, service hooks retain standalone and streaming
  behavior, and Tool guards retain argument transformation.
- Maintainers must identify the operation and owner before registering a “hook.” Import path alone
  is not cosmetic: it determines lifetime, handler signature, and failure semantics.
- `HookPoint` catalog consumers must distinguish enum availability from production wiring. Four
  values currently describe reserved vocabulary only.
- A `TOOL_PRE` denial is effective but not recorded as a HookSignal because recording follows the
  successful chain. Audit consumers cannot infer denied attempts from HookSignal history.
- Session bus attachment currently depends on access order. A Branch can have an observer and no
  HookBus even while its Session later has a bus.
- Declarative Session hook override utilities exist, but the default Session construction path does
  not consume agent/profile declarations.
- Service hook authors must know the actual three-positional-argument stream call and the actual
  `APICalling` return from `create_event()` until the annotations are repaired.
- Service post-invocation failure can replace a successful non-stream result only when exit or
  cancellation semantics require propagation; an `ABORTED` post hook with `exit=False` leaves the
  core result intact. No post hook can retract delivered stream chunks, and a core error remains
  primary when both core and post fail.
- Tool hook authors must know that error handlers are stored but unwired, post handlers are
  dictionary-only, and MCP tools are outside current factory coverage.
- Reversing D1 would be high cost: it requires changing standalone `iModel`, Session dispatch,
  FunctionCalling mutation, settings loaders, tests, and all handler signatures together.
- Reversing D2 or D3 independently would either lose ordered guards/persistence or duplicate the
  Session event record.
- Reversing D4 requires a compatibility wrapper around every provider event and stream path.
- Reversing D5 requires the universal interceptor design in ADR-0044, including revalidation and
  complete Tool-registration coverage.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|---|---|---|
| 1 | Make Session hook attachment independent of lazy access order; accept when branches included before or after `Session.hooks` creation receive the same bus and emit the same tool signals. | S | #1964 |
| 2 | Give the three dormant API `HookPoint` values production semantics through a typed, optional service-to-session observation adapter; accept when a session-bound iModel records API observations without changing service pre-invocation control or standalone iModel behavior. | M | (filled at issue-open time) |
| 3 | Deprecate the unwired `ARTIFACT_CREATED` point until the artifact owner supplies a typed production emit site; accept when no public HookPoint is advertised without a payload contract and an integration test. | S | (filled at issue-open time) |
| 4 | Record blocked `TOOL_PRE` attempts without swallowing the blocking exception; accept when denied calls produce an audit signal and the underlying tool is never invoked. | S | #1967 |
| 5 | Align service hook annotations with runtime behavior; accept when `StreamHandlers` describes the actual stream callback arguments and `iModel.create_event()` has one truthful return type covered by static and runtime tests. | S | (filled at issue-open time) |
| 6 | Either wire declarative Session hook overrides into a production construction boundary or document `build_session_bus(agent_hooks=...)` as an explicit low-level utility; accept when one public construction path and its tests demonstrate the chosen ownership. | M | (filled at issue-open time) |
| 7 | Make the default `MESSAGE_ADD` handler compatible with the Branch emission payload or remove it from the default bus; accept when direct Session/Branch use cannot invoke `persist_message` without its required session and progression context. | S | (filled at issue-open time) |

## Alternatives considered

### Replace all three mechanisms with `HookBus`

One named-point API would reduce vocabulary and allow one declarative loader. It lost because a
Session bus does not exist for standalone `iModel` use, its handler payloads do not carry mutable
Tool arguments, and its flat sequential chain does not model pre-create replacement or streamed
post-delivery behavior. Keeping adapters at the observation boundary buys shared telemetry without
weakening control.

### Use only `SessionObserver` for execution and observation

One typed Flow would give uniform filters, routing, persistence, and replay. It lost as an execution
API because async subscriber completion is not ordered, there is no `StopHook` chain, and the
observer's ordinary `emit()` occurs when an event is reported rather than necessarily before the
operation. It also has Session scope, so it cannot be the sole service hook owner.

### Translate every `HookBus.on()` registration directly to `SessionObserver.observe()`

This would retain named points while deleting the bus's handler dictionary. It lost because
reactive async subscribers are gathered rather than awaited as one ordered chain. Persistence
handlers could race, `StopHook` would have no defined sibling set, and `TOOL_PRE` could no longer
guarantee that every earlier guard completed before invocation.

### Complete the original universal migration with compatibility wrappers

Wrappers could preserve old imports while routing service and Tool handlers through one new core.
That would buy a gradual deprecation path. It lost as the current decision because no common core
contract exists: service handlers accept event objects and status/exit rules, Tool handlers replace
arguments/results, and Session handlers accept loose keyword payloads. Wrappers would hide rather
than resolve those semantic differences. A future universal operation-interceptor design must be
specified first.

### Replace Tool preprocessors with Session `TOOL_PRE`

Moving guards to one Session point would make them visible to Session configuration and HookSignal
telemetry. It lost because the current payload contains only a redacted string summary and cannot
replace arguments. Expanding it to raw arguments would also put sensitive invocation data in the
observer record by default and would still not cover standalone Branches without a Session bus.

### Replace service post hooks with observations of completed API events

Deriving post events from `EventStatus` would remove a bespoke phase name and works for pure
telemetry. It lost as a complete replacement because the service hook can currently fail an
otherwise successful non-stream invocation, runs without a Session, and has distinct stream
semantics. It remains a valid optional adapter for observation-only consumers.

### Use a universal around-middleware chain

An `async handler(context, next)` contract could express pre, post, mutation, and short-circuiting
for every operation. It would buy one powerful abstraction. It lost because Session lifecycle
signals are not all invocations with a meaningful `next`, streaming needs yield-aware middleware,
and converting the provider and Tool hot paths would be a high-blast-radius behavioral rewrite for
no current cross-scope handler requirement.

### Use one process-global bus

A singleton would let service events publish without Session plumbing and make handler registration
easy. It lost because handlers and observations would leak across concurrent Sessions and models,
teardown ownership would be ambiguous, and tests would inherit process-order state. The one
process-global structure that remains is only the loader's name-to-callable registry; actual buses
are per Session.

### Treat all eleven HookPoints as already supported

Keeping the broader catalog in documentation would reserve convenient names and avoid deprecation.
It lost because callers would register handlers that never run and mistake a test-only `emit()` for
production integration. Dormant points are recorded honestly and require a typed emit site before
they become operational claims.

## Notes

In pari materia resolves the word “canonical” against scope, timing, payload, and failure
requirements. It does not require one shared handler ABI. The observer is canonical for Session
recording; HookBus is canonical for ordered Session hook execution; HookRegistry/HookedEvent is
canonical for service event control; Tool preprocessors are canonical for mutable Tool invocation.

Source anchors: `lionagi/hooks/bus.py`, `lionagi/hooks/loader.py`,
`lionagi/hooks/builtins.py`, `lionagi/hooks/persist.py`,
`lionagi/session/signal.py`, `lionagi/session/observer.py`,
`lionagi/session/session.py`, `lionagi/session/branch.py`,
`lionagi/operations/_observe.py`, `lionagi/operations/act/act.py`,
`lionagi/cli/_runs.py`, `lionagi/service/hooks/_types.py`,
`lionagi/service/hooks/_utils.py`, `lionagi/service/hooks/hook_event.py`,
`lionagi/service/hooks/hook_registry.py`, `lionagi/service/hooks/hooked_event.py`,
`lionagi/service/imodel.py`, `lionagi/service/connections/api_calling.py`,
`lionagi/agent/spec.py`, `lionagi/agent/factory.py`,
`lionagi/tools/coding.py`, `lionagi/protocols/action/tool.py`, and
`lionagi/protocols/action/function_calling.py`.
