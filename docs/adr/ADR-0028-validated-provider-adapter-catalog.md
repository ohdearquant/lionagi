# ADR-0028: Validated provider-adapter catalog

- **Status**: Proposed
- **Kind**: Aspirational
- **Area**: service-providers
- **Date**: 2026-07-09
- **Relations**: extends ADR-0027

## Context

ADR-0027 records the shipped resolver as a useful single authority with unvalidated and permissive
inputs. This ADR defines the target catalog without changing that authority.

**P1 — Positional declarations hide field meaning.** Provider authors currently declare endpoints
through enum tuples with four to seven positions. Index 3 may be a deferred `LazyType` that imports
the module being registered. Type checking can confirm that a value is a tuple but cannot name the
slots or prove catalog-wide uniqueness
(`lionagi/service/connections/provider_config.py`; `ProviderConfig`, `LazyType`).

**P2 — Configuration typos can become request data.** `EndpointConfig` moves every unknown
constructor key into `kwargs`, and `Endpoint.create_payload()` merges those values into provider
payload defaults. A misspelled endpoint-control field is therefore accepted at configuration time
and may cross the provider boundary
(`lionagi/service/connections/endpoint_config.py`; `EndpointConfig._validate_kwargs`, and
`lionagi/service/connections/endpoint.py`; `Endpoint.create_payload`).

**P3 — Discovery is central but cannot explain failure.** The registry owns a fixed module list,
suppresses every `ImportError`, appends entries in import order, and selects the first canonical key
or alias that matches. It does not reject a canonical key repeated as its own alias, collisions
between registrations, inconsistent provider aliases, or case variants that later normalize to the
same `EndpointConfig.provider`
(`lionagi/service/connections/registry.py`; `EndpointRegistry`, `_import_all_providers`).

**P4 — Compatible fallback is useful but error-shaped.** An unknown provider, unknown endpoint,
failed provider import, and intentional custom OpenAI-compatible service all reach the same generic
fallback. The resulting config retains `openai_compatible=False`, so inspection cannot distinguish
intent from accident.

**P5 — Provider identity has two owners.** Endpoint metadata lives with provider packages, while
model aliases, effort translation, fast-mode, bypass, and safety kwargs live in
`lionagi/service/providers.py`. Adding a provider can require coordinated edits on both sides of the
generic/provider boundary.

| Concern | Decision |
|---------|----------|
| Provider authoring | D1: provider endpoints are declared as named, immutable `ProviderEndpointSpec` records and request defaults are explicit. |
| Key identity and validation | D2: provider, endpoint, and alias keys are canonicalized and the complete key space is validated before snapshot publication. |
| Bootstrap and availability | D3: bundled inventory is provider-owned; import failures are retained as typed diagnostics while unrelated valid providers remain usable. |
| Lookup and compatible fallback | D4: registered lookup is fail-closed; generic OpenAI-compatible selection requires an explicit flag and produces a marked config. |
| Inspection and model policy | D5: inspection and `parse_model_spec()` read the same immutable catalog snapshot, with vendor policy declared beside provider inventory. |

This ADR deliberately does **not** decide:

- Dynamic package entry-point discovery or a general plugin lifecycle. Such inventory may feed the
  same validation API later; it must not create a second resolver.
- Request admission, deadlines, retries, or circuit policy; ADR-0029 owns execution lifecycle.
- Agentic chunk, session, and cleanup conformance; ADR-0030 owns adapter behavior after selection.
- Provider-specific request fields, command flags, event parsers, model defaults, or safety policy
  values. The catalog locates those vendor-owned contracts but does not standardize them.
- Automatic hyphen/underscore or punctuation rewriting. Only case and surrounding whitespace are
  canonicalized; spelling variants remain explicit aliases.

## Decision

### D1 — Provider authors publish named endpoint specifications

Replace enum tuple authoring with an immutable record under the provider package. The catalog
materializes the existing `EndpointMeta` from this record and binds it to the concrete class. The
target contract is:

```python
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

RequestOptionsFactory = Callable[[], type[BaseModel]]

@dataclass(frozen=True, slots=True)
class ProviderEndpointSpec:
    provider: str
    endpoint: str
    endpoint_type: EndpointType
    endpoint_class: type[Endpoint]
    provider_aliases: tuple[str, ...] = ()
    endpoint_aliases: tuple[str, ...] = ()
    request_options: type[BaseModel] | RequestOptionsFactory | None = None
    base_url: str | None = None
    auth_type: str | None = None
    content_type: str = "application/json"
    api_key_env: str | None = None
    request_defaults: Mapping[str, Any] = field(default_factory=dict)

@dataclass(frozen=True, slots=True)
class ProviderModuleSpec:
    provider: str
    module: str
```

