# ADR-0013: Built-in Tool Provider and Branch Binding

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: actions-tools
- **Date**: 2026-07-09
- **Relations**: extends ADR-0011

## Context

Most standalone built-ins pair a Pydantic request model with an async handler and expose a
cached `Tool` through `to_tool()`. Reader, editor, search, shell, diagnostics, navigation,
and syntax-search adapters use this shape. Their concrete implementations own operational
constraints such as workspace path resolution, subprocess limits, and typed response
models (`lionagi/tools/file/reader.py`, `lionagi/tools/file/editor.py`,
`lionagi/tools/code/bash.py`, `lionagi/tools/code/search.py`,
`lionagi/tools/code/check.py`, `lionagi/tools/code/nav.py`,
`lionagi/tools/code/ast_search.py`).

`LionTool` is the common marker for those adapters. Its only behavioral requirement is
`to_tool()`, and `Branch.register_tools()` handles a `LionTool` by calling that method
before registry insertion. The same base module also contains resource and prompt graph
types that do not participate in tool registration (`lionagi/tools/base.py`,
`lionagi/session/branch.py`).

Three built-in providers require runtime context instead of satisfying that generic path.
`ContextTool` closes over a branch's messages and progression, `LionMessenger` closes over
a branch and exchange roster, and `CodingToolkit` builds a list of tools around branch
state. Each exposes `bind(...)` and intentionally raises from `to_tool()`. Agent construction
therefore special-cases coding-tool binding rather than passing the provider through generic
branch registration (`lionagi/tools/context/context.py`,
`lionagi/tools/communication/messenger.py`, `lionagi/tools/coding.py`,
`lionagi/agent/factory.py`).

`CodingToolkit` adds genuinely branch-scoped behavior: read-before-edit state, context
curation, and optional nudges based on branch history. It also implements local reader,
editor, shell, search, diagnostics, navigation, and syntax-search callables alongside the
standalone adapters with the same responsibilities. Those paths share request models and
some helpers but produce their own response dictionaries and execution behavior
(`lionagi/tools/coding.py`).

Branch cloning copies registered `Tool` descriptors into the clone's manager. It does not
rebuild or rebind their callables. A descriptor produced by `CodingToolkit.bind()`,
`ContextTool.bind()`, or `LionMessenger.bind()` can consequently retain a closure over the
source branch after being registered on the clone (`lionagi/session/branch.py`).

## Decision

The current built-in provider model has these load-bearing invariants:

- Stateless built-ins adapt their concrete implementation and Pydantic request model into
  one regular `Tool`, which the branch can register generically.
- `LionTool` advertises `to_tool()` but does not model construction context, multiple-tool
  output, lifecycle, or clone behavior.
- Context, messenger, and coding providers are branch-bound factories despite inheriting
  from `LionTool`; their supported construction path is `bind(...)`, not `to_tool()`.
- `CodingToolkit.bind()` returns the configured tool set and owns branch-local read state,
  context operations, and nudge integration. Its basic tool operations remain a parallel
  implementation to the standalone adapters.
- Branch cloning reuses registered descriptors as-is. No provider scope, rebinding, or
  cloneability contract is consulted.

## Consequences

Standalone tools remain easy to construct, test, register, and use directly, while
branch-scoped behavior can be assembled once during agent construction. Pydantic request
models give both modes a useful common input vocabulary.

Not every `LionTool` is substitutable through its declared method, so generic registration
cannot reliably consume the hierarchy. Parallel standalone and coding implementations can
drift in response shape, defaults, containment, timeout, and error behavior. Copying a
branch-bound closure into a clone can direct context mutation or persistence at the source
branch. Resource and prompt types in the base module also obscure the boundary of the tool
provider abstraction.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Replace the partial `LionTool.to_tool()` contract with a provider build contract that returns one or more tools from an explicit context; acceptance requires stateless and branch-bound built-ins to use the same construction path without raising from the advertised interface. | M | (filled at issue-open time) |
| 2 | Make branch cloning rebuild branch-bound providers for the clone or reject non-cloneable bindings; acceptance requires context, coding, and messenger callables registered on a clone to reference only the clone's state. | S | (filled at issue-open time) |
| 3 | Refactor `CodingToolkit` to compose canonical standalone operations and add only branch-scoped state and policy; acceptance requires response-schema and failure-semantic parity tests for every operation exposed in both modes. | M | (filled at issue-open time) |
| 4 | Move resource and prompt graph types out of the tool-provider base module; acceptance requires the tool base module to contain only registration and construction abstractions with unchanged public compatibility aliases. | S | (filled at issue-open time) |

## Notes

Removing `LionTool` in favor of ordinary factories is a viable alternative to introducing a
provider protocol. The deciding constraint is whether third-party providers need a stable,
typed construction interface for branch context and multiple-tool output.
