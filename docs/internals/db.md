# `delete_imported_session` — the PostgreSQL table-lock design

`delete_imported_session` (in `lionagi/state/db.py`) tears down a
mirror-imported session and everything the mirror wrote for it. On
PostgreSQL it takes an explicit table lock before its retention checks; this
note preserves the reasoning that used to live inline as a comment on that
lock.

## Why a table lock at all

The retention checks and the deletes they authorise must be serialised
against every writer that could create a new reference, so a reference that
appears after the check cannot be destroyed by the delete. On SQLite the
process write lock already provides this. On PostgreSQL a transaction alone
does not: at READ COMMITTED the check reads a snapshot, and a concurrent
writer could commit a new reference before the delete runs against it. The
table lock closes that window.

The lock is taken before the first read, so everything read afterwards stays
true for the rest of the transaction and no re-check is needed. The three
tables locked (`branches`, `progressions`, `sessions`) are exactly the ones a
survivor's reference can live in: a progression's collection holds message
ids, and sessions and branches point `progression_id` and their message
pointers at rows this teardown would otherwise remove. `EXCLUSIVE` mode
conflicts with the `ROW EXCLUSIVE` that ordinary `INSERT`/`UPDATE` already
take, so the writers need no changes and pay nothing on their own path.

## Two separate guards, because they cover different waits

`SET LOCAL lock_timeout = '250ms'` bounds each lock-acquisition wait,
including the ones after the table locks are already held — the soft-FK
nulling that follows updates `artifacts` and four other tables, and those
rows can be held by someone else. A wait while holding a lock is what a
deadlock cycle is made of, so bounding it is what keeps the teardown from
sitting in one. It is not a deadline for the whole transaction: it caps no
single statement's execution, no sum of successive lock waits, and nothing
about commit or connection checkout. A long statement can still hold all
three `EXCLUSIVE` locks, and nothing here bounds that.

250ms is chosen against PostgreSQL's `deadlock_timeout`, whose default is
1s, so one lock wait gives up well before the detector runs. It is also
meant to sit above the row-lock holds of the ordinary writers, so everyday
contention resolves instead of aborting a teardown — that second half is the
design intent behind the number and is not something this file measures. A
server configured with a `deadlock_timeout` below this bound can still
detect a cycle first; the teardown then aborts with a deadlock error rather
than a lock timeout, which the callers treat identically.

`NOWAIT` covers the acquisition itself, where waiting is pointless rather
than merely bounded. A comma-separated `LOCK TABLE` takes the three locks
one at a time in the written order rather than atomically, so a blocking
form would hold one table while waiting for the next — closing a cycle with
any writer touching the same tables in a different order. `prune_old_data`
is exactly such a writer: it updates `sessions` before deleting from
`progressions`, the reverse of the order here.

## Where the cost falls

Entirely on this rare teardown: a conflicting lock aborts the attempt, and
both callers log the failure and retry on a later sweep.

# `sessions.artifact_contract_json` — why DAG flows get a write exception

`_SESSION_COLUMNS` (in `lionagi/state/db.py`) allowlists
`artifact_contract_json` for exactly two writer classes. This note preserves
the reasoning that used to live inline as a comment on that entry.

ADR-0064 documents `artifact_contract_json` as fixed at session creation for
the single-agent case, where the full contract (playbook + agent profile) is
already known at `create_session` time. DAG flows break that assumption:
which role runs which leg is only known once planning finishes, which
happens after `create_session` (see `_build_dag` in
`cli/orchestrate/flow.py`).

The column is allowlisted for two writer classes, both append-only and both
frozen before the work they describe ever runs:

1. `_build_dag` folds each planned leg's resolved role `artifact_defaults`
   in once, at DAG-build time, strictly before any leg starts executing.
2. `_execute_dag` folds a reactively spawned node's own entries in after
   that node completes, but what is expected of it (role defaults + its
   builder-stamped `spawn_id`) was frozen before it was ever queued, so this
   is still a "before work starts" declaration in substance — see ADR-0064's
   "Reactive-spawn exception" paragraph.

No other writer may touch this column; the anti-drift intent of ADR-0064 (no
changes once what was expected has been acted on) still holds.
