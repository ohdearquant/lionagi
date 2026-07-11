# ADR-0027: Model-service facade and endpoint resolution

- **Status**: Accepted
- **Kind**: Retrospective
- **Area**: service-providers
- **Date**: 2026-07-09
- **Relations**: none

## Context

LionAGI presents one model-facing object even though the work behind that object may be an HTTP
request, a local subprocess, an in-process agent, or a remote agent. The current service package
grew around four concrete problems.

**P1 — Callers need one lifecycle owner.** A caller should not have to derive a provider from a
model string, select an endpoint class, create a call event, configure rate limiting, attach hooks,
retain a provider session identifier, and stop background tasks separately. `iModel` performs all
of those jobs; `iModelManager` provides named ownership for multiple `iModel` instances
(`lionagi/service/imodel.py`; `iModel`, and `lionagi/service/manager.py`; `iModelManager`).

**P2 — Provider and endpoint selection needs one authority.** Provider packages register endpoint
classes, but callers select them by strings such as `openai/chat`, `codex/cli`, or
`ag2/groupchat`. `EndpointRegistry` is the only resolver and materializes a typed, immutable
`EndpointMeta` record on each registered class
(`lionagi/service/connections/registry.py`; `EndpointRegistry`, `EndpointMeta`).

**P3 — HTTP and agentic transports share a call boundary but not transport mechanics.** HTTP
adapters need headers, payload validation, SSRF checks, sessions, status handling, retry, circuit
breaking, and SSE/NDJSON projection. Subprocess and in-process adapters need process or agent
lifecycle control instead. Both still need the same `APICalling` event, hook boundary, normalized
`StreamChunk` output, and executor ownership
(`lionagi/service/connections/endpoint.py`; `Endpoint`,
`lionagi/service/connections/agentic_endpoint.py`; `AgenticEndpoint`, and
`lionagi/service/connections/api_calling.py`; `APICalling`).

**P4 — Vendor grammar must remain vendor-owned.** Request models, endpoint defaults, payload
adaptation, command flags, event parsing, and provider safety settings change at provider cadence.
They live under `lionagi/providers/`; moving them into the generic service layer would make the
service layer a vendor switch statement.

**P5 — The organic resolver is permissive and order-sensitive.** A provider lookup is
case-sensitive even though the resulting `EndpointConfig` lowercases `provider`. Bundled modules
are imported from a fixed list and an `ImportError` is ignored. An unmatched single-endpoint
provider accepts any endpoint string. Any remaining miss becomes a generic endpoint with an
OpenAI-compatible request shape, but `openai_compatible` remains its default `False`. A misspelling,
an unavailable bundled adapter, and an intentional custom compatible service therefore converge on
the same fallback path (`lionagi/service/connections/registry.py`; `EndpointRegistry.match`,
`_import_all_providers`, and `lionagi/service/connections/endpoint_config.py`;
`EndpointConfig._validate_provider`).

The shipped spine is:

```text
Caller / Branch
      |
      v
   iModel ---------> RateLimitedAPIExecutor + service-call hooks
      |
      v
EndpointRegistry ---> EndpointMeta ---> Endpoint | AgenticEndpoint
                                         ^              ^
                                         |              |
                                  HTTP adapters   agentic adapters
                                         +--- lionagi/providers ---+
```

| Concern | Decision |
|---------|----------|
| Caller-facing lifecycle and model registry | D1: `iModel` owns one resolved endpoint, executor, hook registry, and provider session; `iModelManager` owns named facades. |
| Provider/endpoint discovery | D2: `EndpointRegistry` is the sole resolver, populated by provider decorators and a fixed lazy bootstrap list. |
| Generic execution boundary | D3: `Endpoint` and `AgenticEndpoint` execute through `APICalling` and normalize streams as `StreamChunk`; HTTP-only mechanics stay on `Endpoint`. |
| Vendor ownership | D4: provider packages own request schemas, defaults, mappings, event grammars, and agentic mechanics. |

This retrospective ADR deliberately does **not** decide:

- Validated catalog authoring, collision policy, import diagnostics, or explicit compatible fallback;
  ADR-0028 owns that target.
- Bounded admission, unified deadlines, cancellation ownership, or stream retry; ADR-0029 owns that
  target.
- The normalized agentic conformance suite and transport capability vocabulary; ADR-0030 owns that
  target.
