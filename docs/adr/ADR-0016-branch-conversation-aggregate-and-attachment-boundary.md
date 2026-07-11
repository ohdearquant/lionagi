# ADR-0016: Branch conversation aggregate and attachment boundary

- **Status**: Accepted
- **Kind**: Retrospective
- **Area**: session-branch
- **Date**: 2026-07-09
- **Relations**: none

## Context

`Branch` is the stateful aggregate for one conversation. The class is both the place where
conversation resources are assembled and the stable caller-facing facade for operations implemented
under `lionagi/operations/`. Five concrete problems shape that boundary.

**P1 — one conversation needs one authoritative state owner.** Message history and active
progression, registered tools, selected chat and parse models, logs, identity, metadata, memory, and
named operations otherwise become a bag of independently passed managers. That makes it possible for
an operation to receive a message collection from one conversation and a model or tool registry from
another. `Branch.__init__()` constructs the five managers that jointly define the conversation
(`lionagi/session/branch.py`).

**P2 — branch lifetime and serialization lifetime are not identical.** A tool may contain a live
callable, a memory store may be an external backend, and an observer or hook bus belongs to a running
session. Those objects can remain attached for the lifetime of a branch without being valid
conversation-file content. `Branch.to_dict()` therefore exports a deliberate subset rather than all
private state.

**P3 — a branch must work both alone and inside a session.** A standalone branch creates its own
operation registry and lazily creates private in-memory storage. Session inclusion replaces selected
coordination references and may supply a shared memory default. Removal must undo the session-owned
references without deleting the conversation.

**P4 — optional context and capability features must not become parallel conversation stores.**
Context providers inject ephemeral text into one provider request; they do not append that text to
message history. Capability grants affect prompt instructions and interpretation of assistant output;
they do not replace the action registry or session gate.

**P5 — the public operation surface must not duplicate execution algorithms.** `chat`, `parse`,
`operate`, `communicate`, `act`, `interpret`, `ReAct`, `ReActStream`, and `run` are methods on the
aggregate, but their algorithms live under `lionagi/operations/`. Recording, streaming, parsing,
action, and `Middle` semantics are owned by the operations area (see the operations ADR on the Branch
operation facade and turn adapters).

| Concern | Decision |
|---------|----------|
| Conversation composition | D1: `Branch` owns one authoritative set of managers and selected resources. |
| Persistence boundary | D2: serialization exports conversation data, not live private attachments. |
| Optional branch extensions | D3: memory, context providers, and capability grants are branch-scoped with distinct persistence semantics. |
| Session attachment | D4: observer, hooks, ownership, and the shared operation registry are detachable coordination references; adopted memory is retained. |
| Operation and clone behavior | D5: public verbs delegate to operation modules, and cloning creates a new conversation aggregate from an explicit subset. |

This ADR deliberately does **not** decide:

- provider request compilation; that belongs to the messages-context ADR on canonical turn-request
  compilation;
- provider selection, retry, timeout, parsing, action, streaming, or `Middle` algorithms; those
  belong to the operations and service-provider areas;
- Session membership, observer dispatch, hook execution, Exchange routing, or graph scheduling;
  ADR-0017 records the session side of those contracts;
- the target isolation mechanism for overlapping turns; ADR-0018 defines that aspirational change;
- durable encoding for callables, memory backends, providers, or capability models; no such portable
  encoding is shipped.

## Decision

### D1 — Branch is the authoritative conversation aggregate

`Branch` remains the owner of exactly one conversation's identity and manager set. The shipped model
and constructor are (`lionagi/session/branch.py`):

