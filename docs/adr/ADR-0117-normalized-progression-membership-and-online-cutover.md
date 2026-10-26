# ADR-0117: Normalized progression membership and online cutover

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started — no implementation begins before the start trigger
  below is met
- **Area**: persistence-state
- **Date**: 2026-08-11
- **Relations**: supersedes ADR-0055 D3 when accepted; extends ADR-0056; Refs
  [#3104](https://github.com/ohdearquant/lionagi/issues/3104)

## Context

Line citations in this record are at source baseline
`c325f4ef138d72b84eea589a6a590d81cc80ecea`.

ADR-0055 D3 stores an ordered collection of message identifiers as one JSON-array text value.
This gives a simple full reconstruction and idempotent append, but append work grows with the
history already stored. SQLite scans membership and reconstructs the array; PostgreSQL expands
the text as JSON, checks membership, appends and serializes it again
(`lionagi/state/db.py:3202`). Live persistence normally updates both a branch progression and
a session progression in one transaction (`lionagi/state/db.py:3955`).

Bulk insertion already avoids one rewrite per message: it decodes a string-ID collection,
deduplicates a batch with a set and rewrites the collection once
(`lionagi/state/db.py:3275`). This improves amortization but retains full-history work per batch.

### Measured cost and population

Measurements supplied on 2026-10-26 exercised `_persist_live_message`, `insert_messages` and
`get_progression` against fresh SQLite stores. The measured persistence code is unchanged
between revision `1717674a` and that baseline.

| Nominal starting IDs | Live p50 / p95, ms | Bulk b=1 p95, ms/call | Bulk b=10 p95, ms/call | Full-list read p95, ms | Compact JSON bytes, both progressions after live phase |
|---:|---:|---:|---:|---:|---:|
| 1,000 | 1.304 / 2.123 | 4.150 | 6.990 | 0.753 | 93,992 |
| 10,000 | 3.881 / 7.464 | 12.183 | 14.323 | 1.325 | 795,992 |
| 100,000 | 42.909 / 48.113 | 79.694 | 84.276 | 8.127 | 7,815,992 |

Live p95 grows 22.7 times between the nominal 1k and 100k cases. Approximately 7.8 MB of logical
array payload is reconstructed for two 100k progressions. This is not a measurement of physical
disk or WAL bytes. The script uses UUID strings, five warm-up calls, 200 measured live calls,
50 calls per bulk size and 20 reads. Live calls grow the starting length by 205; subsequent
bulk calls grow it by another 605 before reads. Thus these are nominal size buckets, not
constant-length samples. Bulk latency is per call, not per identifier.

The run was on a loaded host and did not measure PostgreSQL, concurrent writers or a production
working set of historical message rows. The numbers establish an observed growth problem and
motivate a regression gate; they are not cross-machine latency guarantees.

A contemporaneous census of the larger retained store reports 31,608 progressions, a maximum
of 286,003 IDs, and approximate text-size thresholds of 308 near/above 1k, 40 near/above 10k and
12 near/above 100k. Large examples are transcript histories, commonly represented by equal-length
branch and session progressions. These row counts are not unique transcript counts. The census
does not provide total membership cardinality or the cost of normalized indexes.

The 12 large transcript-mirror progressions hold approximately 100k–286k IDs each, with no
writes reported in the preceding 56 h. They carry the demonstrated large-history cost but are
not the currently written population. Their append saving would arise only if writes resume.

Recent writes were much smaller: the largest updated progression in the preceding day was
1,295 IDs. A collection of 10,545 branch snapshots had p99 689 and maximum 3,483 messages.
The smaller configured store had ten progressions and maximum eleven IDs. The measurement
therefore does not establish a current latency problem for most runs, and it does not measure
the exact cost at the observed 3,483-ID maximum.

### Why a storage decision is still needed

Mirroring is a supported recurring feature. It has a standalone loop and historical catch-up
mode (`lionagi/cli/mirror.py:47`, `lionagi/cli/mirror.py:59`, `lionagi/cli/mirror.py:1003`), and Studio starts an embedded task
(`lionagi/studio/app.py:225`, `lionagi/studio/app.py:390`). Mirroring is enabled by default when the profile has eligible
sources (`lionagi/studio/config.py:653`, `lionagi/studio/config.py:709`, `lionagi/studio/config.py:717`). A process-name search cannot rule out
the embedded task. A recent-file window does not cap an active transcript's lifetime length.

Other writers also have no lifetime ceiling. Resume reuses existing progression IDs
(`lionagi/cli/_runs.py:2023`); flow branches share a session progression
(`lionagi/cli/orchestrate/_orchestration.py:2202`); run import can create or replace complete
unbounded orders (`lionagi/cli/state.py:247`, `lionagi/cli/state.py:284`). The small current population justifies a
staged rollout, not a permanent assumption that these histories remain small.

Read amplification persists for idle histories. A detail page loads entire arrays before taking
a tail window (`lionagi/studio/services/sessions.py:1292`, `lionagi/studio/services/sessions.py:1318`), counts expand JSON (`lionagi/studio/services/sessions.py:538`),
and streaming expands membership (`lionagi/studio/services/sessions.py:1554`). The measured 8.127 ms read is only a warmed full-list
read near 100k, not endpoint latency at the observed maximum. These are reachable read costs;
the supplied evidence does not measure how often the idle large histories are read.

Choosing one representation for all sizes distributes migration and compatibility costs beyond
the population with the demonstrated large-history problem. For current small writes, the
benefit is preventive and a latency improvement is unmeasured. The decision supports future large
writes and potentially cheaper reads of retained large histories; it does not establish an
immediate performance return on migrating every stored progression.

The column is also a published read surface. Direct readers include retention code that protects
messages referenced by surviving progressions (`lionagi/studio/services/db_maintenance.py:544`;
`lionagi/cli/state.py:884`). Freezing JSON without migrating these readers can hide references
and affect deletion correctness, not merely display stale counts.

## Teardown of the previous premises

- Small present-day runs do not establish a storage invariant. Resume, imports and session
  aggregation permit continued growth.
- A bounded result set does not establish bounded work. Parsing one full array before taking a
  slice is still a full-history read; an indexed `COUNT(*)` still visits membership entries.
- A 500-row backfill limit does not bound JSON parsing, source extraction, verification or writer
  lock duration. Those costs must be separated from write chunks.
- Frozen JSON is a prefix after the first target-only append. Reading that column alone cannot
  be a complete rollback. A fallback that still needs the relational tail is not an independent
  recovery copy.
- A message-unique constraint would reject legacy duplicate-bearing arrays. Duplicate prevention
  for new appends and faithful reconstruction of existing positions are separate obligations.

## Decision

Use relational membership for all progression sizes as the target representation, preserving
logical identities. Deployment proceeds by store and then by progression; the existence of a
new schema does not automatically enable migration. Existing JSON stays authoritative until all
compatibility and parity gates for that progression and store pass.

| Concern | Decision |
|---|---|
| Ordered membership | D1: One row per original position, no message foreign key, exact cached membership count. |
| Writers | D2: One lock protocol for all append, bulk, replacement and migration operations; transactional contiguous ordinals within a generation. |
| Migration | D3: JSON authority during chunked backfill; full scans outside writer locks; revision-checked cutover. |
| Readers and fallback | D4: Explicit authority and versioned reader compatibility; complete prefix-plus-tail fallback after cutover. The rollback contract is open (Open item 1). |
| Rollout | D5: Upgrade readers and writers first; migrate with explicit controls and observable progress. |
| Evidence | D6: Semantic parity, query plans, restart safety and measured real-path performance before completion. |

This decision does not change the runtime ordered-ID abstraction, session identity, message
contents, retention duration or eligibility, artifact storage, or transcript import policy.
Retention implementations must adapt to the new representation while preserving their existing
policy. It does not remove `progressions.collection` in the first implementation.

### D1 — Relational membership and exact counts

The membership relation is:

```sql
CREATE TABLE progression_items (
    progression_id TEXT NOT NULL REFERENCES progressions(id) ON DELETE CASCADE,
    ordinal BIGINT NOT NULL CHECK (ordinal >= 0),
    message_id TEXT NOT NULL,
    PRIMARY KEY (progression_id, ordinal)
);

CREATE INDEX idx_progression_items_message
    ON progression_items(progression_id, message_id, ordinal);
```

There is no uniqueness constraint on message membership and no foreign key to `messages`.
Legacy repeated IDs occupy their original positions. References survive message-row pruning.
Deleting a progression deletes its membership; deleting a session must continue to protect any
progression still shared by another surviving owner.

The canonical runtime schema remains SQLAlchemy metadata, with the compatibility DDL kept in
parity (`lionagi/state/schema_meta.py:84`; `lionagi/state/schema.sql:71`). Existing progression
IDs, timestamps and owner references remain unchanged.

Each progression has a storage-state row (`progression_storage_state`) with these defined fields:

| Field | Contract |
|---|---|
| `progression_id` | Primary key and cascading foreign key to the progression. |
| `incarnation` | Fresh opaque token for each progression/state lifetime; never reused after deletion and recreation of the same progression ID. |
| `phase` | `json`, `dual`, or `items`. JSON is authoritative in the first two; items in the third. |
| `blocked_reason` | Nullable bounded reason code; blocks migration advancement without erasing phase or authority. |
| `generation` | Nonnegative, monotonically incremented on every wholesale replacement or explicit migration reset. |
| `json_revision` | Monotonic version of JSON content, incremented on every actual JSON append or replacement by an admitted writer. |
| `item_count` | Exact logical membership count, including duplicate positions and missing-message references; nullable only while a legacy JSON row has not been counted. |
| `next_ordinal` | Next position; equals `item_count` in initialized `dual` and `items` phases. |
| `source_count`, `backfill_next` | Admission prefix length and next uncopied prefix position. |
| `frozen_count` | Prefix length represented by frozen JSON after cutover. |
| `verified_revision` | JSON revision whose complete contents passed the successful parity check. |
| `source_digest`, `items_digest` | Ordered digests from the last successful verification, with generation and revision context. |
| `compat_read` | Persisted selection of complete prefix-plus-tail full reads after cutover. |
| `shadow_cleanup_pending` | Prevents readmission until abandoned shadow rows have been removed in bounded chunks. |
| `updated_at`, `cutover_at` | Maintenance observability. |

All counters use nonnegative 64-bit integer domains with overflow refused transactionally.
Constraints require initialized `dual`/`items` counts and allocator fields, a backfill cursor
between zero and `source_count`, and complete prefix state for `items`. A missing state row
means legacy JSON, not a missing progression. State creation always checks and locks the owning
progression first. Metadata may be created lazily; initialization assigns its incarnation before
any migration snapshot is taken. Every participating JSON writer must update its revision before
migration can be enabled.

Snapshots, verification results, maintenance work and new storage cursors identify both
incarnation and generation. Every generation comparison below also requires the same incarnation.
Replacement/reset advances generation within a lifetime; deletion and recreation establish a
different incarnation even when a transcript importer deterministically reuses the progression
ID. A stale worker must not populate a newly recreated progression from its predecessor's cache.

Ordinals are contiguous from zero through `item_count - 1` in a completed representation within
one generation. They are allocated by a transactional row update, never by a non-transactional
sequence. Rollback and duplicate no-op consume no position. There is no individual membership
deletion operation in this contract. Wholesale replacement creates a new generation and assigns
positions from zero again. This makes count-based frozen boundaries exact; arbitrary ordinal
gaps are not permitted.

`get_progression(id)` still returns `list[str]`, and still returns `[]` for missing and empty
progressions. It intentionally remains proportional to the requested history. New bounded item
reads expose incarnation, generation and `(ordinal, message_id)` positions. Forward reads seek strictly
after an ordinal; reverse reads seek strictly before it. The two cursor directions are mutually
exclusive. Limit defaults to 500, must be positive and is capped at 1,000. A cursor may carry an
expected incarnation and generation; a mismatch is a stale-cursor error, never silently reused
positions.

`count_progression_items(id)` returns zero for a missing row and the cached `item_count` after
initialization. It counts positions, not distinct IDs or surviving message rows. Uninitialized
legacy JSON may still require a full parse. Target count plans must be primary-key metadata
lookups; `COUNT(*)` over every item is reserved for verification, not the hot count API.

**MAY:** add indexes justified by actual reader plans. **MAY NOT:** add message ownership through
a foreign key, deduplicate historical positions, or expose storage position as message identity.
**Verify by:** round-trip reconstruction including duplicate and pruned-message cases, cached
count parity, stale-cursor cases, and schema parity on both backends.

### D2 — Every writer uses the same transactional authority

SQLite uses its existing `BEGIN IMMEDIATE` transaction ownership. PostgreSQL locks the owning
progression row before creating or locking its storage-state row, which closes the absent-state
initialization race. Transactions touching multiple progressions acquire their locks in sorted
progression-ID order. This protocol must apply consistently to single append, bulk append,
message persistence, replacement, deletion and migration. Integration must also account for the
other message/session locks those transactions take; no separate lock order is introduced in
one helper alone.

In `items` phase, append checks the progression/message index under that lock. If present, it
returns without changing membership, count or allocator. Otherwise it inserts at `next_ordinal`
and increments count and allocator in the same transaction. A missing progression remains a
successful no-op. The normal append path neither parses nor rewrites JSON and never loads the
full normalized list to construct a Python set.

Bulk append preserves incoming order and admits each new ID once, including duplicate IDs within
the batch. Indexed membership checks and insert work scale with batch size and index access, not
the entire prior collection. Message upsert, both progression updates, and session-activity
updates retain their current transaction boundary (`lionagi/state/db.py:3241`, `lionagi/state/db.py:3922`). A
duplicate membership does not suppress an otherwise valid message-content upsert.

In `json`, preserve legacy append and bulk behavior. Increment the JSON revision only when the
collection changes; update an initialized exact count in the same transaction, or leave the
count explicitly unknown until initialization. Do not serve a stale cached count after a JSON
append or replacement.

In `dual`, the legacy JSON result decides whether an ID is new, including duplicates still only
in the uncopied prefix. Successful new IDs receive contiguous positions at or above
`source_count`. JSON, shadow items, count, allocator and revision commit atomically. Full JSON
cost intentionally remains during this finite phase. A target failure must not be hidden behind
a committed JSON-only append presented as a successful dual write.

Creation preserves `ON CONFLICT DO NOTHING`: re-creating an existing progression does not reset
its representation or content (`lionagi/state/db.py:3162`). Newly created progressions may use
`items` only after the store's compatibility gate enables that default. Their supplied collection
is materialized in input order, preserving duplicates, and their initial JSON value is an exact
frozen prefix. Empty creation uses an empty prefix. No count is inferred from message existence.

`set_progression` remains a wholesale replacement and a missing-row no-op
(`lionagi/state/db.py:3172`). It locks the progression, increments generation and JSON revision,
and invalidates pending snapshots and verification. It preserves phase:

- `json`: replace JSON; reset migration evidence. Any abandoned shadow is ineligible for use.
- `dual`: replace JSON and all item rows atomically in input order; set source count, count,
  allocator and backfill cursor to the replacement length. Require a fresh verification before
  cutover, even though the replacement has filled all positions.
- `items`: replace items and the JSON value atomically; the complete replacement becomes the new
  frozen prefix. Set source count and backfill cursor to the replacement length as well as
  resetting count, allocator, prefix length and digests. Subsequent
  appends again leave JSON untouched.

A blocking reason is not cleared merely by replacement. Failed replacement rolls back all
changes. Replacement is explicitly allowed O(old membership + replacement payload) work; it is
not subject to the normal-append performance promise. The frozen-JSON rule applies to append,
not to this explicit whole-collection operation. Very large replacement needs separately
scheduled capacity; the first implementation must not disguise it as chunked non-atomic update.

**MAY:** batch indexed checks and inserts within backend parameter limits. **MAY NOT:** bypass
the router through the bulk fast path, allocate outside the transaction, drop owner restrictions,
or infer duplicate membership from `messages.id`. **Verify by:** concurrent duplicate and unique
appends, reversed progression lists, repeated creation, replacement races and atomic failure on
both backends, including compatibility-mode refusal behavior.

### D3 — Resumable migration without full-history work under writer locks

Migration has three authority phases, with a separate blocking flag:

```text
json  --admit-->  dual  --verified revision check-->  items
                     
blocked_reason may stop advancement in any phase without changing authority.
```

Old writers must be excluded before admission: a JSON mutation that does not advance the revision
would invalidate this protocol. A version marker that old software ignores is insufficient.

**Admission.** Read JSON and its generation/revision in a consistent read snapshot, validate the
array of encodable strings and compute its length outside a writer transaction. Then take the
progression lock and compare generation/revision. If changed, discard the candidate and retry
later. If unchanged, with no pending shadow cleanup, record the prefix count, count/allocator,
zero cursor and `dual` phase in one short transaction. No full JSON parse occurs under this lock.
Malformed or unsupported legacy values stay JSON-authoritative with an explicit reason; their
existing read behavior, including existing parse failures, is not silently repaired.

**Extraction and backfill.** Once admitted, normal append cannot modify the captured prefix.
Read and decode that prefix outside the writer transaction; retain it for successive chunks
rather than expanding the entire JSON value under a lock for every 500 IDs. Process death may
require another full source read, but the committed cursor still determines what remains to copy.
Replacement/reset changes generation, so a worker must discard a stale extracted prefix.

Each write chunk contains at most 500 positions and 4 MiB of encoded identifier text. It locks
the progression, checks phase, generation and expected cursor, inserts the original JSON indexes
as ordinals, and commits cursor movement with the inserted rows. The worker checks a conflicting
existing row's ID: exact equality is an idempotent replay; unequal content blocks migration.
`ON CONFLICT DO NOTHING` alone is not a parity check. A single identifier exceeding the byte
budget blocks automatic migration with an explicit reason, rather than silently exceeding the
budget or changing the value. Backend indexability failures receive the same treatment.

Source reading and decoding remain O(n) and have separately reported memory, elapsed-time and
snapshot-lifetime costs. A row or byte cap bounds the submitted write payload, not the time a
database statement waits for I/O or locks. The service yields between chunks and supports
cancellation; a nominal time budget is cooperative, not a guarantee that an in-flight SQL
statement can be preempted at that instant. Neither admission nor chunk extraction may take
SQLite's writer reservation to do a full-history scan.

**Verification and cutover.** Once `backfill_next == source_count`, obtain a consistent read
snapshot of JSON, ordered items and their state. Verify contiguous ordinals, complete count,
allocator/count equality and an ordered SHA-256 digest. The digest frames each ID as
`uint64_be(utf8_byte_length) || utf8_bytes`, preserving repeated positions without depending on
JSON whitespace. Compute it outside the writer transaction. The read snapshot must not use a
connection hook that acquires `BEGIN IMMEDIATE` merely to begin a read.

Take a short write lock and require exactly the same generation and JSON revision, complete
cursor, expected count and no blocking reason. Only then record both digests, verified revision,
`frozen_count = item_count`, timestamp and `phase = items` atomically. If a writer changed the
revision, retry verification later. The check-to-flip interval contains no unprotected append.
If a consistent-snapshot parity check fails, block that generation's migration with a reason;
do not classify a stale snapshot from a replaced generation as current data corruption.

Verification may starve on a continuously changing progression. A bounded retry budget leaves it
in `dual`, reports the condition and yields. It must not solve starvation by moving the full
verification into a long writer transaction. Explicit scheduling in an idle window is allowed;
automatic write suspension is not part of this decision. Long read snapshots can also affect
WAL retention and must be monitored and cancelled when rollout limits are exceeded.

Before cutover, JSON remains complete. Stopping migration leaves reads on JSON immediately.
An explicit abandonment may return writes to `json`, invalidate the generation and mark shadow
cleanup pending. Cleanup deletes only abandoned shadow data in bounded chunks before readmission;
new JSON appends remain valid throughout. After cutover, there is no automatic representation
reversal. Reader fallback is defined separately in D4.

A blocking reason preserves phase. In `json` or `dual`, existing JSON authority remains readable;
dual appends remain atomic where possible, otherwise the transaction fails without partial
commit. In `items`, a known authority-integrity failure must not silently serve frozen JSON as
complete. Writes stop for the affected progression until authority is repaired; reads may use
the complete fallback only when its prefix and required tail are trustworthy. Unrelated
progressions continue.

**MAY:** cache a validated source snapshot between chunks and adjust chunk sizes downward.
**MAY NOT:** cut over on count alone, reparse full history while holding the writer, reuse a
stale generation, or let one blocked progression change another's authority. **Verify by:**
restart at every chunk boundary, concurrent replacement and append, mismatched conflict rows,
snapshot invalidation, and measured write-lock/WAL behavior at the observed largest size.

### D4 — Complete readers, with a precise fallback contract

| Phase | Full read | Bounded items and membership count |
|---|---|---|
| Missing state / `json` | Current JSON through the legacy decoder | Legacy expansion/counting until initialized; explicitly not the target performance path. |
| `dual` | Current JSON | JSON for ordered items; initialized exact logical count is available from state. |
| `items` | Ordered relational items, or selected complete compatibility read | Ordinal index seeks and primary-key count lookup. |

Routing and data reads must observe one consistent snapshot or validate the generation/phase
again. Separate autocommit queries that can combine pre-replacement JSON with post-replacement
items are not a valid complete read.

In `items`, `compat_read` reconstructs:

```text
the frozen JSON prefix of length frozen_count
  + relational items with ordinal >= frozen_count, ordered by ordinal
```

Validate the prefix length and boundary against state. The tail, if present, starts at
`frozen_count` and is contiguous through `item_count - 1`. A gap, overlap, inconsistent
generation or known corrupt required tail is an error. Ordinary append leaves prefix content
unchanged. Wholesale replacement establishes a new exact prefix under D2.

This is an immediate **reader fallback**, not a downgrade to an old writer and not a second
complete storage copy. It may bypass normalized prefix reads, but still depends on normalized
tail rows. It does not recover lost tail data. Returning to a complete raw JSON column after
items-only writes requires an explicit rebuild and write-routing decision.

Which rollback contract acceptance adopts is not decided here (Open item 1). Under this
reader-fallback contract the acceptance wording is: “A rollback switch returns complete
list-shaped reads using the frozen JSON prefix and the committed relational tail; before cutover
it returns reads to current JSON.” If instead the required behavior is an instant switch to the
raw JSON column after any new append, items-only cutover is not authorized by this decision.
Keeping that column current would retain the performance defect.

#### Required consumer changes

1. **Detail and ordinal windows.** Replace the full-array read at
   `lionagi/studio/services/sessions.py:1292` with bounded membership reads. Preserve current
   first-occurrence message anchors (`lionagi/studio/services/sessions.py:744`) with indexed lowest-ordinal lookup, generation
   checks for new cursor formats, missing-message handling and existing hydration budgets.
2. **Counts.** Replace JSON length queries at `lionagi/studio/services/sessions.py:538`,
   `lionagi/studio/services/admin.py:987`, and full-list counts at `lionagi/cli/state.py:381`
   with cached membership counts. Preserve branch aggregation versus session-order semantics.
   Role counts and surviving-message counts remain distinct and must not be replaced by the
   membership count (`lionagi/studio/services/sessions.py:1353`).
3. **Timestamp streaming.** `lionagi/studio/services/sessions.py:1554` orders by message time,
   message ID and branch ID, not ordinal. Preserve that cursor contract. Replace JSON expansion
   with relational membership and a timestamp-seeking query. A messages-driven seek on a
   composite `(created_at, id)` index with progression/message membership probes is permitted;
   the implementation must supply an actual plan demonstrating that seek and must measure
   unrelated-message selectivity. An ordinal primary key alone does not satisfy this obligation.
   If the required plan cannot be obtained, that consumer's cutover remains disabled until an
   equivalent indexed read design is accepted; silently changing stream order is prohibited.
4. **Full readers and last-ID consumers.** Route `lionagi/cli/_runs.py:1000`, `lionagi/cli/_runs.py:2039` and
   `lionagi/cli/_context_from.py:193` through complete authority. Replace the last-ID-only load
   at `lionagi/state/_mirror_common.py:315` with an indexed one-item tail. Do not describe
   intentionally complete reconstruction or role aggregation as constant-time work.
5. **Deletion and shared references.** Adapt `lionagi/state/db.py:3705`, `lionagi/state/db.py:3728`,
   `lionagi/studio/services/db_maintenance.py:382`, `lionagi/studio/services/db_maintenance.py:544` and `lionagi/cli/state.py:879`, `lionagi/cli/state.py:884`
   before any JSON freeze. Candidate and survivor reference checks must include target-only
   tail items, including shared progressions and retained references to absent message rows.
6. **Other direct readers.** Publish a versioned complete-reader contract for SQL clients,
   including phase interpretation and equivalent queries on both backends. A compatibility
   helper or view may assemble complete JSON on demand at full-read cost; it must not maintain
   that assembly on every append. It must not promise that an unchanged query against the raw
   `collection` column stays complete.

Post-cutover ordinal tail and membership-count paths must not use either backend's JSON expansion
functions. Timestamp streams must seek their equivalent timestamp index, and may not expand the
old collection. Expensive full-history aggregates remain explicitly identified; their cost must
not be hidden inside a claimed bounded page or membership-count operation.

**MAY:** preserve a slow, explicitly requested full compatibility read. **MAY NOT:** freeze JSON
before supported readers are adapted, silently return a prefix, change timestamp cursor order,
or confuse membership with message survival. **Verify by:** current API/cursor behavior, direct
SQL consumer fixtures, post-cutover tails, retained-reference deletion cases and actual plans.

### D5 — Compatibility first, then staged maintenance

Rollout is opt-in and scoped to a store. A compatibility-mode connection to an externally managed
schema must not create tables, migrate progressions, or gain new writes merely because the native
schema has changed. Such stores require their schema owner's explicit versioned contract and
updated admission profiles; otherwise they stay on legacy JSON behavior.

Release order is fixed:

1. Accept this ADR, with Open item 1 resolved and its rollback wording recorded, before any
   representation/read cutover.
   Update ADR-0055 D3 and ADR-0056's append clause to reference phase-specific authority.
2. Deploy additive native schema, all routed writers, compatible readers, retention adaptations,
   diagnostic tools and fallback selection. Leave migration and target defaults disabled.
3. Inventory attached applications and external SQL consumers. Demonstrate that old writers
   cannot mutate admitted progressions and that supported readers obtain complete history.
   Application shutdown/upgrade or enforceable database access controls may provide this
   barrier. Raw readers that cannot upgrade keep the store in JSON mode. A migration flag does
   not protect clients that ignore it.
4. Measure semantic and performance gates on both backends. Run a small representative canary,
   including duplicates, missing message rows, bulk ingestion and a large progression.
5. Enable per-progression migration. After the canary, prioritize known large histories and
   observed hot progressions; do not spend the initial rollout only on already-cheap small rows.
   Enable normalized creation only after the same store compatibility gate passes.

The census is a rollout input, not a migration manifest. Compute exact counts and ownership
relationships at execution time. The 31,608 existing rows require no destructive table rebuild:
they remain authoritative until separately admitted and verified. Space planning must measure
the sum of memberships and both indexes, plus retained JSON and WAL. Unknown or malformed rows
are reported and remain legacy; there is no silent success by omission. The smaller store is a
separate rollout unit and is not merged into the larger store by this decision.

One maintenance service owns admission, chunking, verification, status and fallback selection.
It may run in a maintenance lane or from explicit state CLI commands. Request handlers do not
opportunistically backfill. Controls support stop, resume, per-progression priority, explicit
shadow abandonment and per-progression/global compatibility reads. A global selection applies
through the phase router; it never makes raw frozen JSON a complete source.

Status reports phase counts, blocking reasons, remaining prefix positions, cleanup work,
verification retries, oldest progress time, logical bytes copied, lock-wait and lock-held time,
read-snapshot duration, WAL growth, mismatch count and fallback selection. Logs contain bounded
codes and sanitized metadata rather than message contents. Multiple maintenance processes use
locked generation/cursor checks; a stale worker yields without writing stale rows.

**MAY:** pause rollout without stopping ordinary safe writes, and leave cold JSON data unmigrated
until capacity is available. **MAY NOT:** treat schema installation as admission, silently
migrate an externally managed schema, start migration from a read request, or claim external
reader compatibility without an adopted contract. **Verify by:** mixed-phase operation,
unupgraded-client admission barriers, maintenance restart and explicit stop/resume controls.

### D6 — Completion requires evidence on the real paths

Required implementation evidence:

- SQLite and PostgreSQL parity for missing/empty rows, repeated creation, duplicate appends,
  duplicate-bearing imports, first position, order, pruned-message references, bulk batches,
  wholesale replacement and owner restrictions.
- Race and crash cases covering simultaneous first-state initialization, same-ID retries,
  different IDs, reversed progression lock lists, deletion/recreation under a reused ID,
  backfill insert/cursor commit, replacement
  during extraction/verification, and both sides of cutover. A committed message must not
  appear in only one of its intended progressions through a partial transaction.
- Independent full count/ordered-digest comparison after every restart boundary; stale workers
  and unequal primary-key conflicts must not be mistaken for successful replay.
- Post-cutover query plans for ordinal pages, reverse tails, cached counts and the existing
  timestamp stream. Reject full-array expansion on those paths and reject full item scans for
  membership counts. Include unrelated messages and legacy duplicates in stream plan fixtures.
- Representative end-to-end detail, listing, polling and retention measurements. Explicitly
  separate full-history aggregates from bounded membership operations and measure empty polls.
- Repeated same-host benchmarks at 1k, 10k, 100k and the observed 286k scale, recording host
  conditions and backend settings. At least 200 measured live calls per size after warm-up;
  enough independent repetitions to report dispersion. Alternate size/variant order. Require
  100k live append p95 no greater than twice 1k p95 on each backend after cutover.
- Exercise `insert_messages` separately at b=1, b=10 and the production mirror chunk size of 500 (`lionagi/state/_mirror_common.py:59`).
  Compare like batch sizes across histories; require the same 2x 100k/1k ratio for each admitted
  fixed batch size. Do not divide batch p95 by batch size and present it as single-append p95.
- Structural evidence that no target-only live or bulk append reads, rewrites, serializes or
  hashes full prior membership. Record logical changed rows/bytes, database/WAL growth and
  concurrent lock behavior. The p95 gate does not apply to intentionally dual-writing phases
  or explicit whole-collection replacement.
- Compatibility-read equality after target-only append and replacement, fallback persistence
  across restart, explicit failure when the necessary tail is untrustworthy, and deletion tests
  where a survivor references a message only through its post-cutover tail.

No performance or concurrency result is implied by this proposed text. Query plans establish
structure, not a universal runtime bound; repeated measurements establish performance for their
stated conditions. Both are required before declaring migration complete.

## Alternatives considered

### Retain JSON and segment transcript sessions

A physical ceiling near 1,000 IDs targets the observed large writer and preserves small JSON
rows. However, splitting only branches leaves session membership unbounded. Splitting sessions
breaks deterministic transcript-to-session identity and one-row source-ID lookup unless a new
logical transcript/segment mapping is introduced. Resume, shared session aggregates and general
imports remain unbounded. This is appropriate only with an explicit bounded-session product
contract and changed acceptance requirements, neither of which this ADR establishes.

### Store bounded JSON segments under one progression ID

This preserves identity and bounds tail rewrites, but exact duplicate no-op requires searching
all old segments or maintaining an indexed membership directory. The latter adds most of the
relational membership machinery while retaining segmentation, rollover and directory-parity
invariants. It needs the same online migration and reader coordination. It remains a viable
alternative if measured storage or full-scan benefits exceed that extra complexity; no such
comparison is available.

### Remove only the duplicate scan

This changes duplicate semantics unless another exact check replaces it. It leaves the full JSON
rewrite and all old reader costs. The normal bulk path already uses a Python set rather than the
SQL predicate, so this edit does not improve that path's mechanism. A paired benchmark can
attribute live-path cost, but no measured percentage is presently known.

### Keep JSON current forever alongside items

This preserves direct-column readers and immediate raw-column fallback, but retains the append
rewrite. It is a valid compatibility phase, not the target solution to this performance problem.
It cannot simultaneously meet the target-only write bound.

### One global conversion or only normalize future progressions

A global transaction combines unrelated histories into one lock and failure domain. Normalizing
only future rows is safer to introduce but leaves the measured large histories and resumed
writers unchanged. New-row rollout may be a deployment stage; it is not completion of the
existing-data requirement.

## Rationale

Relational membership addresses the demonstrated growth mechanism without replacing logical
session identity or requiring every writer to adopt a lifetime ceiling. The same representation
supports insertion order, exact indexed duplicate checks, bounded ordinal reads and faithful
legacy reconstruction. The census narrows urgency and migration priority; it does not eliminate
the supported growth paths or direct-reader compatibility obligations.

The migration state is warranted by the existing published read surface and retained data, not
by current write volume alone. Its complexity must be paid once at the persistence boundary,
including bulk writers and maintenance consumers, rather than recreated independently in each
writer or UI query.

## Risks and unknowns

- **Migration size:** total membership and index size are unknown. Measure before enabling a
  store and retain headroom for JSON, target rows, indexes and WAL.
- **Low current urgency:** most observed histories are small. Keep rollout opt-in and prioritize
  compatibility work over rapid conversion; current small-write measurements do not justify
  a forced migration window.
- **Verification churn:** revision checks can repeatedly invalidate a large snapshot. Report
  retries and use idle windows; persistent churn requires a separately chosen scheduling remedy.
- **Reader inventory:** external SQL consumers are not all present in this source tree. Unknown
  reader adoption blocks store cutover; it is not evidence that no readers exist.
- **Timestamp streaming:** an ordinal index does not solve timestamp-order selectivity. Require
  the actual equivalent seek plan and measured behavior before that consumer can cut over.
- **Whole-history operations:** reconstruction, role aggregates and replacement may remain
  linear. Membership normalization alone does not make an entire detail endpoint constant-cost.
- **Fallback dependence:** complete fallback still depends on the relational tail. Recovery from
  loss of that tail requires backups or another independently durable source, outside this ADR.
- **Rollback wording:** undecided (Open item 1). If current raw-column fallback is mandatory,
  keep dual writes and defer items-only cutover.

## Open items

1. **Rollback contract: reader fallback or raw-JSON rollback.** Not decided by this record. D4
   specifies the reader fallback: complete reads from the frozen JSON prefix plus the committed
   relational tail, with no second complete copy. The alternative is a rollback switch that
   returns reads to the raw JSON column after any new append. That requires keeping the column
   current, which retains the append cost this record removes, so under it dual writes stay and
   items-only cutover is not authorized. Acceptance resolves this item and records the chosen
   wording in D4.

## Start trigger

This record stays Proposed with implementation not started. Implementation begins only when one
of these is observed and recorded beside the population figures in Context:

1. **Writes resume on large progressions:** new appends are written to progressions above
   10,000 IDs, such as the large transcript-mirror histories that had no writes in the 56 h
   before the census.
2. **A measured large-history read cost:** a supported read path (a full read, count or ordinal
   window) is measured paying full-history cost on a progression above 10,000 IDs.

Either condition starts rollout planning under D5. Neither authorizes a cutover by itself:
cutover still requires the gates in D3 to D6.

## Consequences and implementation fences

Normal target append writes one membership position and transactional metadata per affected
progression instead of reconstructing the full lifetime. SQLite remains single-writer; PostgreSQL
uses progression-scoped row locks. Both need real-path performance evidence. Indexed lookups are
not a mathematical constant-time claim.

The implementation MAY stage by store/progression, adjust chunk sizes downward, expose compatible
complete full reads at linear cost, and preserve cold JSON rows until scheduled. It MAY NOT drop
the old column, change retention policy or transcript identity, discard duplicate positions,
add a message foreign key, permit stale raw-column reads to masquerade as complete history, or
introduce a second uncoordinated writer path. Verification is D6, including direct SQL and
retention consumers, not merely API unit parity.

ADR-0055 D3 remains binding until this proposed successor is accepted. Accepting this text does
not certify an implementation or authorize an automatic production migration. Reversing the
target representation after target-only writes requires an explicit data migration; the reader
fallback is deliberately a smaller operation.
