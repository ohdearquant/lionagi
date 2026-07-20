# ADR-0011: Function tool descriptor and Branch registry

- **Status**: Accepted
- **Kind**: Retrospective
- **Area**: actions-tools
- **Date**: 2026-07-09
- **Relations**: none

## Context

LionAGI presents Python callables to model providers as function schemas and later
executes provider-selected calls locally. Four concrete problems shaped the shipped
tool layer.

**P1 — A provider descriptor and a Python executable have different persistence
properties.** A provider needs a JSON-compatible function name, description, and
parameter schema. Local execution needs a live callable plus optional pre- and
postprocessors. Python callables and closures cannot be reconstructed safely from a
serialized record, so treating a tool as a restorable data object would promise more
than the runtime can deliver (`lionagi/protocols/action/tool.py`;
`lionagi/protocols/generic/element.py`).

**P2 — Python signatures, provider schemas, and invocation validation are three
distinct contracts.** A Pydantic request model supplies the practical typed path used
by built-ins: its JSON Schema is advertised and its constructor normalizes arguments.
Without one, `function_to_schema()` recognizes only a small primitive annotation map
and advertises every signature parameter as required, including parameters with Python
defaults. At invocation, non-strict validation requires only signature parameters that
have no defaults, while strict validation compares the argument-key set with the
schema's `required` set. The three views therefore diverge for some raw callables
(`lionagi/libs/schema/function_to_schema.py`;
`lionagi/protocols/action/function_calling.py`).

**P3 — Tool visibility is conversation-local.** Different branches can expose
different subsets of the same reusable Python callables. A branch consequently needs a
name-indexed registry that owns its provider schema list, resolves an action request,
and constructs the invocation event without making tools process-global
(`lionagi/protocols/action/manager.py`; `lionagi/session/branch.py`).

**P4 — Remote MCP tools must look ordinary after discovery, but their transport is
not ordinary.** The registry currently accepts a one-entry MCP configuration, discovers
remote schemas, builds a local async proxy, remembers transport policy, and reaches a
process-global client pool. Command and URL transports are fail-closed under direct pool
use, and the two explicit config-loading helpers no longer upgrade an omitted policy to a
permissive one: it stays unset and is passed through unchanged, and reaches the pool's
fail-closed default with no possibility of substituting a remembered authorization from
an earlier caller (see Policy recovery below). So loading a config file is no longer an
implicit trust act at all. Discovered tools
use the remote tool's unqualified name in the branch registry, so remote servers can
collide with each other or with local tools (`lionagi/protocols/action/manager.py`;
`lionagi/service/connections/mcp_wrapper.py`).

| Concern | Decision |
|---|---|
| Executable descriptor | D1: `Tool` combines a serializable provider schema with excluded live callables and is intentionally not deserializable. |
| Input contract | D2: Pydantic `request_options` is the typed path; raw signature derivation and strict/non-strict validation retain their shipped, non-equivalent semantics. |
| Branch visibility | D3: each `Branch` owns an `ActionManager` keyed by provider-visible function name. |
| Remote normalization | D4: the action registry normalizes MCP discoveries into ordinary `Tool` values while using the service-owned process-global connection pool. |

This ADR does **not** decide:

- Invocation, authorization, hooks, event status, or history ordering; those are the
  execution transaction recorded in ADR-0012.
- How built-in providers obtain branch context or behave when a branch is cloned; that
  construction boundary is recorded in ADR-0013.
- Provider-specific outer request envelopes. This ADR fixes the portable function
  object stored in `tool_schema`, not each model service's surrounding payload.
- Session-level governance policy. The registry resolves names; the normal branch
  execution path decides whether a resolved call is authorized.

## Decision

### D1 — `Tool` is a live callable descriptor, not a restorable executable

`Tool` is a Pydantic `Element`. Its shipped field contract is:

