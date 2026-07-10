# ADR-0004: Directed graph structural invariants

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: core-data-model
- **Date**: 2026-07-09
- **Relations**: extends ADR-0003

## Context

LionAGI represents relationships and dependency-aware work with a mutable directed graph. The
generic Graph owns structure, not execution. Five problems determine its shipped contract.

**P1 — Edges must not point outside the resident graph.** A graph traversal cannot resolve an
endpoint that is absent from node storage. Endpoint checks therefore happen before an edge is added,
and removing a node must remove every incident edge.

**P2 — Relationship lookup must not scan every edge.** Predecessor, successor, head, tail, and path
queries are frequent. Graph maintains a derived per-node adjacency map with separate incoming and
outgoing edge maps in addition to the canonical Node and Edge Piles.

**P3 — Structural edits need reusable primitives.** Consumers need to add/remove nodes and edges,
replace a node while preserving incident relationships, and insert a new node after an anchor while
rewiring the anchor's outgoing relationships. These are graph operations, independent of any
execution state machine.

**P4 — Cycles are valid graph data but must be detectable.** A generic directed graph may contain a
self-loop or longer cycle. Execution consumers need an explicit acyclicity query or topological-sort
failure before using the graph as a dependency plan; Graph does not silently outlaw cycles for every
consumer.

**P5 — Mutation coordination and traversal coordination have different scope.** Each shipped Graph
mutation holds a reentrant thread lock. Traversal, serialization, and direct access to public Piles
and the adjacency dictionary are unlocked, so Graph is not an immutable snapshot or a multi-step
transaction.

The primary execution consumer schedules content-bearing Operation nodes over a Graph and applies
stronger dependency and transaction rules above these primitives (see the operations ADR on
operation-graph execution). The defining modules for this ADR remain
`lionagi/protocols/graph/graph.py` and `lionagi/protocols/graph/edge.py`; Node identity and
serialization are defined by ADR-0001.

| Concern | Decision |
|---|---|
| Node and edge representation | D1: Graph stores Node subclasses and directed Edge models; Edge keeps endpoints as UUIDs and condition/label/custom values in a properties dictionary. |
| Adjacency and residency | D2: Every resident node has `in` and `out` maps, and an edge is admitted only when both endpoints are resident. |
| Structural mutation | D3: Add/remove, node replacement, and splice-after update Piles and adjacency together under one mutation lock. |
| Structural queries | D4: Graph exposes adjacency queries, cycle detection, topological sorting, and condition-aware directed BFS over the current live structure. |
| Serialization and concurrency boundary | D5: Node/Edge Piles serialize; adjacency is excluded and rebuilt; mutation locks are method-level and traversal remains unlocked. |

This ADR deliberately does **not** decide:

- whether an executable operation graph may run with cycles, how predecessors complete, or how a
  blocked operation is marked; those are operation-scheduler decisions;
- transactional coordination between Graph mutations and consumer runtime state; consumers that
  need a compound transaction own it above Graph;
- graph database persistence, distributed traversal, or durable graph locking; Graph is an in-memory
  model; or
- a domain ontology for edge labels and properties; generic Edge accepts caller-defined properties.

## Decision

### D1 — Graph stores Nodes and directed property-bearing Edges

**The model contracts** (`lionagi/protocols/graph/graph.py` and
`lionagi/protocols/graph/edge.py`):

```python
class Graph(Element, Relational, Generic[T]):
    internal_nodes: Pile[T] = Field(
        default_factory=lambda: Pile(item_type={Node}, strict_type=False)
    )
    internal_edges: Pile[Edge] = Field(
        default_factory=lambda: Pile(item_type={Edge}, strict_type=False)
    )
    node_edge_mapping: dict = Field(default_factory=dict, exclude=True)
    _lock: threading.RLock = PrivateAttr(default_factory=threading.RLock)
```

```python
class EdgeCondition(BaseModel, Condition):
    source: Any = None

    model_config = ConfigDict(
        extra="allow",
        arbitrary_types_allowed=True,
    )

class Edge(Element):
    head: UUID
    tail: UUID
    properties: dict[str, Any] = Field(default_factory=dict)

    def __init__(
        self,
        head: ID[Relational].Ref,
        tail: ID[Relational].Ref,
        condition: Condition | None = None,
        label: list[str] | None = None,
        **kwargs,
    ): ...
```

**Exact Edge semantics**:

- `head` and `tail` accept a UUID, Element reference, or UUID string and are normalized by
  `ID.get_id`. Direction is head to tail.
