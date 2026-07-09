# ADR-0011: Function Tool Descriptor and Branch Registry

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: actions-tools
- **Date**: 2026-07-09
- **Relations**: none

## Context

Model providers need a portable function schema, while local execution needs a Python
callable and optional processing behavior. `Tool` keeps those concerns in one `Element`:
the callable and processors remain live Python objects excluded from serialization, while
the generated function schema and derived function name are serializable. Arbitrary
deserialization is deliberately unsupported because a serialized record cannot recreate
the callable (`lionagi/protocols/action/tool.py`).

Supplying a Pydantic request model through `request_options` is the practical schema and
validation contract used by the built-in tools. Without one, `function_to_schema()` is a
convenience adapter: it recognizes a small set of primitive annotations and advertises
every signature parameter as required. At invocation, non-strict validation instead
requires only signature parameters without defaults, while strict validation compares the
argument-key set with the schema's `required` set. Provider schema, strict validation, and
Python default semantics therefore do not agree for every raw callable
(`lionagi/libs/schema/function_to_schema.py`,
`lionagi/protocols/action/function_calling.py`).

Each `Branch` owns an `ActionManager` whose registry maps a branch-visible function name to
one `Tool`. Registration accepts a `Tool`, a raw callable, or a one-entry MCP configuration;
duplicate names require `update=True`. The manager resolves action requests, exposes
provider schemas, and can invoke the resulting event. This makes tool availability local
to a branch even when the underlying callable is reusable (`lionagi/protocols/action/manager.py`,
`lionagi/session/branch.py`).

MCP loading is also implemented in this registry layer. `Tool` and `ActionManager` lazily
import the MCP service adapter, discovery copies a server's input schema into a regular
function schema, and discovered tools are stored under their unqualified remote names.
The service connection pool is fail-closed when used directly, but the manager's config
loaders treat an explicitly loaded config as trusted for command and URL transports when
no policy is supplied (`lionagi/protocols/action/manager.py`,
`lionagi/service/connections/mcp_wrapper.py`).

## Decision

The current tool declaration contract has these load-bearing invariants:

- `Tool` is a callable descriptor, not a restorable executable artifact. Its callable and
  processors are excluded from serialization; its schema and function name are retained.
- A Pydantic `request_options` model is the supported path for typed input schema,
  normalization, and validation. Raw signature derivation is a limited keyword-callable
  convenience and currently marks all parameters as provider-required.
- `ActionManager` is the branch-local name registry and schema resolver. A registered
  function name is unique within that manager unless replacement is explicit.
- MCP tools are normalized into ordinary `Tool` descriptors, but discovery, transport
  trust defaults, and connection-pool access currently remain responsibilities of the
  protocol registry rather than a service-owned factory.
- Invocation lifecycle and branch transaction semantics are defined separately in
  ADR-0012.

## Consequences

Provider-facing schemas and Python callables share a small, reusable descriptor, and
branches can expose different tool sets without changing the callables themselves.
Pydantic-backed tools receive rich schemas and normalized keyword arguments with little
adapter code.

Raw callable registration is easy but can advertise a contract different from runtime
behavior. Custom or remote schemas that omit `required` can fail because
`Tool.required_fields` assumes the key exists. Registry code also knows about MCP config,
transport policy, discovery, and process-global pooling, creating an upward dependency
from a foundational protocol package into the service layer. Unqualified remote names can
collide within a branch registry.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Version and narrow raw-callable schema derivation so Python defaults remain optional, unsupported positional-only and variadic signatures require an explicit adapter, schemas without `required` are accepted, and tests prove provider schema and runtime validation agree. | M | (filled at issue-open time) |
| 2 | Move MCP configuration, discovery, namespacing, and pool lifecycle into a service-owned factory that returns ready `Tool` descriptors; acceptance requires `protocols.action` to have no service-layer import and remote identities to be collision-free. | M | (filled at issue-open time) |
| 3 | Require the MCP-loading caller to make an explicit transport-trust decision; acceptance requires omitted policy to preserve the wrapper's fail-closed command and URL defaults and an explicit trusted-config mode to be observable. | S | (filled at issue-open time) |

## Notes

Correcting the all-parameters-required schema is a compatibility change because existing
tests and provider payloads preserve that behavior. It should not be shipped as an
unversioned cleanup.
