# ADR-0078: First-Class Knowledge Layer

**Status**: Proposed
**Date**: 2026-06-03
**Related**: [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md)

## Context

### Problem

lionagi has first-class conversation state, operation orchestration, graph primitives, and
serialization primitives, but it does not have a first-class persistent knowledge substrate.
Knowledge today is either ephemeral in a `Branch` message history, external to lionagi core, or
unstructured text placed in prompts and context files.

The grounded primitives are already present:

- `Element` provides UUID identity, creation time, mutable metadata, polymorphic hydration, and
  `to_dict(mode="python" | "json" | "db")`; DB mode moves `metadata` to `node_metadata`
  (`lionagi/protocols/generic/element.py:52`, `lionagi/protocols/generic/element.py:169`,
  `lionagi/protocols/generic/element.py:186`).
- `Pile` is a thread-safe, ordered, UUID-keyed collection with include/update/get/filter/dump
  surfaces (`lionagi/protocols/generic/pile.py:169`, `lionagi/protocols/generic/pile.py:367`,
  `lionagi/protocols/generic/pile.py:456`, `lionagi/protocols/generic/pile.py:1064`).
- `Observable` is a structural protocol requiring only an `id` property, so compatible objects can
  satisfy it by shape without inheriting from a lionagi base class
  (`lionagi/protocols/contracts.py:21`).
- `Node` already carries arbitrary structured `content` and optional `embedding`
  (`lionagi/protocols/graph/node.py:22`); `Edge` already carries directed `head`/`tail` endpoints
  and relation `properties` (`lionagi/protocols/graph/edge.py:42`).
- `Graph` already stores `Node` and `Edge` as separate `Pile`s and rebuilds its in-memory
  adjacency index from those piles (`lionagi/protocols/graph/graph.py:44`,
  `lionagi/protocols/graph/graph.py:71`).
- `Branch` state is message-manager centric and serializes messages/logs/models, not knowledge
  (`lionagi/session/branch.py:117`, `lionagi/session/branch.py:741`). `Session` stores branches in
  a `Pile[Branch]` and attaches shared runtime services when a branch is included
  (`lionagi/session/session.py:48`, `lionagi/session/session.py:69`).
- `operate` builds `Instruct`/`ChatParam` context, dispatches through `middle`, and invokes actions
  after structured response extraction (`lionagi/operations/operate/operate.py:110`,
  `lionagi/operations/operate/operate.py:325`, `lionagi/operations/operate/operate.py:376`).
  `ReAct` repeatedly calls `operate` and already has a `between_rounds` injection point
  (`lionagi/operations/ReAct/ReAct.py:386`, `lionagi/operations/ReAct/ReAct.py:467`).

The missing layer is therefore not a new graph model. It is a pluggable protocol that gives
`Branch`, `Session`, `operate`, and `ReAct` a stable way to persist, query, and retrieve structured
knowledge across sessions while continuing to exchange lionagi-native `Element`, `Node`, `Edge`,
`Graph`, and `Pile` values.

## Decision

Define an Apache-2.0, storage-agnostic `Knowledge` protocol in lionagi core. The protocol is
structural and runtime-checkable: a backend satisfies it by exposing the required methods and an
`id`, without inheriting from a concrete base class. `Node`, `Edge`, `Graph`, and `Pile` remain the
public data shapes. The knowledge layer stores and retrieves those shapes; it does not replace
`Graph`.

Signature sketch:

```python
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import Field

from lionagi.protocols.contracts import Observable
from lionagi.protocols.generic import Element, ID, Pile
from lionagi.protocols.graph import Edge, Graph, Node


class KnowledgeScope(Element):
    kind: Literal["global", "project", "session", "branch", "agent"]
    name: str | None = None
    parent_id: ID.Ref | None = None


class KnowledgeQuery(Element):
    text: str | None = None
    node_ids: list[ID.Ref] | None = None
    edge_labels: list[str] | None = None
    metadata_filter: dict[str, Any] | None = None
    content_filter: dict[str, Any] | None = None
    semantic: bool = False
    include_edges: bool = True
    limit: int = 10


class KnowledgeWriteResult(Element):
    node_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    scope_id: str | None = None


@runtime_checkable
class Knowledge(Observable, Protocol):
    async def persist(
        self,
        items: Node | Edge | Graph[Node] | Pile[Node | Edge],
        *,
        scope: KnowledgeScope | None = None,
        upsert: bool = True,
    ) -> KnowledgeWriteResult:
        """Persist structured knowledge while preserving Element.id identity."""

    async def retrieve(
        self,
        refs: ID.Ref | Sequence[ID.Ref],
        *,
        scope: KnowledgeScope | None = None,
        include_edges: bool = False,
    ) -> Pile[Node | Edge] | Graph[Node]:
        """Retrieve concrete records by Element/UUID/string reference."""

    async def query(
        self,
        query: KnowledgeQuery | str,
        *,
        scope: KnowledgeScope | None = None,
    ) -> Pile[Node] | Graph[Node]:
        """Query by text, IDs, edge labels, metadata/content filters, or semantic intent."""

    async def neighborhood(
        self,
        node: ID.Ref,
        *,
        scope: KnowledgeScope | None = None,
        direction: Literal["in", "out", "both"] = "both",
        depth: int = 1,
        limit: int = 50,
    ) -> Graph[Node]:
        """Return a graph view around a node."""

    async def delete(
        self,
        refs: ID.Ref | Sequence[ID.Ref],
        *,
        scope: KnowledgeScope | None = None,
        soft: bool = True,
    ) -> KnowledgeWriteResult:
        """Delete or tombstone records without changing the public record shape."""

    def to_dict(
        self,
        mode: Literal["python", "json", "db"] = "python",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Serialize backend configuration or an in-memory snapshot."""
```

`Branch` and `Session` gain a knowledge attachment without changing the existing message contract:

```python
class Session(...):
    def __init__(self, *, knowledge: Knowledge | None = None, ...): ...

    def include_branches(self, branches: ID[Branch].ItemSeq):
        # Existing include path already attaches shared operation manager,
        # observer, and hooks; it also attaches the session knowledge backend.
        branch._knowledge = self.knowledge


class Branch(...):
    def __init__(self, *, knowledge: Knowledge | None = None, ...): ...

    @property
    def knowledge(self) -> Knowledge | None: ...

    async def remember(
        self,
        items: Node | Edge | Graph[Node] | Pile[Node | Edge],
        *,
        scope: KnowledgeScope | None = None,
    ) -> KnowledgeWriteResult: ...

    async def recall(
        self,
        query: KnowledgeQuery | str,
        *,
        scope: KnowledgeScope | None = None,
    ) -> Pile[Node] | Graph[Node]: ...
```

Across sessions, continuity comes from reusing a persistent backend with a stable scope. A new
`Session` configured with the same SQLite-backed `Knowledge` instance or descriptor can query the
same `Node`/`Edge` records created by prior sessions. A `Branch` stores conversation history through
the existing message manager; knowledge records are stored through the `Knowledge` protocol and may
carry provenance such as `session_id`, `branch_id`, tool name, or source message ID in
`Element.metadata`.

Failure modes:

- `persist(Edge)` must reject an edge whose endpoints are absent from the selected scope unless the
  backend explicitly supports deferred endpoint resolution.
- `retrieve` must return an empty `Pile`/`Graph` for missing optional refs or raise a clear
  not-found error when the caller asks for strict retrieval.
- `query(..., semantic=True)` may raise an explicit capability error if the backend has no embedding
  scorer; exact ID, label, metadata, and content filters must remain available.
- Backends must preserve `Element.id` and `created_at`; they must not mint replacement IDs during
  hydration.

## Scope

This ADR defines only the open-source seam lionagi ships: the `Knowledge` protocol, the
`KnowledgeScope`/`KnowledgeQuery`/`KnowledgeWriteResult` sketches, and two reference backends:
in-memory and SQLite. External systems plug in generically by satisfying `Knowledge`.

No backend-specific implementation detail beyond the two reference sketches is part of this
decision.

## Reference Backends

### In-Memory Backend

`InMemoryKnowledge` is the smallest conforming backend:

- stores nodes in `Pile[Node]` and edges in `Pile[Edge]`;
- maintains a `Graph[Node]` view for relation traversal, using `Graph.add_node`, `Graph.add_edge`,
  `Graph.find_node_edge`, `Graph.get_predecessors`, `Graph.get_successors`, and `Graph.find_path`;
