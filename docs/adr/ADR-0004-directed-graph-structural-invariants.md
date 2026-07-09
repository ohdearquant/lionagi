# ADR-0004: Directed Graph Structural Invariants

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: core-data-model
- **Date**: 2026-07-09
- **Relations**: extends ADR-0003

## Context

LionAGI represents dependency-aware work as a directed graph. `Graph` stores Nodes and Edges in
typed Piles and maintains a derived adjacency mapping with separate incoming and outgoing edge maps
for every resident node. An edge can be added only when both endpoint UUIDs are already present, and
removing a node removes its incident edges. `lionagi/protocols/graph/graph.py` and
`lionagi/protocols/graph/edge.py` define this structure.

Graph supports predecessor and successor lookup, cycle detection via `is_acyclic`, and topological
sorting. Its mutating methods use a reentrant lock, but traversal and adjacency reads do not take
that lock. The public `add_node` annotation admits any nominal `Relational`, while the backing Pile
accepts `Node` subclasses; the storage boundary is therefore narrower than the method signature.

The primary consumer of this structure is operation-flow execution, which schedules content-bearing
Operation nodes over a Graph and relies on its acyclicity check, adjacency mapping, and mutation
methods (see the operations ADR on operation-graph execution). That consumer imposes stronger
transactional requirements than any single Graph mutation provides — it coordinates graph changes
with its own runtime state under its own lock — which is why Graph deliberately exposes composable
mutation primitives rather than an execution-shaped transaction API.

## Decision

`Graph` is a mutable directed-graph model whose load-bearing structural invariants are:

- graph storage contains Nodes and directed Edges in typed Piles, and every edge head and tail
  refers to a resident node — insertion of an edge with a non-resident endpoint fails;
- the derived adjacency mapping (incoming and outgoing edge maps per node) agrees with the node and
  edge Piles at all times, including cascade removal of incident edges when a node is removed;
- cycle detection (`is_acyclic`) and topological sorting are structural queries over the current
  graph state, available to any consumer before or during use;
- each mutating method is individually serialized by a reentrant lock; compound multi-step changes
  are the caller's responsibility to coordinate.

Graph's mutation lock protects individual graph mutations. It does not establish a general guarantee
for arbitrary concurrent mutation and unlocked traversal, and it does not provide multi-mutation
transactions — consumers needing atomic compound changes (for example, reactive node insertion
coordinated with runtime completion state) build their own transaction above Graph (see the
operations ADR on operation-graph execution). The implementation anchors are
`lionagi/protocols/graph/graph.py` and `lionagi/protocols/graph/edge.py`.

## Consequences

Dependency lookup is direct, invalid endpoint references fail at insertion, and node removal cannot
leave incident edges behind. Structural queries (acyclicity, topological order) let consumers
validate a graph before committing to execute it, and the primitive-mutation design keeps Graph
reusable for non-executable graphs.

Graph remains a mutable structure rather than an immutable execution plan. Callers cannot safely
assume concurrent traversal during arbitrary mutation, and the method-level `Relational` type is
broader than actual storage. Edge construction accepts a general Condition, while downstream
executable-graph validation narrows conditions further (see the operations ADR on operation-graph
execution), so generic graph validity does not by itself guarantee executability.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Align `Graph.add_node` with its storage boundary; acceptance requires the public type contract and backing Pile to admit the same node family, with tests for accepted and rejected types. | S | (filled at issue-open time) |
| 2 | Align Edge condition admission with downstream executable-graph validation; acceptance requires construction and execution to accept the same documented condition protocol, with tests for custom conditions or an explicit construction-time rejection. | S | (filled at issue-open time) |
| 3 | Publish graph traversal snapshot semantics; acceptance requires supported concurrent read and mutation patterns to be documented and traversal APIs to return stable results under those supported patterns. | M | (filled at issue-open time) |

## Notes

Alternatives considered were allowing cycles and relying on runtime deadlock detection, or moving
all adjacency ownership into the graph's executing consumer. The first makes dependency completion
impossible to reason about before execution; the second duplicates a reusable directed-graph model
and weakens Graph's own integrity guarantees.