```python
class Branch(Element, Relational):
    user: SenderRecipient | None = None
    name: str | None = None

    _message_manager: MessageManager | None = PrivateAttr(None)
    _action_manager: ActionManager | None = PrivateAttr(None)
    _imodel_manager: iModelManager | None = PrivateAttr(None)
    _log_manager: DataLogger | None = PrivateAttr(None)
    _operation_manager: OperationManager | None = PrivateAttr(None)

    def __init__(
        self,
        *,
        user: SenderRecipient = None,
        name: str | None = None,
        messages: Pile[RoledMessage] = None,
        system: System | JsonValue = None,
        system_sender: SenderRecipient = None,
        chat_model: iModel | dict | str = None,
        parse_model: iModel | dict | str = None,
        tools: FuncTool | list[FuncTool] = None,
        log_config: DataLoggerConfig | dict = None,
        system_datetime: bool | str = None,
        system_template=None,
        system_template_context: dict = None,
        logs: Pile[Log] = None,
        use_lion_system_message: bool = False,
        memory: MemoryStore | None = None,
        **kwargs,
    ): ...
```

Construction creates this object graph:

```text
Branch
├── MessageManager(messages) ── messages + progression + system
├── ActionManager() ─────────── registered Tool objects
├── iModelManager(chat, parse) ─ selected model access points
├── DataLogger(...) ─────────── logs + logger configuration
└── OperationManager() ──────── named async-operation registry
```

The manager properties are the authoritative access paths:

| Property | Returned object | Exact current behavior |
|----------|-----------------|------------------------|
| `system` | `System \| None` | Delegates to `MessageManager.system`. |
| `msgs` | `MessageManager` | Returns the sole message manager. |
| `messages` | `Pile[RoledMessage]` | Returns `MessageManager.messages`; no copy is made. |
| `progression` | `Progression` | Uses `metadata["current_progression"]` when present, otherwise the manager progression. |
| `acts` / `tools` | `ActionManager` / `dict[str, Tool]` | `tools` exposes the action registry. |
| `mdls` | `iModelManager` | Returns the sole model manager. |
| `chat_model` / `parse_model` | `iModel` | Setters re-register the named model in the manager. |
| `logs` | `Pile[Log]` | Returns the data logger's pile. |

**Exact construction semantics:**

- If no chat model is supplied, an `iModel` is built from
  `settings.LIONAGI_CHAT_PROVIDER` and `settings.LIONAGI_CHAT_MODEL`.
- If no parse model is supplied, the parse model is the selected chat-model object. Dictionary
  inputs use `iModel.from_dict`; string inputs use `iModel(model=value)`.
- Tools are registered into the new `ActionManager`; duplicate and update behavior is delegated to
  that manager.
- A truthy dictionary log configuration is validated as `DataLoggerConfig`; otherwise, including
  for `None` or `{}`, the default logger uses `settings.LOG_CONFIG`. Supplied logs initialize that
  logger rather than a second pile.
- A system message is added only when at least one of `system`, `system_datetime`, or
  `use_lion_system_message` is truthy. An explicitly supplied empty string or `False` values alone
  do not create one. The Lion system message is prepended when requested.
- `system_template` and `system_template_context` are accepted only for compatibility. A non-`None`
  value emits `DeprecationWarning` and has no effect.
- The message manager's synchronous `on_message_added` callback list receives
  `Branch._schedule_emit`, so later message additions can be projected onto an attached observer.

These choices make the branch broad by design: the managers are cohesive around one conversation,
not independently replaceable public services.

### D2 — Serialization exports a selected conversation projection

The serialized contract is the output of `Branch.to_dict()` rather than the full in-memory object
graph (`lionagi/session/branch.py`):

```python
def to_dict(
    self,
    mode: Literal["python", "json", "db"] = "python",
    db_meta_key: str | None = None,
    include_request_options: bool = False,
    include_logs: bool = True,
    include_log_config: bool = False,
    include_processor_config: bool = False,
    **kw,
) -> dict: ...
```

After the inherited `Element` fields, the branch adds this projection:

```text
messages     always present; empty form is
             {"collections": [], "progression": {"order": []}}
logs         present only when include_logs=True and logs are non-empty
system       present when a system message exists
log_config   present only when include_log_config=True
chat_model   always present
parse_model  present only when it is not the same object as chat_model
```

