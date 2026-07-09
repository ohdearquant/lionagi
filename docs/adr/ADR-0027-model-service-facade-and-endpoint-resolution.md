# ADR-0027: Model-service façade and endpoint resolution

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: service-providers
- **Date**: 2026-07-09
- **Relations**: none

## Context

`iModel` is the caller-facing service façade for API and agentic model providers. It derives a
provider from a `provider/model` specification when needed, resolves an endpoint, creates
`APICalling` events, owns a per-model `RateLimitedAPIExecutor`, applies service-call hooks, and
retains resumable provider session state. `iModelManager` is a named registry of these façades and
shuts them down concurrently with per-model isolation (`lionagi/service/imodel.py`,
`lionagi/service/manager.py`).

`EndpointRegistry` is the sole endpoint resolver. Provider modules decorate concrete endpoint
classes, and the decorator materializes an immutable, typed `EndpointMeta` record on each class.
Provider authors still supply most metadata through positional `ProviderConfig` enum tuples. On
first resolution, the registry imports a fixed module list, ignores `ImportError`, and selects the
first provider and endpoint key or alias that matches (`lionagi/service/connections/registry.py`,
`lionagi/service/connections/provider_config.py`).

`Endpoint` owns generic payload validation, authentication headers, SSRF checks, HTTP transport,
retry, circuit-breaking, and generic stream projection. `AgenticEndpoint` inherits the same
configuration and event boundary but prevents use of the HTTP helpers; concrete subprocess,
in-process, and remote-agent adapters provide their own call and stream implementations. Both
families are exposed through `APICalling`, while provider-specific schemas, request mappings, and
event grammars remain under `lionagi/providers/` (`lionagi/service/connections/endpoint.py`,
`lionagi/service/connections/agentic_endpoint.py`, `lionagi/service/connections/api_calling.py`).

Resolution is deliberately permissive. An unknown provider or endpoint becomes a generic endpoint
with an OpenAI-compatible request shape, and a single-endpoint provider accepts any endpoint name.
Provider matching itself is case-sensitive even though `EndpointConfig` later lowercases the stored
provider. Consequently a misspelling, a failed bundled-provider import, and an intentional custom
OpenAI-compatible provider can reach the same fallback path.

## Decision

The shipped service boundary consists of one `iModel` façade, one `EndpointRegistry`, two endpoint
families, and provider-owned adapters:

```text
Caller / Branch
      │
      v
   iModel ─────> RateLimitedAPIExecutor + service-call hooks
      │
      v
EndpointRegistry ──> EndpointMeta ──> Endpoint | AgenticEndpoint
                                      ^              ^
                                      │              │
                               API adapters    agentic adapters
                                      └──── lionagi/providers ────┘
```

The load-bearing invariants are:

- `iModel` remains the model-facing lifecycle façade. It owns endpoint selection, one executor,
  service-call hook attachment, and provider session state; callers do not select provider classes
  directly.
- `EndpointRegistry` remains the only provider-and-endpoint resolver. Decorator registration and
  the class-bound `EndpointMeta` record are the current registration mechanism; no parallel
  executor registry exists.
- `Endpoint` centralizes provider-neutral HTTP security, request validation, transport, and
  resilience. `AgenticEndpoint` is the non-HTTP specialization. Both execute as `APICalling` events
  and stream provider-neutral `StreamChunk` values.
- Provider adapters own vendor request schemas, defaults, payload adaptations, and event grammar.
  The generic endpoint base does not select a vendor, although the registry currently owns the
  fixed bootstrap list that imports bundled adapters.
- Service hooks observe the `APICalling` boundary. They do not make the provider registry or the
  service package a general event bus (see the hooks ADR on call-boundary hooks).
- The generic fallback, first-match alias resolution, positional provider declarations, and
  differing `invoke()` and `stream()` scheduling paths are recorded as current behavior, not
  endorsed as target contracts.

## Consequences

API, subprocess, in-process, and remote-agent providers share one public façade and event model.
Cross-provider HTTP security and resilience remain centralized, while vendors can override payload
or transport behavior without changing callers. The existing public `iModel`, `Endpoint`,
`AgenticEndpoint`, `APICalling`, and `StreamChunk` names remain stable.

The boundary is not fully dependency-inverted. Provider modules depend on service registration and
base classes, while the service registry imports a fixed list of provider modules at runtime.
Permissive fallback and first-match resolution also make catalog defects observable only after a
request is misrouted or a provider silently disappears.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Replace positional provider declarations with typed authoring records; canonicalize and validate every provider, endpoint, and alias key; report failed bundled imports; and require explicit opt-in for generic OpenAI-compatible fallback, with compatibility coverage for existing custom-provider callers. | M | (filled at issue-open time) |
| 2 | Route `invoke()` and `stream()` through one bounded admission lifecycle that applies request, token, and concurrency limits before provider work; propagate one deadline through queueing, retries, and transport; and prove that cancellation leaves no queued or active orphan. | L | (filled at issue-open time) |
| 3 | Apply retry and circuit policy to HTTP stream establishment before the first emitted chunk, prohibit automatic replay after output begins, and add tests for pre-first-byte failure, mid-stream failure, normal EOF, and caller cancellation. | M | (filled at issue-open time) |
| 4 | Publish an agentic-adapter conformance contract for request construction, normalized chunks, error classification, resume identifiers, and transport cleanup; run it against every subprocess, in-process, and remote adapter while retaining vendor parsers beside their vendors. | M | (filled at issue-open time) |
| 5 | Move named-vendor identity, effort, bypass, and safety tables out of generic service ownership while preserving `parse_model_spec()` as a compatibility façade and testing every existing provider alias. | M | (filled at issue-open time) |
| 6 | Freeze neutral interfaces for token estimation and MCP client security, then move them below protocol callers with compatibility re-exports and unchanged action-layer tool-registration behavior. | M | (filled at issue-open time) |

## Notes

Direct provider construction was rejected as the public model API because it would duplicate
selection, executor, hook, and session-state behavior. A separate executor-provider registry was
rejected because the implemented endpoint registry already resolves both endpoint families. A
single concrete endpoint class was rejected because HTTP and agentic transports have incompatible
connection and cancellation mechanics.