- General hook composition or event-bus behavior; the hooks ADR owns call-boundary hook policy.
- Final package ownership for token estimation or MCP client security. Their consumers and failure
  contracts differ, so they remain migration deltas rather than part of this facade decision.

## Decision

### D1 — `iModel` is the model-service lifecycle facade

`iModel` is the object callers construct, invoke, stream, copy, serialize, and close. Callers do not
instantiate provider classes as the normal model API. The shipped constructor is
(`lionagi/service/imodel.py`; `iModel.__init__`):

```python
class iModel:
    def __init__(
        self,
        provider: str = None,
        base_url: str = None,
        endpoint: str | Endpoint = "chat",
        api_key: str = None,
        queue_capacity: int | None = None,
        capacity_refresh_time: float = 60,
        interval: float | None = None,
        limit_requests: int = None,
        limit_tokens: int = None,
        concurrency_limit: int | None = None,
        streaming_process_func: Callable = None,
        provider_metadata: dict | None = None,
        hook_registry: HookRegistry | dict | None = None,
        exit_hook: bool = False,
        id: UUID | str = None,
        created_at: float | None = None,
        **kwargs,
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

    def create_api_calling(
        self, include_token_usage_to_model: bool = False, **kwargs
    ) -> APICalling: ...

    async def invoke(self, api_call: APICalling = None, **kw) -> APICalling: ...
    async def stream(self, api_call=None, **kw) -> AsyncGenerator: ...
    async def close(self) -> None: ...
    def copy(self, share_session: bool = False) -> iModel: ...
```

The `create_event()` annotation currently claims a two-tuple, but every successful implementation
path returns the `APICalling` alone. That mismatch is part of the shipped source, not a second
supported result shape.

`iModelManager` is a string-keyed registry with two conventional keys and isolated concurrent
shutdown (`lionagi/service/manager.py`; `iModelManager`):

```python
class iModelManager(Manager):
    def __init__(self, *args: iModel, **kwargs): ...
    @property
    def chat(self) -> iModel | None: ...
    @property
    def parse(self) -> iModel | None: ...
    def register_imodel(self, name: str, model: iModel): ...
    async def shutdown(self, *, per_model_timeout: float = 10.0) -> None: ...
```

**Exact semantics**

- **Model specification.** If `kwargs["model"]` contains `/` and no provider was supplied, the
  prefix becomes the provider and is removed from the model. If the model has no `/`, the configured
  default chat provider is used. An explicit `provider` wins.
- **Endpoint object.** Passing an `Endpoint` instance bypasses string resolution. Otherwise
  `match_endpoint(provider, endpoint, **kwargs)` is the only selection path. An explicit `provider`
  and `base_url` overwrite the resolved configuration after construction.
- **Identity.** `id` is normalized through `ID.get_id`; otherwise a new UUID is generated.
  `created_at` must be a float timestamp when supplied; otherwise current UTC time is stored.
- **Executor ownership.** Each facade creates exactly one `RateLimitedAPIExecutor`; copies receive a
  fresh facade id and executor. The endpoint config is deep-copied, while the existing retry and
  circuit-breaker objects are passed to the copied endpoint. Provider runtime handlers are copied
  through `Endpoint.copy_runtime_state_to()`.
- **Event construction.** `create_event()` optionally runs a pre-create hook, builds an
  `APICalling`, and attaches pre/post invocation hooks. A pre-create exit raises its cause before
  the call event is returned. Event types other than `HookedEvent` subclasses are rejected; the
  error text states that only `APICalling` is supported.
- **Resume injection.** For an `AgenticEndpoint`, `create_api_calling()` injects the stored endpoint
  session id as `resume` only when neither `resume` nor `session_id` was supplied. Runtime transport
  arguments declared by the endpoint are removed from provider data and stored in
  `APICalling.call_kwargs`.
- **One-shot invocation.** `invoke()` starts the executor if necessary, appends the event, forwards
  pending work through the processor, and waits at most 10 seconds if the event remains pending or
  processing. Timeout is swallowed. The event is then removed from executor ownership even if it is
  still non-terminal. A returned agentic response dict containing `session_id` updates endpoint
  state. Any exception escaping this sequence is wrapped as `ValueError("Failed to invoke API
  call: ...")`.