Request-option and processor configuration inclusion is forwarded to each model's `to_dict()`.
Tools, the operation registry, memory, provider registry, provider report, capability grant,
pending control, pending signal tasks, observer, hooks, and owning-session reference are not added
to the projection. All are private attributes; they are not made persistent merely because some of
them have branch lifetime.

`from_dict()` consumes the serialized keys `messages`, `logs`, `chat_model`, `parse_model`,
`system`, and `log_config` and passes the remaining compatible values into the constructor. When
both serialized messages and a top-level system are present, it suppresses the top-level system so
the system message already in the pile is not re-added. The method uses `pop()` on the supplied
dictionary, so callers that need the input unchanged must pass a copy.

**Exact edge cases:**

- Empty messages still produce the explicit empty pile shape; absence is not used to mean empty.
- Empty logs are omitted even when `include_logs=True`.
- A distinct parse model is serialized; an aliased chat/parse model is represented once.
- Session coordination references never survive a round trip. A deserialized branch is standalone.
- A serialized branch does not reconstruct live tools, providers, capabilities, memory, or named
  operations because those contracts have no portable representation here.

### D3 — Optional memory, provider, and capability extensions remain branch-scoped

These extensions all belong to a branch, but their lifetimes and error policies differ.

#### Memory contract

```python
@property
def memory(self) -> MemoryStore:
    if self._memory is None:
        self._memory = InMemoryStore()
    return self._memory
```

- An explicit constructor store is retained by identity.
- First property access on a standalone branch creates one private `InMemoryStore`; later access
  returns the same instance.
- There is no public setter. Session adoption uses the private attachment protocol in D4.
- Memory is not included in `Branch.to_dict()` and is not copied by `Branch.clone()`.

#### Context-provider contract

`providers` lazily creates one `ContextProviderRegistry`. Pre-turn execution ordering,
budgeting, and attribution semantics for this registry are recorded in ADR-0008; this section
owns its lifetime and persistence semantics within the Branch aggregate. The registry and
report shapes are (`lionagi/protocols/context_providers.py`):

```python
class ContextProvider(Protocol):
    async def provide(self, branch: Branch, instruction: Instruction) -> str | None: ...

@dataclass(frozen=True)
class ProviderReport:
    blocks: list[str] = field(default_factory=list)
    fired: list[dict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

class ContextProviderRegistry:
    def __init__(self, budget: int = 2000): ...
    def register(
        self,
        provider: ContextProvider,
        *,
        priority: int = 0,
        max_tokens: int | None = None,
        name: str | None = None,
    ) -> None: ...
    async def gather(self, branch: Branch, instruction: Instruction) -> ProviderReport: ...
    async def gather_writeback(self, branch: Branch, action_responses: list) -> None: ...
```

The total default injection budget is **2,000 tokens**. The code records the purpose—bound the
ephemeral injection—but no historical rationale for the exact value. A truthy per-provider
`max_tokens` imposes a lower independent bound. The registry performs no range validation:
`max_tokens=0` disables that check because the implementation tests truthiness, while a negative
value skips every non-empty output. A non-positive total `budget` admits no positive-token output.
Callers are responsible for supplying sensible non-negative limits.

Provider semantics are exact:

- An untouched registry is falsy and the operation path returns without constructing an
  instruction or report.
- Providers run in registration order. A provider exception is logged, its name is appended to
  `failed`, and the turn continues.
- `None` or empty text produces no block and is not recorded as fired.
- Text over that provider's `max_tokens` is recorded as skipped.
- Successful outputs are ranked by descending priority for budget admission; equal priorities keep
  registration order. Retained blocks are rendered back in original registration order.
- A branch without a system message does not invoke providers because there is no injection target;
  the current operation path creates a report with every registered name under `skipped`.
- Rendered blocks are folded into the first system-guidance request and cleared from the branch slot
  immediately after request preparation. They are never appended as durable messages.