```python
# lionagi/protocols/action/tool.py
class Tool(Element):
    func_callable: Callable[..., Any] = Field(..., exclude=True)
    mcp_config: dict[str, dict[str, Any]] | None = None
    tool_schema: dict[str, Any] | None = None
    request_options: type | None = None
    preprocessor: Callable[[Any], Any] | None = Field(None, exclude=True)
    preprocessor_kwargs: dict[str, Any] = Field(default_factory=dict, exclude=True)
    postprocessor: Callable[[Any], Any] | None = Field(None, exclude=True)
    postprocessor_kwargs: dict[str, Any] = Field(default_factory=dict, exclude=True)
    strict_func_call: bool = False

    @property
    def function(self) -> str: ...

    @property
    def required_fields(self) -> set[str]: ...

    @property
    def minimum_acceptable_fields(self) -> set[str]: ...

    @classmethod
    def from_dict(cls, data: dict[str, Any]): ...

    def to_dict(
        self,
        mode: Literal["python", "json", "db"] = "python",
        **kw,
    ) -> dict[str, Any]: ...
```

The inherited `Element` fields are `id: UUID`, `created_at: float`, and
`metadata: dict`; `Element` forbids unknown fields and permits arbitrary Python types
(`lionagi/protocols/generic/element.py`). `Tool.to_dict()` delegates to that element
serialization and adds a derived top-level `function` string. The callable,
preprocessor, postprocessor, and both processor-kwargs dictionaries are excluded by
their fields. `tool_schema`, `mcp_config`, `request_options`, and
`strict_func_call` remain model fields; the live execution object is not recreated from
them.

The public reference aliases retain the accepted input vocabulary:

```python
FuncTool: TypeAlias = Tool | Callable[..., Any] | dict
FuncToolRef: TypeAlias = FuncTool | str
ToolRef: TypeAlias = FuncToolRef | list[FuncToolRef] | bool
```

**Exact semantics**

- **Local construction:** with no `mcp_config`, the before-validator requires a
  callable with a usable name. A non-callable is rejected during model validation.
- **MCP construction:** `mcp_config` and `func_callable` are mutually exclusive.
  `mcp_config` must be a dictionary with exactly one entry. Its key becomes the proxy
  callable's name; its value is passed to `create_mcp_tool()`.
- **Missing schema:** when `tool_schema is None`, the after-validator calls
  `function_to_schema(func_callable, request_options=request_options)` exactly once at
  construction.
- **Supplied schema:** a caller-provided schema is retained without a second schema
  normalization pass. Later properties assume the OpenAI-style
  `tool_schema["function"]` shape.
- **Function identity:** `Tool.function` is
  `tool_schema["function"]["name"]`; registry identity follows the schema name, not
  necessarily `func_callable.__name__` when a custom schema is supplied.
- **Serialization:** `to_dict()` retains descriptor data and adds `function`; excluded
  callables and processors do not appear. Serialization is a record of the declaration,
  not an executable snapshot. `mcp_config` is not redacted, so configured environment or
  transport values remain in the record and must be handled as configuration-sensitive
  data.
- **Deserialization:** `Tool.from_dict(...)` always raises `NotImplementedError`.
  Arbitrary data cannot recreate the callable or its closure.
- **Mutation:** `Tool` is not frozen. Factory code can attach processors after
  construction, and a registry holds the same object by reference.

**Why this way**

One object keeps the provider declaration adjacent to the exact callable it describes,
which makes local registration and schema lookup small. Excluding live objects avoids
pretending closures are portable. The cost is that serialized tool data is diagnostic or
presentational only; restoring executable behavior always requires code-driven
construction.

### D2 — Pydantic request models are the typed contract; raw derivation is limited

The schema adapter is Python-native and emits an OpenAI-format function object:

```python
# lionagi/libs/schema/function_to_schema.py
class FunctionSchema(SchemaModel):
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = Field(
        None,
        validation_alias="request_options",
    )
    strict: bool | None = None

def function_to_schema(
    f_,
    style: Literal["google", "rest"] = "google",
    *,
    request_options: dict[str, Any] | None = None,
    strict: bool = None,
    func_description: str = None,
    parametert_description: dict[str, str] = None,
    return_obj: bool = False,
) -> dict: ...
```

For the request-model path, the `FunctionSchema.parameters` validator converts a
Pydantic model type with `model_json_schema()`. Built-in tools pass model classes such
as `ReaderRequest` and `BashRequest`, even though the helper's annotation says
`dict[str, Any] | None`; runtime validation accepts the model type.