- The constructor places every extra keyword in `properties`. A truthy `condition` is also placed
  under `properties["condition"]` after a nominal `Condition` check. A duck-typed object that only
  exposes an async `apply` method is rejected.
- A truthy string label becomes a one-element list. A truthy list must contain one homogeneous
  string type; other values raise `ValueError`. An omitted or falsey constructor label does not add a
  label property.
- The `label` setter differs slightly: `None` or an empty list writes `[]`; a string is wrapped; a
  homogeneous string list is retained. The `condition` setter accepts `None` or a nominal Condition
  and always writes the property.
- `check_condition(*args, **kwargs)` awaits the condition's `apply` when present and otherwise
  returns true. Condition exceptions propagate to the caller.
- `update_property` overwrites one property. `update_condition_source` changes `.source` only when
  the current condition value is truthy; Graph does not interpret the source.
- Parallel edges, reverse-direction pairs, and self-loops are structurally valid because every Edge
  has its own UUID and the graph imposes no endpoint-pair uniqueness rule.

The custom Edge constructor has an important inherited-envelope consequence: serialized `id`,
`created_at`, `metadata`, and an existing `properties` dictionary arrive through `**kwargs` and are
then nested into a newly assembled properties dictionary while Element generates a new UUID and
timestamp. Direct Edge reconstruction therefore changes identity. In a containing Graph, that new
UUID also disagrees with the old UUID retained by the serialized Edge Pile's Progression, causing
Pile reconstruction to fail. ADR-0001 delta 3 owns the Edge envelope correction; this ADR records
the additional Graph reconstruction contract in delta 4.

### D2 — Derived adjacency mirrors every resident node and edge

The adjacency shape is:

```python
node_edge_mapping = {
    node_id: {
        "in":  {edge_id: edge.head, ...},
        "out": {edge_id: edge.tail, ...},
    },
    ...,
}
```

For an edge `A -> B`, `mapping[A]["out"][edge.id] == B` and
`mapping[B]["in"][edge.id] == A`.

**The mutation entry points**:

```python
def add_node(self, node: Relational) -> None: ...
def add_edge(self, edge: Edge, /) -> None: ...
def remove_node(self, node: ID[Node].Ref, /) -> Node: ...
def remove_edge(self, edge: Edge | str, /) -> Edge: ...
```

The source annotations for `remove_node` and `remove_edge` say `None`, but the implementations
return the removed Node or Edge from their backing Piles.

**Exact semantics**:

- A new Graph has empty typed Piles and an empty mapping.
- `add_node` first checks nominal `Relational`, then inserts through the Node-typed Pile and creates
  an empty adjacency entry. Reusing a UUID becomes `RelationError` through the caught
  `ItemExistsError`.
- The public `add_node` annotation/check is broader than storage: a nominal Relational that is not a
  Node passes the first check and then fails Pile's Node admission. This mismatch remains delta 1.
- `add_edge` requires an Edge instance and checks both endpoint UUIDs against the node Pile before
  mutating any structure. A missing endpoint raises `RelationError`. A repeated Edge UUID is
  translated from `ItemExistsError` to `RelationError`.
- A valid add inserts the Edge, then records one outgoing and one incoming adjacency entry. A
  self-loop records the same Edge ID in both maps for the same node.
- `remove_edge` rejects an absent Edge UUID, removes both adjacency entries, and removes/returns the
  Edge.
- `remove_node` rejects an absent UUID. It removes all incoming edges from predecessor `out` maps and
  edge storage, then all remaining outgoing edges from successor `in` maps and edge storage. For a
  self-loop, removing it during the incoming pass also removes it from the same node's outgoing map,
  so it is not removed twice. Finally the node mapping and Node are removed.
- Direct writes to `internal_nodes`, `internal_edges`, or `node_edge_mapping` bypass these operations
  and can violate residency and mirror invariants.

The redundant mapping is retained because the common predecessor/successor queries become local to
one node rather than scans over all Edges.

### D3 — Replacement preserves edges; splice-after replaces outgoing edge identities

Graph provides two compound structural edits in addition to add/remove.

**The contracts** (`lionagi/protocols/graph/graph.py`):

```python
def replace_node(self, old: Any, new_node: Node) -> Node: ...
def splice_after(self, anchor: Any, new_node: Node) -> list[Edge]: ...
```

**`replace_node` semantics**:

1. Normalize the old reference. Missing old UUID raises `RelationError`.
2. Reject a replacement UUID already resident in the graph.
3. Insert the replacement Node at the end of the Node Pile and create empty adjacency maps.
4. For every incoming Edge, mutate `edge.tail` to the new UUID and update the predecessor's outgoing
   target and the replacement's incoming entry.
5. For every outgoing Edge, mutate `edge.head` to the new UUID and update the successor's incoming
   source and the replacement's outgoing entry.
6. Remove the old adjacency entry and remove/return the old Node.

Existing Edge objects, UUIDs, conditions, labels, custom properties, and relative Edge order are
preserved. Node order is not preserved: the replacement is appended and the old node is removed
from its prior position.

**`splice_after` semantics**:

1. Normalize and require the anchor; reject a new UUID already resident.
2. Append the new Node and snapshot the anchor's outgoing `(edge_id, tail_id)` entries.
3. For every former outgoing Edge, create a new Edge from the new Node to the old tail. Reuse the
   same condition object and any truthy label value, and copy all other properties except the
   separately passed `condition` and `label` keys. An explicitly stored empty label is not
   preserved: `old_edge.label` supplies `[]`, the Edge constructor treats that value as omitted, and
   the replacement has no label property and reads back `None`.
4. Insert each replacement Edge and adjacency, then remove the original Edge and its adjacency.
5. Create a new unconditioned Edge from anchor to new Node.
6. Return a list with the anchor-to-new link first, followed by replacement outgoing Edges.

With no original successors, splice creates only the anchor-to-new Edge. Unlike `replace_node`, it
deliberately creates new identities and timestamps for every rewired outgoing Edge; old Edge
metadata is not copied because only condition, label, and other property entries are forwarded.

Both methods hold the Graph RLock for their full sequence, but neither has rollback. An unexpected
validation or insertion failure after earlier steps can leave partial mutation; the lock prevents
another decorated mutation from interleaving, not transaction failure.

These primitives stay execution-agnostic. A consumer coordinating Graph structure with running or
completed operations must add its own higher-level transaction (see the operations ADR on reactive
operation insertion).

### D4 — Queries inspect the current directed structure

**The contracts** (`lionagi/protocols/graph/graph.py`):

```python
def find_node_edge(
    self,
    node: Any,
    /,
    direction: Literal["both", "in", "out"] = "both",
) -> Pile[Edge]: ...

def get_heads(self) -> Pile[Node]: ...
def get_tails(self) -> Pile[Node]: ...
def get_predecessors(self, node: Node, /) -> Pile[Node]: ...
def get_successors(self, node: Node, /) -> Pile[Node]: ...
def is_acyclic(self) -> bool: ...
def topological_sort(self) -> list[Node]: ...
async def find_path(
    self,
    start: Any,
    end: Any,
    check_conditions: bool = False,
) -> list[Edge] | None: ...
```

**Adjacency-query semantics**:

- `find_node_edge` accepts only `both`, `in`, or `out`; other strings raise `ValueError`. An absent
  node raises `RelationError`. Incoming edges are emitted before outgoing edges for `both`. The
  source annotation says list, but the method returns an Edge-typed Pile.
- `get_heads` returns nodes with no incoming Edge; `get_tails` returns nodes with no outgoing Edge.
  An isolated node is both a head and a tail. A cyclic component may contribute neither.
- Predecessors are the heads of incoming Edges; successors are the tails of outgoing Edges. Return
  Piles preserve adjacency insertion order and accept Node subclasses.
- `Graph.__contains__` returns true for a UUID/Element present in either the Node or Edge Pile.

**Cycle and ordering semantics**:

- `is_acyclic` runs depth-first marking over every node and returns true for an empty graph, false
  for self-loops and longer cycles, and true for disconnected acyclic components. It does not mutate
  or reject the graph.
- `topological_sort` uses Kahn's algorithm. Empty graph returns `[]`; otherwise zero-in-degree nodes
  and outgoing edges follow current dictionary insertion order. If fewer nodes are emitted than are
  resident, it raises `ValueError("Cannot topologically sort graph with cycles")`.

**Path semantics**:

- `find_path` normalizes and separately checks both endpoints, raising `RelationError` for a missing
  start or end. Equal resident endpoints return `[]`.
- Directed breadth-first search returns a shortest-hop list of Edge objects in head-to-tail order.
  No path returns `None`. A visited set makes search terminate on cyclic graphs.
- With `check_conditions=False`, conditions are ignored. With true, each candidate Edge condition is
  awaited with no arguments before its target is visited; false omits that Edge, and an exception
  from condition code propagates.