- `last_context_report` returns the last report reference held by the branch. In current code the
  zero-provider path does not clear an older report; ADR-0018 defines the target turn-scoped repair.
- After `operate()` has actually executed actions and retained at least one non-`None` action
  response, it calls `gather_writeback()` on the registry
  (`lionagi/operations/operate/operate.py`). Providers are visited sequentially in registration
  order. A provider with no `writeback` attribute, or one set to `None`, is skipped. A present hook
  must be async; a non-callable or synchronous hook fails at `await`, is logged and contained like
  any other provider exception, and later providers still run. No writeback occurs when action
  invocation is disabled, yields no responses, or every response is `None`.
- Writeback is an optional provider side effect, not part of `ProviderReport`, Branch serialization,
  or the pre-turn token budget. The registry supplies the hook; each provider decides whether and
  where to persist.

#### Capability contract

The grant and rejection payloads are (`lionagi/session/capabilities.py`):

```python
class CapabilityViolation(BaseModel):
    offending: list[str]
    allowed: list[str]
    block: dict | None = None

class EmissionRejected(BaseModel):
    branch_name: str = ""
    error: str
    block: dict | None = None

CAP_BEGIN = "<!-- lionagi:capabilities -->"
CAP_END = "<!-- /lionagi:capabilities -->"
```

- `grant_capabilities(operable, prompt=True)` stores the runtime grant. With prompt enabled, it
  removes one balanced, previously marked block and appends a freshly rendered schema block to the
  current system prompt. Re-granting therefore replaces rather than stacks a well-formed block.
- Unbalanced markers are left untouched instead of risking prompt corruption.
- `prompt=False` installs only the runtime grant. `revoke_capabilities()` clears the grant and
  removes a balanced prompt block.
- Every observed assistant message first emits `MessageAdded`. Fenced JSON extraction then ignores
  blocks with no granted key, emits `CapabilityViolation` when a block mixes granted and ungranted
  keys, validates wholly granted blocks against a generated Pydantic model, and emits
  `EmissionRejected` on validation failure (`lionagi/operations/_observe.py`).
- Valid bundles are emitted concurrently as `StructuredOutput`; violations and rejections are
  emitted as ordinary `Signal` payloads. These are observation semantics, not action execution or
  authorization.

### D4 — Session references attach and detach without taking conversation ownership

The branch-side coordination fields are:

```python
_observer: Any = PrivateAttr(None)
_hooks: Any = PrivateAttr(None)
_owning_session_id: Any = PrivateAttr(None)
_operation_manager: OperationManager | None = PrivateAttr(None)
_memory: MemoryStore | None = PrivateAttr(None)
```

ADR-0017 owns the membership algorithm. From the branch boundary, the contract is:

| Resource | Standalone | On session inclusion | On removal |
|----------|------------|----------------------|------------|
| ownership | `None` | session id | reset to `None` |
| observer | `None`; `emit()` returns `[]`; `authorize()` returns `True` | session observer | reset to `None` |
| hooks | `None` | session bus only if that bus is already initialized | reset to `None` |
| operation registry | fresh private manager | session's manager by identity | new empty private manager |
| memory | explicit, adopted, lazy private, or `None` before access | session store only when `_memory is None` | retained unchanged |

Consequently, reading `branch.memory` before inclusion counts as adopting a store: later inclusion
does not replace it. A previously session-adopted store also stays with the branch after removal and
reparenting. This first-claim behavior prevents silent replacement of a backend that may already
contain conversation data.

Attachments are excluded from serialization and cloning. Removing a branch does not delete its
messages, progression, selected models, tools, logs, metadata, provider registry, capabilities, or
adopted memory. It does remove access to session-registered operations because the shared registry
is replaced.

### D5 — Public verbs delegate; registry lookup and cloning have explicit precedence

The public Branch methods are adapters. Representative shipped forms are:

```python
async def chat(...) -> tuple[Instruction, AssistantResponse]:
    from lionagi.operations.chat.chat import ChatParam, chat
    return await chat(self, ...)

async def operate(...) -> list | BaseModel | None | dict | str:
    from lionagi.operations.operate.operate import operate, prepare_operate_kw
    return await self._observed_run(operate(self, **prepare_operate_kw(self, ...)))

async def run(...) -> AsyncGenerator[RoledMessage, None]:
    from lionagi.operations.run.run import run
    async for msg in run(self, instruction, RunParam(...)):
        yield msg
```

Branch owns parameter adaptation, resource selection, and API continuity. The imported operation
modules own execution. This separation is why operation algorithms are not copied into this ADR.

Named operation lookup is attribute-first (`lionagi/session/branch.py`,
`lionagi/operations/manager.py`):

```python
def get_operation(self, operation: str) -> Callable | None:
    if hasattr(self, operation):
        return getattr(self, operation)
    return self._operation_manager.registry.get(operation)

class OperationManager(Manager):
    registry: dict[str, Callable]

    def register(self, operation: str, func: Callable, update: bool = False): ...
```

A registered name cannot override any existing Branch attribute. Duplicate registry names fail
unless `update=True`, and non-async functions are rejected. The registry is described as
experimental in its module and is not serialized.

`clone()` creates a distinct Branch rather than copying the whole object:

- the system and every message are cloned; cloned message sender/recipient values are rewritten for
  the new branch;
- registered tools are passed into the new action manager;
- CLI models are copied where needed, while non-CLI model objects are reused; a distinct parse model
  remains distinct;
- the clone receives `metadata={"clone_from": source}` and the source `user`;
- logs, log configuration, memory, providers, provider report, capabilities, pending runtime state,
  named operations, observer, hooks, and session ownership are not copied.

The `clone_from` serializer reduces the source reference to its id, user, creation time, and message
progression. Clone registration into a session is a Session or flow-executor responsibility.

Branch also fixes several numeric facade defaults even though the operation modules own their
algorithms:

| Facade parameter | Default | Current rationale record |
|------------------|---------|--------------------------|
| `parse(max_retries=...)` | `3` | Inherited parse retry default; no Branch-level rationale recorded. |
| `parse(similarity_threshold=...)` | `0.85` | Inherited fuzzy-match threshold; no calibration evidence recorded here. |
| `communicate(num_parse_retries=...)` | `3` | Inherited structured-parse retry default; no Branch-level rationale recorded. |
| `ReAct(max_extensions=...)` / `ReActStream(max_extensions=...)` | `3` | Inherited reasoning-loop extension cap; no Branch-level rationale recorded. |

The adapters forward these values. This ADR records them so callers can distinguish a stable facade
default from an operation implementation detail; changing their policy still belongs to the owning
operation ADR.

`Branch.connect()` remains a facade-adjacent compatibility method. Its queue capacity default is
**100** and capacity refresh default is **60 seconds**; those values are forwarded to `iModel` and
have no recorded Branch-level rationale. Whether the method remains supported is an explicit delta
below rather than a new architectural commitment.

## Consequences

- An operation receives one coherent conversation object instead of separately coordinated
  managers. The cost is a broad aggregate with many imports and private lifetime classes.
- A branch can run standalone, join a session, leave it, and join another without changing message
  identity. An adopted memory backend deliberately survives that lifecycle.
- Serialization is useful for conversation reconstruction but is not a process checkpoint. Live
  tools, memory, providers, grants, operations, and coordination wiring require explicit setup.
- Attribute-first operation lookup preserves built-in method meaning but prevents a session registry
  from overriding or shadowing any Branch attribute.
- Provider injections and capability instructions remain visibly attached to one branch while their
  transient results are not durable messages. Optional provider writeback can persist derived
  action-response knowledge outside the Branch, so reconstructing a Branch does not undo or replay
  that side effect.
