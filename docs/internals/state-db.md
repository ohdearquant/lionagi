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

Three kinds of schema change show up in the code:

- **In-place table rebuilds**, used when SQLite's lack of `ALTER TABLE ...
  DROP CONSTRAINT` means the only way to drop a stale `CHECK` constraint is
  to create a new table with the right constraint, copy every row across,
  drop the old table, and rename the new one into place. Because a legacy
  install might have already been rebuilt in a previous release, each rebuild
  first checks (by inspecting the live `CREATE TABLE` SQL for a marker
  substring) whether the constraint it exists to remove is even still there,
  and no-ops if not.
- **Constraint replacements**, the PostgreSQL counterpart to the rebuild
  above, listed in `MIGRATION_CONSTRAINTS` and applied by
  `_reconcile_constraints`. PostgreSQL can drop and re-add a `CHECK` in
  place, so no copy is needed, but it needs the step for the same reason:
  `metadata.create_all` only creates missing tables, so a store that already
  had the table keeps whatever constraint it was created with, and a value
  added to the declared vocabulary afterwards is rejected by exactly the
  store that has been running longest. The same marker reading applies —
  each statement looks for the newest value in the live definition and does
  nothing if it is already there, so it does not take the table's lock on
  every open, and a constraint that is absent entirely is left alone,
  because a column with no `CHECK` already accepts every value.
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

### Backfills, one at a time

The generic guard above says a backfill runs once. What each one decides is
worth reading separately, because in every case the interesting choice is what
value an old row should be given when the honest answer is "unknown".

`_backfill_dispatched_at` stamps `dispatched_at` on `schedule_runs` rows that
were already `running` before the column existed. Without it such a row is
indistinguishable from a launch that was never confirmed, and
`list_undispatched_schedule_runs()` would re-fire it on the next daemon startup
even though it is still genuinely executing. The stamp uses the row's own
`fired_at`, which the schema guarantees is `NOT NULL`, purely to exclude it from
that scan. That is the same "no signal, so do not auto-retry" resolution
`_backfill_action_cwd()` takes, and it does not pretend to know when the launch
actually happened. A row that really did crash before the migration is still
caught later by `reap_stale_schedule_runs()`'s wall-clock deadline, so nothing
is lost by declining to guess here. The backfill is scoped to
`schedule_id IS NOT NULL` so the ad-hoc task queue, which has its own
dispatch and lease model and never sets `dispatched_at`, is left untouched.

`_backfill_attention_dispositions` fills in the columns that fencing and
ordering added after `attention_dispositions` and
`attention_disposition_history` already existed. Because `metadata.create_all()`
only creates missing tables, a store that already had them gained `revision`,
`sequence` and `attention_disposition_revisions` as inert `DEFAULT`
placeholders rather than real values. Three assignments follow from that.
`sequence` is assigned in `(created_at, id)` order, so an `ORDER BY sequence`
read sees the original append order. `revision` is raised to the item_id's count
of history rows, or to 1 where an active row has none, so a client that already
echoed a revision back never has its value read as a rollback. A pre-upgrade
row with no history therefore starts at 1 rather than 0.
`attention_disposition_revisions` is seeded at that same value for every active
item_id, and also for item_ids that exist
only in history with a delete-shaped latest transition. That last case is the
one worth stating: without it, a PUT written before the upgrade and replayed
after it would recreate a disposition that had been deleted, instead of being
rejected by the fence. This backfill must run before `_reconcile_indexes`,
which recreates the unique index on `attention_disposition_history.sequence`
and would fail against the `DEFAULT 0` placeholder every pre-existing row
shares.

`_backfill_imported_role_label` nulls out `agent_name` on sessions imported
from a desktop transcript. The mirror used to write the engine name into that
field, which is a role field, and stopping the write only fixes imports going
forward. Backfilling the stored rows avoids a permanent split where old
imports render the engine name and new ones render the prompt, since
`resolve_display_name` checks role before prompt. It is scoped by
`source_kind` rather than by the label's value, because a live session can
legitimately run a role literally named "codex". Branch rows are reached
through their session, as `branches` has no `source_kind` of its own.

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

Two writes assign a value inside the statement that writes it, for the same
reason. `insert_session_signal` appends one lifecycle signal and returns the
`seq` it assigned; `seq` is `MAX(seq)+1` for the session, computed in the same
write, so concurrent inserts from different processes under WAL do not collide.
Coroutines sharing one `StateDB` are serialized through the instance write lock
so no two enter `BEGIN IMMEDIATE` on the same async engine, and PostgreSQL takes
an advisory transaction lock keyed on `session_id`.

`insert_artifact` upserts one structured artifact and returns its stable id,
with the natural-key lookup and the write in a single statement. A separate
`SELECT`-then-`INSERT` let two concurrent callers both observe no existing row
and both attempt an insert, and the loser hit one of the four partial unique
indexes as an `IntegrityError` instead of the documented upsert. The
`ON CONFLICT` target and its `WHERE` clause must match one of
`idx_artifacts_natural_key_*` in `schema.sql` exactly, since both SQLite and
PostgreSQL require the conflict target to name the specific partial index;
which one applies follows from which of `invocation_id` and `session_id` is set.

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

`resolve_claimed_session_control` is the one thing that can end a claimed row,
and it is deliberately not automatic. Nothing in the system can tell a consumer
that died before delivering a message from one that died after, so the row
waits for someone who can find out, and this method is that person's write. It
returns None when the row is not claimed, which covers both "already terminal"
and "never taken", so a caller cannot use it to overwrite an outcome the
consumer itself recorded or to skip a row the ordinary teardown sweep should be
rejecting instead. The claim it replaces is kept verbatim in the stored result,
because the record of who held a message and what a human then decided about it
is the whole value of leaving the row standing.

## Status transitions and the terminal-status floor

`get_sessions_for_run` returns every session recorded against a CLI run,
oldest first, as a list rather than one row: one run can persist more than one
session, and a caller deciding whether the run is over has to see all of them.
An empty list means no session was ever recorded under that run id, which is
not the same answer as a session that exists and is not finished.

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

Two shapes the merge deliberately does not destroy. A malformed or non-object
existing value, meaning an array, a scalar, or on SQLite even non-JSON text, is
not silently discarded: it is preserved verbatim under
`_discarded_node_metadata` and `_discarded_at` in the merged result, so the
previous state is recoverable rather than gone. PostgreSQL's native `json`
column can never hold non-JSON text, since the driver rejects it on write, so
that half of the guard is SQLite-only in practice while the array-or-scalar
half applies to both. Separately, a JSON `null` stored as the *whole*
`node_metadata` value is treated the same as SQL NULL, as an absent object to
merge into rather than a foreign shape to preserve. That is not a rare case:
SQLAlchemy's JSON bind type serializes a Python `None` passed as
`node_metadata` to the JSON null literal rather than to an actual SQL NULL, so
it is the column's value on a large share of rows created without that field.

A null already present inside the stored document, at the top level or nested
in a stored object or array, is data rather than noise, and the merge never
strips it. Only keys the patch itself sets to null are removed.

## Schedules: what counts as a fire, and what recovers one

A `schedule_runs` row is an occurrence: the record that a schedule's cursor
moved. Whether anything actually *ran* is a separate question, and several
methods exist only to keep the two apart.

`count_schedule_runs` answers "how much of its budget has this schedule
spent", for `max_runs` and one-shot auto-disable. It counts `chain_depth = 0`
rows only, since `on_success`/`on_fail` chain children do not consume the
parent's budget, and its default status set excludes `skipped` (a missed-fire
or overlap skip never ran) and `running` (not yet terminal). `timed_out` does
count: a reaped run fired and consumed real work, so a bounded schedule must
not silently re-fire because its only run timed out instead of completing.

`schedule_health_evidence` reads two independent top-1-per-schedule rows, and
each one filters to the rows that qualify *before* ranking them. Ranking an
unfiltered window and filtering inside it, which is what it used to do, can
push a real execution out of the window once enough non-qualifying rows pile
up in front of it, which manufactures "never happened" out of "did not fit in
the slice". A *recorded* row is any top-level row in any status, proving
only that an admission decision reached the table. It does not prove the cursor
moved: a capacity deferral records a `skipped` row and deliberately leaves
`next_fire_at` untouched, so the occurrence is still due. An *executed* row is
further restricted to `EXECUTED_RUN_STATUSES`, because `skipped`, `queued`,
`waiting_dependency` and `retry_wait` are all recorded without a run happening.

`schedule_run_exists_since` distinguishes "never fired" from "fired but
crashed before follow-up bookkeeping" for missed-fire recovery. It excludes
`skipped` rows, so a capacity-deferred skip (whose `next_fire_at` is
deliberately left untouched) still counts as due and retries rather than
reading as handled, and it excludes chain children, so a chain fire cannot
mask a due top-level occurrence.

Two recovery scans sit either side of the launch. `list_undispatched_schedule_runs`
returns rows whose transaction committed but whose external process launch was
never confirmed: the cursor has already moved, so ordinary missed-fire recovery
will never reconsider them, so the scheduler revisits them at startup. Not all of
them are re-fired. A chain child is tombstoned rather than retried, because a
chain is re-entered from its root and not from the middle, and so is a run whose
owning schedule has since been deleted or disabled, because re-firing it would
run work the operator has already withdrawn. What is left, a top-level run of a
still-enabled schedule, is re-fired under a fresh run id.
`list_dispatched_running_schedule_runs` returns rows that were confirmed
dispatched and never reached a terminal status. The scheduler that dispatched
one of those may have crashed before recording its outcome, or the process may
still be genuinely alive and working, so this method surfaces candidates for
reconciliation and does not itself decide liveness.

`tombstone_and_replace_schedule_run` flips an undispatched orphan to a terminal
status and inserts its replacement in one transaction, so a crash leaves either
both writes durable or neither. Its compare-and-set also requires
`dispatched_at IS NULL`: if a launch confirmation lands between the recovery
scan and this write, the row no longer qualifies and the call is a no-op rather
than tombstoning a run that actually launched.

`create_schedule_run_and_advance` inserts the occurrence and advances the
owning schedule's cursor together, so a crash can only discard an occurrence
that was never durably recorded. It can never leave the cursor pointing before
one that was, which would make a restart re-fire it.

The cursor advance also carries `expect_next_fire_at`, and it runs first: the
occurrence is written only if the schedule still holds the cursor the caller
selected on, and a caller that lost it gets `False` having written nothing.
Selecting a due schedule and firing it are separate statements, and every
admission gate above this one lives in the firing process's own memory, so
without this predicate two schedulers reading one due row both commit, each
with its own run id and each launching a child. The predicate is NULL-safe
because a schedule with no cursor is a real state, and it is required rather
than defaulted so that a new caller has to decide what it is claiming instead
of silently claiming nothing. It bounds the race it names and no more: if the
update carries no new `next_fire_at`, the cursor does not move and the next
caller holds the same claim.

The predicate is spelled as a branch on the Python value, `IS NULL` for a claim
of nothing and `= :param` otherwise, rather than as a single NULL-safe operator.
`IS` accepts a bound parameter in sqlite and is a syntax error in postgres, and
their common spelling, `IS NOT DISTINCT FROM`, is newer than the sqlite versions
this project still runs against. The dual-backend tests skip without `asyncpg`
installed, so the shape of the generated statement is asserted directly.

Not every fire claims a due instant. A chain child runs because its parent
finished, a manual trigger runs because a person asked, and a startup re-fire
replaces an occurrence whose cursor already moved; those pass `NO_CURSOR_CLAIM`
and are guarded by their own mechanisms, the CAS-tombstone of the orphan row in
the re-fire's case. A `github_poll` batch is one due instant however many events
it carries: the first dispatched event claims it, and the rest are separated by a
claim on `github_cursor` instead. That second claim is not a convenience. Every
event of one batch resolves to the same `next_fire_at`, so claiming that value
would either refuse every event after the first or, since the value does not
change between them, match twice and separate nothing. `github_cursor` advances
per event, in the same transaction as that event's occurrence, and is the only
value in the row that tells one event of a batch from the next. Without the claim
the column is written but never read, which leaves a window: a second scheduler
polling after this one commits event 1 reads the advanced cursor, starts its poll
at event 2, and dispatches it while this one has not reached it yet. The claim
follows what was written, not how far the poll has read, because a filtered-out
event moves the read position without writing anything. Missed-fire recovery reserves the cursor itself before
dispatching, so its reserve is where it claims the instant, and the fire that
follows claims the value the reserve wrote rather than the pre-reserve value its
local snapshot still holds.

`_build_update_schedule_stmt` is the single choke point for both the field
allowlist and the SQL shape, shared by `update_schedule` and by the folded-in
update inside `create_schedule_run_and_advance`, so the two write paths cannot
drift. Its `guard_cursor_forward` option makes the `github_cursor` assignment
monotonic: the column moves only if the new value sorts above the stored one.
The scheduler passes it because a poll reads the cursor at tick start and
writes it back much later, so its value is a snapshot that an operator's
deliberate cursor move can outrun; without the guard the stale write silently
undoes that move and a backlog the operator had declined becomes eligible
again. It is a per-column condition rather than a row predicate on purpose,
since the same statement carries `last_fired_at` and `next_fire_at` and those
must land whether or not the cursor is allowed to advance.

## Spend and metrics: unreported is not zero

`total_cost_usd` is NULL when the engine that ran a session does not price
itself, not when the session was free. Every read that aggregates it has to
decide what to do about that, and the answer differs by what the caller does
with the number. A panel can render "unreported" and let a human judge it, so
the panel reads never coerce. A budget gate and a threshold alarm have to
compare against a limit, so they take `COALESCE(..., 0)` and carry the gap in a
companion count instead. What no read does is coerce silently: wherever a zero
could mean "unmeasured", the unmeasured rows are counted too, in the same row
for the gate and by a companion method for the metric.

`spend_stats` leaves `reported_usd` as None whenever no row in the window
reported a cost, so an entirely unreported window does not read as a genuine
$0. It anchors on the same `COALESCE(ended_at, started_at, created_at)`
timestamp as `activity_stats`, so the spend panel and the activity panel
describe the same population for the same window. `spend_rollup` carries the
same window anchor and the same never-coerce rule at session grain, one row
per distinct dimension value, with unreported-only groups sorted last. Both
express this as `SUM(CASE WHEN total_cost_usd IS NOT NULL THEN total_cost_usd
END)`, which is NULL rather than 0 for a window nothing reported in.

`sum_schedule_spend` backs the pre-fire budget gate, so it does sum with
`COALESCE(..., 0)`, joining `schedule_runs` to sessions through
`invocation_id`, and exposes
`unreported_sessions` beside the total. That count covers terminal sessions
only: a still-running session's cost is expected to be unknown and is not a
gap. `metric_unreported_sessions` is the same companion for the
`total_cost_usd` threshold metric, and it is the only metric with a
NULL-versus-reported distinction to expose.

`metric_value` aggregates a threshold-alert metric from `window_start` onward.
The predicate is a lower bound only, so a row timestamped in the future is
included rather than held back until its time arrives. Three of its members do
not fit even that shape. `p95_latency_ms` needs a sorted
sample, which SQLite has no percentile function for, so it fetches raw
invocation durations and computes the percentile in Python.
`github_poll_healthy_age_minutes` is a point-in-time gauge rather than a
windowed aggregate, so it accepts `window_start` for signature parity and
ignores it, reading "now" fresh inside the method.
`github_poll_consecutive_401` is point-in-time for the same reason: it reports
the longest consecutive-401 streak across enabled `github_poll` schedules, so a
payload can tell a token problem from a network blip. A streak is a property of
now rather than of a window, so this one leaves `:window_start` unused. The invariant to
preserve
is that every member of the studio's `VALID_METRICS` is answered somewhere in
`metric_value`, not that it is answered by the shared aggregate query.

The `failed_sessions` metric counts distinct *causes* rather than rows, and
the reason is worth keeping. A fan-out spawns one session per worker, so a
single wall, such as a provider refusing every worker of one invocation, lands
as many rows carrying one cause, and a fan-out wider than the threshold would
breach it on that single cause by construction. Grouping by
`(invocation, reason)` makes the observed value the number of distinct things
that went wrong. Both columns fall back to the session id rather than grouping
on NULL, and that is the whole correctness of the query: NULL is not a shared
value, rows without an invocation are rows whose grouping is unknown, and
letting SQL treat them as equal merges unrelated failures into one. Most
failed sessions carry no invocation id, so the naive form collapses nearly the
whole population to one row per reason and the alarm stops being able to fire.
The fallback is namespace-tagged rather than bare, because a bare one puts two
different namespaces in one column: a session with no invocation whose id
happened to equal some other session's `invocation_id` would share a grouping
key with it, and two distinct causes carrying the same reason would then count
once. That is the direction that suppresses an alert, so the prefix is worth
spending.

## Writes that fail closed

`update_session` takes a `set_if_null` set naming fields to write as
`COALESCE(col, :col)` rather than as a plain assignment. The write lands only
while the column is still NULL, so two callers racing to stamp the same field
converge on whichever value committed first instead of the later one silently
winning. It is a single atomic UPDATE rather than a read-then-write, so the
guarantee holds under concurrency and not merely under interleaving that
happens to be lucky.

`set_session_provenance` writes attribution fields without touching
`updated_at`. Where a session came from is not evidence that it is live, so
these writes must not move the clock `reconcile_session_status` and the phantom
reaper read. `project` and `project_source` are written together, since the
source is meaningless alone, and the session update and the projects-registry
upsert run as one locked write so neither commits without the other.
`artifacts_path` is written with `COALESCE` rather than a plain assignment: a
mirrored CLI session's artifact root is a weaker signal than a launcher-set one,
so a later and more precise write is never clobbered by an earlier guess.

`delete_imported_session` refuses on ownership twice. The row's `source_kind`
must equal the caller's `require_source_kind` exactly, and that value must
itself start with `imported_`. The first check stops one importer reaching
another importer's rows; the second stops any caller reaching a live run's
session at all, since a live session's `source_kind` never carries that prefix.
A progression or message still referenced by a surviving session or branch is
retained rather than deleted.

`read_only_open_supported` answers a narrower question than its name suggests:
whether the configured store resolves to a SQLite file path at all. It is
exactly `state_db_file() is not None`, so it does not check that the file
exists, and it returns True for a path that is absent while
`StateDB(readonly=True)` raises `FileNotFoundError` there — a read-only open
never creates the file. Read-only is SQLite-only: it is a `mode=ro` URI open,
and the read-only branch of `StateDB.open()` is not dialect-gated, so an
unconditional `readonly=True` fails at open on a server-backed store rather
than degrading to a writable connection. Callers use this as a cheap
pre-filter and still handle the open failing. It is not a safety check: it says
nothing about whether the connection you get back can write.

`state_db_known_absent` answers the neighbouring question: whether the store a
default `StateDB()` would open is known absent, which separates "no store, so no
record of anything" from "the store is there and reading it went wrong". Callers
act differently on each. It checks the store that will actually be opened,
honouring `LIONAGI_STATE_DB_URL` rather than the default path, and only a True
is confident: where existence is not a filesystem question it returns False and
leaves the open attempt to give the real answer.

## Where the reasoning lives

Docstrings in `db.py` are one line: what the method does, and the single
condition a caller most needs to know. Everything else, meaning the dialect
differences, the concurrency arguments, the migration order and the reasons a
value is computed one way rather than another, lives in this document. A
comment survives at a call site only when it explains a line that would
otherwise read as arbitrary, and it is kept to a sentence or two.

That split is deliberate. Reasoning kept beside the code is read once, by
whoever is already editing that method; reasoning kept here is read by anyone
asking how the store behaves. When a change makes a paragraph here wrong, the
paragraph is what to fix, not a docstring somewhere in a seven-thousand-line
module.