- Edge construction accepts any nominal `Condition`, while the executable operation consumer
  currently narrows its accepted conditions further. Generic graph validity therefore does not by
  itself guarantee operation-graph executability; delta 2 owns contract alignment.

`to_networkx` is an optional projection to `networkx.DiGraph`; absent NetworkX raises an
installation-oriented ImportError. Node/Edge IDs become strings and their remaining serialized
fields become attributes. Because the target is `DiGraph` rather than `MultiDiGraph`, parallel
Edges with the same head and tail collapse to one projected NetworkX edge even though canonical
Graph storage retains them as distinct UUID-bearing Edges. `display` additionally requires
matplotlib and draws a spring layout. These helpers do not change Graph's canonical storage and are
not a lossless export format.

### D5 — Adjacency is derived state and locking is mutation-only

Graph serializes `internal_nodes` and `internal_edges` through their Pile dictionaries.
`node_edge_mapping` is excluded. If both Pile fields validate, Graph clears the mapping, creates an
empty `in`/`out` entry for every resident Node, then replays every Edge into the two maps.

**Exact reconstruction semantics**:

- The serialized payload has Element envelope fields plus `internal_nodes` and `internal_edges`;
  it does not contain the adjacency map or lock.
- Field validators call `Pile.from_dict` for both stores. The output of even an empty Graph currently
  fails this step: default stores serialize `lion_class` as the concrete non-parameterized `Pile`,
  while Graph's field validation binds parameterized `Pile[T]` and `Pile[Edge]` classes whose
  metadata validator expects their own generated qualified names. The result is a metadata-class-
  mismatch ValidationError.
- With resident Edges there is a second independent failure reason: Edge reconstruction generates a
  new UUID, while the serialized Edge Pile Progression still names the old UUID, so Pile rejects the
  dictionary/order mismatch. Both reasons surface together in the same pydantic validation pass (one
  ValidationError), not as two temporally separate failures.
- If those Pile failures are repaired, Graph then trusts that every Edge endpoint exists; malformed
  serialized input with an absent endpoint fails while indexing the mapping rather than through the
  friendly `add_edge` RelationError path.
- Because Pile excludes `item_type` from serialization, reconstructed stores do not carry their
  original runtime type constraint inside the nested Pile payload. Graph's mapping rebuild still
  assumes Node and Edge contents.
- Because validation fails before the model validator, `Graph.from_dict(graph.to_dict())` does not
  currently return a rebuilt Graph. Delta 4 defines the self-round-trip contract.

The RLock is acquired by `add_node`, `add_edge`, `remove_node`, `remove_edge`, `replace_node`, and
`splice_after`. It is reentrant and per Graph instance.

**Concurrency semantics**:

- Each decorated mutation excludes other decorated mutations on the same Graph and updates Piles
  and adjacency within that critical section.
- Query methods, algorithms, serialization, `__contains__`, and optional projections do not acquire
  the Graph lock.
- The backing Piles have their own locks, but direct Pile mutation is not coordinated with the Graph
  lock or adjacency updates.
- A traversal concurrent with mutation has no stable-snapshot guarantee and can observe changing
  dictionaries or missing entries.
- Multiple decorated mutations are not one transaction when invoked as separate calls. Callers
  needing atomic compound changes own a lock/transaction above Graph.

## Consequences

Endpoint validation and mirrored adjacency make predecessor/successor lookup direct, and supported
node removal cannot leave incident edges behind. Generic callers can represent cycles while
execution-oriented consumers can preflight with `is_acyclic` or `topological_sort`. Replacement and
splice primitives keep graph-editing logic in one reusable model rather than duplicating adjacency
updates in each consumer.

The costs are concrete:

- every supported mutation must update canonical Piles and derived adjacency consistently;
- direct public-field mutation can bypass invariants;
- Graph is mutable and unlocked traversal is not a snapshot;
- `replace_node` preserves Edge identity but changes Node order, whereas `splice_after` replaces
  outgoing Edge identities and metadata and drops an explicit empty label;
- the method-level Relational annotation is broader than actual Node storage;
- generic Condition admission is broader than executable-operation validation; and
- current Edge and parameterized-Pile reconstruction defects prevent a Graph from round-tripping its
  own serialized output.