- Contributors moving a private field must first classify it as conversation state, serializable
  projection, branch-scoped live extension, session attachment, or per-turn state. Treating those
  categories as interchangeable is the principal maintenance hazard.
- Reversing D1 or D5 would change almost every operation signature. Reversing D2–D4 is narrower but
  requires a migration format for objects that currently have no stable encoding.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Implement the turn-scoped execution context and same-Branch serialization contract from ADR-0018; acceptance requires two overlapping top-level turns to neither overwrite provider data, consume each other's control, drain each other's signal tasks, nor change the branch's configured model. | M | (filled at issue-open time) |
| 2 | Remove the deprecated no-op `system_template` and `system_template_context` constructor arguments under the public deprecation policy, and classify `Branch.connect()` as supported or deprecated after a repository-wide consumer inventory; acceptance requires updated API documentation and compatibility tests for the selected outcome. | S | (filled at issue-open time) |

## Alternatives considered

### Pass every manager separately to every operation

This would make dependencies locally explicit and could reduce the apparent size of `Branch`.
It loses because the managers jointly describe one conversation and must share identity,
progression, and attachment lifecycle. Passing them independently makes cross-conversation mixtures
representable and expands every operation signature.

### Make Session the durable owner of branch messages, models, logs, and memory

This would centralize persistence and simplify some multi-branch reporting. It loses the standalone
Branch contract and makes removal or reparenting a data migration. The shipped behavior instead
retains conversation data on the branch and treats Session references as attachments.

### Serialize every private field as a complete process checkpoint

This would make a single `to_dict()` appear sufficient for restart. It loses because tool callables,
backend handles, observer subscriptions, tasks, and registries do not have stable portable encodings.
A partial encoding would be more dangerous than the current explicit projection because it would
look complete while silently restoring different behavior.

### Put execution algorithms directly on Branch

This would remove the import-and-delegate adapters. It loses because recording, streaming, parsing,
and action logic would become inseparable from aggregate construction and serialization. The shipped
modules allow operation behavior to evolve while the caller-facing object remains stable.

### Let registered operations override built-in attributes

Registry-first lookup would permit local replacement of `chat`, `run`, or even non-operation
attributes. It buys extensibility but makes the meaning of a core Branch method depend on session
attachment. The current attribute-first lookup preserves the built-in surface. No historical design
note records whether that precedence was initially deliberate; this ADR records the shipped contract.

### Eagerly allocate memory and the provider registry

This would remove lazy branches from the accessors. It loses because a branch that never uses memory
or context injection would still allocate those facilities, and eager private memory would prevent a
new branch from adopting the session default. Lazy construction is also the mechanism behind the
current first-claim memory rule.

### Treat context providers as read-only pre-turn injectors

This would keep provider behavior one-directional and make operation completion free of provider
side effects. It loses the existing opt-in post-action feedback seam: providers that can extract a
deterministic lesson from action responses would need a second registry or operation-specific hook.
The shipped registry therefore owns both ordered pre-turn gathering and contained post-action
writeback, while keeping writeback absent unless a provider implements it and `operate()` produced
action responses.

### Clone the complete live Branch object

This would carry providers, capability grants, logs, memory, tasks, observer subscriptions, and
session ownership into the clone. It buys superficial behavioral similarity but creates shared
backend and task ownership without an explicit policy, and it can make a clone appear owned before
Session has registered it. The explicit-subset clone keeps a new conversation operationally clean.

### Store capability output directly as actions

This would turn structured assistant blocks into immediate side effects. It loses the separation
between observation, validation, authorization, and action execution. The shipped path emits typed
signals so a session policy can decide what, if anything, follows.

## Notes

The aggregate diagram is intentionally asymmetric: durable and live branch-scoped resources are
reachable through Branch, while session resources are detachable references.

```text
                      Branch
          conversation identity and public facade
               /       |       |       \
        messages    actions   models    logs
             \         |       |        /
              memory, providers, capabilities
                         |
               detachable coordination
          observer, hooks, ownership, operations
```
