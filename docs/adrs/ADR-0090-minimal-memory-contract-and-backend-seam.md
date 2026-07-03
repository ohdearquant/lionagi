# ADR-0090: Minimal Memory Contract and Pluggable Backend Seam

**Status**: Proposed
**Date**: 2026-07-03

## Context

An audit of closed issues against the current tree found the 2025 Memory System epic
(#488-#495, plus #566) describes capability that never landed anywhere in `lionagi/` under any
name. #1683 consolidates that epic into a single scope-and-contract decision; #495 is reopened
as the epic's tracking anchor; #566 is reopened because its 2025-04-22 "COMPLETED, superseded by
the stateless-core refactor" close is not supported by the source (the four issues it redirected
to are themselves closed NOT_PLANNED, and no `MemoryManager`, `branch.memory` proxy, or
conversation-summarization surface exists today under any name).

### What already exists (source-verified)

- `lionagi/protocols/generic/element.py` (`Element`): UUID + timestamp + metadata identity, the
  base every typed record in the tree builds on.
- `lionagi/protocols/generic/pile.py` (`Pile`): an O(1) UUID-keyed, thread/async-safe collection,
  already the storage primitive behind `Branch.messages`, `Session.branches`, and every other
  in-process collection in the tree.
- `lionagi/session/branch.py` (`Branch`): a thin facade over four managers
  (`MessageManager`, `ActionManager`, `iModelManager`, `DataLogger`), each exposed as a property
  (`.msgs`, `.acts`, `.mdls`, `.logs`). `Session.include_branches()` (`lionagi/session/session.py`)
  wires shared, session-scoped resources into each branch it takes in:
  `branch._operation_manager`, `branch._observer`, `branch._hooks`. This is the existing
  precedent for "a session-scoped resource made available on every branch it owns," and this ADR
  reuses that precedent rather than inventing a new wiring mechanism.
- `lionagi/tools/context/context.py` (`ContextTool`): caller-invoked eviction/restore/compaction
  of a *single branch's own* active message view. It curates what one branch sees of its own
  conversation; it does not share context across branches or sessions, and it is not a
  persistence or recall surface. It is the nearest adjacent tool and is explicitly out of this
  ADR's scope, not a precedent to build on.
