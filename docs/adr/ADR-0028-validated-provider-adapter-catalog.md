# ADR-0028: Validated provider-adapter catalog

- **Status**: Proposed
- **Kind**: Aspirational
- **Area**: service-providers
- **Date**: 2026-07-09
- **Relations**: extends ADR-0027

## Context

The resolver already creates typed `EndpointMeta` records, but provider authors declare them
indirectly through positional enum tuples with four to seven slots. The representation is compact,
yet field meaning depends on index position and a deferred type may import the module currently
being registered. Unknown `EndpointConfig` constructor keys are also retained as request defaults,
so a misspelled configuration field can cross the boundary as payload data
(`lionagi/service/connections/provider_config.py`,
`lionagi/service/connections/endpoint_config.py`).

Registration is local to an adapter, but discovery is central and unvalidated. The generic registry
contains every bundled provider module name, suppresses all `ImportError`, and resolves the first
matching canonical name or alias. It does not reject a canonical key repeated as its own alias,
collisions across registrations, or case variants that fall through to the generic endpoint
(`lionagi/service/connections/registry.py`).

The fallback is useful for custom OpenAI-compatible services, but it is currently implicit. The
resulting `EndpointConfig` does not mark `openai_compatible=True`, and the same branch handles an
unknown provider, an unknown endpoint, and a provider that failed to import. Catalog inspection
therefore cannot explain whether an adapter is absent, invalid, unavailable, or deliberately
generic.

Provider identity policy is split as well. Endpoint metadata lives with provider packages, while
CLI aliases, effort translation, and bypass settings live in `lionagi/service/providers.py`. That
makes generic service code a second source of vendor truth and requires vendor additions to touch
both sides of the package boundary.

## Decision

Retain `EndpointRegistry` as the sole resolver and make its inputs a validated catalog. Provider
authors declare a named record rather than a positional tuple; registration materializes the
existing `EndpointMeta` shape and associates it with the concrete endpoint class.

```python
@dataclass(frozen=True, slots=True)
class ProviderEndpointSpec:
    provider: str
    endpoint: str
    endpoint_type: EndpointType
    endpoint_class: type[Endpoint]
    provider_aliases: tuple[str, ...] = ()
    endpoint_aliases: tuple[str, ...] = ()
    request_options: type[BaseModel] | Callable[[], type[BaseModel]] | None = None
    base_url: str | None = None
    auth_type: str | None = None
    content_type: str = "application/json"
    api_key_env: str | None = None
```

The catalog contract has these invariants:

- Provider and endpoint identifiers are trimmed and case-folded before indexing. Punctuation is not
  rewritten; hyphen and underscore variants require declared aliases. Canonical names and aliases
  are unique in their scope, and a redundant self-alias is invalid.
- Catalog construction validates the complete key space before serving a lookup. Duplicate keys,
  invalid metadata, and unresolved request-option types produce typed diagnostics and cannot win by
  registration order.
- Bundled provider inventory moves to the provider package and is passed into the registry through
  one bootstrap boundary. A failed bundled import records provider, module, and cause. Other valid
  providers remain usable, while selection of the failed provider raises `ProviderUnavailable`.
- Registered lookup is fail-closed. An absent provider or endpoint raises `EndpointNotFound` with
  available keys. Generic OpenAI-compatible construction occurs only when the caller passes an
  explicit `openai_compatible=True` selection mode, and the resulting configuration carries that
  value.
- The transition first warns on implicit fallback and identifies the explicit replacement, then
  removes implicit fallback under the repository deprecation policy. Existing public imports and
  `match_endpoint()` remain compatibility façades during the transition.
- `list_providers()` reports canonical identifiers, aliases, endpoint type, request schema,
  availability, and diagnostics from the same catalog snapshot used for resolution.
- Provider-specific identity, effort, fast-mode, bypass, and safety policy moves beside the provider
  catalog declaration. `parse_model_spec()` retains its public import path and delegates to that
  catalog so existing aliases and normalization behavior do not change accidentally.

Bundled declarative inventory is the required discovery mechanism for this decision. Third-party
entry-point discovery may feed the same validation API later, but this ADR does not require a plugin
system or a second resolver.

## Consequences

Provider metadata becomes reviewable by field name, and every lookup has deterministic resolution
or a typed failure. Custom OpenAI-compatible services remain supported through an explicit contract
rather than an error-shaped fallback. Catalog inspection can explain unavailable optional adapters
without disabling unrelated providers.

Catalog construction becomes a startup validation step, and previously tolerated aliases or
misspellings may fail. The staged fallback migration and compatibility façade add temporary code,
but they keep the behavioral change visible. Moving vendor policy out of service requires parity
tests because model-spec normalization is consumed outside the provider packages.

## Notes

Keeping positional tuples was rejected because static type checking cannot express slot meaning or
catalog-wide uniqueness. Immediate dynamic entry-point discovery was rejected because bundled
inventory and deterministic validation solve the present problem with less mechanism. A separate
executor-provider registry was rejected because it would duplicate the selection authority retained
by `EndpointRegistry`.
