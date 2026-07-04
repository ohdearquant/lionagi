# ADR-0091: khive as a Pluggable lionagi MemoryStore Backend

**Status**: Accepted
**Date**: 2026-07-04

## Context

ADR-0090 (merged, PR #1687) defined a minimal `MemoryStore` Protocol and a zero-dependency
`InMemoryStore` default, deliberately leaving richer backends (vector stores, graph stores,
managed memory services) as a documented seam rather than shipping them in core. Neither ADR-0090
slice 1 (Protocol types + `InMemoryStore` + a Protocol-level test fence) nor slice 2
(`Branch`/`Session` access surface) has landed in code yet -- only the ADR document itself is
merged.

Separately, Ocean issued a P0/strategic directive (relayed by Leo, gtd anchor `5018674e`) to
integrate khive -- Ocean's own knowledge/graph/memory substrate, already the memory layer across
the wider Lion ecosystem -- as a pluggable backend behind exactly this seam. The explicit posture
constraint: khive must stay an opt-in backend, never a hard dependency, so lionagi's Apache-2.0
OSS core stays clean and khive remains something users choose to plug in, not something baked
into lionagi's own package graph. This keeps lionagi acting as a distribution funnel toward khive
rather than coupling the OSS project to a commercial product.

This ADR is a joint product of a lambda:lionagi / lambda:khive design conversation (opened and
closed 2026-07-04), scoped against both codebases' actual source rather than paraphrased
assumptions, and captures Leo's sequencing ruling to bundle ADR-0090 slice 1 with this integration
rather than landing it separately first.

## Decision

### Sequencing: bundle ADR-0090 slice 1, do not serialize it

(Leo ruling, 2026-07-04.) ADR-0090 slice 1 (the `MemoryItem`/`MemoryQuery`/`MemoryStore` Protocol
types, `InMemoryStore`, and a Protocol-level test fence exercising the Protocol directly, not just
the default implementation) lands as part of this same effort, with `KhiveMemoryStore` as the
concrete second implementor that fence already anticipated. ADR-0090 slice 2 (the
`Branch`/`Session` access surface: `memory` constructor param, `_memory` PrivateAttr, lazy
read-only property, `include_branches()` wiring) follows immediately after, since
`KhiveMemoryStore` needs somewhere to plug in to be useful. The exact Protocol/`InMemoryStore`/
access-surface shapes are unchanged from ADR-0090's own Decision section -- reproduced there, not
duplicated here, to avoid the two documents drifting out of sync.

### KhiveMemoryStore lives outside the lionagi repo

The adapter class itself is implemented in khive's own codebase, or in a thin separate connector
package, importing lionagi's public `MemoryStore` Protocol. It does not live inside `lionagi/`.
This is a deliberate, confirmed architecture call (Leo, 2026-07-04; khive concurs on the technical
merits): it makes "pluggable, not a hard dependency" and "no commercial specifics in the public
OSS repo" true by construction, rather than by ongoing discipline. lionagi's own repo carries zero
khive-specific code, zero khive-specific imports, and zero khive naming beyond what the
seam-documentation slice of ADR-0090 already allows as a generic example.

Whether the connector package itself ships as public (a thin MCP wire-protocol client with no
khive internals, riding along with lionagi's OSS distribution) or from khive's private tooling is
a packaging/licensing call, not a technical one -- flagged as an Open Question below, since
khive's core is currently a private repo and this is Leo's/Ocean's call to make, not assumed here.

### Transport: existing MCP stdio, no bypass needed

lionagi's `ActionManager.register_mcp_server()` (`lionagi/protocols/action/manager.py`) plus
`MCPConnectionPool` (`lionagi/service/connections/mcp_wrapper.py`) already provide generic, pooled
MCP tool registration: `MCPConnectionPool.get_client()` caches a live client keyed by server name
or command and reuses it across calls (checking `is_connected()` before reuse) rather than
spawning a fresh process per call. Any `Branch` can already call `register_tools()` ->
`register_mcp_server(server_config=...)` pointed at khive's MCP server today, with zero new
lionagi core code.

Confirmed with khive (2026-07-04): this assumption holds on khive's side too, with an extra layer
of margin. One khive-mcp stdio process already serves a whole client lifetime -- the only thing
that spawns a fresh process is a client reconnect, never a per-call spawn. On top of that,
`khived` (khive's ADR-049, accepted and shipped) is a long-lived daemon behind a Unix socket that
the stdio process auto-spawns on first request and forwards every subsequent call to, keeping the
ANN index and embedder models warm *across* stdio reconnects too. So even if lionagi's
`MCPConnectionPool` ends up cycling connections more often than the ideal one-per-`Branch` (e.g.
per agent/session instead), the daemon absorbs that cost: cold stdio process, warm daemon
underneath. Auto-spawn falls back to pure in-process dispatch if the socket is unreachable
(sandboxed/read-only filesystem) or `KHIVE_NO_DAEMON=1` is set -- never a hard failure. This
fallback is a process-startup-time choice, not a live mid-run switch: whichever path (daemon-
backed or in-process) is live when the stdio process starts is the path for that process's entire
lifetime, and a running `KhiveMemoryStore` session does not transparently hop between them
partway through. Net: stdio is the right path; the real latency floor is query-execution time
(FTS5+ANN fusion inside khive), not transport overhead. No embedded-client bypass is warranted on
latency grounds. The one case where an embedded path would matter is a hosting model where
holding a persistent child process per `Branch` is itself awkward (serverless/ephemeral) -- not a
known constraint today, noted for later if it becomes one.

The MCP-tools-breadth half of the original ask ("expose khive verbs as MCP tools registerable on
lionagi agents") is therefore a documentation/example deliverable (a worked `server_config`
pointed at khive's MCP server, in the seam-documentation slice), not new core plumbing -- and it
is scoped to `memory.*` verbs plus, separately, plain-tool registration for `gtd.*`/`comm.*`
(task-lifecycle and messaging primitives, not memory-shaped; expose those as ordinary MCP tools if
task/messaging capability is wanted, don't fold them into `MemoryStore`).

### Verb-to-Protocol mapping

Confirmed against khive source (`khive-pack-memory/src/handlers/{remember,recall}.rs` +
`common.rs`), 2026-07-04:

`memory.remember` request: `content:string` (required), `memory_type?:"episodic"|"semantic"`
(default episodic), `salience?:f64 [0,1]`, `decay_factor?:f64>=0` (alias `decay`),
`source_id?:string` (UUID or 8-char prefix, alias `source`, creates an `annotates` edge),
`tags?:string[]`, `embedding_model?:string`, `namespace?:string` (override). Response: `{id,
kind, salience, decay_factor, memory_type, created_at, edge_id?}`.

`memory.recall` request: `query:string` (required, min 2 alpha/CJK chars), `limit?:u32` (default
10), `top_k?:usize` (overrides limit), `memory_type?`, `min_score?:f64`, `min_salience?:f64`,
`score_floor?:f32`, `tags?:string[]`, `tag_mode?:"any"|"all"` (default any), `entity_names?:string[]`,
`embedding_model?`, `fusion_strategy?:string`, `full_content?:bool` (default true),
`include_breakdown?:bool`. Response: bare JSON array of `{id, score, rank_score, raw_score,
content, salience, decay_factor, memory_type, created_at}` sorted by `rank_score` desc. **The
response carries no `tags` field and no free-form properties field at all** -- `tags` is
write-only on the `memory.remember` side; `memory.recall` never echoes it back.

Mapping onto the Protocol, stated explicitly per direction (an earlier draft of this ADR claimed a
symmetric round-trip that khive's actual response schema doesn't support -- corrected here):

- **Write path** -- `store(item: MemoryItem) -> UUID` <-> `memory.remember`: `item.content` ->
  `content`, `item.tags` -> `tags` (write-through -- khive accepts and stores it), parse the
  returned `id` string to `UUID`.
- **Read path** -- `search(query: MemoryQuery) -> list[MemoryItem]` <-> `memory.recall`: map
  `MemoryQuery.text` to `query`, `MemoryQuery.limit` to `limit`, fold `memory_type`/`min_score`/
  `min_salience`/`tags`/`tag_mode`/`entity_names` into `MemoryQuery.filters` as same-named keys.
  **`MemoryItem`s returned from `search()` are reduced-fidelity relative to what `InMemoryStore`
  returns for the same stored item**: `content`, `salience`, `decay_factor`, `memory_type`, and
  `created_at` all round-trip, but `tags` comes back `[]` on every `KhiveMemoryStore` search hit,
  because `memory.recall`'s response schema doesn't carry it at all. This is a documented backend
  capability difference, not a bug to paper over. Ruling: `KhiveMemoryStore.search()` does **not**
  issue a hidden per-hit `get(id)` call to rehydrate `tags` for each result -- that would turn one
  `memory.recall` into N+1 khive calls per search and silently change the performance profile of
  every caller. A caller that needs a hit's full tag set calls `retrieve(item.id)` explicitly for
  that one item, paying the extra round-trip only when it actually needs it.
- `retrieve(item_id: UUID) -> MemoryItem | None` does **not** map to `memory.recall` (recall is
  ranked search, not fetch-by-id -- caught in review before this shipped). It maps to the kg-pack
  `get(id)` verb instead, which auto-detects entity/note/edge by UUID. **Type guard, stated as a
  hard contract**: `retrieve()` returns a `MemoryItem` only when `get(id)` resolves to a `note`
  entity that itself carries a `memory_type` property (i.e. a note created by `memory.remember`,
  not an arbitrary kg record). A missing/unresolvable `id` and a valid id that resolves to
  something else (a plain kg note without `memory_type`, an entity, an edge) both return `None` --
  `retrieve()` never coerces a non-memory kg record into a `MemoryItem`.

### Provenance: a documented lossy projection for v1, not a round-trip

`MemoryItem` inherits `metadata` from `Element` (an arbitrary key-value bag lionagi callers use
for things like `branch_id`/session provenance). khive has no matching arbitrary-bag field:
`memory.remember` accepts exactly the fields listed above and nothing else, and `memory.recall`
echoes back exactly the fields listed above and nothing else. A true opaque round-trip of
`metadata` is unreachable today without a khive-side schema change -- there is no field to stash
it in and no field to read it back out of.

Decision (Leo, 2026-07-04): `KhiveMemoryStore` persists only the `MemoryItem` fields that have a
native khive home, and explicitly **drops** the rest, rather than smuggling `metadata` into
`content` or a synthetic tag as a workaround:

- `content` -> `content` (write-through).
- `tags` -> `tags` (write-through on `store()`; reads back `[]` on `search()` per the read-path
  note above; full fidelity only via an explicit `retrieve()`).
- `memory_type`, `salience`, `decay_factor` -> same-named khive fields (write-through, round-trips
  on both `search()` and `retrieve()`).
- `source_id` -> khive's `annotates` edge (single-parent provenance, not an arbitrary key-value
  bag).
- `metadata` (everything else -- `branch_id`, arbitrary caller-set keys) -> **dropped**. Not
  stored, not round-tripped, not silently truncated into another field.

This resolves what was previously listed as an Open Question in this ADR (see Open Questions
below). Migration path, when a real caller needs queryable `metadata` (e.g. filtering recall by
`branch_id`): khive adds a native field for it -- its own schema change, its own decision on
khive's side -- rather than lionagi working around the gap with an encoding trick first.

### Cross-backend Protocol contract (what the test fence enforces)

The Protocol-level test fence bundled from ADR-0090 slice 1 (see Sequencing above) runs against
every `MemoryStore` implementor -- `InMemoryStore` and `KhiveMemoryStore` alike. Stated explicitly
so the fence has a concrete contract to assert:

- **Store-then-retrieve fidelity is guaranteed on every backend.** For any `item` passed to
  `store()`, an immediately following `retrieve(returned_id)` must return a `MemoryItem` whose
  native-home fields (per the provenance mapping above) match what was stored. This is the one
  guarantee that holds regardless of backend.
- **Search fidelity and read-after-write timing are backend-specific, not part of the shared
  contract.** `InMemoryStore.search()` is exact and immediately consistent (it's a dict scan).
  `KhiveMemoryStore.search()` is reduced-fidelity per the read-path note above, and khive's ANN
  index warms *asynchronously* -- a `search()` issued immediately after `store()` is not
  guaranteed to surface that item yet. The test fence must not assert immediate
  read-after-write search visibility as a cross-backend requirement -- only `InMemoryStore`'s own
  backend-specific tests may assert that.

### GraphStore is explicitly out of scope for this ADR

kg (entity/note/edge) does not belong inside `MemoryStore.search(filters=...)`. kg entities use a
closed 9-kind enum, edges use a closed 17-relation enum with per-relation endpoint validation
(khive's ADR-002/ADR-017), and kg's own `query()` verb takes GQL or SPARQL -- a different grammar
from free-text `MemoryQuery.text` entirely. Squeezing that into `filters:dict[str,Any]` would lose
the thing that makes kg useful. A `GraphStore`-shaped Protocol (`create`/`link`/`search`/
`neighbors`/`traverse`/`query`, mirroring kg's own verb surface) is the right fast-follow seam --
separate from `MemoryStore`, matching what ADR-0090 already names as a distinct seam-candidate
example ("graph stores"). This ADR scopes to `MemoryStore` only; `GraphStore` is deferred to its
own follow-up ADR once `MemoryStore` has a live caller.

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| Ship `KhiveMemoryStore` inside `lionagi/` (e.g. gated behind an optional extra) | Puts khive-specific code and naming inside the public Apache-2.0 repo; extra-gating only prevents the *dependency* from installing by default, it doesn't prevent the *code and references* from being visible in the public source tree. Violates the never-leak-commercial-in-public-repo constraint by visibility, not just by installability. |
| Land ADR-0090 slices 1-2 as their own separate PR(s) first, khive integration strictly afterward | Leo's explicit ruling: bundle instead. Landing the Protocol in a vacuum, with only `InMemoryStore` as an implementor, doesn't prove the seam generalizes -- `KhiveMemoryStore` as an immediate second implementor is the real test of whether the Protocol is shaped correctly, which is exactly what slice 1's own test-fence language anticipated. |
| Build a bespoke lionagi-side khive HTTP/embedded client instead of reusing MCP | Duplicates transport machinery lionagi already has (pooled, persistent `register_mcp_server`/`MCPConnectionPool`), and khive confirmed the MCP path already holds a persistent session with a warm daemon underneath -- no latency case for a bypass. |
| Fold kg into `MemoryStore.search(filters=...)` | kg's closed entity/edge enums and GQL/SPARQL query grammar don't compress into a generic `dict[str, Any]` without losing validation and expressiveness. Confirmed with khive; deserves its own `GraphStore` seam instead. |
| Fold `gtd.*`/`comm.*` into `MemoryStore` | Task-lifecycle and messaging primitives, not memory-shaped. Already coverable as plain MCP tools via `register_mcp_server` -- no new Protocol needed for these at all. |

## Consequences

**Positive**

- lionagi's OSS core gains zero new mandatory dependencies; `InMemoryStore` remains the
  zero-config default.
- The Protocol gets proven against a real, structurally different second backend before it
  ships, not just the trivial in-memory case.
- khive integration requires no new lionagi core plumbing for the MCP-tools-breadth half of the
  ask -- existing `register_mcp_server` covers it, backed by a persistent stdio session and a
  warm daemon on khive's side.
- The pluggable-not-hard-dependency and no-commercial-in-public-repo postures hold by
  construction (repo boundary), not by ongoing reviewer vigilance.
- The verb-to-Protocol mapping is source-verified on both sides, not assumed -- including a
  mapping bug (`retrieve` -> `recall`) caught before any code was written.

**Negative**

- Slice 1 and slice 2 landing together (per Leo's bundling ruling) is a larger single PR than
  either slice alone would have been -- more surface for one review pass.
- `MemoryItem`'s inherited `metadata` (branch_id, source, etc., from `Element`) has no matching
  khive-side field today and is explicitly **dropped** by `KhiveMemoryStore`, not round-tripped --
  see "Provenance: a documented lossy projection for v1" above. Provenance written through
  `KhiveMemoryStore` is limited to what `source_id`'s single-parent `annotates` edge can express,
  not an arbitrary key-value bag, until khive adds a native field for it.
- The connector-package public/private packaging question is unresolved -- see Open Questions --
  and could affect how/where `KhiveMemoryStore` is installed by end users, though not the
  Protocol or verb mapping itself.

### Implementation notes (non-blocking, for whoever picks up slice 1/2)

- Land ADR-0090 slice 1 and slice 2 as **separate commits within this one bundled PR**, not
  squashed together -- so slice 2 (the `Branch`/`Session` access surface) can be reverted cleanly
  on its own without also reverting the Protocol/`InMemoryStore`/test-fence foundation slice 1
  provides.
- Before slice 2 merges, confirm the `include_branches()` sharing bug flagged in ADR-0090's own
  advisor review is actually resolved in current code -- check the live state of that path rather
  than assuming it was fixed as part of ADR-0090 landing.

## Open Questions (for Leo / Ocean)

1. **Connector-package packaging/licensing.** khive's core is currently a private repo. Should
   the `KhiveMemoryStore` connector package itself be public (a thin MCP wire-protocol client
   with no khive internals, distributed alongside lionagi's OSS ecosystem) or ship from khive's
   private tooling? Doesn't block this ADR or the Protocol/verb-mapping work -- the interface is
   identical either way -- but affects how end users actually obtain and install the adapter.

**Resolved (Leo disposition, 2026-07-04):** the connector lands in the khive monorepo now,
owned by the khive seat, and the khive python SDK is the named public exit -- the connector
ships publicly as part of that SDK when it ships, not as its own standalone package before it.
Rationale: a standalone public package would commit to stability of khive's memory verb surface
while it is still moving, and would spend the works-with-lionagi story before the SDK launch can
use it; the irreversible step (public commitment) lands exactly once, at SDK time. A standalone
public package is revisited only if concrete OSS demand appears before the SDK ships. The
lionagi-side Protocol conformance fence (`tests/protocols/test_memory.py`) remains the
acceptance gate for the connector regardless of where it lives.

**Resolved during spec-gate (Leo, 2026-07-04):** the provenance schema gap (previously listed
here as Open Question 1) is decided, not open -- see "Provenance: a documented lossy projection
for v1" above. `KhiveMemoryStore` persists only native-home fields and explicitly drops the rest;
no opaque round-tripping workaround. The migration path is a future khive-side schema addition,
not a lionagi-side encoding trick.

## References

- ADR-0090: Minimal Memory Contract and Pluggable Backend Seam
  (`docs/adrs/ADR-0090-minimal-memory-contract-and-backend-seam.md`), PR #1687
- khive ADR-049 (khived daemon, accepted/shipped) -- cited by lambda:khive, lives in khive's own
  repo, not reproduced here
- gtd anchor `5018674e` (Ocean directive, relayed by Leo)
- comm thread `a7d4d75b` (lambda:lionagi <-> lambda:leo scoping exchange)
- comm thread `e64077aa` (lambda:lionagi <-> lambda:khive transport/field-mapping/kg-placement
  exchange)