Reversing D2 requires replacing every adjacency query with scans or another index. Reversing D3
changes consumer-visible Edge identities and ordering. Strengthening D5 to snapshot isolation or
transactions requires closing direct mutation paths and coordinating Graph and Pile locks.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|---|---|---|
| 1 | Align `Graph.add_node` with its storage boundary; acceptance requires the public type contract and backing Pile to admit the same node family, with tests for accepted and rejected types. | S | (filled at issue-open time) |
| 2 | Align Edge condition admission with downstream executable-graph validation; acceptance requires construction and execution to accept the same documented condition protocol, with tests for custom conditions or an explicit construction-time rejection. | S | (filled at issue-open time) |
| 3 | Publish graph traversal snapshot semantics; acceptance requires supported concurrent read and mutation patterns to be documented and traversal APIs to return stable results under those supported patterns. | M | (filled at issue-open time) |
| 4 | Restore Graph self-round-trip reconstruction; acceptance requires empty and populated `Graph.from_dict(graph.to_dict())` calls to succeed, preserve every Node and Edge UUID and property, rebuild equivalent adjacency, and handle parameterized Pile discriminators without metadata mismatch, after the Edge envelope repair in ADR-0001 delta 3. | M | (filled at issue-open time) |
| 5 | Preserve Edge label semantics through `Graph.splice_after`; acceptance requires omitted, empty, scalar, and non-empty-list labels to retain their documented presence and value on replacement Edges, with custom-property coverage. | S | (filled at issue-open time) |

The inherited Edge envelope repair is not duplicated in row 4; ADR-0001 delta 3 owns direct Edge
reconstruction, while row 4 owns Graph's parameterized nested-Pile and adjacency round trip.

## Alternatives considered

### Permit non-resident endpoints

This would support partially loaded or externally resolved graphs and make edge-first construction
possible. Every adjacency and path query would then need a missing-node result, and node removal
could not define a complete local cascade. The in-memory Graph chooses closed endpoint residency;
external/distributed references require a different graph contract.

### Scan the Edge Pile for every query

One canonical list would remove the risk of adjacency drift. Predecessor, successor, head, tail,
cycle, and BFS operations would repeatedly scan all Edges. The derived mapping trades memory and
mutation complexity for direct local adjacency, which matches the query-heavy consumers.

### Store adjacency only in the executing consumer

Operation scheduling could derive a private dependency map and leave generic Graph as two Piles.
Non-executable graph users would lose efficient structural queries, and each consumer would
reimplement endpoint validation and cascade cleanup. Graph owns reusable structural integrity;
execution state remains above it.

### Reject cycles at `add_edge`

Insertion-time cycle prevention would make every Graph a DAG and give operation consumers a strong
invariant. Generic relationship graphs may legitimately contain cycles and each insertion would need
a reachability check. Graph permits cycles, exposes explicit detection, and leaves DAG enforcement
to consumers that require it.

### Allow cycles and rely only on runtime deadlock detection

Omitting `is_acyclic` and topological-sort failure would simplify the model but make dependency
failure diagnosable only after execution stalls. The shipped structural queries let consumers detect
the problem before committing to execution without banning cycles globally.

### Immutable graph values

Persistent immutable Graph versions would give traversal stable snapshots and make concurrent reads
simple. Each edit would copy or structurally share Piles and adjacency, while current consumers rely
on in-place insertion and reactive changes. The shipped model remains mutable; stable snapshot
semantics are delta 3.

### One graph-wide transaction API

A transaction object could compose several edits with rollback and a single lock. Graph does not
know the consumer state that often needs to commit with those edits, and rollback for arbitrary
mutable Node/Edge properties is broader than adjacency. It exposes composable primitives; consumers
own higher-level transactions.

### Replace outgoing Edges in place during splice

Mutating each original Edge's head from anchor to new Node would preserve Edge identity and metadata.
It would change the meaning of an existing relationship without a new record and still require a
new anchor link. The current splice treats rewired relationships as new Edges, while `replace_node`
is the explicit identity-preserving primitive.

### Restrict Edge conditions to EdgeCondition

Requiring the Pydantic EdgeCondition base would align serialization and its `source` helper. The
generic `Condition` ABC deliberately permits custom async conditions, and tests pin that acceptance.
The narrower executable-operation contract is not silently imposed here; delta 2 requires the two
owners to choose one documented boundary.

## Notes

Graph's structural contract is intentionally narrower than operation-flow execution. Cycle refusal
at execution, predecessor waiting, path-condition scheduling, skip propagation, and reactive
runtime-state rollback are not Graph invariants and remain behind cross-references to the operations
area.