- `lionagi/state/db.py` + `lionagi/state/schema.sql`: `StateDB`, a SQLAlchemy-Core async backend
  (SQLite by default, Postgres-pluggable per ADR-0086) for CLI/Studio run bookkeeping: sessions,
  branches, plays, shows, schedules, artifacts. It persists conversation *history* for the CLI's
  own run-tracking needs. It is not a general-purpose application memory abstraction, and `Branch`
  and `Session` (the library's core objects) work identically whether or not any `StateDB` is in
  play, and a library consumer never needs one to use `Branch.chat()`/`operate()`. Coupling a new
  core memory contract's default backend to this CLI-scoped layer would force that dependency onto
  every library consumer, which is the wrong default.
- `lionagi/config.py`: `LIONAGI_STORAGE_PROVIDER: str = "async_qdrant"` has zero runtime consumers
  (only `config.py` itself and `tests/test_config.py` reference it) and names a client
  (`qdrant-client`) that is not a dependency of this package under any extra: `grep qdrant
  pyproject.toml` returns nothing. `LIONAGI_QDRANT_URL` and `LIONAGI_DEFAULT_QDRANT_COLLECTION`
  are the same: config-only, zero consumers. Checking the rest of that same block (lines 62-74,
  the same never-built storage/embedding scaffold) for the same pattern turns up four more fields
  with identical zero-runtime-consumer shape: `LIONAGI_EMBEDDING_PROVIDER`, `LIONAGI_EMBEDDING_MODEL`,
  `LIONAGI_AUTO_STORE_EVENT`, `LIONAGI_AUTO_EMBED_LOG` (`grep -rn` for each across `lionagi/` and
  `tests/` returns only `config.py`'s own definition and `tests/test_config.py`'s default-value
  assertions). `LIONAGI_STATE_DB_URL`, defined in the same neighborhood, is explicitly excluded from
  this list: it has a real consumer (`lionagi/state/db.py:297`) and stays.
- `lionagi/state/schema.sql`'s `messages.embedding` column (`BLOB, -- packed float32 vec or NULL;
  sqlite-vec indexes these`) has a full write path (`StateDB.insert_message()` binds
  `msg.get("embedding")`; `cli/state.py` passes it through) but zero producers: no call site in
  the tree ever sets a message's `.embedding` field. The field itself is inherited from
  `lionagi/protocols/graph/node.py`'s `Node.embedding: list[float] | None`, a general graph
  primitive used for arbitrary node/embedding workflows, and that field is not dead and is out of
  scope here. What is dead is the SQL column's write/read path for *messages specifically*: it is
  wired end to end and populated by nothing, the same shape of dead surface as
  `LIONAGI_STORAGE_PROVIDER`, one layer lower.
- No memory-oriented `store`/`retrieve`/`search` contract, no concrete vector/graph/document
  store, and no unified access surface exist anywhere in the tree today.

### The crux

Two positions were on the table (per #1683): lionagi core stays persistence-agnostic and only
documents a seam external memory systems attach to, or core owns a minimal contract plus a
default backend so the framework has a real out-of-the-box memory story. Ocean's ruling (#1683,
2026-07-03) settles this as both, combined: a minimal async contract and one thin default backend
in core, with the documented seam as the production path for anything beyond that default. This
ADR encodes that ruling (contract shape, default backend choice, seam documentation, access
surface, and the two dead-surface dispositions) as the design record #1683 asks for.

## Decision

Core gets one Protocol (`store`/`retrieve`/`search` over a typed item built on `Element`, no new
base-class hierarchy), one thin in-process default backend built on `Pile`, and a single read-only
`.memory` property on `Branch`/`Session` that reuses the existing `_observer`/`_hooks` wiring
pattern for sharing. Everything beyond that default (vector search, graph traversal, managed memory
services) is the pluggable seam's job, documented but not implemented in core.

### 1. The typed item and the contract

```python
class MemoryItem(Element):
    """A single stored memory record."""
    content: Any = None
    tags: list[str] = Field(default_factory=list)
    # `metadata` (inherited from Element) carries provenance: branch_id, source, etc.


class MemoryQuery(BaseModel):
    """Search parameters, kept as data rather than a query-string DSL."""
    text: str | None = None
    tags: list[str] | None = None
    filters: dict[str, Any] | None = None
    limit: int = 20


class MemoryStore(Protocol):
    async def store(self, item: MemoryItem) -> UUID: ...
    async def retrieve(self, item_id: UUID) -> MemoryItem | None: ...
    async def search(self, query: MemoryQuery) -> list[MemoryItem]: ...
```

`MemoryItem` is an `Element` subclass, not a new base class: it gets UUID identity, a creation
timestamp, and a metadata dict for free, exactly like every other typed record in the tree
(`Message`, `Node`, `Log`). `store`/`retrieve`/`search` are the entire contract. No `update`,
`delete`, `list`, transactions, or pooling: those are exactly the kind of thing an external memory
system is free to add on top of `store`+`retrieve`+`search`, but core does not need them to have
a working memory story, and adding them now would be speculative generality against no named
consumer. Even minimal service memory contracts elsewhere in the ecosystem often include
`delete`; it is left out here until a named consumer needs it, consistent with not adding methods
speculatively.

`MemoryQuery.filters` exists so `metadata` is not a dead end: `MemoryItem.metadata` is documented
above as carrying provenance (`branch_id`, `source`, and similar), and a query object with only
`text`/`tags` would give no way to filter on it, forcing any backend that needs "match where
source=X" to bypass the Protocol immediately. `filters` is a plain `dict[str, Any]`, not a second
DSL: `text` and `tags` stay the fast, structured common path, and `filters` lets vector, graph, and
managed-service backends express metadata predicates in the same typed shape instead of inventing
their own. Query scope (this branch's memories vs. this session's vs. global) is not a query
parameter at all: it is structural, determined by which `MemoryStore` instance the caller holds
(see [§6](#6-access-surface-566s-answer)), which keeps the query object itself backend-agnostic.

### 2. The default backend: in-process, `Pile`-backed, zero new dependencies

```python
class InMemoryStore:
    """Default MemoryStore: a Pile[MemoryItem] with substring/tag search."""
    def __init__(self) -> None:
        self._items: Pile[MemoryItem] = Pile(item_type={MemoryItem})

    async def store(self, item: MemoryItem) -> UUID: ...
    async def retrieve(self, item_id: UUID) -> MemoryItem | None: ...
    async def search(self, query: MemoryQuery) -> list[MemoryItem]: ...
```

**Decision: in-process `Pile`-backed, not SQLite-backed of any kind.** Three backend shapes were
weighed, not two, because "SQLite" is not one option:

- **Pure in-memory, `Pile`-backed** (chosen): zero new dependencies (`Pile` is already core),
  zero setup (works the instant a `Branch` is constructed, no file, no schema migration), and
  correctly scoped: it is a library-level feature, so it must not require the CLI/Studio state
  layer to function. The cost is that it does not survive process exit; a `Branch` used as a
  library object in a one-shot script loses its memory when the script ends.
- **SQLite via `StateDB`** (rejected as the *default*): real persistence across process restarts
  "for free" in the CLI, since `sqlalchemy[asyncio]` and `aiosqlite` are already core dependencies
  post-ADR-0086. Rejected as the default because `StateDB`'s schema and lifecycle are scoped to
  CLI/Studio run bookkeeping (sessions, branches, plays, artifacts), not general application
  memory; wiring a core `Branch` feature through it would mean every library consumer who never
  touches the CLI state layer pays for (and depends on) machinery that exists for a different
  purpose.
- **SQLite via the stdlib `sqlite3` module directly, with its own small table** (rejected as the
  default, kept as a documented seam example, not the same thing as the `StateDB` option above):
  no `StateDB`/`SQLAlchemy` coupling at all, so it avoids that option's objection, and it would
  give real cross-restart persistence with zero third-party dependencies since `sqlite3` is
  stdlib. Still rejected as the *default* rather than promoted to a second shipped backend,
  because the ruling asks for one thin default backend, and a stdlib-only file-backed store still
  carries file-lifecycle questions (where does the file live, who cleans it up, concurrent-process
  access) that the in-process default sidesteps entirely for the zero-config case. It is exactly
  the kind of small, dependency-free recipe the seam should document.

A related, still-open design (PR #1264, `docs/adrs/ADR-0078-first-class-knowledge-layer.md`,
Status: Proposed, not merged) proposes a first-class Knowledge protocol over the same primitives
(`Element`/`Node`/`Graph`/`Pile`) with two shipped reference backends, in-memory and SQLite. That
proposal is a different, broader problem (persistent, queryable, cross-session knowledge, built on
`Graph` traversal) than this ADR's narrower one (a `Branch`/`Session`-scoped conversational memory
contract), so shipping only one reference backend here is not an inconsistency with it, but the
divergence is worth naming rather than leaving silent: if that sibling proposal lands, its
SQLite reference backend is a candidate the seam-documentation slice here could point to instead
of writing a second one from scratch.

The tradeoff is explicit and accepted: v1's default memory does not persist across process
restarts. Anything that needs to persist across restarts, or search semantically rather than by
substring/tag, is precisely what the seam below is for.

### 3. The pluggable seam is the production path

`MemoryStore` is the only contract an external memory system needs to implement to plug into a
`Branch` or `Session`. Core never grows backend-specific logic for any of these; they are named
here only as the category of thing the seam accepts, not a commitment to build or bundle any of
them:

- **Vector stores** (semantic search over embedded content).
- **Graph stores** (relationship-aware retrieval).
- **Managed memory services** (hosted, cross-session memory products).

An implementer of any of these writes a class satisfying `store`/`retrieve`/`search` and hands it
to `Branch(memory=...)` or `Session(memory=...)`; nothing else in core changes. This is the
production path for real workloads; the in-process default exists so core has *a* working answer,
not *the* answer for everyone.

### 4. Explicitly declined in core

The 2025 epic's heavier scaffold is declined, each for a specific reason, not a blanket "too much
work":

| Declined | Reason |
|---|---|
| Memory tiers (working/long-term/episodic) | No named consumer needs tiering yet; it is exactly the kind of policy an external memory system should own, not core. |
| Attention mechanisms | A model-architecture concept, not a storage-contract concept; does not belong behind `store`/`retrieve`/`search`. |
| Consolidation / summarization engine | Overlaps `ContextTool`'s caller-driven `compact` action, which already exists and is caller-controlled; a second, automatic consolidation path would compete with it rather than complement it. |
| Resource monitors | Operational tooling for a specific deployment, not a contract concern. |
| LlamaIndex integration | Ties core to one third-party framework's node/index model for zero contract benefit; an adapter belongs in the seam (an external `MemoryStore` implementation), never in core. |

### 5. Dead-surface disposition

Both surfaces named in #1683 get a verdict, verified against the current tree (see
[Context](#what-already-exists-source-verified) above), not left ambient:

- **`LIONAGI_STORAGE_PROVIDER` (`"async_qdrant"` default) + `LIONAGI_QDRANT_URL` +
  `LIONAGI_DEFAULT_QDRANT_COLLECTION` + `LIONAGI_EMBEDDING_PROVIDER` + `LIONAGI_EMBEDDING_MODEL` +
  `LIONAGI_AUTO_STORE_EVENT` + `LIONAGI_AUTO_EMBED_LOG`: REMOVE, all seven.** #1683 names the
  `async_qdrant` default specifically, and that trio is zero-consumer as described. Verifying the
  rest of the same never-built storage/embedding block in `config.py` (see
  [Context](#what-already-exists-source-verified)) turns up four more fields with the identical
  shape: defined, asserted on in `tests/test_config.py`, and read by nothing else in the tree. All
  seven are the same disposition for the same reason, so they get the same verdict rather than
  cleaning three and leaving four siblings behind in an inconsistent half-cleaned state. The
  client the storage default names (`qdrant-client`) is not installable through this package under
  any extra. If Qdrant support, or a configurable embedding provider, is wanted later, it belongs
  as a documented seam example (a `MemoryStore` implementation an application supplies), not a
  hardcoded core config default with nothing behind it.
- **`messages.embedding` column (`lionagi/state/schema.sql`) and its write path
  (`StateDB.insert_message()`, `cli/state.py`): REMOVE.** Fully wired (bind param, upsert clause,
  passthrough) and populated by nothing: no call site in the tree ever sets a message's
  `.embedding`. Removing it is a schema migration (SQLite `ALTER TABLE ... DROP COLUMN`, or the
  rebuild-table pattern `StateDB` already uses for `schedules` if the SQLite version in use
  predates column-drop support) plus deleting the four call sites that reference it
  (`lionagi/state/db.py:787-808`, `lionagi/state/schema_meta.py:53`, `lionagi/cli/state.py:61`,
  `lionagi/adapters/async_postgres_adapter.py:70`). This is a separate concern from `Node.embedding`
  (`lionagi/protocols/graph/node.py`), which stays: it is a general graph-primitive field with
  uses outside message persistence and is not part of this disposition. If a future feature wants
  semantic search over conversation history, it should land as its own ADR with a real producer
  and consumer in the same change, not by resurrecting idle plumbing.

### 6. Access surface (#566's answer)

Issue #566 asked for "a consistent approach to how branches share or retrieve context in a session,"
and its 2025 design proposed a `MemoryManager` class plus a `branch.memory` proxy. The ruled
direction is explicit that the answer is a minimal access surface to the contract above, not a
manager subsystem and not a proxy layer. The smallest coherent shape, given `Branch`'s existing
manager-facade pattern and `Session.include_branches()`'s existing resource-wiring precedent:

```python
class Branch(Element, Relational):
    _memory: MemoryStore | None = PrivateAttr(None)

    def __init__(self, *, memory: MemoryStore | None = None, ...):
        ...
        self._memory = memory

    @property
    def memory(self) -> MemoryStore:
        if self._memory is None:
            self._memory = InMemoryStore()
        return self._memory
```

```python
class Session(Node, Relational):
    _memory: MemoryStore | None = PrivateAttr(None)

    @property
    def memory(self) -> MemoryStore:
        if self._memory is None:
            self._memory = InMemoryStore()
        return self._memory

    def include_branches(self, branches):
        def _take_in_branch(branch: Branch):
            ...
            if branch._memory is None:
                branch._memory = self.memory  # reads the property: lazily
                # creates the session's own store on first use, then shares
                # that instance, the same target state `_observer`/`_hooks`
                # wiring reaches, but conditionally, not unconditionally: a
                # branch explicitly constructed with its own store keeps it.
            ...
```

There is deliberately no `@memory.setter` on either class. The only way to give a `Branch` or
`Session` its own store is the constructor parameter; after construction, `.memory` is read-only.
This is narrower than mirroring `_observer`/`_hooks` verbatim (their attributes are plain and
reassignable), and the narrowing is intentional: a public setter would let a caller hot-swap a
live store out from under in-flight `store`/`search` calls, which is exactly the kind of
write-conflict and lifecycle ambiguity a "minimal access surface" should not introduce. Constructor
param plus a lazy read-only property is the smallest surface that still satisfies both "a
standalone `Branch` gets a usable default with zero configuration" and "an explicitly constructed
`Branch`/`Session` can supply its own backend."

`Branch.memory` and `Session.memory` are direct references to a `MemoryStore`, not a wrapper
object: `branch.memory.search(...)` calls the Protocol method directly. There is no
`BranchMemoryProxy`, no key-namespacing-by-branch-id indirection, and no separate manager class:
the object behind the property already satisfies the whole contract, so wrapping it would add a
layer with nothing to do. A standalone `Branch` (no `Session`) lazily gets its own private
`InMemoryStore` on first access, so `branch.memory.store(...)` works with zero configuration. A
`Branch` taken into a `Session` via `include_branches()` inherits the session's shared store the
same way it already inherits `_observer` and `_hooks`, mirroring the one cross-branch-sharing
mechanism the codebase already has, rather than inventing a second one. This directly answers
Issue #566's "consistent approach to how branches share context in a session": the session's default
store is the shared instance every branch it owns sees, unless a branch is explicitly constructed
with its own.

Two things this ADR does not need to solve, and says so rather than leaving ambient: connection
lifecycle for networked backends (a vector-store or managed-service `MemoryStore` that owns a
socket or client handle) is the seam implementer's responsibility; core provides no close/teardown
hook for `.memory`, the same way it provides none for `.acts`/`.mdls` today. And concurrent writes
from multiple branches into one shared store are not a new hazard: `Pile` is already
thread/async-safe, so the default backend serializes them the same way it already serializes
concurrent access to `Branch.messages`.

## Consequences

**Positive**

- Core ships a real, working memory story (`Branch().memory.store(...)`) with zero new
  dependencies and zero required configuration.
- The contract is small enough that an external system (vector store, graph store, managed
  service) can satisfy it in well under a hundred lines, and core never special-cases any of them.
- The access surface reuses an existing wiring mechanism (`Session.include_branches()`) instead of
  inventing a manager subsystem or proxy layer, keeping `Branch`'s facade thin per its existing
  design.
- Two genuinely dead-surface clusters (a seven-field storage/embedding config block naming an
  uninstallable client, a schema column with a write path and no producer) are resolved with a
  verdict instead of staying ambient.
- #566 and #495 both get a real, source-grounded answer instead of a redirect to closed issues.

**Negative**

- The default backend does not persist across process restarts; a library consumer who wants that
  must supply their own `MemoryStore` (e.g., a SQLite-backed one) via the seam from day one: v1
  does not hand them a persistent option out of the box.
- Default `search()` is substring/tag matching, not semantic. Consumers who need semantic recall
  must supply a vector-store-backed `MemoryStore`; core does not compute embeddings for them.
- Removing `messages.embedding` is a schema migration touching four call sites in the CLI/Studio
  state layer, and must be sequenced so it does not race ADR-0086's Postgres-adapter work on the
  same table.
- Declining consolidation/summarization in core means any future automatic (non-caller-invoked)
  summarization still has no home; it was evaluated and explicitly left for a future ADR, not
  solved here.

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| Stay fully persistence-agnostic (position 2 alone, no default backend) | Rejected by Ocean's ruling: core would have zero out-of-the-box memory story, only documentation, which is weaker than "a real standalone memory story" the ruling asks for. |
| Own the whole 2025 epic scaffold (tiers, attention, consolidation, resource monitors, LlamaIndex) | Explicitly declined; no named consumer for any of it today, and each piece competes with or duplicates an existing, narrower mechanism (see [Explicitly declined](#4-explicitly-declined-in-core)). |
| Default backend on `StateDB`/SQLite | Persistent "for free," but couples a core `Branch` feature to the CLI/Studio run-bookkeeping schema; every library consumer would depend on machinery scoped to a different purpose. Documented as a seam example instead. |
| Default backend on stdlib `sqlite3` directly, own table, no `StateDB` coupling | Avoids the `StateDB` objection above, but still adds file-lifecycle questions (file location, cleanup, concurrent-process access) that the in-process default has none of; the ruling asks for one thin default, not a second shipped backend. Documented as a seam example, same as the `StateDB` option, not promoted to default. |
| A `BranchMemoryProxy` / `MemoryManager` (the original #566 design) | The manager-subsystem and proxy-layer shapes the ruled direction explicitly rejects; the object behind `.memory` already satisfies the Protocol, so a wrapper adds indirection with no behavior to justify it. |
| Wire `LIONAGI_STORAGE_PROVIDER` to an actual Qdrant client | Would add a new dependency to satisfy a config default with zero current consumers; Qdrant support belongs as a seam example an application supplies, not a hardcoded core default. |
| Keep `messages.embedding` as reserved-for-later | Leaves a fully-wired, always-NULL column ambient indefinitely, the exact state #1683 asks to resolve. A future producer/consumer pair should arrive with its own schema change, not resurrect idle plumbing. |
| Query-string DSL for `search()` instead of a typed `MemoryQuery` | A string DSL invites backend-specific query leakage (the same leak `capabilities()` prevents in ADR-0089's sandbox seam); a small typed query object keeps every backend answering the same shape of question. |

## Implementation fences

- **MAY**: add `MemoryItem`, `MemoryQuery` (including its `filters: dict[str, Any] | None` field),
  `MemoryStore` (Protocol), and `InMemoryStore` in a new module (e.g. `lionagi/protocols/memory.py`,
  beside the other structural-typing concepts in `protocols/_concepts.py`); add
  `Branch(memory=...)` / `Session(memory=...)` constructor parameters, `_memory` `PrivateAttr`s,
  and read-only `.memory` properties (no setter); extend `Session.include_branches()` to wire
  `branch._memory` from `self.memory` (the property, not the raw private attribute) the same way it
  wires `_operation_manager`/`_observer`/`_hooks`, guarded by `if branch._memory is None`; write a
  SQLite-backed `MemoryStore` as a documented seam *example* (stdlib `sqlite3` or `StateDB`-backed,
  either as a recipe, not a core default); remove `LIONAGI_STORAGE_PROVIDER`, `LIONAGI_QDRANT_URL`,
  `LIONAGI_DEFAULT_QDRANT_COLLECTION`, `LIONAGI_EMBEDDING_PROVIDER`, `LIONAGI_EMBEDDING_MODEL`,
  `LIONAGI_AUTO_STORE_EVENT`, `LIONAGI_AUTO_EMBED_LOG` from `lionagi/config.py`; remove the
  `messages.embedding` column and its four call sites in a dedicated migration.
- **MAY NOT**: add memory tiers, attention mechanisms, an automatic consolidation engine, resource
  monitors, or a LlamaIndex adapter to core; give `MemoryStore` any method beyond
  `store`/`retrieve`/`search` without a new ADR; give `MemoryQuery` a string query-DSL field instead
  of the typed `filters` dict; make the default backend depend on `StateDB`, SQLAlchemy, or any new
  third-party package; add a public `@memory.setter` to `Branch` or `Session`; introduce a
  `BranchMemoryProxy`, `MemoryManager`, or any other subsystem/proxy between `Branch`/`Session` and
  the `MemoryStore` instance; touch `Node.embedding` (`lionagi/protocols/graph/node.py`) as part of
  the `messages.embedding` cleanup, since they are unrelated surfaces; remove or wire any dead
  config field or the dead column without deleting every call site named above in the same change.
- **Verify by**: (1) a `Branch()` constructed with no arguments can `await
  branch.memory.store(MemoryItem(content=...))` and `await branch.memory.search(MemoryQuery(text=
  ...))` and get it back, with zero configuration and zero new dependencies imported; (2) a
  `Session` with two branches taken in via `include_branches()` share one `MemoryStore` instance
  (writing through one branch's `.memory` is visible through the other's); (3) a fake
  `MemoryStore` satisfying only the Protocol (no inheritance from `InMemoryStore`) can be passed
  to `Branch(memory=...)` and used identically, proving the seam does not require subclassing;
  (4) after the dead-surface cleanup slice, `grep -rn "LIONAGI_STORAGE_PROVIDER\|LIONAGI_QDRANT\|
  LIONAGI_EMBEDDING\|LIONAGI_AUTO_STORE_EVENT\|LIONAGI_AUTO_EMBED_LOG\|messages.embedding"
  lionagi/` returns nothing outside historical CHANGELOG entries, and the full test suite
  (including `tests/test_config.py` and the `StateDB` message round-trip tests, updated for the
  dropped column and fields) passes.

## Slice plan

- **Slice 1, contract + default backend.** `MemoryItem`, `MemoryQuery`, `MemoryStore` Protocol,
  `InMemoryStore`; unit tests exercising `store`/`retrieve`/`search` directly against the Protocol
  (not just the default implementation), so a future backend has a contract test to run against.
- **Slice 2, access surface.** `Branch(memory=...)` / `Session(memory=...)`, the read-only `.memory`
  property, and the `include_branches()` wiring; tests proving the sharing and
  standalone-lazy-default behavior in [Verify by](#implementation-fences) items 1-2.
- **Slice 3, dead-surface cleanup.** Remove `LIONAGI_STORAGE_PROVIDER` + `LIONAGI_QDRANT_URL` +
  `LIONAGI_DEFAULT_QDRANT_COLLECTION` + `LIONAGI_EMBEDDING_PROVIDER` + `LIONAGI_EMBEDDING_MODEL` +
  `LIONAGI_AUTO_STORE_EVENT` + `LIONAGI_AUTO_EMBED_LOG` from `config.py`; migrate `schema.sql` to
  drop `messages.embedding` and remove its four call sites; update `tests/test_config.py` and any
  `StateDB` message tests that assert on the removed surfaces. Ordered last so the contract and
  access surface (which do not touch the CLI state layer at all) can land and be reviewed
  independently of the schema migration's blast radius.
- **Deferred (seam documentation, not core code).** A worked SQLite-backed `MemoryStore` example
  and a worked vector-store-backed example in developer docs, demonstrating the seam without
  adding either as a core dependency.

## Open Questions for Ocean

- **Module location.** This ADR proposes `lionagi/protocols/memory.py`. An alternative is a new
  top-level `lionagi/memory/` package if the seam-documentation slice grows large enough to want
  its own subdirectory (multiple worked backend examples, contract-test helpers). Either is a
  small, reversible choice; flagging it rather than silently picking one.
- **Migration sequencing for `messages.embedding`.** Slice 3's schema migration should avoid
  colliding with any in-flight ADR-0086 Postgres-adapter work on the same table
  (`async_postgres_adapter.py` also defines the `embedding` column). This ADR assumes slice 3 is
  sequenced after that work is stable, but the exact ordering is a scheduling call, not a design
  one.

## References

- `lionagi/protocols/generic/element.py`
- `lionagi/protocols/generic/pile.py`
- `lionagi/protocols/_concepts.py`
- `lionagi/session/branch.py`
- `lionagi/session/session.py`
- `lionagi/tools/context/context.py`
- `lionagi/state/db.py`
- `lionagi/state/schema.sql`
- `lionagi/state/schema_meta.py`
- `lionagi/cli/state.py`
- `lionagi/adapters/async_postgres_adapter.py`
- `lionagi/protocols/graph/node.py`
- `lionagi/config.py`
- [ADR-0086](ADR-0086-statedb-sqlalchemy-core-backend-unification.md)
- [ADR-0089](ADR-0089-sandbox-backend-seam-and-measurement-loop.md)
- PR #1264 (open, not merged: a sibling "first-class Knowledge protocol" design over the same
  primitives, with in-memory and SQLite reference backends; related but broader scope than this
  ADR, see [§2](#2-the-default-backend-in-process-pile-backed-zero-new-dependencies))
- Issue #1683 (consolidating decision)
- Issue #566 (reopened; access-surface ask answered by [§6](#6-access-surface-566s-answer))
- Issue #495 (reopened; tracking anchor)
