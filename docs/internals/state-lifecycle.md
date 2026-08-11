# StateDB lifecycle transitions

`StateDB.update_status()` and the transition-policy layer around it (`PolicyRegistry`,
`SQLAlchemyLifecycleService`) enforce how sessions, invocations, plays, schedule runs and
other entities move between statuses. This document collects the regression-shaped
knowledge that the lifecycle test suite pins, so it lives somewhere other than scattered
module docstrings.

## Two different failure contracts, easy to confuse

`update_status()` has two failure modes that look similar but are not:

- A **CAS miss** — a stale `expected_statuses` or `expected_updated_at` guard — returns
  `False` silently. No row changes, no exception. This is an ordinary lost race: someone
  else updated the row first.
- Attempting to move a **terminal** entity to a different status without `override=True`
  raises `TransitionRejectedError` instead. This is a policy violation, not a race.

A caller, or a refactor, that treats one of these as the other is a live bug. The two
contracts also interact: a terminal row with a stale version guard is still rejected and
audited, because terminal-exit policy is evaluated before the write-only version conflict
is even reached.

## One vocabulary, several sources of truth

Valid statuses per entity type are declared in three places that must stay in sync: a
schema `CHECK` constraint, the `PolicyRegistry`, and the `VALID_STATUSES_BY_ENTITY_TYPE`
facade in `lionagi/state/db.py`. A status added to one without the others is a silent
widening or narrowing of what `update_status()` accepts. The lifecycle gate tests pin an
exact, sorted status list per entity so any addition shows up as a deliberate diff against
that list, not a change that only a end-to-end scenario would eventually surface.

## PolicyRegistry only guards the public entry point

The transition-policy edge graph in `PolicyRegistry` is enforced by
`SQLAlchemyLifecycleService.transition()`, the public entry point. `StateDB.update_status()`
itself does **not** enforce it — see `enforce_edges` in
`lionagi/state/lifecycle/adapters.py`. Every declared edge for session, invocation, play,
and schedule_run is expected to apply; sampled undeclared edges must come back as a
`"rejected"` outcome with an `admin_events` audit row, never a raise and never a silent
write. Code that calls `StateDB.update_status()` directly bypasses policy enforcement by
construction — that is a property of the two-layer design, not an oversight to fix in the
lower layer.

## The reaper guard pattern

Reapers throughout `lionagi/studio/services/lifecycle.py` guard a stale-row transition on
**both** `expected_statuses` and `expected_updated_at`. Dropping the `expected_updated_at`
half is easy to miss: a change that drops it still passes every status-only test, because
status membership alone cannot distinguish "still stale" from "just re-touched between the
scan and the write." A row that is genuinely stale must be reaped; a row whose
`updated_at` moved between the scan and the write must not be, even though its status still
matches. Both outcomes need to be asserted in the same test, against the two-guard shape
used in production, or a weakened reaper would silently pass.

## SQLite vs PostgreSQL concurrency

`StateDB._tx()` is the sole write choke point. On SQLite it holds a process write lock, so
several classes of race that are structurally possible on PostgreSQL cannot happen there at
all:

- **Natural-key upsert race** (`insert_artifact`): the lookup used to run as a separate
  autocommit `SELECT` before the write transaction opened, so two processes racing the same
  natural key could both see no existing row and both `INSERT`, with the loser hitting a
  partial unique index as an `IntegrityError` instead of the documented upsert-with-latest-
  wins behavior. The fix moved the lookup inside the same atomic statement as the write.
- **Session-control admission vs. a terminalizing transaction**: a plain status `UPDATE`
  takes `FOR NO KEY UPDATE` on the session row. That lock mode matters specifically because
  it does not conflict with the `FOR KEY SHARE` a control insert's foreign key already
  takes — so an admission that only reads the session would sail past a `FOR UPDATE` holder
  and pass even with the ordering bug present. Only `FOR NO KEY UPDATE` discriminates.
- **Compare-and-set resolution racing its own claimant**: `resolve_claimed_session_control`
  reads a claim, decides from it, then writes under a CAS. On PostgreSQL the claimant can
  commit its own outcome between those two statements; the CAS correctly refuses, but the
  caller must not get a receipt for a write that never happened.
- **Delete vs. an open writer** (`delete_imported_session`): at READ COMMITTED, a delete's
  retention check reads a snapshot, so a reference committed after that check is invisible
  to it while the delete it authorized still proceeds — destroying a message a survivor is
  by then pointing at. The fix takes a table lock as the transaction's first statement,
  which works because ordinary `INSERT`/`UPDATE` already hold `ROW EXCLUSIVE`, and that
  conflicts with `EXCLUSIVE`.
- **Teardown vs. maintenance deadlock**: teardown and `prune_old_data` reach `sessions` and
  `progressions` in opposite orders — exactly the shape a lock cycle needs. A blocking
  `LOCK TABLE` would hold two tables while waiting on a third already held by a maintenance
  pass, and PostgreSQL's deadlock detector would abort one side with `40P01` — a whole pass
  lost, not a slow one. `NOWAIT` avoids waiting during acquisition; a bounded
  `lock_timeout` on the later FK-nulling step (which is not covered by `NOWAIT`) makes the
  teardown give up on its own, inside PostgreSQL's `deadlock_timeout`, so it returns the
  retryable `55P03` instead of ever reaching `40P01`.

The general lesson: any place `StateDB` takes a lock and touches more than one table on
PostgreSQL is a deadlock candidate against another writer that touches the same tables in a
different order, and the fix is either strict lock ordering or a bounded wait — never a
blocking wait held across multiple tables.