The resulting provider payload has this shape:

```json
{
  "type": "function",
  "function": {
    "name": "callable_name",
    "description": "docstring-derived or supplied text",
    "parameters": {
      "type": "object",
      "properties": {},
      "required": []
    }
  }
}
```

When `strict` is truthy, `function_to_schema()` also adds
`function.strict`. That provider-schema flag is distinct from
`Tool.strict_func_call`, which controls LionAGI's local key-set check.

Invocation normalization occurs when a `FunctionCalling` is constructed:

```python
# lionagi/protocols/action/function_calling.py
class FunctionCalling(Event):
    func_tool: Tool = Field(..., exclude=True)
    arguments: dict[str, Any] | BaseModel

    # before validation: BaseModel -> model_dump(exclude_unset=True)
    # after validation:
    #   request_options(**arguments) -> model_dump(exclude_unset=True)
    #   strict=True  -> keys(arguments) == tool.required_fields
    #   strict=False -> minimum_acceptable_fields <= keys(arguments)
```

**Exact semantics**

- **Pydantic path:** `request_options(**arguments)` performs the request model's
  coercion, constraints, defaults, and extra-field policy. The normalized arguments are
  dumped with `exclude_unset=True`, so a model default not supplied by the caller is not
  automatically forwarded to the Python function.
- **Empty request-options value:** `function_to_schema()` tests `request_options` by
  truthiness. `None` and an empty dictionary both select raw signature derivation; a
  Pydantic model type selects the typed path.
- **Raw schema types:** without `request_options`, only `str`, `int`, `float`, `list`,
  `tuple`, `bool`, and `dict` have an explicit Python-to-JSON map. Both `int` and
  `float` map to JSON Schema `number`. An unannotated parameter defaults to `string`;
  other annotations contribute their `__name__` as the schema type.
- **Raw required set:** every signature parameter is appended to provider
  `required`, whether or not the Python signature gives it a default. Positional-only,
  `*args`, and `**kwargs` are not represented specially.
- **Non-strict local check:** the minimum set is computed with
  `inspect.signature()` from parameters having no default, then parameters literally
  named `kw`, `kwargs`, or `args` are removed. Empty arguments are accepted when all
  ordinary parameters have Python defaults. Extra keys pass this set check and can fail
  later when expanded as `func_callable(**arguments)`.
- **Signature-inspection failure:** raw schema construction propagates an
  `inspect.signature()` failure unless the caller supplies `tool_schema`. During later
  non-strict validation, `minimum_acceptable_fields` catches inspection failure and
  returns an empty set, so the key-presence check accepts any argument dictionary and
  leaves failure to callable invocation.
- **Strict local check:** the normalized argument keys must equal the schema's
  `required` set exactly. Missing required keys and supplied optional keys both fail.
  A schema without a `required` key can raise `KeyError` through
  `Tool.required_fields`.
- **Validation failure:** request-model construction or either key-set check raises
  before the callable starts. The higher execution layer decides whether that exception
  becomes a response or propagates (ADR-0012).
- **Callable invocation:** all arguments are eventually expanded as keyword arguments.
  Positional-only parameters and `*args` therefore require an explicit adapter even if
  schema generation succeeds. A raw `**kwargs` callable can execute in non-strict mode,
  but derivation advertises one required `kwargs` string property rather than the open
  keyword object the callable accepts; provider-correct use therefore needs an explicit
  schema or request model.

**Why this way**

Pydantic provides one artifact for provider schema, coercion, and field constraints, so
it is the reliable path for maintained tools. Raw derivation keeps simple keyword
callables convenient. The shipped raw behavior was retained because tests and existing
provider payloads assert that even defaulted parameters are advertised as required; a
correction is a compatibility change, not a formatting cleanup.

### D3 — `ActionManager` is the branch-local name registry and resolver

The manager's public contract is:

```python
# lionagi/protocols/action/manager.py
class ActionManager(Manager):
    def __init__(self, *args: FuncTool, **kwargs) -> None: ...
    def __contains__(self, tool: FuncToolRef) -> bool: ...
    def register_tool(self, tool: FuncTool, update: bool = False) -> None: ...
    def register_tools(
        self,
        tools: list[FuncTool] | FuncTool,
        update: bool = False,
    ) -> None: ...
    def match_tool(
        self,
        action_request: ActionRequest | BaseModel | dict,
    ) -> FunctionCalling: ...
    async def invoke(
        self,
        func_call: BaseModel | ActionRequest,
    ) -> FunctionCalling: ...

    @property
    def schema_list(self) -> list[dict[str, Any]]: ...

    def get_tool_schema(
        self,
        tools: ToolRef = False,
        auto_register: bool = True,
        update: bool = False,
    ) -> dict: ...
```

Its concrete store is `registry: dict[str, Tool]`. Every `Branch` constructs a new
empty manager, registers its constructor-supplied tools into that manager, and exposes
the same object as `branch.acts`; `branch.tools` exposes the registry dictionary
(`lionagi/session/branch.py`).

**Exact semantics**

- **Constructor input:** positional arguments and keyword values are flattened with
  `to_list(..., dropna=True, flatten=True)` and registered with `update=True`.
- **Callable registration:** a raw callable becomes `Tool(func_callable=callable)`.
- **Descriptor registration:** a `Tool` is stored as-is under `tool.function`.
- **MCP dictionary registration:** a dictionary becomes `Tool(mcp_config=dict)` and
  must therefore contain exactly one entry.
- **Unsupported registration:** any other value raises `TypeError`.
- **Duplicate local name:** for a `Tool`, callable, or string membership check, an
  existing name raises `ValueError` unless `update=True`; with update, the registry
  entry is replaced.
- **Dictionary duplicate edge:** `ActionManager.__contains__` has no dictionary arm.
  A second one-entry MCP dictionary with the same derived name therefore bypasses the
  pre-conversion duplicate check and overwrites the entry even when `update=False`.
- **Request matching:** dictionaries must contain `function` and `arguments` keys.
  `ActionRequest` and other Pydantic models are read through attributes. Unsupported
  envelope types raise `TypeError`; an absent registry name raises `ValueError`.
- **Resolution result:** a successful match returns an uninvoked
  `FunctionCalling(func_tool=tool, arguments=args)`.
- **Manager invocation:** `invoke()` matches, awaits `FunctionCalling.invoke()`, and
  returns the event regardless of its terminal status. It does not raise an ordinary
  callable exception captured by `Event.invoke()`.
- **Schema selection:** `get_tool_schema(True)` returns
  `{"tools": schema_list}`; `False` returns the empty list `[]`. A specific registered
  name or descriptor returns `{"tools": schema}`. A list returns
  `{"tools": [schema, ...]}`. A one-item list or tuple is collapsed first.
- **Empty selection:** `get_tool_schema([])` is distinct from `False` and returns
  `{"tools": []}`. Constructing an empty manager likewise produces an empty registry and
  empty `schema_list`.
- **Auto-registration:** requesting the schema for an unregistered raw callable
  registers it when `auto_register=True`; otherwise it raises. A dictionary supplied to
  schema selection is returned directly and is not registered.
- **Schema references:** `schema_list` and specific lookups return the dictionaries held
  by each `Tool`, not defensive copies. Mutating a returned schema mutates what that
  manager will advertise on later calls.
- **Ordering:** `schema_list` follows dictionary insertion order. Replacing an existing
  key retains Python dictionary key position.
- **Branch locality:** registering or replacing a tool affects that manager only. The
  underlying `Tool` object can still be shared by reference across managers.

**Why this way**

The registry keeps branch-visible capability selection separate from callable
definition. Function name is the provider's lookup key, so the same key naturally
resolves the model request and provider schema. The design deliberately keeps
`ActionManager` small, but MCP loading in D4 stretches it beyond pure registry duties.

### D4 — MCP discoveries are normalized into ordinary tools in the registry layer

The MCP-facing manager signatures are:

```python
async def ActionManager.register_mcp_server(
    self,
    server_config: dict[str, Any],
    tool_names: list[str] | None = None,
    request_options: dict[str, type] | None = None,
    update: bool = False,
    security: MCPSecurityConfig | None = None,
) -> list[str]: ...

async def ActionManager.load_mcp_config(
    self,
    config_path: str,
    server_names: list[str] | None = None,
    update: bool = False,
    mcp_security: MCPSecurityConfig | None = None,
) -> dict[str, list[str]]: ...

async def load_mcp_tools(
    config_path: str | None = None,
    server_names: list[str] | None = None,
    request_options_map: dict[str, dict[str, type]] | None = None,
    update: bool = False,
    mcp_security: MCPSecurityConfig | None = None,
) -> list[Tool]: ...
```

The service-layer security dataclass and pool state are:

```python
# lionagi/service/connections/mcp_wrapper.py
@dataclass(frozen=True)
class MCPSecurityConfig:
    allow_commands: bool = False
    command_allowlist: frozenset[str] | None = None
    allow_urls: bool = False
    url_allowlist: frozenset[str] | None = None
    env_denylist_patterns: frozenset[str] = <sensitive-name defaults>
    filter_sensitive_env: bool = True
    max_connections_per_server: int = 5

class MCPConnectionPool:
    _clients: dict[str, Any] = {}
    _configs: dict[str, dict] = {}
    _security: MCPSecurityConfig | None = None
    _server_security: dict[str, MCPSecurityConfig] = {}

    @classmethod
    async def get_client(
        cls,
        server_config: dict[str, Any],
        security: MCPSecurityConfig | None = None,
    ) -> Any: ...

    @classmethod
    async def cleanup(cls): ...
```

Auto-discovery projects the remote schema into the same descriptor form as a local
tool:

```json
{
  "type": "function",
  "function": {
    "name": "remote_tool_name",
    "description": "remote description or null",
    "parameters": { "...": "remote inputSchema copied verbatim" }
  }
}
```

**Exact semantics**

- **Explicit tool names:** the manager creates a proxy without calling
  `list_tools()`. `_original_tool_name` is placed in the config. If no request model or
  schema is supplied, schema generation sees the proxy's `**kwargs` signature. This loop
  has no per-tool exception isolation: the first construction or duplicate-registration
  error aborts the method and later explicit names are not attempted.
- **Request-model lookup:** before either explicit registration or discovery, the
  manager mutates each `request_options` key that does not start with
  `"<server_name>_"` by adding that prefix. It later looks up the model by the actual
  `tool_name`/`tool.name`, without applying the same prefix. An ordinary unqualified
  remote name therefore misses an unqualified caller mapping after it is renamed. A
  mapping attaches only when the remote name already equals the retained or rewritten
  key; the manager performs no alias reconciliation.
- **Discovery:** with no `tool_names`, the manager obtains a pooled client, awaits
  `client.list_tools()`, and creates one `Tool` per returned item. A dictionary
  `inputSchema` is copied verbatim into `function.parameters`. Client acquisition or
  `list_tools()` failure occurs before the per-tool loop and propagates from
  `register_mcp_server()`.
- **Discovery degradation:** an exception while reading one remote schema logs a warning
  and falls back to proxy signature derivation. An absent, `None`, or non-dictionary
  `inputSchema` falls back without that warning. Failure to construct or register one
  tool logs a warning and continues discovering siblings.
- **Names:** discovered descriptors are registered under `tool.name`, unqualified by
  server. `_original_tool_name` preserves the remote call target but does not prevent a
  registry collision.
- **Proxy call:** the proxy removes underscore-prefixed metadata before acquiring the
  client, then calls `client.call_tool(actual_tool_name, kwargs)`.
- **Proxy response:** a single text content item is unwrapped to its text; other
  `result.content` values are returned as content; a one-item list containing a text
  dictionary is also unwrapped; all other results pass through.
- **Direct pool trust:** command and URL transports are denied by default. Commands
  require `allow_commands=True`; an allowlist, when present, accepts bare command names
  only. URLs require `allow_urls=True`, an `https` or `wss` scheme, and an optional host
  allowlist.