- **Streaming invocation.** `stream()` appends only calls it creates itself. It starts the executor,
  acquires the processor semaphore if one exists, and calls `api_call.stream()` directly; it does
  not enqueue through `forward()` and therefore does not ask the rate/token permission method. Each
  yielded item passes through the hook registry or `streaming_process_func`. The event is removed
  from the executor pile in `finally`. Ordinary exceptions are wrapped as `ValueError`; cancellation
  is a `BaseException`, so it reaches `finally` and propagates.
- **Chunk processing.** A registered chunk hook takes priority over `streaming_process_func`. A hook
  exit raises its supplied cause or a `RuntimeError`. With no handler, the original chunk is yielded.
- **Serialization.** `to_dict()` stores id, creation time, provider metadata, endpoint state, and by
  default processor config. Request schemas are omitted unless explicitly requested. `from_dict()`
  reconstructs an endpoint, resolves a fresh registered endpoint to recover environment-backed API
  key state, overlays serialized config, and restores executor config.
- **Close and manager shutdown.** `close()` stops the executor. Manager shutdown closes all models
  concurrently, gives each model an independent timeout, logs and swallows timeout, exception, and
  cancellation failures, and returns immediately for an empty registry.
- **Name conflict.** Registering the same manager key again replaces the prior model. There is no
  duplicate-name error and no automatic close of the displaced model.

Current numeric defaults are recorded rather than rationalized after the fact:

| Value | Shipped meaning | Recorded rationale |
|-------|-----------------|--------------------|
| `capacity_refresh_time=60` seconds | Default fixed-window replenishment cadence. | Inherited; no design rationale is recorded in source. |
| API `queue_capacity=100` | Processor cycle capacity and fallback executor concurrency property; it is not a physical queue bound. | Inherited; no design rationale is recorded. |
| Agentic `queue_capacity=10`, `concurrency_limit=3` | Defaults from `AgenticEndpoint`; individual adapters may override. | Inherited; no numeric rationale is recorded. |
| Hook timeouts `10/30/30` seconds | Pre-create, pre-invocation, and post-invocation local caps. | The split is shipped; no numeric rationale is recorded. |
| `invoke()` wait `10` seconds | Safety wait before the event is popped and returned. | Inherited; source records the behavior but no numeric rationale. |
| Manager close `10` seconds per model | Prevents one close from blocking all other closes. | Isolation is documented; the exact number is inherited. |

**Why this way.** A single facade keeps provider selection, event creation, hook attachment,
executor lifetime, serialization, and session state from being reimplemented by Branch and every
operation. `iModelManager` adds only named ownership and shutdown rather than another dispatch
abstraction. The cost is a broad facade: admission defects and provider-session details can leak
into an object that callers otherwise treat as a model client.

### D2 — `EndpointRegistry` is the sole endpoint resolver

Provider classes register by decorator. Registration attaches `_ENDPOINT_META` to the class and
appends a registry entry. The shipped metadata contract is
(`lionagi/service/connections/registry.py`; `EndpointType`, `EndpointMeta`):

```python
class EndpointType(Enum):
    API = "api"
    AGENTIC = "agentic"

@dataclass(frozen=True, slots=True)
class EndpointMeta:
    provider: str
    endpoint: str
    endpoint_type: EndpointType
    aliases: tuple[str, ...] = ()
    provider_aliases: tuple[str, ...] = ()
    options: type[BaseModel] | None = None
    base_url: str | None = None
    auth_type: str | None = None
    content_type: str | None = None
    api_key_env: str | None = None

    def create_config(self, **overrides: Any): ...
```

The registration and lookup surface is:

```python
class EndpointRegistry:
    @classmethod
    def register(
        cls,
        provider: str,
        endpoint: str,
        aliases: list[str] | None = None,
        endpoint_type: EndpointType = EndpointType.API,
        provider_aliases: list[str] | None = None,
        options: type[BaseModel] | None = None,
        base_url: str | None = None,
        auth_type: str | None = None,
        content_type: str | None = None,
        api_key_env: str | None = None,
    ): ...

    @classmethod
    def match(cls, provider: str, endpoint: str = "", **kwargs) -> Any: ...

    @classmethod
    def list_providers(cls) -> list[dict[str, Any]]: ...

# defined in lionagi/service/connections/match_endpoint.py, not registry.py
def match_endpoint(provider: str, endpoint: str, **kwargs) -> Endpoint: ...
```

