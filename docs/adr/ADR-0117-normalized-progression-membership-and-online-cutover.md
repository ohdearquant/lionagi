# ADR-0117: Normalized progression membership and online cutover

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started
- **Area**: persistence-state
- **Date**: 2026-08-11
- **Relations**: supersedes ADR-0055 D3 when accepted; extends ADR-0056

## Context

ADR-0055 D3 chose one JSON-array text value as the persistence representation of a
progression's ordered message identifiers. That choice preserves the runtime collection shape in
one row and makes duplicate append a no-op, but its cost grows with the history that preceded the
append.

**P1 — append cost grows with branch lifetime.** The SQLite append scans `json_each(collection)`
for membership and rewrites the full JSON value with `json_insert`. PostgreSQL casts the full text
value to `jsonb`, checks membership, appends, and casts it back. A live message normally updates
both its branch progression and its session progression in one transaction. The bytes inspected
and rewritten over a progression's lifetime are therefore quadratic.

Synthetic measurements using 32-character identifiers and the production SQLite statement shape
showed median append cost rising from 0.185 ms at 1,000 identifiers to 21.452 ms at 100,000. The
100,000-identifier value was about 3.5 MB, and a real store contained a progression with 100,949
identifiers in a 3.94 MB JSON value. Updating the branch and session progressions puts roughly
43 ms of JSON work on one message before WAL flush or lock wait.

**P2 — page and tail reads cannot seek.** The stored value provides no indexable ordinal. A caller
that needs the last 200 identifiers must parse or expand the full array before discarding the
prefix. Counts have the same lifetime-shaped cost.

**P3 — the store must preserve compatibility, not merely order.** Current behavior includes more
than an ordered happy path: duplicate appends retain the first position; append to a missing
progression is a successful no-op; `get_progression()` returns `[]` for both missing and empty;
progressions may retain identifiers whose message rows were pruned; and administrative import can
replace a collection wholesale. A migration that adds a message foreign key, changes missing-row
behavior, or renumbers a retry would be a contract change disguised as optimization.

**P4 — a large live store cannot be converted under one writer transaction.** A global migration
would hold SQLite's writer, grow the WAL, and force every progression to share one failure domain.
Backfill must be resumable and independently verifiable per progression while ordinary appends
continue.

**P5 — rollback and hot-write performance pull in opposite directions.** Keeping the legacy JSON
column current after cutover would retain the full-value rewrite on every append and fail the
purpose of this ADR. Stopping JSON writes makes that value a correct prefix, not a complete
fallback. The rollback contract must say this explicitly; returning stale history is not a
rollback.

| Concern | Decision |
|---------|----------|
| Ordered storage | D1: Store progression membership as `(progression_id, ordinal, message_id)` rows without a message foreign key. |
| Append semantics | D2: Serialize allocation per progression and preserve duplicate, missing-row, and wholesale-replace behavior. |
| Online migration | D3: Admit, dual-write, chunk-backfill, verify, and cut over each progression independently. |
| Reads and rollback | D4: Route reads by per-progression phase; after cutover, compatibility fallback combines the frozen JSON prefix with the normalized tail. |
| Operations | D5: Make migration bounded, observable, resumable, and fail-closed through one maintenance service. |
| Performance gate | D6: Require seek plans and lifetime-independent append cost before accepting cutover as implemented. |

This ADR does **not** decide:

- Retention duration, archive policy, or compaction. Issue #2769 owns how much history remains;
  this ADR changes how retained order is represented.
- The runtime `Progression` abstraction. It remains an ordered identifier collection.
- Message or artifact storage. In particular, no message foreign key is introduced.
- A universal event log or database change-data-capture system. The migration journal here is
  specific to progression representation.
- Removal of the JSON column. The first implementation keeps it as a frozen compatibility prefix.

## Decision

### D1 — Ordered membership is relational and progression-scoped

The target representation stores one row per position. Ordinals are zero-based, monotonically
allocated within a progression, and never reused by append. They are storage positions, not new
runtime identities.

**The contract.** The canonical schema target is:

```sql
CREATE TABLE progression_items (
  progression_id TEXT   NOT NULL REFERENCES progressions(id) ON DELETE CASCADE,
  ordinal        BIGINT NOT NULL,
  message_id     TEXT   NOT NULL,
  PRIMARY KEY (progression_id, ordinal)
);

CREATE INDEX idx_progression_items_message
  ON progression_items(progression_id, message_id);
```