`request_defaults` is the only catalog field whose contents become provider payload defaults.
Unknown endpoint-control kwargs no longer acquire that meaning implicitly.

The compatibility construction surface remains:

```python
def match_endpoint(
    provider: str,
    endpoint: str,
    *,
    openai_compatible: bool = False,
    request_defaults: Mapping[str, Any] | None = None,
    **config_overrides: Any,
) -> Endpoint: ...
```

`config_overrides` accepts only declared `EndpointConfig.model_fields`. `request_defaults` accepts
only fields in the selected request model when one exists; for a deliberately schema-less compatible
endpoint it accepts arbitrary JSON-serializable values. `iModel` remains a compatibility facade:
it classifies its legacy `**kwargs` against endpoint-config fields and the selected request model,
warns during the deprecation window when an unclassified value would previously have entered
`EndpointConfig.kwargs`, and then requires the caller to use `request_defaults` explicitly.

**Exact semantics**

- **Named fields.** Every author-supplied value is named. A provider module may construct specs
  directly or use a provider-local helper, but positional tuple interpretation is not a catalog API.
- **Deferred request model.** A request-options factory is called exactly once while building a
  snapshot. The result must be a `BaseModel` subclass. Factory failure becomes a validation
  diagnostic and prevents snapshot publication; lookup never triggers a new import.
- **Endpoint class.** `endpoint_class` must subclass `Endpoint`. An API spec must not name an
  `AgenticEndpoint` subclass. An agentic spec must name an `AgenticEndpoint` subclass.
- **Defaults.** `request_defaults` is copied into the immutable snapshot. Mutable caller mappings
  are not retained by reference. Each endpoint instance receives a fresh dict.
- **Config typo.** An unknown `config_overrides` key raises `EndpointConfigurationError` before
  endpoint construction. It is never silently moved into request data.
- **Request typo.** When a request model exists, an unknown request-default key raises
  `EndpointConfigurationError` during catalog build or explicit call configuration. Runtime request
  validation remains the provider model's responsibility.
- **Existing metadata.** `_ENDPOINT_META` remains attached to each endpoint class for compatibility,
  but it is derived from the validated snapshot rather than being the source of truth.

**Why this way.** Named immutable records make declarations reviewable and allow static tools to
check field types. Keeping `EndpointMeta` as the class-bound projection avoids an unnecessary change
to endpoint construction. Separating request defaults from endpoint control closes the current typo
channel without forcing provider payload fields into generic service configuration.

### D2 — Canonicalize keys and validate the complete catalog atomically

One key function is used by registration, lookup, inspection, diagnostics, and model-spec policy:

```python
def canonical_provider_key(value: str) -> str:
    return value.strip().casefold()

def canonical_endpoint_key(value: str) -> str:
    return value.strip().casefold()
```

No other punctuation transformation occurs. `claude-code` and `claude_code`, or `findSimilar` and
`find_similar`, are equivalent only when both are explicitly declared through canonical name plus
alias.

Validation reports stable typed diagnostics:

```python
CatalogDiagnosticCode = Literal[
    "empty_key",
    "self_alias",
    "duplicate_provider_key",
    "duplicate_endpoint_key",
    "inconsistent_provider_aliases",
    "invalid_endpoint_class",
    "invalid_request_options",
    "invalid_request_default",
    "import_error",
]

@dataclass(frozen=True, slots=True)
class ProviderDiagnostic:
    code: CatalogDiagnosticCode
    provider: str
    endpoint: str | None
    module: str | None
    key: str | None
    message: str
    cause_type: str | None = None

class ProviderCatalogError(RuntimeError): ...

class CatalogValidationError(ProviderCatalogError):
    diagnostics: tuple[ProviderDiagnostic, ...]

class ProviderUnavailable(ProviderCatalogError):
    provider: str
    diagnostic: ProviderDiagnostic

class EndpointNotFound(ProviderCatalogError):
    provider: str
    endpoint: str
    available: tuple[str, ...]

class EndpointConfigurationError(ProviderCatalogError):
    field: str
```

**Exact semantics**

- **Empty values.** A canonical provider or endpoint key that becomes empty is invalid. Empty
  aliases are invalid rather than ignored.
- **Self alias.** An alias canonicalizing to its own canonical key is invalid. It is not silently
  deduplicated because the redundant declaration usually signals author confusion.
- **Provider scope.** Canonical provider names and aliases share one global key space. One key may
  identify exactly one canonical provider.