- **Loader trust:** `load_mcp_config()` and top-level `load_mcp_tools()` leave an
  omitted policy unset and thread it through to registration unchanged. In a process
  where that transport has not already been authorized, it reaches the pool's own
  fail-closed default and a command or URL transport is denied exactly as it is under
  direct pool use. A caller that wants both transport classes allowed passes
  `MCPSecurityConfig.trusted()` explicitly, and that choice is threaded per load without
  mutating the process-global default. A transport `PermissionError` is logged and
  re-raised; other server failures become an empty registered-name list or a warning.
- **Policy recovery is process-scoped and proxy-only.** An explicit policy is
  remembered against the resolved transport so the proxy's stored callable can recover
  the same authorization on a later reconnect with no policy of its own. That recovery
  is keyed by the same policy identity described under Policy reuse — server name plus
  resolved transport content for a named server, content alone for an inline config —
  but it is reachable only through `MCPConnectionPool._get_reconnect_client()`, a
  private method whose only caller is the proxy built by `create_mcp_tool`. The public
  `get_client()` accepts only an explicit `MCPSecurityConfig` or `None` for its
  `security` argument (anything else raises `TypeError`) and never recovers a
  remembered policy, so a fresh loader call that omits a policy is denied even when an
  earlier caller already authorized the identical resolved transport.
- **Loader input failure:** config-file existence, JSON shape, and parsing errors occur
  before the per-server recovery loop and propagate. Top-level `load_mcp_tools()` also
  raises `ValueError` when neither `server_names` nor a config path supplies a server
  set.
- **Policy reuse:** an explicit policy is remembered so the proxy's later `get_client()`
  call can recover the same authorization. For a named server the key binds the server's
  name together with its resolved transport content, so an authorization granted for one
  named server is never recovered for a different one that happens to resolve to the same
  transport. An anonymous inline configuration has no name to bind, so its key is derived
  from content alone.
- **Pool identity:** a named config's cache identity contains the server name together
  with its resolved transport content, with `server:<name>` only its prefix, so a
  connected client is reused only when both match. Reloading a different command or URL
  under the same name yields a different identity and the stale client is dropped rather
  than reused. Inline configs use the command plus the Python dictionary's
  object identity, so reuse occurs only when the same dictionary object is passed again;
  the MCP proxy creates a fresh metadata-stripped dictionary for each call and has no
  stable inline reuse key. A disconnected cached client is dropped, and `cleanup()`
  attempts every client exit before clearing the map.
- **Environment:** command transport starts with the parent environment plus configured
  values, removes variables whose names contain sensitive patterns by default, and adds
  quiet logging defaults unless debug mode is active.
- **Declared connection cap:** `max_connections_per_server` defaults to `5`, but the
  current pool stores one client per cache key and never reads that field. The value is
  inherited configuration with no enforced budget or recorded rationale in this path.

**Why this way**

Normalizing remote calls to `Tool` lets every downstream resolver and provider-schema
path ignore transport. Lazy imports keep the foundational action modules importable
without FastMCP installed. The tradeoff is architectural: the protocol registry knows
about service configuration, transport trust, discovery, and global pool lifecycle, and
unqualified names make collision handling a branch-registration concern.

## Consequences

- Local and remote callables share one provider descriptor and one branch resolver.
- Excluding executable objects does not make a serialized descriptor public-safe:
  `mcp_config` remains visible and may carry transport or environment configuration.
- Branches can expose different tool sets while reusing descriptor objects and
  callables.
- Pydantic-backed tools get provider schema, normalization, constraints, and keyword
  payloads from one request model.
- Raw callable registration is concise, but provider-required keys can disagree with
  Python defaults and non-strict runtime validation.
- Custom schemas that omit `required` remain usable until strict validation asks
  `Tool.required_fields`; that access can fail with `KeyError`.
- Registry lookup is deterministic by function name, but MCP dictionary registration
  has a duplicate-check hole and remote unqualified names can collide.
- MCP per-tool request-model selection is key-fragile: the manager can rename a caller's
  mapping key and then miss it, silently falling back to the proxy's raw `**kwargs`
  schema rather than the intended typed model.
- Reversing D1 requires a new code-address or factory identity contract; serialized
  callables cannot be made restorable by changing `from_dict()` alone.