There is deliberately no uniqueness constraint on `(progression_id, message_id)`. Runtime append
continues to prevent new duplicates under the per-progression allocator lock, while backfill can
faithfully preserve a legacy collection that already contains the same identifier more than once.
The non-unique message index makes the duplicate check seekable and lets compatibility tooling
identify such rows without changing them.

There is deliberately no foreign key from `message_id` to `messages.id`. A progression may retain
an identifier after retention prunes the message row, as ADR-0055 records. The foreign key to the
progression itself is safe: deleting the owning progression already deletes its ordering state,
and cascading the normalized rows only makes that existing ownership explicit.

The public full-read behavior remains:

```python
async def get_progression(progression_id: str) -> list[str]: ...
```

Bounded consumers gain an ordinal-bearing read rather than loading the full list:

```python
async def list_progression_items(
    progression_id: str,
    *,
    after_ordinal: int | None = None,
    before_ordinal: int | None = None,
    limit: int = 500,
    newest_first: bool = False,
) -> list[tuple[int, str]]: ...

async def count_progression_items(progression_id: str) -> int: ...
```

**Exact semantics.**

- `get_progression()` still returns `[]` for a missing progression and an existing empty one.
- Full reads order by `ordinal ASC` and return message identifiers only.
- Forward bounded reads seek `ordinal > after_ordinal`; reverse reads seek
  `ordinal < before_ordinal` and order by `ordinal DESC`. A caller reverses a reverse page when it
  needs chronological presentation.
- `limit` is positive and capped at 1,000. The default 500 matches existing bounded state-query
  chunks and limits response allocation without declaring a UI page size.
- Ordinal gaps are legal. Allocation may consume an ordinal before a transaction discovers a
  conflict or rolls back on a backend whose sequence primitive is non-transactional. Ordering uses
  comparison, never `ordinal == count`.
- Existing duplicate identifiers, if any, reconstruct at every stored ordinal. New appends of an
  identifier already present remain a no-op and retain the first position.
- A pruned or never-created message row does not invalidate its membership row.

**Why this way.** A composite primary key makes tail, forward page, reverse page, and count plans
progression-local. A separate surrogate item id would add identity without helping any contract.
A unique message constraint would simplify duplicate admission but could make an otherwise
readable legacy progression impossible to migrate faithfully.

Code anchors for the current boundary: `lionagi/state/schema.sql`;
`lionagi/state/schema_meta.py`; `lionagi/state/db.py`.

### D2 — One allocator lock defines append and replacement semantics

Every normalized append is serialized against the migration state row for that progression.
SQLite already enters writes with `BEGIN IMMEDIATE`; PostgreSQL additionally locks that one state
row with `SELECT ... FOR UPDATE`. Appends to different progressions remain concurrent on
PostgreSQL.

**The contract.** Per-progression storage state is:

```sql
CREATE TABLE progression_storage_state (
  progression_id      TEXT PRIMARY KEY
                            REFERENCES progressions(id) ON DELETE CASCADE,
  phase               TEXT NOT NULL
                            CHECK (phase IN ('json', 'dual', 'items', 'blocked')),
  source_count        BIGINT,
  backfill_next       BIGINT NOT NULL DEFAULT 0,
  next_ordinal        BIGINT NOT NULL DEFAULT 0,
  frozen_count        BIGINT,
  source_digest       TEXT,
  items_digest        TEXT,
  blocked_reason      TEXT,
  updated_at          REAL NOT NULL,
  cutover_at          REAL
);
```

`source_count` is the JSON length captured when the progression enters `dual`. New dual-written
items allocate at or above that boundary while backfill owns ordinals below it. `frozen_count` is
the exact JSON-prefix length at cutover.

Normalized append behaves conceptually as:

```python
async def append_to_progression(progression_id: str, message_id: str) -> None:
    async with progression_write_lock(progression_id) as state:
        if state is None:
            return
        if await membership_exists(progression_id, message_id):
            return
        ordinal = state.next_ordinal
        await insert_item(progression_id, ordinal, message_id)
        await advance_next_ordinal(progression_id, ordinal + 1)
```

The real `dual` path also performs the legacy JSON append in the same database transaction and
adds the item only when the JSON statement appended it. A duplicate that exists only in the
not-yet-backfilled JSON prefix therefore remains a no-op; backfill later copies its original
position.

**Exact semantics.**