`EndpointMeta.create_config()` supplies these registration-derived defaults:

```python
{
    "name": f"{provider}_{endpoint}",
    "provider": provider,
    "base_url": declared_base_url or ("internal" if agentic else ""),
    "endpoint": endpoint,
    # agentic -> "internal"; declared api_key_env -> settings value, or the
    # "dummy-key-for-testing" sentinel when the env var is unset; no declared
    # api_key_env (e.g. Ollama) -> None
    "api_key": api_key,
    "request_options": options,
    "timeout": 3600 if agentic else 600,
    "auth_type": declared_auth_type or "bearer",
    "content_type": declared_content_type or "application/json",
    "method": "POST",
}
```

The 3,600-second agentic cap, 600-second registered API cap, and 300-second generic
`EndpointConfig` default are inherited values with no recorded numeric rationale. They are
transport caps, not a propagated request deadline.

The fixed bootstrap currently materializes the following source-owned catalog; the scripted row is
test support but is imported through the same bootstrap
(`lionagi/service/connections/registry.py`; `_import_all_providers`):

| Provider family | Registered endpoints | Family |
|-----------------|----------------------|--------|
| `openai` | chat, speech, transcription, image generation/edit, embeddings, responses | API |
| `anthropic` | messages | API |
| `ollama` | chat, embeddings, generate | API |
| `tavily`, `exa`, `firecrawl` | search/extract, search/contents/similar, scrape/map/crawl | API |
| `perplexity`, `deepseek`, `gemini`, `openrouter` | chat | API |
| `nvidia_nim` | chat, embeddings | API |
| `groq` | chat, transcription | API |
| `codex`, `claude_code`, `gemini_code`, `pi` | one CLI endpoint each | Agentic |
| `ag2` | group chat, in-process agent, remote NLIP | Agentic |
| `scripted` | chat/CLI aliases | Agentic test support |

**Exact semantics**

- **Lazy bootstrap.** The first `match()` or `list_providers()` call imports the fixed modules under
  a process-local lock. `_loaded` is set after the loop. Later calls do not rescan modules.
- **Import failure.** Each `ImportError` is ignored without a diagnostic. Other exception types
  escape and prevent `_loaded` from being set.
- **Registration order.** Entries are appended. There is no canonical-key, alias, endpoint-class,
  or metadata validation and no duplicate detection.
- **Provider match.** The raw provider must equal the canonical string or one of its aliases. The
  comparison is case-sensitive and does not trim. Normalization in `EndpointConfig` happens only
  after a class or fallback has already been chosen.
- **Endpoint match.** Empty endpoint selects the first entry for the provider. Otherwise canonical
  endpoint or alias selects the first match. Punctuation is literal.
- **Single-endpoint provider.** If a provider has exactly one canonical registration, any unmatched
  non-empty endpoint selects it. The requested endpoint string is not retained as a mismatch.
- **Miss.** Every other miss creates the base `Endpoint` with provider as supplied, endpoint or
  `chat/completions`, bearer JSON POST settings, `requires_tokens=True`, and name
  `openai_compatible_chat`. It does not set `openai_compatible=True`.
- **Instantiation.** A match instantiates `entry.cls(None, **kwargs)`; class metadata creates a fresh
  `EndpointConfig`. Registry entries store classes and immutable metadata, not shared endpoint
  instances.
- **Inspection.** `list_providers()` returns one dict per entry with exactly `provider`, `endpoint`,
  `aliases`, `type`, `class`, and `options`. Provider aliases, URLs, auth, availability, and ignored
  imports are absent.
- **Restart.** Registry state is process-local. A restart resets entries and `_loaded`; imports
  reconstruct the catalog.

**Why this way.** Decorator registration keeps each concrete adapter beside its schema and avoids a
large construction switch. A single registry prevents caller-specific routing rules. The lazy fixed
bootstrap makes built-in discovery deterministic, but it creates a dependency from generic service
code back to every bundled provider module and makes ignored imports indistinguishable from absent
providers. ADR-0028 retains the single resolver while replacing the unvalidated inputs.

### D3 — `Endpoint`, `AgenticEndpoint`, and `APICalling` share one event boundary

The base configuration is a Pydantic model. Unknown constructor keys are moved into `kwargs` before
validation (`lionagi/service/connections/endpoint_config.py`; `EndpointConfig`):

