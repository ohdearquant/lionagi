# ADR-0135: Bounded opt-in session and run listing

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started — the versioned request/response and candidate
  continuation contract below are not implemented; compatible SQL projection improvements
  do not implement this contract
- **Area**: studio
- **Date**: 2026-10-23
- **Relations**: Refs [#3106](https://github.com/ohdearquant/lionagi/issues/3106);
  standalone listing decision, with ADR-0071 D4/D5 and ADR-0087 D3 as limited precedents
- **Disposition**: standalone. No decision or status of ADR-0071 or ADR-0087 is replaced or
  amended. ADR-0071 governs task claims; ADR-0087 D3 governs evidence projection receipts.
  Their bounded-scan and cursor principles inform this separate mutable-session listing.

This is a proposal for review. The caps and wire names below are engineering proposals,
not accepted policy, measured workload optima, or statistical latency guarantees.
Dependent v2 implementation waits for design sign-off. This documentation patch does not
change either existing endpoint.

For #3106, this proposal decides scope items 1 (keyset paging), 4 (bounded search), and
5 (optional exact totals). Item 2 (page-first projection without outer regrouping or
re-sorting) is deferred to a separate change. Item 3 (sargable status filtering) is
already implemented at the source baseline below: `lionagi/studio/services/sessions.py`
lines 374–375 use `status IN (...)` with the explicit NULL arm only for completed.
This proposal does not claim that the whole issue is implemented or complete.

## Context

**P1 — A bounded result is not a bounded query.** At source baseline
`fd20dca9ecc197e68ef9ece0640d51eeb00a45ab`,
`lionagi/studio/services/sessions.py` selects a LIMIT/OFFSET page before branch aggregates,
but deep OFFSET must still pass preceding rows. Its default exact count is separate
store-sized work. Removing outer regrouping alone does not remove those paths.

**P2 — Contains filters can inspect the store before finding one result.**
`SessionFilter.where()` uses escaped, case-insensitive contains expressions for name
fields and playbook. Limiting returned matches cannot bound examined nonmatches.
Tags, metadata predicates, and other residual filters also need an honest work contract.

**P3 — Existing clients rely on exact totals and numbered pages.**
`runs.py:_RunListQuery`, `list_runs_route()`, and frontend `lib/api.ts:RunListResponse`
use page/per_page and numeric total/total_pages/has_next. Session listing uses
limit/offset and total/truncated. Changing those defaults would alter v1 clients.

**P4 — Mutable sort keys do not define a historical snapshot.** Recent order uses
updated_at, and cost order puts unreported NULL after reported values, including zero.
Neither currently has an id tie-breaker. A cursor must preserve these semantics and must
not imply snapshot isolation across requests when rows change.

**P5 — Filter labels do not authorize data access.** Studio authentication and configured
store selection precede listing. A client project, status, tag, or cursor is not a trusted
scope grant. A candidate bound applied after fetching another scope would be too late.

**P6 — Cursor progress must include nonmatches.** A candidate window may contain no
matches while older candidates still exist. A last-returned-row cursor either stalls
on an empty page or repeatedly examines rejected candidates.

Precedents have precise limits:

- [ADR-0071 D4](ADR-0071-durable-ad-hoc-task-queue.md#d4--one-host-worker-claims-executes-and-reaps-leased-rows)
  separates a keyset candidate scan bound from eligible claims; D5 separates placement
  from authorization. This proposal does not inherit its numeric caps or lease semantics.
- [ADR-0087 D3](ADR-0087-evidence-backed-governed-execution.md#d3--evidencestore-is-append-only-and-authoritative)
  proposes an opaque projection cursor that advances past the page's receipts and survives
  restart. It remains a Proposed governance contract. A session listing is mutable,
  not an append-only evidence stream, so its exactly-once source visibility is not
  transferred to this listing.

| Concern | Decision |
| --- | --- |
| Compatibility | D1: Existing v1 defaults and shapes remain; v2 is explicit opt-in. |
| Ordering and continuation | D2: Versioned opaque cursor binds query context and a total sort tuple. |
| Bounded residual work | D3: Scan an indexed authorized candidate window before user predicates. |
| Totals and truthful response | D4: Total is unknown unless explicitly requested exact; empty windows advance. |
| Caps and index authority | D5: Separate proposed offset and candidate caps; require schema/migration and real seek proof. |
| Adoption and acceptance | D6: Gate clients, documentation, compatibility, work growth, and later deprecation separately. |

Out of scope:

- Progression representation, session-detail pagination/statistics, and live stream brokers:
  those are separate storage/detail contracts, including Proposed ADR-0117.
- A tenant system, a new authentication mechanism, or authorization via filter labels.
- Immutable export snapshots or exactly-once enumeration while sort keys are updated.
- New display-name precedence, status aliases, kind vocabulary, cost valuation, or run identity.
- FTS/prefix indexing as the initial search mode; those can be separately versioned later.
- New PostgreSQL support for Studio's currently local-file listing routes.
- Default timing claims inferred from a synthetic or historical benchmark population.

## Decision

### D1 — Opt-in v2 leaves v1 intact

Both existing endpoints retain their paths:

```text
GET /api/sessions/
GET /api/runs/
```

Proposed selector: `listing_version=2`. Omitted selector, or explicit
`listing_version=1`, uses v1. Unknown versions fail validation rather than silently
falling back. New v2-only parameters with v1 are refused; they cannot silently alter a
v1 response.

The v1 contracts remain:

| Endpoint | Existing paging and result contract |
| --- | --- |
| Sessions | limit/offset/sort; sessions, numeric total, limit, offset, truncated. |
| Runs | page/per_page plus current filters/sort; runs, page, per_page, numeric total, total_pages, has_next, has_prev, status_ended_at_mismatches (recomputed for every response). |

V1 retains its current matching-row OFFSET semantics, uncapped legacy offset depth,
exact totals, and existing contains behavior. This is an explicitly disclosed
compatibility cost, not a bounded-work promise. No default cap, unknown total, or cursor
is inserted into a v1 response.

V2 uses the endpoint's existing row DTO and filter vocabulary. It adds:

```python
listing_version: Literal[2]
cursor: str | None = None
exact_total: bool = False
```

The sessions endpoint keeps limit; runs keeps per_page. Existing defaults and maximum
row-page sizes remain endpoint-specific. Cursor paging is primary. This draft proposes
allowing sessions offset and runs page under D5's candidate-position compatibility rule,
subject to the explicitly unresolved filtered-offset choice in D6.
A cursor cannot be combined with a nonzero offset or a runs page other than its initial
page. Contradictions fail validation; they are not resolved by precedence.

Neither version adds a sessions filter vocabulary: that endpoint keeps its current
limit/offset/sort surface plus the v2 controls above. Runs keeps its existing status/kind
normalization and filter syntax. Sharing an internal SessionFilter implementation does
not add query parameters to sessions. New sessions filters need a separate scoped change.

Why this way: one explicit selector allows parallel client migration without changing
existing paths or pretending an unknown total is the old numeric total.

### D2 — Cursors bind the query and total ordering

A cursor is an opaque, versioned server-issued continuation. Its logical contents are:

```text
cursor schema version
endpoint / row projection
configured store incarnation and trusted authorization-context identity
canonical user-filter and product-eligibility-policy fingerprint
sort mode and comparison version
last EXAMINED candidate's complete sort key
```

Clients store and return it; they do not construct or edit its fields. The wire codec,
integrity mechanism, durable store-incarnation binding, and rotation/lifetime policy are
explicit implementation blockers in D6 until the owner selects them. A cursor never
authenticates the caller or changes the scope selected by the server.

Canonical filters normalize repeated set-like status/kind/tag inputs deterministically,
including existing status aliases and show-kind expansion. Preserve distinctions such as
project-null versus project, and preserve current text matching/escaping semantics rather
than replacing SQLite LOWER with a different Unicode policy. Bind the normalized filter,
not raw query ordering. The row limit and exact_total flag are not filter membership and
may vary between requests; limit remains validated. A different endpoint, sort, filter,
policy version, store incarnation, or trusted scope invalidates the cursor.

The explicit SQL order is:

```text
recent:
  updated_at DESC NULLS LAST, id DESC

cost:
  (total_cost_usd IS NULL) ASC,
  total_cost_usd DESC,
  updated_at DESC NULLS LAST,
  id DESC
```

The NULLS LAST notation specifies comparison semantics; the adapter uses syntax supported
by its SQLite version. It must not coerce NULL updated_at to numeric zero in a cursor.
The cost NULL flag preserves reported zero before unknown cost. Equal primary keys use
the stored id's database ordering/collation as the last, unique tie-breaker.

Seek is lexicographic strictly after the full key in the chosen order. For example,
a non-NULL recent frontier admits lower updated_at, then lower id at equal updated_at,
then the NULL-updated partition. A NULL recent frontier admits only lower ids within
that NULL partition. Cost seek follows lower reported cost, then updated/id ties,
then the unknown-cost partition; within that partition, seek uses updated/id only.
SQL comparisons must branch explicitly for NULL, rather than using a tuple comparison
that turns NULL into UNKNOWN. Numeric keys retain their stored precision.

Malformed/unsupported cursor versions or invalid field types are validation errors.
A valid token whose context no longer matches requires a restart with an explicit
context-change error, not an empty-success response. Missing/deleted frontier rows do
not require a database lookup by id: the saved key is sufficient. Unsupported stored
sort keys produce an explicit store/order error rather than an invented key.

Each response is generated from one short read snapshot, including its cursor and any
requested exact count. Separate pages are separate snapshots:

- Inserts newer than an examined frontier do not move older unchanged rows into a
  previous page or duplicate them.
- Inserts strictly after the frontier can appear on later requests.
- Equal timestamps/costs remain ordered by id.
- A row whose sort key or eligibility changes can move across a frontier. This proposal
  does not promise a complete immutable enumeration or no duplicates for such updates.
  Clients identify rows by existing Session id and may deduplicate refresh results.
- Restarting from the beginning is the way to see newly inserted earlier rows.

The outer result ORDER BY is explicit. No contract depends on incidental CTE/subquery
iteration order, even when a current query planner preserves it.

### D3 — Candidate budget precedes residual matching

Define three different operations:

1. **Trusted scope selection:** existing authentication, configured-store resolution,
   and any server-enforced authorization predicate. These are mandatory before a
   candidate's metadata is fetched or returned.
2. **Candidate traversal:** seek a total-order indexed frontier within that trusted
   scope, with the bound below.
3. **User/product matching and response hydration:** apply current user filters and
   mandatory product exclusions to those candidates, then hydrate only returned rows.

In today's local-file deployment, user project/status/kind/tag labels do not create row
authorization. This ADR introduces no new trusted scope source. If a future deployment
has row scopes, it must provide an indexable authorized traversal and proof that another
scope's payloads/keys/cursors are not fetched as application candidates. Fetching globally
and filtering authorization afterward is forbidden.

The initial v2 mode uses all authorized sessions as its candidate universe. User search,
status, kind, project, playbook, tags, and mirrored-engine-child suppression are residual
predicates within the window. Mirror rows remain unreturnable; they may consume the
candidate budget. This fixes a stable meaning for a window rather than letting whichever
index the optimizer chooses redefine the user-visible scan.

An optional facet-specific indexed universe is **deferred**. Adding it requires a named
mode/candidate-basis contract and distinct cursor fingerprint, proof of identical stated
semantics, and work tests. It cannot silently change when an index is added. V1's existing
sargable status path remains in force and its SEARCH regressions remain required.

Proposed algorithm:

```text
resolve and enforce trusted scope
validate version, cursor context, limit and compatibility offset
seek candidate keys in the requested total order
for each candidate, up to candidate_limit:
    mark this candidate examined
    evaluate product eligibility and all requested user predicates
    if it matches: append its bounded listing row
    if returned row limit reached: stop
stop also at candidate_limit or source exhaustion
next cursor = last EXAMINED key when candidates remain
```

Candidate enumeration projects only keys and the metadata necessary for these filters.
Branch/progression aggregates and run-row hydration operate on matching returned rows,
with at most the requested row count. Never decode session message histories to search a
listing. Existing listing count semantics for branches and progression membership stay.

The candidate bound is not a new bound on branch fanout or JSON membership size inside a
returned session. Those selected-row projection costs remain visible and must be measured
separately; this ADR does not approximate counts or assume Proposed ADR-0117 has landed.
Store-growth tests hold selected-row payload/fanout fixed so candidate traversal is not
confused with a different progression-representation problem.

Search mode is `bounded_contains` when a supported contains predicate is requested, and
`none` otherwise. Matching retains the current escaped contains semantics over that
endpoint's supported fields, within examined candidates. Literal percent/underscore retain
their existing escaping. A no-match window does not establish no match in the store.

A batch may read at most candidate_limit keys plus one ordered lookahead key. The lookahead
is not user-filter evaluated or hydrated; it exists only to determine whether candidates
remain. If processing stops at the result limit within the batch, already-known unexamined
keys establish more candidates. Unexamined prefetched keys do not advance the cursor.
No fresh scan from the beginning is used to answer has_more_candidates.

For every nonterminal continuation, the new key is strictly after the request key,
including a window returning zero rows. At exhaustion, next_cursor is null. No cursor
based only on the last matching row may replace this progress rule.

Candidate count bounds metadata rows, not arbitrary text length, total database size,
or latency. Existing request/field limits remain; this proposal does not introduce a new
input-size or execution-guard policy.

### D4 — Unknown totals and honest partial pages

V2 does not reuse v1 total_pages/has_next or truncated to imply exhaustive matching.
It returns the same endpoint row field (`sessions` or `runs`) with this paging envelope:

```json
{
  "listing_version": 2,
  "per_page": 20,
  "runs": [],
  "pagination": {
    "next_cursor": "<opaque continuation>",
    "has_more_candidates": true,
    "returned": 0,
    "examined_candidates": 10000,
    "candidate_limit": 10000,
    "stop_reason": "candidate_limit",
    "offset_basis": "candidates",
    "search_mode": "bounded_contains"
  },
  "total": {
    "kind": "unknown",
    "value": null
  }
}
```

This is an illustrative limit case, not a measured frequency claim. `stop_reason` is
one of `row_limit | candidate_limit | exhausted`. The response also carries the effective
row limit at top level using the endpoint's existing limit/per_page name. If lookahead
finds no remaining candidate, stop_reason is exhausted; otherwise it names row_limit when
that limit stopped evaluation, or candidate_limit. A full-looking returned page can still
have more candidates; an empty page can too. has_more_candidates says nothing about whether
those candidates will match.

By default no exact-count query runs. With explicit `exact_total=true`, total is
`{"kind": "exact", "value": n}` for all matching rows in the trusted scope at that response's
read snapshot. This count uses the full product/user predicates, not just the candidate
window. It is explicitly outside the bounded default-path work guarantee, can scan the
authorized store, and does not make the returned window exhaustive. Errors cannot silently
downgrade an explicitly requested exact total to unknown.

Clients must not compute total_pages from an unknown total or interpret zero returned
rows as exhaustion. A client may offer “continue search” for another candidate window;
it must not silently loop to the end and disguise unbounded work as one user request.
An exact-count action must disclose that it can take work proportional to matching/search
scope and be separately cancellable through the ordinary request lifecycle.

### D5 — Separate proposed caps and index proof

The initial engineering proposals are:

| Control | Proposed value | What it bounds |
| --- | ---: | --- |
| V2 compatibility offset | 10,000 | Ordered authorized candidate keys skipped before a window. |
| Residual candidate window | 10,000 | Candidates receiving product/user matching in one request. |
| Lookahead | One key | Bounded frontier probe, no residual filter/hydration. |

These are separate budgets. A compatibility request may skip its capped key prefix and
then inspect one capped window; its key traversal is not falsely reported as only the
window count. Offset keys skipped and lookahead keys read are exposed in diagnostic
evidence. The response's examined_candidates counts residual evaluations.

**Proposed branch, not yet selected by the owner:** v2 offset counts candidate positions,
not matching rows. Sessions offset maps directly; runs uses (page - 1) × per_page. The
value is at or below the cap; larger values fail validation with a cursor-required
explanation. Offset is a starting compatibility bridge, not carried into continuations.
This deliberate v2 semantic difference belongs in the request schema/client migration
guide. The retained alternative refuses nonzero offset when user filters are present;
then filtered v2 requests use cursors only. Owner selection between these branches is a
sign-off blocker. Neither branch changes v1 matching-row offsets.

Rationale: finite key skipping permits a shallow bridge from numbered clients; the
candidate cap prevents a rare-match prefix from monopolizing one request while allowing
useful batching. Equal initial values simplify the first implementation's resource
accounting. They are not derived from a statistical population or claimed to meet a
universal latency percentile. Owners may revise these proposal values before sign-off.
A later cap change is explicit protocol/configuration behavior, not hidden retuning.

Required access paths include a recent (updated_at DESC, id DESC) seek and a cost access
path matching NULL partition, descending cost, updated_at and id. A cost implementation
may use a matching expression/composite index or explicit reported/unknown partitions;
it must preserve the single logical order. Current single updated/status indexes alone
are not proof of bounded tie-heavy seeks.

Any new index uses the canonical schema/version/migration process, with fresh-install,
upgrade, repeated-open and data-preservation tests. A query cannot silently migrate a
read-only store. Stores missing a required access path must either use a proven bounded
fallback or explicitly refuse v2 as not ready; a store-sized hidden sort is not a fallback.
V1 remains available under its old contract.

EXPLAIN plus real VM-step/rows-visited tests prove the frontier's work. Merely returning
few rows, or limiting rows after a full scope/filter scan or temp sort, fails this gate.
Scope predicates need matching index proof too; a syntactically bounded candidate query
must not walk an unbounded prefix to find authorized rows.

### D6 — Adoption, acceptance, and remaining design gates

Sequence:

1. Owner reviews this Proposed ADR and routes design sign-off before dependent v2 source.
   A docs-only merge does not imply v2 is implemented or remove the implementation gate.
2. Resolve the blockers below, including complete wire validation, cursor persistence and
   offset behavior. Update this proposal before sign-off; no implicit implementation choice.
3. Implement versioned endpoints and required indexes/migrations; preserve v1 conformance.
4. Add a separate typed v2 client rather than changing RunListResponse's numeric-total
   promise. Test unknown/exact totals, empty/partial windows, context restart and invalid
   cursors. The history view's numbered-page/has_next pager must explicitly adopt continuations.
5. Update generated request/response schema and Studio surface/operator documentation;
   disclose bounded_contains scope, candidate offsets, exact-count cost, update semantics,
   and keyboard/load-more behavior on empty windows.
6. Only then enable v2 for an opting-in first-party client. Third-party v1 remains.
   Any future deprecation must record the then-applicable scope of the repository's
   [deprecation policy](../governance/standards/deprecation-policy.md), release communication
   and replacement path. That policy scopes its alias/warning cycle separately from its
   mandatory removal record; this proposal invokes no exception to remove v1 and sets no
   removal release/date.

Acceptance is staged, with before/after evidence pinned to implementation commits;
this proposal replaces #3106's original latency criterion — “At 20k and 200k seeded
sessions, warm p95 for the unfiltered first 20 rows is under 25 ms on the benchmark
host and grows at most 2x between those sizes” — with the work-counter acceptance
below plus host-pinned timing, and accepting that replacement is the owner's decision.

| Stage | Required proof |
| --- | --- |
| V1 compatibility | Existing response shapes/defaults, full matching offsets/count/search, labels/status/cost/identity and regression suites remain; v1 status SEARCH still holds. |
| Cursor | Version/filter/sort/scope/store binding; complete id tie-break; NULL/zero/timestamp ties; deleted frontier; restart policy; malformed/context errors. |
| Bounded traversal | On fixed small/larger stores, real page/route work includes all SQL and excludes an unrequested exact count. Deep keyset page 500 is comparable to page 1 by frontier work, including tie-heavy and cost partitions. |
| Search | Rare/no-match windows enforce candidate/lookahead caps before residual work; empty page advances by last examined; no return-LIMIT masquerades as a scan bound. |
| Scope | Existing authentication remains mandatory; applicable required row scope precedes metadata fetch, aggregates and cursors; cross-scope poison witnesses and scope-index work proof. |
| Mutations | Concurrent insert cases do not duplicate unchanged rows; equal-key order stable; tests explicitly demonstrate the documented limitations for updated sort keys. |
| SQL/schema | Explicit result ORDER BY; no outer regroup/distinct or hidden store-sized sort for the bounded route; index-seek plan and work proof; migration/version/fresh/upgrade coverage. |
| Exact total | Absent by default; opt-in counts all matching authorized rows in the response snapshot, correctly labeled and tested separately from bounded page work. |
| Client/docs | Empty/partial continuation and unknown total handled; filter change cancels/drops old pages; new API mode named and documented before adoption. |

Timing measurements supplement work counters on the named host/fixture; they do not
generalize a convenience fixture to an external user population. Any later measured
claims added to this ADR need the applicable math/population verification before sign-off.
This draft makes no such measured-number claim.

**Unresolved owner decisions — dependent implementation remains blocked:**

- Accept/change the selector and envelope field names, initial candidate-basis choice,
  and proposed caps. This draft adds no sessions filters and preserves the existing
  runs vocabulary; any desired expansion needs its own explicit scope.
- Candidate-position offsets with filters versus refusing nonzero filtered offsets.
  Preserve v1 matching offsets whichever v2 choice is approved.
- Cursor codec/integrity, durable store-incarnation identifier, restart/key rotation/
  lifetime and exact public error shapes. Do not treat an encoding as authorization.
- Concrete cost/NULL/tie seek indexes and any required scope-index combination, proven
  without undocumented iteration order or full-store sorting. The listing issue's
  selected-page no-resort criterion also needs plan evidence; retaining a bounded final
  page sort must be reported to the owner as remaining acceptance, never hidden by
  removing the explicit ORDER BY.
- Client adoption and documented default-transition timing, if any. V1 removal is not
  decided here.

## Consequences

- V2 can bound residual candidate evaluations without claiming that contains search
  exhaustively searched the store.
- Opaque cursors become compatibility state; their comparison/canonicalization version
  must remain readable or fail with an explicit restart path.
- Partial/empty windows impose real client work. A client that uses the old has_next or
  numbered-page model would misrepresent completeness.
- V1 retains a costly path intentionally during migration. It must not be counted as
  satisfying the v2 bounded-work guarantee.
- Indexes add write/storage and migration maintenance. Missing-index refusal is more
  visible than an implicit slow fallback, and leaves the old endpoint mode usable.
- Reversal before adoption is cheap; after clients retain cursors or depend on candidate
  offsets, a changed meaning requires versioning and a transition rather than silent edits.

## Alternatives considered

### Change v1 defaults in place

This would move all clients to bounded work quickly. It loses compatibility: numeric
totals, has_next and matching-row offsets have existing consumers. Explicit v2 makes
the changed search/completeness contract visible.

### Only rewrite page aggregates

This is compatible and independently useful. It does not remove OFFSET skip, unbounded
contains matching, or exact-count work. It can land separately but cannot stand in for
this versioned contract.

### LIMIT returned matches after contains search

This preserves full matching semantics but may examine an arbitrary prefix before one
match. It does not bound the named work and cannot be the v2 default search guarantee.

### Cursor from the last match

This is familiar for fully indexed matching queries. With a bounded residual window,
an empty page has no such cursor and rejected candidates repeat. Last examined is the
progress fact.

### FTS or indexed prefix as the first search contract

These can give different, stronger search access paths, but change arbitrary contains
semantics and add index/migration/extension choices. They remain possible named modes
with their own fingerprints and documented semantics; they are not silently substituted.

### Eliminate the outer ORDER BY by trusting a page subquery

This may remove a current temp tree, but incidental iteration order is not the result
contract. Explicit ordering and index/work proof are required even if that delays a
planner optimization.

### Claim a stable cross-request snapshot

This would simplify exhaustive export and update deduplication, but needs retained
snapshot/version machinery beyond a lightweight mutable listing. This proposal states
insert and update behavior separately instead.

## Notes

Public source anchors for the qualitative design baseline are
`lionagi/studio/services/sessions.py` (SessionFilter, _SESSION_SORTS, list_sessions,
count_sessions and route), `runs.py` (_RunListQuery/list_runs_route),
`lionagi/studio/services/_db.py` (configured local-file guard), `lionagi/studio/app.py`
(authentication), and `apps/studio/frontend/src/lib/api.ts` (v1 client contract).
No runtime source change is part of this ADR patch.