- **Endpoint scope.** Canonical endpoint names and aliases share one key space within a canonical
  provider. The same endpoint spelling may be used by different providers.
- **Repeated provider declarations.** Multiple endpoint specs for one provider must declare the
  same canonical provider-alias set. Disagreement is an error; registration order does not choose a
  winner.
- **Duplicate class.** The same endpoint class may appear only once in a snapshot. Reusing a class
  for another key requires an explicit new subclass so its class-bound metadata is unambiguous.
- **Validation order.** All modules are collected, then every key and metadata record is validated.
  Indexes are built only if the successfully imported specification set has no validation error.
- **Atomic publication.** A valid `CatalogSnapshot` replaces the prior snapshot in one assignment.
  If a refresh fails validation, the prior valid snapshot remains active. On first build, a
  validation failure leaves no resolvable catalog and raises `CatalogValidationError`.
- **No first-match behavior.** A valid snapshot contains no ambiguous keys, so resolution is a map
  lookup and cannot depend on import or registration order.
- **Restart.** A process restart rebuilds and revalidates the snapshot. No catalog state is persisted.

**Why this way.** Case-folding aligns resolver identity with `EndpointConfig.provider` and accepts
case variants without multiplying aliases. Refusing punctuation rewriting keeps public spellings
auditable. Whole-set validation is necessary because uniqueness is a property of the catalog, not
of one decorator call; an incremental last-writer-wins map would preserve the current order bug.

### D3 — Provider-owned bootstrap records availability and import diagnostics

The bundled module inventory moves under `lionagi/providers/` and is passed through one bootstrap
boundary. Generic service code receives an iterable of `ProviderModuleSpec`; it no longer names
every bundled module.

```text
lionagi/providers/
├── _catalog.py                  bundled ProviderModuleSpec inventory
├── <vendor>/_config.py          ProviderEndpointSpec records
└── <vendor>/<endpoint>.py       concrete endpoint classes and request models

lionagi/service/connections/
├── catalog.py                   validation + immutable CatalogSnapshot
├── registry.py                  EndpointRegistry facade over snapshot
└── match_endpoint.py            public compatibility facade
```

**Exact semantics**

- **Import unit.** Each inventory row names the canonical provider expected from that module. The
  loader imports every row and collects its specs before validation.
- **Failed import.** Any `Exception` raised while importing a bundled module is captured as an
  `import_error` diagnostic with provider, module, exception type, and sanitized message.
  `BaseException` control signals are never swallowed.
- **Partial availability.** Import failure marks that inventory provider unavailable but does not
  prevent unrelated successfully imported providers from being validated and published.
- **Selection of failed provider.** A canonical provider key known from inventory but lacking a
  loaded spec raises `ProviderUnavailable`; it does not fall through to `EndpointNotFound` or a
  compatible endpoint.
- **Validation failure.** Invalid metadata or collisions among successfully imported specs are not
  partial failures. They prevent snapshot publication because serving a subset could make an alias
  silently change owner.
- **Repeated lookup.** A failed provider is not re-imported per request. Availability is fixed for
  the snapshot. A deliberate catalog refresh or process restart performs another import attempt.
- **Unknown provider.** A provider absent from both inventory and indexes is `EndpointNotFound`, not
  `ProviderUnavailable`.
- **Thread safety.** Lazy first-build remains lock-protected. Lookups after publication are immutable
  snapshot reads and need no registry mutation.

**Why this way.** Provider-owned inventory removes the reverse list dependency from generic service
code while keeping deterministic bundled discovery. Capturing imports lets inspection distinguish
"not registered" from "registered package unavailable." Import failures are isolated because they
do not create key ambiguity; validation collisions are global and therefore fail publication.

### D4 — Registered resolution fails closed; compatible fallback is explicit

Resolution uses the validated snapshot first. The target lookup state machine is:

```text
canonicalize provider
        |
        +-- known unavailable --------------------> ProviderUnavailable
        |
        +-- unknown -------------------------------+-- openai_compatible=False
        |                                          |      -> EndpointNotFound
        |                                          +-- openai_compatible=True
        |                                                 -> marked generic endpoint
        v
resolve canonical provider
        |
        +-- endpoint key matches -----------------> instantiate registered class
        |
        +-- endpoint empty + exactly one endpoint -> instantiate that endpoint
        |
        +-- miss ----------------------------------+-- openai_compatible=False
                                                   |      -> EndpointNotFound
                                                   +-- openai_compatible=True
                                                          -> marked generic endpoint
```

The compatible construction result is explicit:

```python
EndpointConfig(
    name="openai_compatible_chat",
    provider=canonical_provider,
    base_url=required_base_url,
    endpoint=endpoint or "chat/completions",
    method="POST",
    auth_type="bearer",
    content_type="application/json",
    request_options=OpenAIChatCompletionsRequest,
    openai_compatible=True,
    requires_tokens=True,
    kwargs=dict(request_defaults or {}),
)
```

**Exact semantics**

- **Known match wins.** If provider and endpoint resolve to a registered entry, that entry is used
  even when `openai_compatible=True`; the flag authorizes fallback, not replacement of a valid
  registration.
- **Multiple endpoints with empty selection.** Empty endpoint is accepted only for a provider with
  exactly one endpoint. A provider with multiple endpoints raises `EndpointNotFound` with sorted
  canonical endpoint keys. There is no first-entry default hidden in registration order.
- **Known provider, unknown endpoint.** The miss raises unless compatible fallback was explicitly
  authorized. The old single-endpoint "accept any string" behavior is removed through the same
  staged deprecation as implicit fallback.
- **Compatible URL.** Explicit compatible construction requires a non-empty `base_url`; absence is
  `EndpointConfigurationError("base_url")`. The generic service does not guess a vendor URL.
- **Failed bundled provider.** `openai_compatible=True` does not mask `ProviderUnavailable` for a
  provider key owned by bundled inventory. A caller intending a separate compatible service must use
  a distinct provider key.
- **Diagnostics.** `EndpointNotFound` carries canonical provider, requested canonical endpoint, and
  sorted available canonical endpoint keys. It does not include secrets or request defaults.
- **Migration.** The first compatibility release preserves implicit fallback with a deprecation
  warning naming `openai_compatible=True` and the explicit `base_url` requirement. After the
  repository deprecation window, implicit fallback raises. Public imports and `match_endpoint()`
  remain available throughout.

**Why this way.** Custom compatible services remain supported, but intent becomes observable in
configuration and inspection. Registered lookup fails closed because a typo should not select a
different transport. Requiring a URL prevents a marked compatible endpoint from deferring an
obvious configuration error until transport construction.

### D5 — Inspection and provider model policy share the snapshot

Inspection returns a stable typed dict projection of the same snapshot used for lookup:

```python
class EndpointCatalogItem(TypedDict):
    endpoint: str
    aliases: list[str]
    endpoint_type: Literal["api", "agentic"]
    endpoint_class: str
    request_options: str | None
    base_url: str | None

class ProviderCatalogItem(TypedDict):
    provider: str
    aliases: list[str]
    availability: Literal["available", "unavailable"]
    endpoints: list[EndpointCatalogItem]
    diagnostics: list[dict[str, str | None]]

class EndpointRegistry:
    @classmethod
    def list_providers(cls) -> list[ProviderCatalogItem]: ...
```

Rows and nested endpoints are sorted by canonical key. Diagnostics come from the same snapshot and
are sorted by `(provider, endpoint or "", code, key or "")`. No API key, request default, or secret
value is exposed.

Vendor model identity moves beside provider inventory:

```python
EffortMode = Literal["none", "kwarg", "model_name"]

@dataclass(frozen=True, slots=True)
class ProviderModelPolicy:
    provider: str
    provider_aliases: tuple[str, ...] = ()
    model_aliases: Mapping[str, str] = field(default_factory=dict)
    effort_mode: EffortMode = "none"
    effort_kwarg: str | None = None
    effort_levels: tuple[str, ...] = ()
    yolo_kwargs: Mapping[str, Any] = field(default_factory=dict)
    bypass_kwargs: Mapping[str, Any] = field(default_factory=dict)
    fast_kwargs: Mapping[str, Any] = field(default_factory=dict)

def parse_model_spec(spec: str) -> ModelSpec: ...
```

**Exact semantics**

- The provider key and aliases in `ProviderModelPolicy` must resolve to the same catalog provider.
  Conflicts are catalog validation errors.
- Model aliases are canonicalized by surrounding whitespace and case only for lookup; the expanded
  provider/model string retains the provider package's declared spelling.
- Effort policy has exactly one mode. `kwarg` requires `effort_kwarg`; `model_name` forbids it;
  `none` rejects an effort suffix as current `parse_model_spec()` does for providers without effort.
- `parse_model_spec()` retains its public import path and returns the existing frozen
  `ModelSpec(model: str, effort: str | None)`. It delegates provider aliases, model aliases, and
  effort classification to the snapshot.
- Existing aliases and normalization are parity-tested before the service-owned tables are removed.
  The move changes ownership, not accepted public spellings.
- Inspection of an unavailable provider includes its import diagnostic and an empty endpoint list.
  Inspection of a valid provider contains no historical diagnostics from prior snapshots.

