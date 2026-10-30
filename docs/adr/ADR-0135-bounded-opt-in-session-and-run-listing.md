# ADR-0135: Bounded opt-in session and run listing

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started — the v2 protocol, cursor lifecycle, candidate
  traversal, and same-response read transaction defined here are not implemented.
- **Area**: studio
- **Date**: 2026-10-23, revised 2026-10-26
- **Source baseline**: `1717674a2140d3571fcb90e3f68204845048ef80`
- **Relations**: Refs [#3106](https://github.com/ohdearquant/lionagi/issues/3106);
  independent listing contract; limited precedents in ADR-0071 D4/D5 and
  Proposed ADR-0087 D3; schema authority remains ADR-0056 D2; progression representation
  remains ADR-0055 D3 while ADR-0117 is Proposed.
- **Disposition**: no existing endpoint default, accepted storage decision, or
  authentication boundary is replaced.

This record specifies proposed behavior. The caps, token lifetime, wire names and
acceptance thresholds are engineering choices, not measured optima or latency claims.
V2 implementation and adoption require acceptance of this record and the tests in D6.

## Context

**P1 — A bounded result is not a bounded query.** At the source baseline the page query uses
a limited CTE, selected-session correlated aggregates, a CROSS JOIN and a final ORDER BY
(`lionagi/studio/services/sessions.py:540-594`). Status filtering uses an IN predicate with
a NULL arm for completed (`lionagi/studio/services/sessions.py:371-376`). OFFSET remains
(`lionagi/studio/services/sessions.py:545`) and a separate exact count is read
(`lionagi/studio/services/sessions.py:406`, `lionagi/studio/services/sessions.py:1741-1742`).
A later `list_sessions` implementation includes a `recent_unfiltered` path and a query-plan
fallback when its required index or ordering plan is unavailable. The existing projection
growth test measures the page statement rather than the entire route
(`tests/apps_studio_server/test_session_listing_projection.py:188`,
`tests/apps_studio_server/test_session_listing_projection.py:217`).

**P2 — Contains can reject an arbitrary prefix before producing a result.** The filter
uses escaped, case-insensitive contains on name fields and playbook
(`lionagi/studio/services/sessions.py:356-366`, `lionagi/studio/services/sessions.py:350-355`).
The page applies those predicates before LIMIT (`lionagi/studio/services/sessions.py:540-545`).
A returned-row limit therefore does not limit rejected candidates. The existing tag
predicate can also group rows across the tag table (`lionagi/studio/services/sessions.py:396-401`).

**P3 — Existing clients rely on numeric totals and numbered transport.** Runs returns
page/per_page, total, total_pages, has_next and has_prev; sessions returns limit/offset,
total and truncated (`lionagi/studio/services/runs.py:257`,
`lionagi/studio/services/sessions.py:1741-1748`). The runs client requires those numeric fields
(`apps/studio/frontend/src/lib/api.ts:979`). The visible history control already uses
Load more, but its pager increments page numbers and consumes has_next
(`apps/studio/frontend/src/components/fleet/FleetView.tsx:489`,
`apps/studio/frontend/src/components/fleet/fleetReducer.ts:316`).

**P4 — Mutable sort keys do not define a historical snapshot.** Current recent order is
updated_at descending; cost orders reported values before NULL, including reported zero,
then updated_at. Neither includes an id tie-break
(`lionagi/studio/services/sessions.py:427-429`). Fresh schemas require updated_at, but legacy
additive migration permits NULL (`lionagi/state/schema.sql:124`,
`lionagi/state/schema_migrations.py:10`). Cursor keys must come from stored values,
before the listing DTO's NULL-to-zero timestamp fallback
(`lionagi/studio/services/sessions.py:611`).

**P5 — Query labels and cursor encodings are not authorization.** Authentication and
origin controls run before API routes (`lionagi/studio/app.py:592`). File-store guards and
configured store resolution precede listing (`lionagi/studio/services/_db.py:27`,
`lionagi/studio/services/_db.py:64`, `lionagi/studio/services/sessions.py:555`). A project
label is an ordinary user predicate (`lionagi/studio/services/sessions.py:430`). This
deployment exposes the configured store to an admitted caller; it does not establish a
new row-level tenant boundary.

**P6 — Progress must include rejected candidates.** A candidate window can return no
matches while later candidates remain. Continuation based only on the last returned row
cannot advance such a window. Separately, persisted page fields and totals currently
come from different connections (`lionagi/studio/services/sessions.py:450`,
`lionagi/studio/services/sessions.py:563`); a shared response snapshot requires new work.

Accepted records constrain this proposal without selecting its new wire details:

- ADR-0056 D2 establishes runtime schema authority and compatibility DDL parity
  (`docs/adr/ADR-0056-statedb-sqlalchemy-core-backend.md:142`).
- ADR-0055 D3 retains JSON progression membership until its proposed successor is accepted
  (`docs/adr/ADR-0055-operational-state-persistence-boundary.md:198`).
- ADR-0082 D6 requires required-field changes to be versioned before client consumption
  (`docs/adr/ADR-0082-vscode-studio-observability-client.md:336`).
- ADR-0076 D4 governs the existing network/authentication boundary
  (`docs/adr/ADR-0076-studio-daemon-route-registry-and-local-control-plane.md:250`).
- ADR-0071's candidate-versus-claim distinction and non-authorizing labels are precedents
  for a different collection, not inherited numeric limits or sort keys
  (`docs/adr/ADR-0071-durable-ad-hoc-task-queue.md:377`,
  `docs/adr/ADR-0071-durable-ad-hoc-task-queue.md:411`).
- ADR-0087 remains Proposed; its projection cursor does not establish this mutable
  collection's codec or consistency (`docs/adr/ADR-0087-evidence-backed-governed-execution.md:3`).

| Concern | Proposed decision |
| --- | --- |
| Compatibility | D1: explicit query-selected v2 on existing paths; v1 stays unchanged. |
| Ordering and cursor | D2: both existing sorts with id ties, signed opaque context-bound cursor, finite lifetime. |
| Search work | D3: fixed all-authorized-session candidate basis, matching after the candidate bound. |
| Totals and snapshots | D4: explicit unknown by default; opt-in exact count in the same persisted read snapshot. |
| Offsets and indexes | D5: no nonzero filtered offset; named seek indexes and explicit readiness refusal. |
| Adoption and proof | D6: separate typed client, explicit history opt-in, whole-route work tests, no default transition. |

Out of scope:

- A new authentication system, row-level tenancy, or authorization through filters.
- Session-detail paging, progression migration, live stream protocols, or new run identity.
- Immutable exports or exactly-once enumeration while rows change sort or eligibility.
- New display-name precedence, status aliases, filter vocabulary, or cost valuation.
- FTS or prefix search as the initial mode; these require a separately named contract.
- New server-backed database support for these local-file listing routes.
- V1 removal or a date for changing any API or client default.

## Decision

### D1 — Explicit v2 preserves v1 paths, defaults, and shapes

Both endpoints retain their existing paths:

```text
GET /api/sessions/
GET /api/runs/
```

The selector is `listing_version=2`. Omission or explicit `listing_version=1` selects v1.
An unsupported version fails validation. Supplying a reserved v2 control (`cursor` or
`exact_total`, even false) with v1 fails validation instead of silently altering behavior.
Do not otherwise broaden or tighten legacy unknown-parameter handling: runs currently
forbids extras, while sessions uses individual query arguments
(`lionagi/studio/services/runs.py:35-36`, `lionagi/studio/services/sessions.py:1723-1735`).

| Endpoint | Preserved v1 contract |
| --- | --- |
| Sessions | limit/offset/sort; sessions, numeric total, limit, offset, truncated. |
| Runs | page/per_page, existing filters/sort; runs, page, per_page, numeric total, total_pages, has_next, has_prev, status_ended_at_mismatches. |

V1 retains matching-row offsets, uncapped legacy offset depth, global contains semantics,
exact totals and current ordering. Do not add id ties, candidate caps or unknown totals
to v1. Its continuing cost is an explicit compatibility property.

V2 uses existing endpoint row DTOs and filter meanings. Its new controls are:

```python
listing_version: Literal[2]
cursor: str | None = None
exact_total: bool = False
```

Sessions retains `limit` (default 500, maximum 500); runs retains `per_page` (default 20,
maximum 500). Those existing bounds are defined at
`lionagi/studio/services/sessions.py:300`, `lionagi/studio/services/sessions.py:1724-1728`,
and `lionagi/studio/services/runs.py:40`. A requested row count must be at least one.
`candidate_limit` is server policy reported in the response, not a request override.

Sessions gains no runs filters. Runs preserves current status alias expansion, kind
validation/expansion, playbook/name contains, project/project-null, and AND-composed tags
(`lionagi/studio/services/runs.py:69`, `lionagi/studio/services/runs.py:86`,
`lionagi/studio/services/sessions.py:381`). Canonicalization follows effective predicates:
deduplicate and sort set-like inputs, expand existing aliases, and discard project when
project_null selects the NULL predicate. Preserve text matching and escaping; do not
introduce a different Unicode-folding policy. Ineffective empty inputs remain ineffective.

Cursor requests use sessions offset zero or runs page one. Contradictory cursor/offset
inputs fail validation. Noncursor compatibility offsets follow D5. Empty cursor strings,
duplicate scalar controls, unknown v2 parameters, and invalid scalar types are rejected.
Repeated set-like filters remain valid. Structured request failures use HTTP 422:

```json
{
  "detail": {
    "code": "listing_request_invalid",
    "message": "Invalid listing request.",
    "restart_required": false
  }
}
```

Use this v2 request shape for invalid limits, conflicting controls and unsupported
versions. Over-cap offsets use the same shape with code `listing_cursor_required` and
message `Use a cursor beyond the compatibility offset limit.` Filtered nonzero offsets
use code `listing_filtered_offset_unsupported` and message
`Filtered listing must start at the beginning or use a cursor.` Existing ordinary v1
validation remains unchanged; the new selector/control checks are explicit additions.

The generated API describes separate v1/v2 request and response contracts. Typed clients
select the version deliberately; existing numeric-total types are not widened silently.
Keep existing trailing slashes and the shared authenticated transport.

### D2 — Signed cursors bind context and complete ordering

V2 offers both `recent` and `cost` from its first release. Their logical total order is:

```text
recent:
  updated_at DESC NULLS LAST, id DESC

cost:
  reported cost before unknown cost,
  total_cost_usd DESC,
  updated_at DESC NULLS LAST,
  id DESC
```

The last tie-break is the existing Session id using its database text ordering/collation,
not nullable run provenance (`lionagi/state/schema.sql:110`,
`lionagi/studio/services/sessions.py:540-548`). Reported zero precedes unknown cost. Negative
finite values keep ordinary numeric order. NULL updated_at is distinct from numeric zero.
Capture the raw key before DTO conversion. Unsupported nonnumeric/nonfinite stored numeric
keys or invalid ids encountered within the bounded candidate/lookahead traversal produce
the D5 store-order error; do not invent a comparable value. Readiness must not scan the
store to validate every key on the default path.

Seek strictly after the complete key. Use explicit disjoint equality/range branches for
NULL semantics and for the indexes in D5, rather than a NULL-bearing tuple comparison or
an unproven large OR expression. Recent with non-NULL time traverses equal time/lower id,
then lower non-NULL time, then NULL time. Recent with NULL time traverses lower id within
that partition only. Cost follows the analogous time/id branches at equal reported cost,
then lower reported costs, then the unknown-cost partition. An unknown-cost frontier
continues only within its time/id order. Branches are traversed in logical order and
share one candidate/lookahead allowance. A deleted frontier row needs no lookup: its
saved key remains sufficient.

The cursor is an opaque signed token, with the proposed wire form:

```text
lc1.<key-id>.<base64url(canonical-json-payload)>.<base64url(hmac-sha256)>
```

Use unpadded base64url. The MAC covers the literal version prefix, key id and encoded
payload, with a listing-specific domain separator. Key ids are bounded ASCII identifiers;
the entire token is at most 8,192 ASCII bytes. Verify the signature before trusting any
payload field. Parse with an explicit bounded schema: reject duplicate/unknown fields,
invalid types, unsupported versions and invalid timestamp intervals. Never reflect token
contents in public errors. Opaque is a client abstraction, not encryption.

The payload binds:

- Cursor schema and comparison versions, endpoint and row projection.
- Canonical effective-filter fingerprint and mandatory product-eligibility revision.
- Sort mode and last **examined** candidate's complete raw key.
- Stable configured logical store id and a persisted store-incarnation UUID.
- Current server-derived authorization scope/policy identity.
- First issuance time and expiration of the continuation chain.

The row count and exact_total flag are not membership filters and may change between
requests. Fingerprint a versioned canonical representation with a collision-resistant
digest. Encode numeric key components as tagged integer-decimal or binary64-hex strings,
preserving the exact database comparison value; encode SQL NULL separately. This avoids
rounding a cursor through a display value or a JavaScript number. Equivalent effective
set predicates produce the same fingerprint; different endpoint, filter, sort, scope,
store or policy invalidates continuation.

Provision a separate durable signing keyring. It must not reuse bearer credentials.
The active key issues tokens; retained keys verify them. A chain expires 24 hours after
its first issuance, and successor tokens keep that expiration rather than extending it.
Normal rotation retains a verification key until all tokens it issued have expired.
Emergency removal invalidates its tokens immediately. Restarts preserve keys, configured
logical store identity and persisted incarnation. Missing keyring/identity capability
refuses v2; do not create an ephemeral signing domain in a listing request.

The canonical schema adds a singleton `listing_store_identity` record containing the
incarnation UUID. Normal writes and restarts retain it. Store reset, restore, or cloning
as a new logical store must rotate the incarnation through the storage maintenance
lifecycle before v2 becomes available. A copied UUID cannot itself detect a byte-for-byte
restore; reliable invalidation depends on that explicit lifecycle. The configured logical
store id also separates distinct store selections without putting a filesystem path in
the token. An absent store needs no token or identity record to return an exhausted result.

Cursor errors are exact, listing-specific responses:

| Condition | HTTP | detail.code | detail.message |
| --- | ---: | --- | --- |
| Malformed token, unsupported codec, wrong field types, bad signature | 422 | listing_cursor_invalid | Invalid listing cursor. |
| Expired chain, unavailable key id, or valid token with changed endpoint/filter/sort/store/scope/policy | 409 | listing_cursor_restart_required | Listing context changed or cursor expired; restart without a cursor. |

Both bodies contain only `detail` with `code`, `message`, and `restart_required: true`.
A well-formed token naming an unavailable key receives the restart response without
trusting its payload. Foreign contexts share one response; do not expose foreign ids.
Authentication refusal precedes token processing and keeps the existing authentication
response, including configured-token 401 (`lionagi/studio/app.py:613`).

Every page must execute the existing authentication/origin boundary, resolve the configured
store independently of the token, establish current trusted scope, and validate cursor
bindings before fetching candidate metadata. The cursor never supplies permission. Current
trusted scope is access to the configured store. A future row-scoped deployment must
provide an authorized index traversal before candidate keys or payloads are fetched;
global fetch followed by authorization filtering is forbidden.

Each response has its own persisted read snapshot under D4. Across requests:

- Inserts before a saved frontier do not duplicate unchanged rows after it.
- Inserts after the frontier can appear in a later window.
- Equal timestamps/costs remain ordered by id; frontier deletion does not prevent progress.
- Changes to sort keys or eligibility can move a row across the frontier, causing an
  omission or repetition. V2 does not promise immutable exhaustive enumeration.
- Refreshing from the beginning discovers newly inserted earlier rows. Clients may
  deduplicate by existing Session id, but deduplication does not recover omitted moves.

### D3 — Candidate bounds precede residual matching

The candidate basis is **all sessions in the trusted scope**, traversed in the D2 order.
User search, status, kind, project, playbook, tags and mandatory mirror suppression are
residual predicates. Mirrors remain unreturnable but consume candidate budget. This
stable basis is independent of which user-filter indexes the optimizer could choose.
Facet-specific candidate modes require separate semantics and cursor fingerprints.

The sequence is mandatory:

1. Authenticate, pin the configured store, and establish trusted scope.
2. Validate version, cursor, effective filters, row limit and offset.
3. In the D4 read transaction, verify identity/readiness and seek ordered authorized keys.
4. Skip only an allowed D5 candidate offset, without fetching skipped-row payloads.
5. Examine candidates in order, applying product/user predicates after the candidate bound.
6. Hydrate only matching returned rows, stopping at row limit, candidate limit or exhaustion.

The initial candidate limit is 10,000. Read at most that many candidate keys plus one
key-only lookahead, in addition to a separately capped initial offset. Lookahead is not
matched or hydrated. If row limit stops evaluation before all prefetched candidates,
already-known unexamined keys prove that candidates remain. Do not advance past them.
Do not scan from the beginning to decide whether more candidates exist.

```text
examined = 0
returned = []
for candidate in ordered bounded window:
    mark its complete key examined
    examined += 1
    if all product/user predicates match:
        hydrate and append its listing row
    stop if row limit reached
    stop if candidate limit reached
next_cursor = last examined key if an unexamined candidate remains, otherwise null
```

Every nonterminal request advances strictly beyond its incoming frontier, including one
returning zero rows. At exhaustion there is no next cursor. Last-match continuation is
forbidden. If the source ends exactly at either limit, lookahead establishes exhaustion
and exhaustion takes precedence in stop_reason.

The candidate relation projects keys and only the metadata needed for residual predicates.
Do not decode message histories to search a listing. Do not apply LIMIT after a global
contains WHERE clause and describe it as a scan bound. Evaluate contains using existing
SQLite LOWER, COALESCE and escaping semantics over the already bounded candidates.
`search_mode` is `bounded_contains` when an effective search or playbook contains predicate
is present, and `none` otherwise. Name, stored-label and playbook precedence/matching
remain unchanged.

Tag matching must use candidate-constrained probes on `(session_id, tag)`; this primary
key already exists (`lionagi/state/schema.sql:97`). Do not reuse the global grouped tag
subquery in the default bounded path (`lionagi/studio/services/sessions.py:396-401`). Only
returned-row tag hydration may enumerate all tags for those rows. Request tag count and
returned tag fanout remain explicit work dimensions.

This contract bounds candidate rows and residual evaluations, not arbitrary metadata
bytes, predicate input size, lock waits, or latency. Branch and progression counts retain
existing membership semantics. Current projection counts selected branches and JSON array
lengths (`lionagi/studio/services/sessions.py:581-591`), including duplicate and pruned
membership tested at `tests/apps_studio_server/test_session_listing_projection.py:80`.
Selected-row fanout and membership payload can be large and must be measured separately.
ADR-0117's proposed progression ordinal index does not supply a sessions seek index
(`docs/adr/ADR-0117-normalized-progression-membership-and-online-cutover.md:138`).

### D4 — Explicit unknown totals and one persisted response snapshot

V2 returns the same endpoint row field and effective row limit with this envelope:

```json
{
  "listing_version": 2,
  "per_page": 20,
  "runs": [],
  "status_ended_at_mismatches": 0,
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
  "total": {"kind": "unknown", "value": null}
}
```

Sessions substitutes `sessions` and `limit` and omits the run-specific diagnostic. Runs
recomputes status_ended_at_mismatches on returned rows, preserving its page-local meaning
from `lionagi/studio/services/runs.py:276`. V2 does not emit v1 page/offset echoes,
total_pages, has_next, has_prev or truncated.

The envelope invariants are:

- `returned` equals the response array length and never exceeds the effective row limit.
- `examined_candidates` counts residual evaluations, excluding skipped keys and lookahead.
- `has_more_candidates` is true exactly when `next_cursor` is non-null; it makes no claim
  that any remaining candidate will match.
- `stop_reason` is `exhausted` if no candidate remains; otherwise `row_limit` when that
  limit stopped matching, or `candidate_limit`. If both limits are reached with candidates
  remaining, row_limit takes precedence.
- `offset_basis` is always `candidates`, including cursor requests that skip zero keys.
- An empty response with continuation is a successful incomplete search window.

No exact-count query executes by default. The `total` field is present, but its numeric
value is absent: `{"kind":"unknown","value":null}`. With `exact_total=true`, it becomes
`{"kind":"exact","value":n}`, where n counts **all** authorized sessions satisfying all
product/user predicates in that response snapshot. It is not a count of the window, the
remaining suffix, or the offset prefix. The count may inspect the whole authorized store
and is explicitly outside the default bounded traversal contract. No estimated kind is
offered. A requested exact-count failure fails the response; it never silently returns
unknown. Unknown cannot be interpreted as zero or converted into total_pages.

Use one pinned configured store and one explicit read transaction for candidate keys,
identity/readiness reads, filter metadata, tag probes, returned-row persisted fields,
branch/progression aggregates, lookahead and optional exact count. Begin before the first
database read that contributes to the response. Materialize the response and close the
transaction before delivery. Cancellation closes the connection/transaction and must
interrupt the ongoing database work rather than leave an abandoned count running.

Current page/count helpers independently open connections, and tag hydration opens
another connection and executes lazy table/index creation
(`lionagi/studio/services/sessions.py:450`, `lionagi/studio/services/sessions.py:563`,
`lionagi/studio/services/run_tags.py:24`, `lionagi/studio/services/run_tags.py:89`).
V2 requires shared-transaction read helpers; it must not reuse these behaviors unchanged.
On an otherwise compatible store without a run_tags table, tag membership and hydration
are empty; nonempty tag filters therefore match none. Listing performs no lazy DDL.

The snapshot guarantee covers persisted fields, membership/count and cursor state.
Process liveness remains a live observation and is not part of the SQLite snapshot;
the current run projection already makes that observation separately
(`lionagi/studio/services/runs.py:224`). An exact count or large selected-row payload can
extend transaction duration; “one response snapshot” is not a latency guarantee.

Clients may display `No matches in this window. Continue search.` when candidates remain.
At exhaustion with no collected matches, display `No matches found in this traversal.`
Separate mutable snapshots cannot establish that the current store has no matches: a
match may have been inserted before an earlier frontier. Only an explicitly requested
exact zero supports no matches in that response snapshot. Local display filtering also
cannot turn an empty display into an exhaustive server-filter claim.
An exact-count action must identify its all-matches cost
and remain cancellable through the ordinary request lifecycle. It does not authorize
automatic traversal to exhaustion or make a single window exhaustive.

### D5 — Separate offsets, access paths, and readiness

| Control | Proposed value | Meaning |
| --- | ---: | --- |
| Initial compatibility offset | 10,000 | Authorized candidate keys skipped before matching. |
| Candidate window | 10,000 | Candidates receiving residual product/user evaluation. |
| Lookahead | One key | Ordered existence probe, without matching or hydration. |
| Cursor chain lifetime | 24 hours | Fixed from the first token issuance. |

Sessions offset maps directly to candidate positions. Runs maps `(page-1)*per_page` to
candidate positions only when there are no effective user filters. Refuse nonzero filtered
offsets; filtered requests start at the beginning and use cursors. Mandatory mirror
suppression still applies, so even unfiltered candidate offsets are not numbered matching
pages. Offset greater than 10,000 fails with the D1 cursor-required error. Offset and
candidate budgets are separate: diagnostics must expose actual skipped keys, examined
candidates and lookahead reads. Offset is never added again on continuation.

These caps are initial policy choices. Their equal values simplify accounting but do
not imply a measured optimum. Changes must be documented protocol/configuration behavior;
do not silently retune the meaning of a window.

Required proposed indexes are:

```sql
CREATE INDEX idx_sessions_listing_recent
    ON sessions(updated_at DESC, id DESC);

CREATE INDEX idx_sessions_listing_cost_reported
    ON sessions(total_cost_usd DESC, updated_at DESC, id DESC)
    WHERE total_cost_usd IS NOT NULL;

CREATE INDEX idx_sessions_listing_cost_unknown
    ON sessions(updated_at DESC, id DESC)
    WHERE total_cost_usd IS NULL;
```

They are absent at the source baseline. Current time/status indexes omit the id tie-break,
and current session index declarations contain no equivalent cost path
(`lionagi/state/schema.sql:203`, `lionagi/state/schema_meta.py:237-238`,
`lionagi/state/schema_migrations.py:281`). An index's presence alone does not prove that
the chosen seek query avoids a prefix scan, tie-group sort or full-store sort. Test every
NULL/equality/cost-partition boundary with the actual route SQL and supported SQLite runtime.

Recent readiness requires its index; cost readiness requires both cost indexes. Both sorts
must pass before v2 is released. A future row-scope index must put the trusted scope equality
prefix before the applicable ordering terms and prove that unauthorized keys/payloads are
never fetched as candidates. An ordinary project index does not establish authorization.

Readiness also verifies the access paths used after candidate selection: Session id and
progression id point lookups, an index with leading branches.session_id, and the
`(session_id, tag)` primary key whenever run_tags exists. The baseline supplies those
through `lionagi/state/schema.sql:110`, `lionagi/state/schema.sql:72`,
`lionagi/state/schema.sql:307` and `lionagi/state/schema.sql:97`. An equivalent branch
index is sufficient; a full scan over unrelated branches/tags is not selected-row fanout.
Missing required hydration access paths receive the same readiness refusal.

Add indexes and the singleton identity through the canonical schema/version/migration
lifecycle, including fresh schema, upgrade and repeated-open parity. Runtime MetaData is
the authority, and compatibility DDL must agree. Existing-table index additions need the
explicit migration path because create_all does not repair them
(`lionagi/state/schema_migrations.py:270`, `lionagi/state/db.py:1743-1751`, `lionagi/state/db.py:1864-1867`). Listing reads must
not run a migration or fabricate an incarnation on an old read-only store.

Missing required index, identity or keyring capability returns HTTP 503:

```json
{
  "detail": {
    "code": "listing_not_ready",
    "message": "Listing v2 is unavailable for this store.",
    "restart_required": false
  }
}
```

An unsupported stored sort key returns HTTP 409 with the same three fields, code
`listing_store_order_invalid`, message `Stored ordering values are unsupported.`, and
restart_required false. These failures require store/configuration repair; clients must
not retry automatically or silently substitute v1. V1 remains explicitly selectable.
An absent configured file returns an exhausted empty page, with exact zero only if
requested, and creates no database. If a supplied cursor's store has disappeared, return
the D2 restart response. Non-file backends retain the existing unsupported-store response
(`lionagi/studio/app.py:567`); this ADR does not add backend support.

The final result ORDER BY remains explicit. A final sort over at most the requested
returned rows is allowed and must be visible in plan/work evidence. A candidate sort
over the store is forbidden. This chooses bounded selected-page sorting over a blanket
no-resort requirement. Do not remove ORDER BY to rely on subquery iteration. Likewise,
do not regroup all branches or decode off-page progression JSON to hydrate a page.

### D6 — Adoption and executable acceptance

The design choices are explicit: query selector and named envelope; both sorts; signed
opaque cursor with durable identity/keyring and finite lifetime; all-authorized-session
candidate basis; 10,000 candidate and offset caps; refusal of nonzero filtered offsets;
unknown/exact totals without estimates; shared persisted read snapshot; named indexes;
explicit readiness refusal; bounded final page sorting; and no default transition.
Alternatives are recorded below rather than left as implicit implementation choices.

Implement and adopt in this sequence:

1. Add canonical schema/migration support and read-only readiness checks. Preserve v1
   fixtures and the accepted authentication boundary.
2. Add version-specific validation, cursor lifecycle, bounded traversal, shared read
   transaction and explicit result ordering. Generate separate request/response schemas.
3. Add `listRunsV2` and `listSessionsV2` with distinct types and optional AbortSignal,
   using shared authenticated fetch. Leave existing helpers unchanged. Current shared
   fetch already accepts request options (`apps/studio/frontend/src/lib/api.ts:247`).
4. Add an explicit history mode for v2 after all acceptance gates. The initial history
   page and subsequent pages use the same version/query generation. V1 stays the initial
   selection; no API, SDK, extension or UI default changes in this record.
5. Document contains-window semantics, candidate offsets, cursor expiry/restart, exact
   count cost, restore identity rotation, mutation limits and keyboard continuation.

The history mode uses explicit Load more for row-limited windows and Continue search for
candidate-limited windows. One activation requests one window. Automatic observer-driven
continuation is disabled for this mode so a visible sentinel cannot drain empty windows.
Retain the last successful cursor on failure; retry the same frontier. Only one request
may advance a chain at a time. Filter/sort/store-context changes abort requests and discard
both rows and cursors from the old generation; identity checks still reject late results.

The current UI already has a load-more observer, stale-result guards and a separate
first-page polling path (`apps/studio/frontend/src/components/fleet/FleetView.tsx:363`,
`apps/studio/frontend/src/components/fleet/FleetView.tsx:892`,
`apps/studio/frontend/src/components/fleet/useFleet.ts:79`). Changing only the older-page
pager is insufficient. A refresh in v2 starts a new chain and replaces its rows/frontier
together; a poll cannot overwrite an established continuation or silently fetch later
windows. Deduplicate repeated ids within accumulated results without treating that as a
complete solution to mutable-key omissions.

Preserve server order for both v2 sorts. This is a deliberate presentation difference:
current recent history re-sorts by ended time, while cost preserves server order
(`apps/studio/frontend/src/components/fleet/fleetReducer.ts:281`,
`apps/studio/frontend/src/components/fleet/fleetReducer.ts:285`,
`apps/studio/frontend/src/components/fleet/FleetView.tsx:840`). Keep existing derived
display-status behavior; it may hide rows locally and therefore needs loaded-window
wording and continuation even when no rows are displayable. Keep focus on the continuation
control and announce added rows or an empty searched window. Never derive page numbers
from an exact total because candidate windows are not matching pages.

The following named tests are required future acceptance tests, not assertions that
they exist or pass at the source baseline. Existing v1 tests remain in force.

| Contract | Test that fails if broken |
| --- | --- |
| V1 shapes, defaults, matching offsets, totals, search and sort remain unchanged | `test_v1_default_and_explicit_version_identical`; `test_v1_matching_offsets_unchanged`; existing `test_runs_list_search_composes_with_pagination` at `tests/apps_studio_server/test_runs_list.py:405`. |
| Selector, endpoint-specific controls and response discriminator | `test_v2_selector_validation_and_schema`; `test_v1_rejects_v2_controls`; `test_v2_sessions_does_not_add_filters`; `test_v2_run_page_diagnostics`. |
| Complete raw ordering, NULLs, zero, equal timestamp/cost, deleted frontier | `test_v2_recent_keyset_equal_and_null_timestamps`; `test_v2_cost_keyset_zero_null_equal_cost_and_timestamp`; `test_v2_deleted_frontier_uses_saved_tuple`; `test_v2_raw_key_not_dto_fallback`. |
| Codec/types/signature/context/errors without information echo | `test_v2_cursor_codec_roundtrip_and_tamper`; `test_v2_cursor_context_binding`; `test_v2_cursor_errors_do_not_echo_input`; `test_v2_cursor_allows_limit_and_count_change`. |
| Restart, rotation, expiration, restore and absent store | `test_v2_cursor_restart_rotation_expiry`; `test_v2_restore_changes_incarnation`; `test_v2_absent_store_creates_nothing`. |
| Authentication every page and scope before keys/payload/hydration | `test_v2_auth_precedes_cursor`; `test_v2_scope_precedes_candidate_payload`; cross-scope poison rows and scope-index work assertions. |
| Candidate bound before matching, literal contains and candidate-local tags | `test_v2_rare_match_stops_before_result_limit`; `test_v2_literal_contains_semantics`; `test_v2_tag_work_ignores_unrelated_rows`; `test_v2_mirrors_consume_budget_but_never_return`. |
| Empty/partial progress, lookahead and stop-reason precedence | `test_v2_empty_window_advances_last_examined`; `test_v2_lookahead_not_evaluated`; `test_v2_unexamined_prefetch_does_not_advance`; `test_v2_exhaustion_and_limit_precedence`. |
| Offset basis, filtered refusal, cap boundary and cursor conflicts | `test_v2_filtered_nonzero_offset_refused`; `test_v2_unfiltered_candidate_offset_boundary`; `test_v2_cursor_offset_conflict`. |
| Unknown default, exact full-scope count, failure and persisted snapshot | `test_v2_default_route_executes_no_exact_count`; `test_v2_exact_total_full_scope_not_window`; `test_v2_exact_total_failure_not_unknown`; `test_v2_page_tags_total_share_read_snapshot`. |
| No lazy DDL, empty legacy tag table, cancellation releases work | `test_v2_read_path_performs_no_ddl`; `test_v2_missing_tags_table_is_empty`; `test_v2_cancel_exact_count_closes_transaction`. |
| Explicit final order, page-only projection/sort and index lifecycle | `test_v2_reverse_unordered_reads`; `test_v2_final_sort_input_is_returned_page_only`; `test_v2_listing_indexes_fresh_upgrade_reopen_parity`; `test_v2_missing_index_refuses_without_store_mutation`; `test_v2_missing_hydration_or_tag_access_path_refused`. |
| Mutable collection semantics | `test_v2_inserts_preserve_unchanged_row_progress`; `test_v2_updated_sort_keys_show_documented_move_limits`; `test_v2_cost_partition_crossing_work`; `test_v2_empty_traversal_does_not_claim_current_store_empty`. |
| Whole-route bounded traversal and separate payload/count costs | `test_v2_route_work_store_growth`; `test_v2_route_work_first_vs_page_500`; `test_v2_exact_work_reported_separately`; `test_v2_selected_payload_cost_separate`. |
| Typed client, abort, empty continuation, order, refresh and keyboard | `test_v2_history_empty_window_explicit_continue`; `test_v2_history_no_auto_drain`; `test_v2_history_filter_change_aborts_and_discards`; `test_v2_history_refresh_starts_new_chain`; `test_v2_history_preserves_server_order`; `test_v2_history_unknown_total_and_keyboard_progress`. |

Work acceptance uses controlled 20,000- and 200,000-session fixtures. Keep returned-row
branch fanout, progression size, metadata length and request filter size fixed while
growing unrelated rows. Exercise first page and deep page 500 with 20-row pages for both
sorts on dense distinct-key, tie-heavy, NULL-heavy and all-unknown-cost fixtures with
enough matching rows to reach that frontier. Separately compare equal-budget windows
for sparse/no-match, mirror-heavy and unrelated-tag-growth fixtures. Those cases need not
reach page 500; empty-window tests cover full candidate budgets and exact exhaustion.

Measure all SQL in the real endpoint request, including schema/identity checks, candidate
seeks, residual probes, hydration and optional counts. Record keys visited, skipped keys,
residual evaluations, lookahead keys, tag probes, hydrated rows and SQLite VM steps.
For a default cursor page, candidate keys read are at most 10,001 and residual evaluations
at most 10,000; an allowed initial offset adds its actual skipped keys. No unrequested
count runs. Index range plans must exclude full-store sorting and unbounded prefix work.

In addition to those direct bounds, comparable default windows must stay within a proposed
2x VM-step regression threshold between the fixture sizes and between first/deep pages,
with fixed payload dimensions. This is a test threshold, not an observed result or proof
of all asymptotic behavior. A cursor-depth test obtains and replays the frontier and
measures that request separately from all prior navigation requests. Count queries,
selected-row JSON/fanout, index build/storage/write cost and cancellation/transaction
retention are measured in separate fixture dimensions, not hidden by the default-path total.

Host-pinned timing accompanies these counters and records runtime versions and fixture
construction. This record chooses work-counter acceptance plus timing evidence over a
fixed universal latency claim. A later supported-host latency target requires its own
stated workload and measurement. No test result or measured-number claim is asserted here.

Any later default transition needs an explicit compatibility and adoption decision.
V1 removal is not selected. A future removal must follow the then-applicable deprecation
policy, including its scoped transition requirements and mandatory removal record.

## Consequences

- V2 bounds candidate traversal and residual evaluations without pretending a contains
  window exhaustively searched the store. Selective filters may need more interactions
  than an indexed exhaustive v1 query.
- Exact totals remain available at explicitly requested all-scope cost. Persisted page
  data and count agree within one response; cross-request mutations remain visible.
- Durable cursor keys and store incarnation become compatibility state with concrete
  rotation, restore and expiration duties. Signing enforces issuance, not authorization.
- Supporting both sorts adds indexes and write/storage/migration cost. Explicit readiness
  refusal makes unsupported old stores visible while retaining their v1 path.
- Candidate-local tags and shared read helpers are required work, not a wrapper around
  the existing global predicate and multi-connection helpers.
- Selected-row aggregation can still depend on large membership payloads. This ADR does
  not claim page size alone bounds total latency or replace progression storage decisions.
- Explicit continuation and backend ordering change the opting-in history experience.
  Existing clients and defaults retain their current contracts.
- A bounded final page sort is permitted. Store-sized candidate sorting, hidden exact
  counts, and incidental result ordering do not satisfy v2.

## Alternatives considered

### Keep only compatible v1 SQL improvements

This is sufficient for a projection-only objective and avoids new protocol/client state.
The baseline already contains page-first correlated aggregation. It does not eliminate
deep matching OFFSET, global residual search or required exact counts. Choose this
alternative if the product requirement is narrowed to the compatible projection problem;
do not claim it provides the v2 contract described here.

### Recent-only v2, with cost retained on v1

This reduces indexes, migrations and NULL/cost seek cases. Both current sorts are selected
to avoid an incomplete v2 navigation mode. Reconsider recent-only if measured index
maintenance exceeds a stated supported-deployment budget or cost seeks fail the work gate.
No such measurement is claimed in this record.

### One cost expression index

An index beginning with `(total_cost_usd IS NULL)` states the complete order compactly
and may reduce lifecycle complexity. Separate reported/unknown indexes are selected to
make partition seeks explicit. Replace them if actual NULL/equality/frontier plans and
VM counters prove the same bound with lower measured write/storage cost. An index name
in a plan alone is insufficient.

### Unsigned opaque cursor or transparent tuple

Typed validation and independently enforced scope can preserve authorization and work
bounds without signing secrets. Transparent tuples are easy to inspect and interoperate
with. Signed opacity is selected for server issuance, context integrity and codec evolution,
not because signing grants permission or hides data. If durable key/restore lifecycle
cannot be delivered, explicitly select unsigned opaque cursors with the same scope/work
tests before adoption; do not invent restart durability from ephemeral keys.

### Indefinite cursors or process-local cursor state

Indefinite lifetime helps long browsing sessions but requires unbounded old-codec/key
support. Process-local state is simple but invalidates continuations on every restart.
The selected finite chain lifetime with durable keyring makes both costs explicit.
Reconsider its duration using actual long-session needs, not an assumed optimum.

### Prefix/FTS or facet-indexed candidate modes first

These can reach rare matches with fewer empty windows and exploit selective filters.
They require different text/completeness semantics, index maintenance, and candidate-basis
definitions. Bounded contains preserves current literal matching within a stated window.
If sparse-search interaction tests demonstrate that this is unusable, define a named
indexed mode or retain v1 search rather than automatically draining windows.

### Return-LIMIT after global matching, or last-match continuation

Global matching keeps exhaustive matching-page semantics but can inspect an arbitrary
prefix. Last-match continuation works for fully indexed matching collections but cannot
advance a bounded window with no matches. Neither meets the selected candidate-work
contract; last-examined progress is required.

### Candidate offsets even with user filters

Uniform candidate offsets let informed clients jump to shallow candidate positions.
They also make a familiar page number differ sharply from matching-page meaning under
sparse filters. Refusing nonzero filtered offsets is selected because a new client already
needs cursors. A demonstrated consumer requiring explicit filtered candidate jumps could
justify this alternative before adoption. V1 matching offsets remain in either case.

### Always exact, estimated, or omitted totals

Always-exact counts give familiar completeness information but retain all-scope work.
Estimates can provide orientation, but need coverage, freshness and error semantics plus
an implemented estimator. Omitting total entirely is smaller but leaves unknown implicit.
A fixed unknown/exact object is selected; no estimate is implied and no failure becomes
unknown silently. A separately validated estimator could add a named kind later.

### New versioned paths or a version header

Separate paths simplify generated schemas and isolate handlers. Headers keep URLs shorter
but need correct cache variation and less visible negotiation. The query selector retains
paths and makes opt-in reproducible. Choose separate paths before adoption if generated
client/schema tests cannot safely represent the conditional contract. Existing v1 paths
must continue serving their unchanged shapes under any alternative.

### Immediate default transition or automatic history continuation

Immediate adoption spreads bounded-default behavior quickly; automatic continuation
feels smoother for dense matches. Both expand the migration impact, and automatic empty
windows can turn a single action into store-sized work. Explicit history opt-in and one
window per activation are selected. Usability evidence may favor keeping v1 or developing
indexed search, but does not justify hiding unbounded iteration.

### Independent page/count snapshots

Independent reads reuse current helpers and shorten some read transactions. They allow
persisted page metadata, tags and exact counts to disagree. One response transaction is
selected; if measured retention is unacceptable, a separately timestamped count operation
needs a changed contract rather than a silent second connection.

### Legacy fallback or no final sort

Legacy fallback improves old-store availability but cannot silently keep the v2 label
while losing the work bound. A proven bounded fallback could replace readiness refusal.
Eliminating a final sort is desirable only when explicit result ordering remains correct.
Bounded returned-page sorting is allowed; dropping ORDER BY and trusting CTE iteration is
not. Compare actual plans and work rather than inferring order from one execution.

### Latency-only acceptance or a cross-request snapshot

Latency targets express user experience but can hide linear work on a fast fixture;
work counters need payload-sensitive timing evidence to avoid the opposite blind spot.
Both forms of evidence are retained without an unsupported universal latency assertion.
An immutable cross-request snapshot would simplify exhaustive export but requires retained
version/snapshot machinery beyond this mutable listing. V2 states its update limitations
and uses refresh for a new traversal.