```python
class EndpointConfig(BaseModel):
    name: str
    provider: str
    base_url: str | None = None
    endpoint: str
    endpoint_params: list[str] | None = None
    method: str = "POST"
    params: dict[str, str] = Field(default_factory=dict)
    content_type: str | None = "application/json"
    auth_type: Literal["bearer", "x-api-key", "none"] = "bearer"
    default_headers: dict = {}
    request_options: type[BaseModel] | None = None
    api_key: str | SecretStr | None = Field(None, exclude=True)
    timeout: int = 300
    max_retries: int = 3
    openai_compatible: bool = False
    requires_tokens: bool = False
    context_window: int | None = None
    kwargs: dict = Field(default_factory=dict)
    client_kwargs: dict = Field(default_factory=dict)
    allow_local_network: bool = False
    serialize_by_alias: bool = False
```

The generic method boundary is (`lionagi/service/connections/endpoint.py`; `Endpoint`):

```python
class Endpoint:
    is_cli: ClassVar[bool] = False
    transport_arg_keys: ClassVar[tuple[str, ...]] = ()

    def __init__(
        self,
        config: dict | EndpointConfig | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        retry_config: RetryConfig | None = None,
        **kwargs,
    ): ...

    def create_payload(
        self,
        request: dict | BaseModel,
        extra_headers: dict | None = None,
        **kwargs,
    ): ...  # unannotated in source; returns (payload, headers)

    async def call(
        self,
        request: dict | BaseModel,
        cache_control: bool = False,
        skip_payload_creation: bool = False,
        **kwargs,
    ): ...

    async def stream(
        self,
        request: dict | BaseModel,
        extra_headers: dict | None = None,
        **kwargs,
    ): ...
```

Agentic endpoints reuse the config and event interface but block the base HTTP helpers
(`lionagi/service/connections/agentic_endpoint.py`; `AgenticEndpoint`):

```python
class AgenticEndpoint(Endpoint):
    is_cli: ClassVar[bool] = True
    DEFAULT_CONCURRENCY_LIMIT: ClassVar[int] = 3
    DEFAULT_QUEUE_CAPACITY: ClassVar[int] = 10

    def __init__(self, config: dict | EndpointConfig = None, **kwargs): ...
    @property
    def provider_session_id(self) -> str | None: ...
    @property
    def session_id(self) -> str | None: ...
```

`APICalling` is the Pydantic event envelope
(`lionagi/service/connections/api_calling.py`; `APICalling`):

```python
class APICalling(HookedEvent):
    endpoint: Endpoint = Field(..., exclude=True)
    payload: dict
    headers: dict = Field(default_factory=dict, exclude=True)
    call_kwargs: dict = Field(default_factory=dict, exclude=True)
    cache_control: bool = Field(default=False, exclude=True)
    include_token_usage_to_model: bool = Field(default=False, exclude=True)

    @property
    def required_tokens(self) -> int | None: ...
    async def _core_invoke(self): ...
    async def _core_stream(self): ...
```

All endpoint streams project to this dataclass
(`lionagi/service/types/stream_chunk.py`; `StreamChunk`):

```python
ChunkType = Literal[
    "system", "thinking", "text", "tool_use",
    "tool_result", "result", "error",
]

@dataclass(slots=True)
class StreamChunk:
    type: ChunkType
    content: str | None = None
    tool_name: str | None = None
    tool_id: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_output: Any | None = None
    is_error: bool = False
    is_delta: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
```

**Exact payload and execution semantics**

- **Configuration input.** A dict is validated as `EndpointConfig`; an existing config is deep
  copied and updated; `None` requires class-bound `_ENDPOINT_META`. Any other type raises
  `ValueError`.
- **Unknown config keys.** The pre-validator moves unknown keys into `EndpointConfig.kwargs`. These
  keys become provider request defaults during `create_payload()`. This is why a misspelled control
  field can cross into provider data.
- **Request schema.** With `request_options`, payload keys are filtered to model fields and the model
  validates them. Alias serialization returns a validated `model_dump`; otherwise the original
  filtered values are returned after validation. Without a schema, a fixed list of LionAGI control
  keys is removed and other values pass through.
