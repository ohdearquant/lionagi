# How StateDB persists and protects session state

`lionagi/state/db.py` is the async SQLAlchemy layer behind every session,
branch, message, schedule, and artifact that lionagi records. It runs against
either SQLite (the default local store) or PostgreSQL (a shared server store),
and most of its complexity exists to make those two backends behave the same
way under concurrent writers, even though SQLite serializes writes for free
and PostgreSQL does not.

## Schema shape and how it evolves

The schema is defined once, in `schema_meta.py`, as SQLAlchemy `Table`
objects; `db.py` never hand-writes DDL that could drift from that source of
truth (`schema.sql`/`schema_meta.py` parity is test-enforced). `SCHEMA_VERSION`
records the shape this code understands. A writable `StateDB.open()` rewrites
an older on-disk database into the current shape and stamps the new version;
if the on-disk version is *higher* than `SCHEMA_VERSION`, open refuses rather
than guessing what a later release's shape means (`SchemaTooNewError`).
Read-only opens apply no schema migration at all and are unaffected by this
check.

Two kinds of schema change show up in the code:

- **In-place table rebuilds**, used when SQLite's lack of `ALTER TABLE ...
  DROP CONSTRAINT` means the only way to drop a stale `CHECK` constraint is
  to create a new table with the right constraint, copy every row across,
  drop the old table, and rename the new one into place. Because a legacy
  install might have already been rebuilt in a previous release, each rebuild
  first checks (by inspecting the live `CREATE TABLE` SQL for a marker
  substring) whether the constraint it exists to remove is even still there,
  and no-ops if not.
- **Backfills**, used when a later release adds a column to a table that
  already existed, and old rows need real values instead of the `DEFAULT`
  placeholder `ALTER TABLE` gave them. Every backfill is guarded by a durable
  claim row in `schema_meta` (`INSERT ... ON CONFLICT (key) DO NOTHING`), so
  it runs exactly once even if an earlier release already added the column
  without running the corresponding update.

### Historical session end times are approximate, not measured

Every live transition from a nonterminal session status to a terminal one
persists `ended_at` in the same transaction as `status`; when `started_at` is
known it also persists the measured `duration_ms`. Older databases can contain
terminal rows from before that invariant. Schema version 4 repairs those rows
in batches of at most 500 per transaction, choosing the latest available value
among `updated_at`, `last_message_at`, `started_at`, and `created_at` as an
explicit approximation. It sets `ended_at_is_approximate = 1` and deliberately
leaves `duration_ms` null: the evidence proves the run was no longer active by
roughly that time, not its exact wall-clock duration.

The batch completion marker is written only after an empty probe. If an open is
interrupted, repaired rows remain excluded by `ended_at IS NULL` and the next
writable open resumes the remaining batches. Rows with `status IS NULL` are not
eligible: that state means a terminal status itself was never recorded and is
owned by the stale-session reaper. Filesystem imports apply the same provenance
rule prospectively: a manifest-provided end is measured, while an `st_mtime`
fallback is marked approximate. Consumers such as Operator expose an
approximate end but report duration as unknown rather than deriving a number or
letting a terminal row's clock grow against the current time.

### The SQLite rebuild hazard: PRAGMA foreign_keys inside a transaction

Rebuilding a table that other tables have a foreign key into (`schedules`,
referenced by `schedule_runs.schedule_id ON DELETE CASCADE`) is dangerous:
dropping the old table while `PRAGMA foreign_keys` is enforced cascades away
every referencing row, even ones already safely copied into the new table.
The fix looks obvious — turn the pragma off before the rebuild — but SQLite
treats `PRAGMA foreign_keys` as a no-op while a transaction is open, and
`engine.begin()` opens its transaction before the first statement runs. So
toggling the pragma through an ordinary SQLAlchemy connection silently does
nothing (this was verified: it cascade-deleted `schedule_runs` rows in this
exact rebuild before the fix landed). The correct sequence goes through the
raw driver connection instead, so the pragma flip is real autocommit rather
than swallowed by a pending transaction, and stays *outside* any transaction
entirely — SQLite only honors the pragma between transactions.

That in turn means the CREATE/copy/DROP/RENAME/index sequence that follows
needs its own explicit transaction. Running those steps as independent
autocommit statements is not safe either: a failure between DROP and RENAME
(cancellation, I/O error, a bad index statement) would leave only
`schedules_new` on disk, and the next open's `metadata.create_all` would then
create a fresh *empty* `schedules` table, stranding every original row.
`BEGIN IMMEDIATE` wraps the sequence to restore the atomicity the old
`engine.begin()` path had, without reintroducing the pragma-inside-transaction
bug. After any rebuild, `_restore_foreign_keys()` turns enforcement back on:
it runs from every rebuild's `finally` (including failure paths), closes any
transaction still open first (since the pragma is inert otherwise), reads
`PRAGMA foreign_keys` back rather than assuming the write took, and
invalidates the pooled connection if enforcement can't be confirmed, so a
connection with enforcement silently off is never handed back to the pool.

## Locking model: SQLite serializes for free, PostgreSQL does not

Most of the file's harder invariants exist because SQLite's single writer
lock (`BEGIN IMMEDIATE`) gives free serialization that PostgreSQL's
`READ COMMITTED` isolation does not. Three patterns recur:

1. **Row-level `FOR UPDATE`**, used where one write depends on a value read
   moments earlier from the same row — for example `attach_session_invocation`
   re-pointing a session's `invocation_id`: without locking the prior value
   before reading it, a second concurrent attach on PostgreSQL could read the
   same prior `invocation_id` a first attach is about to move away from, then
   decrement that now-stale value after the first attach already committed.
2. **Admission conditions evaluated inside the write itself**, used where a
   caller-side check-then-write would leave a race window — for example
   `insert_session_control`, which makes "session still running" part of the
   `INSERT ... WHERE EXISTS (...)` rather than a separate `SELECT` first. On
   PostgreSQL, `EXISTS` under `READ COMMITTED` reads a snapshot, so a plain
   form can admit a control against a session another transaction is
   simultaneously terminalizing, and commit after that session's terminal
   sweep already looked — leaving a pending control nobody will ever consume
   (measured directly on PostgreSQL 16). The fix takes a row lock on the
   session as part of the insert's own source query, so a concurrent terminal
   transition waits for the admission to finish rather than racing past it.
3. **Explicit multi-table `LOCK TABLE ... EXCLUSIVE MODE NOWAIT`**, used by
   the two teardown paths that delete across `branches`, `progressions`, and
   `sessions` (`delete_imported_session` and the analogous prune path). A
   transaction alone isn't enough here because the *retention check* — "does
   any survivor still reference this row" — is a read whose snapshot can go
   stale if a concurrent writer commits a new reference right after it. The
   table lock is taken **before the first read**, so nothing written after it
   is missed; it's `NOWAIT` because a comma-separated `LOCK TABLE` acquires
   its targets one at a time rather than atomically, and a *blocking* wait
   there could deadlock against another writer (`prune_old_data`) that
   touches the same tables in the reverse order. A `SET LOCAL lock_timeout =
   '250ms'` additionally bounds every lock-acquisition wait inside the
   transaction, including ones after the table locks are already held (the
   soft-FK nulling that follows touches several more tables whose rows a
   concurrent writer can hold). 250ms is chosen to sit below PostgreSQL's
   default `deadlock_timeout` (1s), so a lock wait gives up before the
   detector would even run, and above the row-lock hold times of ordinary
   writers, so everyday contention resolves instead of aborting a teardown.
   It is *not* a deadline for the whole transaction — it bounds no single
   statement's execution and nothing about commit. Either way the cost falls
   entirely on the rare teardown: a conflicting lock aborts the attempt, and
   both callers log the failure and retry on a later sweep.

## The session-control queue: a worked example

`session_controls` is a small durable queue of verbs (pause, resume, message
delivery, etc.) that a running session's poller drains. Three methods carry
its full lifecycle, and they're worth reading as one sequence:

1. **`insert_session_control`** admits a new control row only if the target
   session is still `running` (see the admission-condition pattern above),
   and returns the new control's id, or `None` if the session had already
   terminalized.
2. **`mark_session_control_applying`** is how a consumer claims a pending row
   before attempting it: a compare-and-set that moves `result` from `NULL` to
   `applying[:<owner>]`, returning the exact claim string it wrote, or `None`
   if another consumer already claimed the row. The claim string has to come
   back to the caller — not be reconstructed later — because
   `finalize_session_control(expect_claim=...)` needs the caller's *own*
   claim to avoid overwriting an outcome someone else recorded while it was
   working. `applied_at` stays `NULL` through this step, so a poller crash
   right after claiming is visible as a stuck row, not silently lost.
3. **`finalize_session_control`** stamps the terminal result. Two mutually
   exclusive guards are available: `expect_claim` (the write lands only if
   the row still carries that exact claim string — used by the message-
   delivery path, where a specific consumer owns the row) or
   `only_if_unclaimed` (the write lands only if the row is still pending —
   used by sweeps, which read a snapshot of pending rows and must not
   overwrite one a consumer claimed and delivered in the meantime). Passing
   both is a caller bug. This is a compare-and-set between cooperating
   consumers, not an authorization boundary: the claim string lives in a
   column every reader can see, so what it prevents is a consumer
   overwriting a row it hasn't re-read — not a consumer that means to write
   it, since anything that can call this method could write the row
   directly anyway.

`list_pending_session_controls` reads the queue back, and distinguishes
"never touched" (`result IS NULL`) from "a consumer is or was mid-apply"
(`result` starts with `applying`) so a status surface or a stuck-claim
detector can tell them apart; `claimed_at` next to it gives the age of a
claim that hasn't resolved.

## Status transitions and the terminal-status floor

`update_status` is the single path every entity's status write goes through.
Two optional guards make it safe under concurrency: `expected_statuses`
performs the write only if the current status is a member of a given set
(pass `None` in the set to match a SQL NULL status); `expected_updated_at`
adds an optimistic-lock version check — the row's `updated_at` must still
equal the value the caller read, and any status write bumps it — which lets a
caller distinguish "the row I read is still current" from "someone already
re-touched it" in cases where status membership alone can't (a reaper racing
a fresh claim on the same reapable status, for instance).

On top of that sits an integrity floor: once an entity's status is terminal
(per `TERMINAL_STATUSES_BY_ENTITY_TYPE`), any write that would *change* it is
rejected and recorded in `admin_events` — a terminal record must never
silently move back to running or oscillate to a different terminal value. A
same-status write is not treated as a transition and passes through
untouched, since callers rely on it to refresh a reason code on an
already-terminal row. The one deliberate escape hatch is
`override=True` with both `override_actor` and `override_justification`
required — an operational repair that does change a terminal value, recorded
in `admin_events` distinctly from an ordinary transition so the two are never
confused when reading the audit trail later.

`finalize_branch` applies the same terminal-status discipline to individual
branch rows: the incoming status must itself be a genuine terminal outcome
(rejecting, for example, the "running" that linked-engine reconciliation can
produce when it suppresses a phantom "failed" back to "running"), and the
existing row must still be in a pre-terminal state (`NULL` or `"running"`) —
any other existing value, whichever terminal status it already holds, is
immutable. A branch row that was never created at all (a DAG leg that never
emitted a first message) simply matches zero rows.

## `node_metadata` merges: read-modify-write without the read

`merge_session_node_metadata` and `merge_invocation_node_metadata` both exist
to close a clobber: the pattern they replaced was a `get_*()` read followed
by an `update_*(node_metadata=...)` write, which let two concurrent callers
each read the same row and overwrite each other's patch. Both now run as a
single dialect-specific `UPDATE`, so the merge is serialized by the database
itself (SQLite's write lock; PostgreSQL's ordinary row-level MVCC locking)
instead of racing in Python. A patch value that is itself a nested dict is
rejected before any SQL runs, because SQLite's `json_patch` merges nested
objects recursively while PostgreSQL's `jsonb ||` replaces them shallowly —
allowing either silently would make the two backends persist different state
from the same call.

On PostgreSQL the merge SQL also has to reproduce RFC 7396's "null in the
patch deletes the key" semantics by hand, because `jsonb ||` keeps an
explicit null instead of deleting it (unlike SQLite's `json_patch`, which
already implements RFC 7396). Rather than stripping *all* nulls from the
merged document — which would also strip nulls that pre-date this patch and
have nothing to do with it — the statement subtracts exactly the set of keys
the incoming patch itself set to null.

## What was deliberately left alone

Some large docstrings in `db.py` remain close to their original length
because they encode invariants a caller genuinely needs and trimming further
risked losing a fact rather than a word — the guiding rule throughout this
pass was "when in doubt, keep the sentence."