- Reversing D3 requires changing the provider-selection boundary because Branch,
  `operate()`, and model request construction consume manager-local schema sets.
- Contributors adding a maintained tool must decide whether it is a raw convenience
  callable or publish a Pydantic request model; only the latter aligns the advertised
  and normalized contracts reliably.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Version and narrow raw-callable schema derivation so Python defaults remain optional, positional-only and `*args` signatures require an explicit adapter, open `**kwargs` callables require an explicit schema, schemas without `required` are accepted, and tests prove provider schema and runtime validation agree. | M | (filled at issue-open time) |
| 2 | Move MCP configuration, discovery, namespacing, and pool lifecycle into a service-owned factory that returns ready `Tool` descriptors; acceptance requires `protocols.action` to have no service-layer import, remote identities to be collision-free, and per-tool request models to resolve by that canonical identity without key mutation or silent fallback. | M | (filled at issue-open time) |
| 3 | Require the MCP-loading caller to make an explicit transport-trust decision; acceptance requires omitted policy to preserve the wrapper's fail-closed command and URL defaults and an explicit trusted-config mode to be observable. | S | delivered — an omitted policy is denied on every load, not only the first, because policy recovery is reachable only through the proxy's private reconnect path and never through the loader-facing `get_client()` call (see Policy recovery above) |

## Alternatives considered

### A. Serialize and restore arbitrary callables

This would have made `Tool` a conventional persisted `Element` and allowed branch state
to reconstruct tools from data alone. It lost because closures, bound methods, imported
code versions, and security-sensitive execution context have no stable data-only
representation. The shipped `from_dict()` rejection makes the limitation explicit.

### B. Split declaration and executable into unrelated objects

A pure `FunctionSchema` registry plus a separate callable map would make the persistence
boundary cleaner. It lost for the local-first implementation because every registration,
replacement, and invocation would need to keep two name-indexed stores synchronized.
`Tool` instead keeps the schema adjacent to the callable while excluding live fields
from serialization.

### C. Require Pydantic request models for every tool

This would eliminate the raw schema/default mismatch and give every tool a rich
validation contract. It lost because registration of small ordinary callables is a core
convenience and existing tests rely on it. The system adopted Pydantic as the maintained
built-in path without removing the raw adapter.

### D. Treat Python signature inspection as the only contract

This would avoid request-model duplication and derive provider schema and validation
from a callable alone. It lost because Python annotations do not express the same
constraints, descriptions, discriminated choices, and JSON Schema details as Pydantic
models, and positional or variadic signatures do not map cleanly to provider keyword
calls.

### E. Use a process-global tool registry

A global catalog would reduce per-branch setup and avoid copying registry entries. It
lost because model-visible capability selection is branch-specific; a shared global map
would make isolation and per-branch replacement indirect policy rather than explicit
state.

### F. Qualify every MCP name as `server.tool`

Qualification would prevent remote/local collisions and preserve source identity in the
registry. It lost in the shipped path because discovery copied server-provided schemas
and names directly into the existing function interface. The collision cost is now
recorded as a delta rather than hidden.

### G. Keep all MCP construction in the connection service

A service factory returning ready `Tool` descriptors would preserve the dependency
direction and centralize transport policy and pool lifecycle. It was not the organic
shape: MCP support was added at the registry's existing normalization point, using lazy
imports to soften the dependency. Delta 2 retains the service-factory design for a
future correction.

### H. Treat loading a config file as an implicit trust act

The original decision let the explicit config loaders replace an omitted policy with one
allowing both transport classes, on the reasoning that selecting and loading a config
file was itself a trust decision. That convenience created a semantic split: the same
omitted policy meant "deny" through the pool and "allow" through a loader, and nothing
in the calling code made the difference visible. Delta 3 replaced it with the behavior
now described under Loader trust, where an omitted policy denies through every loader
call, first or later, and trust is a named, observable choice. The remembered-policy
recovery described above is scoped to the proxy's own reconnect and is not reachable
through a loader call at all, so the original convenience does not survive even in
narrower form.

## Notes

Correcting the all-parameters-required raw schema is a compatibility change because
tests and provider payloads preserve that behavior. It cannot be shipped as an
unversioned cleanup.