- A missing progression has no storage-state row. Append updates nothing and returns normally.
- A duplicate lookup happens while holding the per-progression allocation lock. Concurrent
  retries cannot create two new positions.
- An append transaction either commits its membership and allocator movement together or commits
  neither. In `dual`, the JSON and item writes share that transaction too.
- `create_progression(id, collection)` creates item rows in input order when normalized storage is
  the configured default. It may preserve duplicate identifiers supplied by import, even though
  subsequent append treats them as already present.
- `set_progression(id, collection)` is an explicit wholesale operation, not an append. It locks the
  progression, replaces the JSON value once, replaces item rows in input order, resets allocator
  and migration metadata, and commits atomically. Its work is proportional to the replacement
  input, which is the operation's declared payload, not to hidden prior history.
- A failed bulk replacement leaves both representations and phase unchanged.
- A blocked progression continues to serve from its last authoritative representation but refuses
  automatic cutover. Ordinary appends follow that authoritative representation and surface a
  metric; they do not guess at repair.

**Why this way.** Deriving `MAX(ordinal) + 1` on every append is seekable but still lets two
PostgreSQL writers race. A global allocator would serialize unrelated branches. A per-progression
row gives migration and append one lock and one source of phase truth.

### D3 — Migration is a per-progression state machine

Migration proceeds independently for each progression:

```text
          admit             verified cutover
  json ------------> dual --------------------> items
    ^                  |  \                       |
    | rollback         |   \ parity/error        | read fallback only
    +------------------+    +-----> blocked <-----+
```

**Admission (`json` to `dual`).** In one short write transaction:

1. lock or create the storage-state row;
2. validate that `collection` is a JSON array of strings;
3. capture its length as `source_count` and initialize `next_ordinal` to that value;
4. set `backfill_next = 0` and `phase = 'dual'`.

After admission, append writes JSON and normalized membership atomically. New items receive
ordinals at or above `source_count`; the captured prefix is append-only and remains safe for
chunked backfill.

**Backfill.** Each transaction copies no more than 500 JSON elements or 4 MiB of identifier text,
whichever comes first, beginning at `backfill_next` and stopping at `source_count`. It inserts
their original JSON indexes as ordinals, advances `backfill_next`, and commits. Repeating a chunk
uses primary-key conflict no-ops and is safe after process death. The byte cap prevents one chunk
of unusually large identifiers from defeating the row cap.

**Verification.** Once the prefix is copied, verification compares count and an ordered digest
over the complete JSON value and item rows while holding the progression lock. The digest is
SHA-256 over repeated `uint64_be(byte_length) || utf8(message_id)` frames. It does not depend on
JSON whitespace or backend serialization.

If count and digest match, the same transaction records `frozen_count`, both digests,
`cutover_at`, and `phase = 'items'`. JSON is complete through `frozen_count - 1` and is never
mutated by later append. If parity fails or the JSON is malformed, phase becomes `blocked` with a
bounded reason code; automatic work stops for that progression.

**Exact semantics.**

- Backfill never scans beyond the admission prefix. Dual-written tail rows already exist.
- A crash before cursor commit repeats at most one chunk. A crash after commit resumes at the next
  ordinal.
- Admission, append, verification, and cutover all observe one per-progression lock. No append can
  land between parity verification and the phase flip.
- Migration order across progressions is oldest-smallest first by default, but it is not semantic.
  Operators may prioritize a hot progression explicitly.
- One blocked progression does not stop migration or appends for another.
- Malformed JSON, a non-string member, a parity mismatch, or an ordinal collision blocks cutover;
  no code silently drops or coerces an identifier.
- The first implementation does not drop or rewrite the legacy column after cutover.

**Why this way.** A global flag cannot distinguish complete from partially copied progressions and
turns one bad row into a fleet-wide stop. Per-progression state makes the failure and rollback
boundary match the data being changed. Capturing the prefix length lets live append continue
without making backfill chase a moving end.

### D4 — Read authority follows phase, with an honest compatibility fallback

The read router chooses its source from storage state:

| Phase | Authoritative full read | Bounded read |
|-------|-------------------------|--------------|
| absent / `json` | legacy JSON | JSON expansion, explicitly legacy-slow |
| `dual` | legacy JSON | JSON expansion until verified |
| `items` | `progression_items` | composite-key seek |
| `blocked` | last recorded authority | no automatic cutover |

