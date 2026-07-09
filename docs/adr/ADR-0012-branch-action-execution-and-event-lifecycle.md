# ADR-0012: Branch Action Execution and Event Lifecycle

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: actions-tools
- **Date**: 2026-07-09
- **Relations**: extends ADR-0011

## Context

`FunctionCalling` is the low-level invocation record for a `Tool`. It normalizes arguments
through the optional request model, runs a preprocessor, invokes a sync or async callable,
and then runs a postprocessor. As an `Event`, it records status, duration, response, and
errors. Ordinary exceptions from any of those three stages are captured as `FAILED` rather
than re-raised; cancellation-class failures are recorded as `CANCELLED` and re-raised
(`lionagi/protocols/action/function_calling.py`,
`lionagi/protocols/generic/event.py`).

The conversation-level transaction is `Branch.act()`, implemented by
`operations.act`. It validates the request envelope, asks the branch's session observer to
authorize the raw invocation, emits a blocking `TOOL_PRE` hook, invokes the manager, emits
`TOOL_POST`, persists and emits the event, and appends linked action request and response
messages. Denial stops before hooks and invocation and is returned as an error-shaped tool
result. Standalone branches without an observer authorize every call
(`lionagi/operations/act/act.py`, `lionagi/session/branch.py`,
`lionagi/session/observer.py`, `lionagi/hooks/bus.py`).

The branch action operation supports concurrent and sequential batches. Structured
`operate()` calls and the LNDL action bridge route model-generated requests through that
operation, so the common model-facing paths receive session authorization, hooks, event
logging, and message history (`lionagi/operations/operate/operate.py`,
`lionagi/operations/lndl_middle/lndl_middle.py`).

The two layers do not yet share an unambiguous outcome contract. `ActionManager.invoke()`
returns a `FunctionCalling` even when its callable failed, and the branch action operation
does not inspect the returned status. It emits `TOOL_POST` and writes the event response,
usually `None`, to history. `TOOL_ERROR` and the `suppress_errors` response path run only
when matching, validation, or invocation raises before a `FunctionCalling` is returned.
A failed tool body is therefore indistinguishable in conversation history from a
successful tool that returned `None` (`lionagi/protocols/action/manager.py`,
`lionagi/operations/act/act.py`, `lionagi/protocols/messages/action_response.py`).

Authorization and preprocessing are separate seams. Session authorization sees the raw
argument dictionary before request-model normalization. Permission policies and coding
guards are attached as `Tool` preprocessors and run later inside `FunctionCalling`; a
configured security preprocessor runs before user preprocessors and again afterward when
user preprocessors exist. Public access to `branch.acts.invoke()` and
`Tool.func_callable` also permits deliberate low-level calls that bypass session policy,
HookBus events, action messages, and branch event logging (`lionagi/agent/factory.py`,
`lionagi/agent/permissions.py`).

## Decision

The current execution contract has these load-bearing invariants:

- `FunctionCalling` is a failure-capturing execution `Event`; callers must inspect its
  status to distinguish completion, failure, and cancellation.
- `Branch.act()` and `operations.act` form the normal conversation transaction. The order
  is request-envelope normalization, session authorization, `TOOL_PRE`, manager
  resolution and event invocation, `TOOL_POST` for any returned event, event emission and
  logging, then action request and response history.
- A session denial is a tool result and history entry, not an exception. An exception that
  escapes manager invocation emits `TOOL_ERROR` and is either re-raised or converted to an
  error-shaped response according to `suppress_errors`.
- Returned event status is currently not mapped into the action response. In particular,
  a captured tool failure follows the same post-call path as a completed event.
- Sequential and concurrent batches preserve the same per-call transaction, while direct
  manager or callable access remains an explicitly possible but ungoverned low-level path.
- Sync callables and sync processors execute inline on the event loop; awaitability is
  determined from the callable declaration, so an awaitable returned by a sync declaration
  is not awaited.

## Consequences

Tool failures can be retained as data, allowing a concurrent batch or iterative reasoning
loop to continue without abandoning the whole branch. The event record, hook bus, branch
log, and message history provide distinct integration points, and session denial can be
shown to the model so it can adapt.

The status-blind boundary loses the most important distinction in the event record and can
mislead the next model turn. Policy behavior also depends on which public entry point a
caller chooses and whether it reasons about raw or normalized arguments. Inline sync work
can stall the event loop, while a sync wrapper that returns an awaitable produces a sharp
edge for decorators and callable objects.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Make the branch action transaction inspect `FunctionCalling.status`, emit `TOOL_ERROR` for captured failures, persist an error-bearing `ActionResponse`, and reserve `TOOL_POST` for completed calls; acceptance must distinguish successful `None` from failure in sequential and concurrent regression tests. | M | (filled at issue-open time) |
| 2 | Publish one authoritative branch action executor and reduce `ActionManager` to registry and resolution responsibilities; acceptance requires Branch, operate, iterative reasoning, and LNDL paths to use the executor while any no-history invocation is explicitly named and documents its bypass semantics. | M | (filled at issue-open time) |
| 3 | Define one normalized call context and explicit authorization, intrinsic-policy, agent-policy, transform, and revalidation phases; acceptance requires every phase to receive the same normalized arguments and security revalidation not to depend on whether a user preprocessor exists. | M | (filled at issue-open time) |
| 4 | Define and enforce a sync-tool execution policy; acceptance requires blocking sync work either to be offloaded by the executor or to require an explicit inline opt-in, with returned awaitables handled consistently. | S | (filled at issue-open time) |

## Notes

The existing `ActionResponseContent.error` field can carry a structured failure without
changing successful output values. Whether branch execution returns failures as values or
raises them is separable from whether history represents the failure truthfully.