- supports `persist`, `retrieve`, `query`, `neighborhood`, and `delete` in process;
- serializes snapshots with `Pile.to_dict()` and `Graph.to_dict()`;
- is appropriate for tests, examples, notebooks, and short-lived scripts, but not for cross-process
  persistence.

### SQLite Backend

`SQLiteKnowledge` is the local durable reference backend. It is not the same object as `StateDB`;
the existing state persistence helper keeps runtime objects DB-unaware and writes sessions through
`to_dict(mode="db")` (`lionagi/state/persist.py:4`, `lionagi/state/persist.py:31`,
`lionagi/state/persist.py:97`). The knowledge backend follows the same serialization convention
without making `Branch` or `Session` depend on SQLite.

Schema sketch:

```sql
CREATE TABLE knowledge_scopes (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT,
  parent_id TEXT,
  node_metadata JSON,
  created_at REAL NOT NULL
);

CREATE TABLE knowledge_nodes (
  id TEXT PRIMARY KEY,
  scope_id TEXT REFERENCES knowledge_scopes(id),
  created_at REAL NOT NULL,
  node_metadata JSON,
  content JSON,
  embedding BLOB
);

CREATE TABLE knowledge_edges (
  id TEXT PRIMARY KEY,
  scope_id TEXT REFERENCES knowledge_scopes(id),
  created_at REAL NOT NULL,
  node_metadata JSON,
  head TEXT NOT NULL REFERENCES knowledge_nodes(id),
  tail TEXT NOT NULL REFERENCES knowledge_nodes(id),
  properties JSON NOT NULL
);
```

The column shape intentionally mirrors existing state rows where messages and branches already use
`id`, `created_at`, `node_metadata`, content/config JSON, and optional embeddings
(`lionagi/state/schema.sql:41`, `lionagi/state/schema.sql:97`,
`lionagi/state/schema.sql:198`). The SQLite backend hydrates records through `Element.from_dict`
and returns `Node`, `Edge`, `Pile`, or `Graph` objects, never raw DB rows.

### External Backend Plug Point

Any external backend may satisfy `Knowledge` if it:

- has an `id` property and the protocol methods above;
- accepts and returns `Node`, `Edge`, `Graph`, `Pile`, `KnowledgeScope`, `KnowledgeQuery`, and
  `KnowledgeWriteResult` values;
- preserves `Element.id`, `created_at`, and `to_dict(mode="db")` metadata semantics;
- maps its native indexes, search, traversal, or persistence mechanisms behind the protocol without
  exposing those mechanisms to `Branch`, `Session`, `operate`, or `ReAct`.

## Integration

`Knowledge` is a sibling runtime manager to messages, actions, models, logs, operations, observer,
and hooks. The dependency direction is one-way:

```text
Session
|-- Branch
|   |-- MessageManager
|   |-- ActionManager
|   |-- OperationManager
|   `-- Knowledge protocol
`-- Knowledge protocol

Knowledge protocol --> Node / Edge / Graph / Pile / Element serialization
```

`operate` integration:

1. Add optional operation parameters:
   `knowledge_query: KnowledgeQuery | str | None = None` and
   `knowledge_write: Literal["none", "explicit", "tool_observations"] = "none"`.
2. During `prepare_operate_kw`, after `Instruct` construction and before `ChatParam` creation,
   `Branch.recall(knowledge_query)` resolves knowledge into a compact `Graph` or `Pile` projection
   and appends its `to_dict(mode="json")` output to `Instruct.context`.
3. After action invocation, if `knowledge_write == "tool_observations"`, structured tool
   observations that are already `Node`, `Edge`, `Graph`, or `Pile[Node | Edge]` are passed to
   `Branch.remember`. Raw strings are not auto-promoted to knowledge.
4. If no backend is attached, `operate` behaves exactly as it does today: messages are added to the
   branch, actions run, and no knowledge read/write occurs.

`ReAct` integration:

1. ReAct inherits `operate` knowledge behavior because each round calls `operate`.
2. `between_rounds` may call `Branch.recall` to inject updated knowledge between analysis rounds.
3. ReAct intermediate analysis is not automatically durable knowledge; only explicit `Node`/`Edge`/
   `Graph`/`Pile` values or configured tool observations are persisted.

`Graph`/`Node` relationship:

- `Node.content` is the structured payload for facts, observations, entities, or arbitrary typed
  `Element` content; `Node.embedding` is the optional semantic query vector.
