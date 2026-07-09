# ADR-0023: Dependency-aware operation-graph execution kernel

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: operations
- **Date**: 2026-07-09
- **Relations**: none

## Context

Graph execution uses `Operation` as the executable node. An operation stores a branch-method or
registered-operation name plus parameters, receives its branch immediately before invocation, and
dispatches through `Branch.get_operation()`. `OperationGraphBuilder` constructs dependency,
expansion, aggregation, and conditional graph shapes but does not execute them
(`lionagi/operations/node.py`, `lionagi/operations/builder.py`).

`Session.flow()` delegates a `Graph[Operation]` to `DependencyAwareExecutor`. The executor rejects
cycles, validates edge conditions, preallocates branches, creates a completion event per operation,
and starts operation coroutines under a capacity limiter. Explicitly assigned branches are reused;
dependent or context-inheriting operations without one receive session-owned clones. Every terminal,
failed, skipped, cancelled, or exceptional path releases its completion event
(`lionagi/session/session.py`, `lionagi/operations/flow.py`).

The executor injects predecessor results and shared flow context into operation parameters. A primary
dependency may also supply inherited conversation messages. Successful response dictionaries with a
`context` key are deep-merged into one shared context state. Parallel writes to the same key have no
declared ordering or reducer and therefore resolve by completion timing (`lionagi/operations/flow.py`).

`ReactiveExecutor` extends the same kernel. It observes spawn and escalation requests, schedules
initial and injected nodes in one task group, synchronizes graph mutation, rejects cycles and
duplicates, and enforces a spawn cap. `flow_stream()` yields a typed event per settled node through a
memory channel, with an asyncio task driving the reactive executor. Checkpoint writing and graph
reconstruction remain caller-owned and are not kernel capabilities (see the scheduling-control-plane
ADR on flow checkpoint and resume semantics).

## Decision

`flow.py` is the execution kernel for operation graphs, not for every branch verb. Static dependency
execution is the base contract; reactive graph growth is a constrained extension of that contract.

```text
OperationGraphBuilder ──> Graph[Operation]
                               │
                               v
                  DependencyAwareExecutor
                    │ dependency events
                    │ branch allocation
                    │ capacity + pause gate
                    │ context materialization
                    v
                     Branch operation dispatch
                               ^
                               │ inherits execution semantics
                       ReactiveExecutor
                    spawn bus + guarded mutation
```

The load-bearing invariants are:

- Execution requires an acyclic graph and valid edge-condition types. An `Operation` invokes only a
  built-in or session-registered branch operation.
- An explicit `branch_id` reuses that branch. Otherwise, dependent or context-inheriting nodes are
  preallocated isolated clones; accepted reactive children receive clones when injected.
- Dependencies are represented by per-node completion events. Every execution path sets its event,
  so downstream waiters cannot hang solely because an upstream operation failed or was skipped.
- The capacity limiter encloses preparation and invocation. A pause gate stops new operations at the
  operation boundary without interrupting operations already past that boundary.
- Predecessor results and the shared flow context state are passed as operation context. Conversation
  inheritance is explicit metadata and follows one primary dependency.
- Reactive injection is synchronized, capped, and cycle-checked. Dependent spawns receive an edge
  from their emitter; independent spawns do not. Bus delivery and returned-request scanning are
  de-duplicated by request identity.
- Pre-marked terminal nodes may be short-circuited with restored responses, but persistence, topology
  serialization, and branch-history reconstruction are outside the kernel.
- The current `completed_operations` result contains every settled result except skipped nodes,
  including failed operations. `FlowEvent.status` is the accurate completed/failed/skipped signal.

## Consequences

Static and reactive callers share dependency ordering, concurrency limits, pause behavior, branch
isolation, context propagation, and operation dispatch. A fixed graph can resume from caller-restored
terminal nodes without coupling the reusable kernel to a checkpoint format.

The shared context state can be nondeterministic when parallel nodes write colliding keys. The result
name can mislead consumers into reporting failures as successes. Reactive execution adds observer,
mutation, streaming-driver, and branch-allocation paths to an already broad module, and its streaming
surface is asyncio-specific.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Replace `completed_operations` with explicit completed, failed, and skipped collections, retaining a deprecated compatibility alias whose settled-not-skipped meaning is documented. | M | (filled at issue-open time) |
| 2 | Define deterministic shared-context semantics by namespacing node outputs by default and requiring an explicit reducer for colliding shared keys; add parallel collision tests. | M | (filled at issue-open time) |
| 3 | Pass `on_branch_created` into reactive execution and invoke it for every injected clone; add identical persistence-hook coverage for preallocated and spawned branches. | S | (filled at issue-open time) |
| 4 | Extract reactive graph mutation and streaming-driver concerns behind internal collaborators while retaining one public executor contract; document the required async backend. | M | (filled at issue-open time) |
| 5 | Specify durable reactive continuation with persisted spawned topology, parent edges, operation requests, branch reconstruction, context, and conversation-history policy; keep the current fail-closed refusal until all fields can be restored. | L | (filled at issue-open time) |

## Notes

A sequential traversal was rejected because it would discard independent-node concurrency. A second
reactive scheduler was rejected because it would duplicate dependency, branch, pause, and context
semantics. Checkpoint persistence inside the kernel was rejected because durability formats and
degradation policy belong to callers rather than graph scheduling.