- **Headers and keys.** `HeaderFactory` constructs bearer, x-api-key, or no-auth headers. API key
  config accepts a secret, settings key, environment key, literal, or special local/testing value;
  it is excluded from serialization.
- **URL and SSRF.** `full_url` is base plus endpoint, with format parameters when declared. Both
  call and stream assert that the hostname is safe unless `allow_local_network=True`.
- **One-shot HTTP status.** Status 200 returns JSON. Status 429 and status 500 or above are retryable
  transport failures. Other non-200 statuses are wrapped in an internal non-retryable sentinel so the
  retry layer gives up immediately. Only the native path (no explicit `RetryConfig`) unwraps the
  sentinel and re-raises the original `aiohttp.ClientResponseError`; with an explicit `RetryConfig`
  the sentinel is neither excluded by default (`RetryConfig.exclude_exceptions` defaults empty) nor
  unwrapped, so the caller sees the internal sentinel exception rather than the original — a
  current-behavior asymmetry, recorded as-is.
- **One-shot resilience.** An explicit `RetryConfig` wraps `_call`; an explicit circuit breaker
  wraps the resulting attempt function when caching is off. With no `RetryConfig`, native aiohttp
  retry uses `EndpointConfig.max_retries` as a total-attempt cap. Cached calls apply the same chosen
  wrappers inside the cached function.
- **HTTP stream.** `stream()` creates a payload then calls `_stream_aiohttp()` directly. It applies
  neither the endpoint retry config nor the circuit breaker. The transport sets `stream=True`,
  requires status 200, parses SSE framing and plain JSON lines, ignores comments and SSE metadata,
  recognizes `[DONE]`, and maps unrecognized JSON objects to a `system` chunk containing raw data.
- **Agentic transport.** `AgenticEndpoint` raises `NotImplementedError` from session, aiohttp call,
  and aiohttp stream helpers. Concrete adapters override `_call()` and/or `stream()`.
- **Token estimation.** `APICalling.required_tokens` returns `None` when the endpoint does not
  require tokens. Otherwise it estimates messages, Responses-style input, or embedding input. A
  caller-requested usage annotation is appended to the final message before dispatch.
- **Event state.** `Event` begins `PENDING`, becomes `PROCESSING`, and terminalizes as `COMPLETED`,
  `FAILED`, or `CANCELLED`; `SKIPPED` and `ABORTED` are also terminal states. Terminal transitions
  set a lazily created completion event. Ordinary exceptions from event invoke/stream are captured
  on the event and are not re-raised by `Event`; `BaseException` marks cancellation and is re-raised.
- **Hook order.** `HookedEvent` runs pre-invocation, core work, then post-invocation. A one-shot post
  hook runs even after core failure but cannot replace that core failure. A post-stream hook runs
  only after normal stream exhaustion; once data has been sent, its failure is logged rather than
  re-raised.
- **Empty stream.** A transport that produces no chunks and no exception reaches normal EOF and the
  event becomes `COMPLETED`; the generic layer does not require a result chunk.

The shipped resilience values are:

| Value | Meaning | Recorded rationale |
|-------|---------|--------------------|
| `EndpointConfig.max_retries=3` | Native HTTP path makes at most three total attempts. | Inherited; no numeric rationale is recorded. |
| `RetryConfig.max_retries=3` | Explicit retry path makes one initial attempt plus three retries. | Inherited; the different interpretation is current behavior. |
| Backoff `1s`, cap `60s`, factor `2.0`, jitter `0.2` | Defaults in `RetryConfig`/`retry_with_backoff`. | Exponential jitter is intentional; exact values are inherited. |
| Native HTTP jitter `0.5` | Used when no explicit `RetryConfig` exists. | Inherited; no reason for differing from `0.2` is recorded. |
| Circuit threshold `5`, recovery `30s`, half-open probes `1` | Default circuit transition policy. | Pattern intent is clear; exact values are inherited. |

**Why this way.** A stable event and chunk boundary lets operations consume both transport families
without vendor selection logic. HTTP security and response framing benefit from one implementation.
Agentic transports cannot safely share HTTP session or cancellation mechanics, so inheritance is
limited to configuration, event, and normalized output. The current `is_cli=True` marker on every
agentic endpoint is an overloaded routing signal; ADR-0030 names the capability split.

### D4 — Provider adapters own vendor request and event grammar

The package boundary is:

```text
lionagi/service/
├── imodel.py                         facade and session ownership
├── manager.py                        named facade lifecycle
├── rate_limited_processor.py         current admission/executor
├── resilience.py                     generic retry and circuit policy
└── connections/
    ├── registry.py                   selection authority and metadata
    ├── endpoint_config.py            generic endpoint configuration
    ├── endpoint.py                   generic HTTP transport/projection
    ├── agentic_endpoint.py           non-HTTP base
    └── api_calling.py                event boundary

lionagi/providers/
├── <vendor>/_config.py               provider identity and endpoint tuples
├── <vendor>/<endpoint>.py            request models and adapter behavior
├── _cli_subprocess.py                shared subprocess mechanics
├── _agentic_handlers.py              shared runtime callback handling
└── _provider_errors.py               provider error classification
```

Provider declarations currently use four-to-seven-position enum tuples
(`lionagi/service/connections/provider_config.py`; `ProviderConfig`):

```python
(
    endpoint_path,       # index 0
    aliases,             # index 1
    endpoint_type,       # index 2
    request_options,     # index 3, optional; type or LazyType
    base_url,            # index 4, optional
    auth_type,           # index 5, optional
    content_type,        # index 6, optional; default application/json
)
```

`LazyType("module:Class")` defers request-model import and caches the resolved type. Each enum class
adds `_PROVIDER`, `_PROVIDER_ALIASES`, and optionally `_API_KEY_ENV`; its member's `.register`
decorator forwards named values into the generic registry.

**Exact ownership semantics**

- API providers own their Pydantic request models, metadata tuples, any provider header additions,
  payload remapping, multipart transport arguments, and response normalization.
- Subprocess providers own command grammars, model defaults, permission modes, event parsers, UX
  callbacks, and session synthesis. Shared NDJSON/process mechanics remain generic only where at
  least two adapters use them.
- In-process and remote agent adapters own their request objects and agent/network calls while still
  yielding `StreamChunk`.
- `lionagi/service/providers.py` currently remains a second vendor-identity table for model aliases,
  effort mapping, fast mode, bypass, and safety kwargs. `parse_model_spec()` is a public compatibility
  surface consumed outside provider packages.
- A provider adapter depends on the service registry decorator and base endpoint. Conversely the
  service registry imports a fixed list of provider modules at runtime. This is a real two-way
  package dependency, not full dependency inversion.
- Provider-specific failures may arrive as raised exceptions, error chunks, or a final session with
  `is_error`; the generic base does not currently enforce one adapter conformance contract.

**Why this way.** Request and event grammars change with provider releases and are easiest to test
beside their fixtures. Pulling those grammars into `Endpoint` would make the generic transport
select vendors. Conversely, leaving subprocess framing, SSRF checks, or error taxonomy duplicated
would cause security and cleanup drift. The boundary therefore centralizes mechanics, not vendor
meaning. ADR-0028 removes generic ownership of the bootstrap/vendor tables, and ADR-0030 tightens
the shared agentic output and cleanup contract.

## Consequences

- API, subprocess, in-process, and remote-agent providers share one public `iModel` facade and one
  `APICalling`/`StreamChunk` event model. Existing public `iModel`, `Endpoint`, `AgenticEndpoint`,
  `APICalling`, and `StreamChunk` names remain stable.
- Provider selection and construction are inspectable at one resolver. A new provider must still
  coordinate its local registration with the generic bootstrap list and, where model aliases or
  effort behavior apply, the second table in `service/providers.py`.
- HTTP adapters inherit one SSRF and transport implementation. Agentic adapters cannot accidentally
  use the HTTP helpers because those helpers fail explicitly.
- Reversing D1 would require changing Branch, operation, serialization, and lifecycle call sites;
  it is high cost. Replacing D2's registration mechanism is medium cost if `match_endpoint()` remains
  the facade. Splitting D3's event model is high cost because operations consume `APICalling` and
  `StreamChunk`. Moving a vendor grammar under service is mechanically easy but raises ongoing
  coupling.
- Maintainers must know that `queue_capacity` is not a physical queue limit, `stream()` bypasses
  rate/token permission, `invoke()` may return a non-terminal event after ten seconds, and HTTP
  stream setup does not use retry/circuit policy. ADR-0029 exists because these are one lifecycle
  problem, not independent tuning defects.