After cutover, an operator may enable `compat_json` read mode for one progression or globally.
That adapter returns the legacy list-shaped API from:

```text
JSON collection[0:frozen_count]
  + progression_items WHERE ordinal >= frozen_count ORDER BY ordinal
```

This is the rollback switch for reader implementation or query-plan regressions. It uses the
frozen JSON as the historical prefix and only the post-cutover relational tail. It does not claim
that the raw JSON column is current.

**Exact semantics.**

- Before cutover, `dual` can return to `json` immediately because both writes were atomic and JSON
  remains current. Shadow rows may be retained for diagnosis or deleted later.
- After the first items-only append, writes never return automatically to JSON. Doing so would
  restore the O(n) hot-path rewrite this ADR removes.
- `compat_json` may replace normalized prefix reads when item-prefix corruption or a query-plan
  regression is suspected. It still needs relational tail rows written after cutover. If those
  rows are untrustworthy, no complete second source exists; the progression becomes `blocked` and
  reads fail closed rather than return stale history.
- The fallback must compare the frozen prefix length and first tail ordinal. A gap or overlap is an
  error, not something to sort away.
- Public callers continue to receive `list[str]`. The source selection and ordinals remain a
  persistence detail unless they call the bounded item API.

**Why this way.** An instant switch to the raw JSON column after items-only writes would silently
lose the tail. Continuing to rewrite JSON forever would preserve that switch but preserve the
performance defect too. A frozen prefix plus normalized tail is the only immediate compatibility
path that is complete, avoids reverse-synthesizing millions of rows, and removes full-history work
from append.

### D5 — One maintenance service owns bounded migration and evidence

Migration is driven by an idempotent state service that can run from the daemon's maintenance lane
or an explicit CLI command. Request handlers never opportunistically backfill.

**The contract.** The service surface is:

```python
class ProgressionMigrationService:
    async def step(
        self,
        *,
        progression_id: str | None = None,
        max_items: int = 500,
        max_bytes: int = 4 * 1024 * 1024,
        max_seconds: float = 1.0,
    ) -> MigrationStep: ...

    async def verify(self, progression_id: str) -> VerificationResult: ...
    async def set_compat_read(self, progression_id: str, enabled: bool) -> None: ...
    async def status(self, *, limit: int = 100) -> MigrationStatus: ...
```

The CLI exposes the same owner rather than implementing SQL separately:

```text
li state progression-migrate status
li state progression-migrate step [--progression ID]
li state progression-migrate verify PROGRESSION_ID
li state progression-migrate compat-read PROGRESSION_ID --enable|--disable
```

**Exact semantics.**

- Automatic maintenance is disabled until schema creation, dual-write tests, and rollback reads
  have shipped. Enabling it is an explicit release/configuration step.
- One `step()` respects all three caps. Hitting a cap commits completed chunks and yields; it is not
  an error.
- Status reports counts by phase, remaining prefix items, oldest update time, blocked reason-code
  counts, and the hottest still-JSON progressions by observed append count. It does not expose
  message identifiers.
- Error fields store bounded codes and sanitized context, never message content.
- Two maintenance processes may race. Optimistic cursor checks and primary keys make one winner;
  the loser rereads state and continues without duplicating rows.
- The service records duration, rows, bytes, and parity outcome for each step so rollout can be
  paused on lock latency, WAL growth, or mismatch.

**Why this way.** Hiding migration inside a read makes latency unpredictable and gives every UI
request authority to mutate storage. A separate idempotent service provides backpressure and an
operator-visible stop button while reusing the same code from daemon and CLI.

### D6 — Cutover is gated by plans, parity, and scale behavior

Implementation is not complete when the new tables exist. It is complete only when the hot path
and the migration satisfy deterministic gates on both supported backends.

**The contract.** Required gates are:

- ordered reconstruction, missing-row, empty-row, legacy duplicate, duplicate append,
  first-position, wholesale replace, and pruned-message-reference parity on SQLite and PostgreSQL;
- crash injection after item insert, after cursor movement, during dual append, and immediately
  before and after cutover;
- concurrent append tests proving one position per new message and unchanged first position on a
  duplicate retry;
- `EXPLAIN` assertions that post-cutover tail and count use the
  `(progression_id, ordinal)` primary-key range and do not invoke `json_each` or
  `jsonb_array_elements_text`;