- `Edge.properties["label"]` is the relation label surface; additional relation data stays in
  `properties`.
- `Graph` is the returned view for relationship queries and neighborhoods. It remains an in-memory
  projection with serialized node/edge piles; `node_edge_mapping` remains a runtime index rebuilt
  during hydration.

Serialization:

- All knowledge records use `to_dict(mode="python" | "json" | "db")`.
- SQLite and external durable backends use DB mode so `metadata` is stored as `node_metadata`.
- Backend configuration may be serialized separately, but `Branch.to_dict()` does not inline a full
  durable backend. Branch snapshots may include only a backend reference or scope metadata if Ocean
  accepts that follow-up.

Coupling estimate: the new design adds direct dependencies from `Session`, `Branch`, `operate`, and
`ReAct` to the `Knowledge` protocol, while the protocol depends on existing data primitives. With
five runtime components (`Session`, `Branch`, `operate`, `ReAct`, `Knowledge`) and four directed
runtime dependencies, kappa = 4 / (5 x 4) = 0.20, below the 0.30 target.

## Alternatives Considered

| Alternative | Trade-off |
|-------------|-----------|
| Extend `Graph` into a persistent graph subclass | Reuses existing graph methods, but couples persistence, query semantics, and runtime adjacency to an in-memory data structure whose adjacency map is intentionally excluded from serialization. It also makes external backends emulate `Graph` internals instead of satisfying a stable protocol. |
| Adapter-only: keep `Branch`/`Session` unaware and add adapters for `Graph`/`Pile` | Lowest immediate API cost, but knowledge remains external to operations. `operate` and `ReAct` would have no common way to retrieve context or persist structured observations across sessions. |
| Claim-specific API from ADR-0039 | Provides a stronger lifecycle opinion, but it introduces a claim/evidence vocabulary before the current task has settled the OSS storage seam. The first layer should be typed around existing `Node`/`Edge`/`Graph` primitives; claim lifecycle can be layered later as typed `Node.content` or a specialized backend policy. |
| Chosen: new structural `Knowledge` protocol over existing primitives | Adds a small first-class API, keeps storage pluggable, preserves `Element` serialization, and lets in-memory, SQLite, and generic external backends satisfy the same contract without inheritance. |

## Consequences

**Positive**

- Knowledge becomes persistent, queryable, and cross-session when a durable backend is configured.
- `Branch` and `Session` gain a stable API without depending on a specific database.
- Existing `Element`, `Node`, `Edge`, `Graph`, `Pile`, and `to_dict` semantics remain the public
  contract.
- External storage remains behind an OSS protocol seam.

**Negative**

- The protocol adds a new manager surface to `Branch` and `Session`.
- SQLite reference behavior must define capability errors for semantic query if no vector scorer is
  configured.
- The minimal `Node`/`Edge` contract leaves domain-specific lifecycle rules to later typed content,
  backend policy, or a follow-up ADR.

## Open Questions For Ocean

1. Should a default `Branch()` attach no knowledge backend, an in-memory backend, or a no-op backend?
2. What is the accepted scope precedence when global, project, session, branch, and agent scopes all
   match a query?
3. Should `Branch.to_dict()` persist a backend descriptor/scope reference, or should backend
   selection remain entirely constructor/configuration driven?
4. Should semantic query in SQLite require an optional vector dependency, perform a simple local scan,
   or raise a capability error until configured?
5. Does ADR-0078 supersede ADR-0039, or should ADR-0039 remain as a later claim-lifecycle layer over
   this protocol?
6. Should automatic retrieval during `operate` be opt-in per call, opt-in per branch/session config,
   or only exposed through explicit `Branch.recall` calls?

## References

- Issue #1175: `feat: add knowledge substrate as first-class concept in lionagi`
- [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md)
- `lionagi/protocols/contracts.py`
- `lionagi/protocols/generic/element.py`
- `lionagi/protocols/generic/pile.py`
- `lionagi/protocols/graph/node.py`
- `lionagi/protocols/graph/edge.py`
- `lionagi/protocols/graph/graph.py`
- `lionagi/session/branch.py`
- `lionagi/session/session.py`
- `lionagi/operations/operate/operate.py`
- `lionagi/operations/ReAct/ReAct.py`
- `lionagi/state/persist.py`
- `lionagi/state/schema.sql`