- The resolver can silently hide an unavailable or mistyped adapter behind a generic endpoint.
  Catalog failure becomes visible only when a later call is misrouted or fails.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Replace positional provider declarations with typed authoring records; canonicalize and validate every provider, endpoint, and alias key; report failed bundled imports; and require explicit opt-in for generic OpenAI-compatible fallback, with compatibility coverage for existing custom-provider callers. | M | #2026 |
| 2 | Route `invoke()` and `stream()` through one bounded admission lifecycle that applies request, token, and concurrency limits before provider work; propagate one deadline through queueing, retries, and transport; and prove that cancellation leaves no queued or active orphan. | L | (filled at issue-open time) |
| 3 | Apply retry and circuit policy to HTTP stream establishment before the first emitted chunk, prohibit automatic replay after output begins, and add tests for pre-first-byte failure, mid-stream failure, normal EOF, and caller cancellation. | M | (filled at issue-open time) |
| 4 | Publish an agentic-adapter conformance contract for request construction, normalized chunks, error classification, resume identifiers, and transport cleanup; run it against every subprocess, in-process, and remote adapter while retaining vendor parsers beside their vendors. | M | (filled at issue-open time) |
| 5 | Move named-vendor identity, effort, bypass, and safety tables out of generic service ownership while preserving `parse_model_spec()` as a compatibility facade and testing every existing provider alias. | M | (filled at issue-open time) |
| 6 | Freeze neutral interfaces for token estimation and MCP client security, then move them below protocol callers with compatibility re-exports and unchanged action-layer tool-registration behavior. | M | (filled at issue-open time) |

## Alternatives considered

### Direct provider construction as the public API

Callers could import `OpenaiChatEndpoint`, `CodexCLIEndpoint`, or another concrete class and manage
it directly. This would make provider behavior explicit and remove one lookup. It lost because each
caller would then need to reproduce provider/model parsing, API-key restoration, executor creation,
hook attachment, transport-argument separation, session resume, copy, serialization, and close.
Those responsibilities already converge in `iModel`; duplicating them would create behaviorally
different clients for the same provider.

### Separate executor-provider registry

A second registry could select executor implementations independently of endpoints. It would make
transport scheduling replaceable. It lost because the live endpoint registry already resolves both
HTTP and agentic endpoint families, and each `iModel` already owns its executor. A second string
authority would need collision, alias, fallback, and lifecycle rules while still depending on the
endpoint selection result.

### One concrete endpoint class for every transport

One class could branch internally on provider or transport type. That would reduce the number of
base classes. It lost because HTTP sessions/status/retry and subprocess/process-group cleanup have
incompatible resource and cancellation mechanics. The current `AgenticEndpoint` makes accidental
HTTP use fail immediately while retaining the shared event/config boundary.

### Put all provider schemas and event parsers in `service/connections`

This would make the full catalog visible from one directory and remove provider-to-service
registration imports. It lost because generic service code would then own vendor releases, flags,
event grammars, and fixtures. Provider-local request models and parsers are the evidence-bearing
units; only generic transport and normalized output belong in service.

### Resolve by direct import path instead of a registry

Callers could pass `module:Class` and bypass aliases and bootstrap. That would support arbitrary
extensions without a central list. It lost for the primary API because model specs and public aliases
would become Python package paths, provider availability could not be inspected uniformly, and every
caller would become responsible for validating the imported type. A validated catalog can accept
declarative external inventory later without changing the resolver facade.

### Make resolution strict immediately

Unknown providers and endpoints could have raised from the first registry version. That would have
made typos visible. It was not the shape the code grew: the generic fallback supports custom
OpenAI-compatible services, and single-endpoint aliases were treated permissively. ADR-0028 keeps
that use case but makes compatibility an explicit selection mode with a staged migration.

## Notes

This ADR records current behavior, including the permissive fallback, first-match alias resolution,
positional provider declarations, and different `invoke()`/`stream()` scheduling paths. It does not
endorse them as target contracts. The principal source anchors are `lionagi/service/imodel.py`,
`lionagi/service/manager.py`, `lionagi/service/rate_limited_processor.py`,
`lionagi/service/connections/{registry,provider_config,endpoint_config,endpoint,agentic_endpoint,api_calling}.py`,
`lionagi/protocols/generic/{event,processor}.py`, and the provider modules under
`lionagi/providers/`.