- a warmed synthetic benchmark at 1,000 and 100,000 existing items, at least 100 measured appends
  per size, where 100,000-item append p95 is no more than twice 1,000-item p95 on the same host;
- a restart test after every backfill chunk boundary, producing identical final count and ordered
  digest;
- a compatibility-read test with post-cutover tail appends, proving the frozen JSON prefix plus
  normalized tail exactly equals the authoritative item read.

Performance numbers are regression gates, not cross-machine promises. CI may run a structural
statement-count and query-plan gate while a documented benchmark job owns the p95 ratio.

**Why this way.** Unit parity alone can ship the same O(n) statement behind a new helper. Query
plans prove bounded reads structurally, while the size-ratio benchmark catches accidental
full-history work in allocation, duplicate detection, or serialization.

## Consequences

- A normal append after cutover writes one small membership row and one allocator update rather
  than scanning and rewriting the progression's lifetime.
- Tail and count consumers can pay for the requested window. `get_progression()` intentionally
  remains a full-history API and can still allocate O(n) memory when a caller asks for all ids.
- The representation adds rows and indexes. A progression with 100,000 identifiers no longer hides
  them in one value; backup and page-cache behavior changes and must be measured during rollout.
- PostgreSQL gains per-progression concurrency instead of full-value update contention. SQLite
  remains one-writer-at-a-time, but each append holds that writer for bounded work.
- Migration has more states than a one-shot schema bump. Contributors must preserve the phase
  authority rules and may not add a third write path in a request handler.
- Existing malformed or duplicate-bearing JSON remains readable. Malformed data blocks automatic
  cutover rather than being silently repaired.
- JSON is retained as a frozen compatibility prefix in the first implementation. Removing it is a
  later ADR amendment with backup, downgrade, and support-window evidence.
- Reversing D1 after cutover requires an explicit representation migration. Reversing D2 changes
  retry and ordering behavior. Reversing D3/D4 without a migration can lose live tails. D5 and D6
  can change tooling and thresholds, but not phase authority or parity guarantees.

## Alternatives considered

### Keep JSON and add faster SQL expressions

Backend-specific JSON indexes can accelerate some membership predicates, but append still creates
a new full array value. PostgreSQL native arrays or `jsonb` would also diverge from SQLite and keep
full-value write amplification. This loses on the measured lifetime curve.

### Rewrite JSON periodically in larger batches

Buffering identifiers and appending them in batches reduces the number of rewrites but introduces
a durability window or another journal, while tail reads still expand the full value. Once a
durable ordered journal exists, it is already the normalized representation under another name.

### Dual-write JSON forever for instant raw-column rollback

This makes rollback simple and leaves the hot path quadratic. It directly fails P1 and D6. The
frozen-prefix compatibility adapter preserves complete reads without pretending the raw column is
current.

### One global migration flag and transaction

This is simpler to reason about on an empty test database. On a multi-gigabyte live store it creates
one writer exclusion window, one WAL spike, and one bad-row failure domain. Per-progression state
costs another table but permits bounded, resumable work.

### Unique `(progression_id, message_id)` membership

A unique constraint would make duplicate admission concise. It cannot preserve a legacy array that
already contains a repeated identifier, even though current create and wholesale-set paths can
store one. The allocator lock plus a non-unique lookup index preserves old bytes and prevents new
append duplicates.

### Foreign-key membership to messages

This would give referential cleanup and make orphan detection easy. It would also delete or reject
memberships whose messages retention intentionally pruned, changing ADR-0055's contract. Message
retention and progression ordering remain separate.

### Linked-list or fractional-rank ordering

Linked rows make append cheap but make reverse pages, integrity checking, and recovery depend on
pointer chains. Fractional ranks help insertion between existing items, which progression append
does not need, and require rebalancing. Monotonic ordinals fit the append-only order contract.

### A global event log as the source of progression order

An event log could serve more than progressions and enable replay. It would introduce global
sequence, compaction, and projection-lag contracts far beyond the measured defect. This ADR keeps
the authority progression-scoped.

## Notes

- ADR-0055 D3 remains binding until this record is accepted. The first dependent implementation PR
  is the acceptance trigger under the ADR corpus lifecycle.
- Issue #3104 carries the motivating measurements and implementation tracker.
- The word “rollback” here distinguishes reader rollback from representation reversal. Before
  cutover, writes can return to JSON. After cutover, only the complete compatibility read can be
  switched immediately; restoring JSON writes would require a new migration decision.