**Why this way.** Lookup, inspection, and model parsing must not answer provider identity from
different tables. Provider-local policy keeps vendor additions local; the registry remains the
validator and projection point. Keeping the `parse_model_spec()` facade prevents a package move from
becoming an unrelated public API break.

## Consequences

- Provider metadata becomes reviewable by field name. Every lookup either selects exactly one
  validated entry, raises a typed failure, or follows an explicit compatible path.
- Catalog construction becomes a startup or refresh gate. A collision stops publication instead of
  letting import order decide. This makes adapter defects visible earlier at the cost of stricter
  startup.
- A failed optional provider no longer disables unrelated providers and no longer disappears
  silently. The unavailable row remains inspectable with a sanitized cause.
- Existing callers relying on implicit fallback, arbitrary endpoint strings for single-endpoint
  providers, or unknown config keys receive a staged warning and then a typed failure. Compatibility
  work is concentrated in `match_endpoint()` and `iModel`, not spread through adapters.
- Custom OpenAI-compatible services remain supported. They must state intent, URL, and request
  defaults; their config carries `openai_compatible=True`.
- Provider additions stop requiring a generic bootstrap edit and, after policy migration, stop
  requiring changes to `service/providers.py`.
- Reversing D1/D2 is medium cost because provider declarations must be rewritten, but the resolver
  facade remains stable. Reversing D4 after warnings ship is high cost because callers will rely on
  typed failure. Adding a future inventory source is low cost if it emits the same two spec types.
- Maintainers must treat snapshot validation as a closed-world operation: local registration success
  is insufficient until global provider and endpoint key spaces validate.

## Alternatives considered

### Keep positional `ProviderConfig` tuples

This preserves compact declarations and avoids a migration. It lost because slot meaning remains
implicit, factories and types cannot be validated by field name, and tuple-local checks cannot prove
catalog-wide uniqueness. The current four-to-seven-slot shape is exactly the ambiguity this ADR is
removing.

### Incrementally validate decorators as modules import

Each decorator could reject collisions against entries already seen. That would catch some errors
with little new machinery. It lost because the first imported registration would still win the
diagnostic framing, inconsistent provider-alias sets are only visible across multiple modules, and a
partially mutated registry would remain after a later failure. Atomic snapshots make serving state
all-valid or previously-valid.

### Last-writer-wins maps

A dictionary assignment could make lookup fast and deterministic within one import order. It would
also make overrides easy. It lost because changing module order would change the selected adapter,
and inspection could not reveal the displaced definition. Duplicate public identity is a catalog
error, not an extension mechanism.

### Automatic punctuation normalization

The resolver could treat hyphens, underscores, dots, and slashes as interchangeable. This would
reduce explicit aliases. It lost because punctuation is meaningful in endpoint paths and public
provider spellings; broad rewriting can collapse distinct keys. Explicit aliases are more verbose
but auditable.

### Remove generic compatible fallback entirely

Strict registered-only lookup would make every miss obvious and simplify selection. It lost because
the existing fallback supports custom OpenAI-compatible services without a dedicated adapter. The
problem is implicit intent, not compatibility itself; the explicit flag and marked config retain the
valid use case.

### Immediate dynamic entry-point discovery

Package entry points could remove the fixed bundled list and enable external adapters at once. They
would also require package precedence, refresh, trust, diagnostics, and conflict rules. It lost for
this decision because provider-owned bundled inventory plus deterministic validation solves the
present dependency and observability defects. A future entry-point source must emit the same specs
and pass the same validator.

### Keep vendor policy tables in generic service code

This avoids moving `BACKENDS`, effort, bypass, and fast-mode mappings and keeps one familiar module.
It lost because it preserves a second provider identity authority. Parity tests and the retained
`parse_model_spec()` facade make ownership movement safer than continued split truth.

### Add a separate executor-provider registry

An executor catalog could model HTTP, subprocess, in-process, and remote execution separately. It
lost because `EndpointRegistry` already resolves concrete endpoint families and ADR-0029 places
admission behind `iModel`. A second registry would duplicate provider keys, aliases, availability,
and diagnostics without removing the endpoint catalog.

## Notes

This is a target-state ADR. It does not claim that `ProviderEndpointSpec`, atomic snapshots, typed
catalog errors, or explicit fallback are shipped. The existing source contracts that constrain the
migration are `lionagi/service/connections/{provider_config,registry,endpoint_config,endpoint,match_endpoint}.py`,
the provider declarations in `lionagi/providers/*/_config.py`, and the compatibility tables and
`ModelSpec` in `lionagi/service/providers.py`.
