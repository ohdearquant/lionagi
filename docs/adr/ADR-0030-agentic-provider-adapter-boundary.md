# ADR-0030: Agentic provider-adapter boundary

- **Status**: Proposed
- **Kind**: Aspirational
- **Area**: service-providers
- **Date**: 2026-07-09
- **Relations**: extends ADR-0027, ADR-0029

## Context

`AgenticEndpoint` represents providers whose work is not a normal HTTP request. Codex, Claude Code,
Pi, and Gemini adapters launch command-line processes; AG2 adapters run in-process or connect to a
remote agent. They share `APICalling`, `StreamChunk`, and resumable session state with API endpoints,
which lets operations consume them through `iModel` without vendor selection logic
(`lionagi/service/connections/agentic_endpoint.py`, `lionagi/service/types/`).

The subprocess adapters already share meaningful mechanics. `lionagi/providers/_cli_subprocess.py`
owns NDJSON framing, bounded stderr capture, process-group isolation and termination, workspace
validation, prompt extraction, and declarative flag emission.
`lionagi/providers/_agentic_handlers.py` validates callbacks and constructs typed requests. Vendor
modules correctly retain their own flag grammars, safety settings, event parsers, UX callbacks, and
session synthesis.

The common result contract is less explicit than the common machinery. Each adapter decides how to
map provider events, end-of-stream, errors, final sessions, and resume identifiers. Some streams
convert a final session to a result chunk while others suppress it; provider error chunks do not
uniformly set `is_error`; and the `is_cli` flag is also true for non-CLI agentic adapters because
operations use it as the general streaming-path selector.

These differences are not a reason to centralize vendor parsers. They are a reason to name the
boundary, distinguish transport capability from operation routing, and test the normalized contract
that every adapter promises.

## Decision

Retain `AgenticEndpoint` as the implemented extension boundary and establish an internal
`lionagi/providers/_agentic/` support lane. Move the existing generic subprocess and handler helpers
into that lane with compatibility re-exports. Vendor request models, command grammars, event parsers,
and provider-specific safety policy remain under their vendor packages.

Every `AgenticEndpoint` declares immutable capabilities:

```python
@dataclass(frozen=True, slots=True)
class AgenticCapabilities:
    transport: Literal["subprocess", "in_process", "remote"]
    resumable: bool
    emits_tool_events: bool
    reports_usage: bool
```

The adapter conformance contract is:

- `create_payload()` constructs the provider's typed request and separates runtime handlers from
  serializable provider data. Unknown handler names and invalid request fields fail before transport
  starts.
- `stream()` yields only provider-neutral `StreamChunk` values. Text, reasoning, tool use, tool
  result, final result, and error use the closed chunk vocabulary; a provider-declared error sets
  both `type="error"` and `is_error=True`.
- A resumable adapter publishes the provider session identifier in a system chunk as soon as it is
  known and in the non-streaming result mapping. `iModel` owns the stored identifier and injects it
  on the next request through the provider's typed resume field. Non-resumable adapters do not
  synthesize an identifier.
- Text is carried only by text chunks. A successful stream emits at most one result chunk after
  content, containing terminal metadata and any non-duplicated final result; an internal
  `CLISession` is not exposed as a chunk. Normal EOF is not an error. Transport or parser failure
  raises a provider-classified exception; cancellation propagates after resource cleanup. ADR-0029
  owns admission, terminal event state, and whether an HTTP stream may retry before its first chunk.
- Subprocess adapters use argument-vector process creation, validate the working directory before
  launch, isolate the process group, drain bounded stderr concurrently, and terminate and reap the
  group on normal close, failure, timeout, or cancellation. Shell-string execution is not part of
  the common lane.
- Shared code is admitted only when at least two adapters use the same semantics. Provider event
  interpretation, model defaults, flags, permission modes, and fallback grammar stay vendor-owned.

Add `is_agentic` as the canonical operation-routing capability. Preserve `is_cli` as a deprecated
compatibility alias during migration; `transport="subprocess"` is the precise test for CLI-specific
behavior. This naming change must not alter which endpoints use the streaming operation path.

One conformance suite runs against every agentic adapter. All adapters cover request construction,
normalized chunk order, declared capability behavior, provider error classification, cancellation,
and session identifiers. Subprocess adapters additionally cover workspace containment, argument
emission, stderr saturation, process-group teardown, and missing executable behavior. Vendor parser
fixtures remain beside their adapters.

## Consequences

Operations receive a stable stream and session contract without learning vendor event formats.
Subprocess safety and teardown behavior evolve once, while provider owners can change flags or event
grammar locally. In-process and remote agent adapters no longer need to masquerade as command-line
transports merely to select the streaming path.

The conformance boundary deliberately permits provider-specific capabilities, so not every stream
will contain tool or usage events. Moving shared helpers and introducing `is_agentic` require
compatibility exports and a staged deprecation. Tightening error chunks may change consumers that
currently infer failure from content instead of `is_error`.

## Notes

A new executor-provider protocol was rejected because `AgenticEndpoint` and `EndpointRegistry`
already provide the live extension and resolution seams. Moving every agentic adapter into one
package was rejected because it would separate request and event grammar from vendor ownership.
Forcing one universal parser or command schema was rejected because the shared invariant is the
normalized output and lifecycle contract, not identical vendor inputs.
