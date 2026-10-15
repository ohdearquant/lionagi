# ADR-0128: Writing run state into a store another schema lineage manages

- **Status**: Proposed
- **Kind**: Aspirational
- **Implementation-status**: not-started
- **Area**: persistence-state
- **Date**: 2026-10-14
- **Relations**: extends ADR-0122 (optional runtime profiles); constrains every writable `StateDB` open;
  read with ADR-0123 (canonical run identity), whose readers this record must keep correct, and
  ADR-0117 (normalized progression membership), whose cutover changes the SQL effects of every
  progression operation a profile names

## Context

Runtime features that a downstream deployment built on its own copy of this package are moving into
this package. While they move, runs started by this package write into a state store that the
deployment's application still manages at a later schema. This record governs that interim. It does
not replace moving the features, which is how the store comes to have one manager again.

`StateDB` has two ways to open a store today. A writable open builds the engine, applies connection
settings, runs `_apply_schema()` (rebuilds, `create_all`, backfills, version stamp) and then serves
every public method. A read-only open skips all of that. A writable open of a store whose
`schema_meta.version` is higher than `SCHEMA_VERSION` raises `SchemaTooNewError`.

That refusal is correct and too coarse at once. One store can be shared by two programs: an older
lionagi command line and a newer application that has migrated the store further, or a lionagi
install and a different application that grew the same tables in its own direction. The older
writer cannot open the store at all, even to append a run whose rows fit the shared tables. Removing
the refusal is not a fix: the same open then migrates, rebuilds and stamps a store it does not
manage.

Four facts decide the design.

**P1: a table that fits is not an operation that is safe.** `set_progression(existing_id, [])`
replaces a progression's whole ordering with an UPDATE of an allowed column, and the import command
calls it. `update_workflow_def` updates a projection that a newer reader may ignore in favour of a
head revision. A wrong-owner upsert of a message is an ordinary `INSERT ... ON CONFLICT DO UPDATE`.
None of these needs DDL, a DELETE or an unlisted table.

**P2: the writable open mutates before it admits.** The engine applies `journal_mode=WAL`,
`wal_autocheckpoint` and other settings on every writable connection, and `open()` creates the
engine before `_apply_schema()` runs. Skipping `_apply_schema()` alone still changes the store.

**P3: generic SQL reaches the store.** `execute`, `transaction`, `fetch_one` and `fetch_all` accept
arbitrary SQL. Any guard placed only on named methods is bypassed by them, by a new method, or by a
second pool connection.

**P4: version numbers are ordered only within one lineage.** `schema_meta` carries no lineage key.
Two programs that each reach "version 5" by different migrations hold different schemas under the
same number.

| Concern | Decision |
|---------|----------|
| Open modes | D1: a third mode, compatibility, separate from native and read-only; it never migrates, stamps or sets persistent store settings. |
| Admission | D2: an explicitly selected, versioned compatibility profile, checked against the store's structure; never a version number alone. |
| Enforcement | D3: a default-deny SQLite authorizer on every compatibility connection, plus a named-operation surface; a denial aborts the transaction. |
| Write set | D4: an operation manifest (operation, SQL effects, row predicates, transaction boundaries, reader invariants), not a table list. |
| Ownership | D5: ownership written explicitly when the profile requires it; no reliance on column defaults; upserts and updates carry the owner predicate. |
| Refusals | D6: maintenance and history-rewriting operations refuse with a typed error that no persistence fallback may swallow. |
| Lineage | D7: native stores carry a lineage key; native migration requires lineage, version and shape together; compatibility mode never writes it. |
| Acceptance | D8: a coexistence test matrix gates each operation before it is enabled. |

**Out of scope.** Protecting a store from arbitrary code with file access: an in-process guard binds
only connections lionagi opens. Synchronizing two stores. Changing what the managing application
writes. Which operations a given deployment enables first; that is the profile's content.

## Decision

### D1: compatibility is a third open mode

`StateDB` gains a compatibility mode beside native (today's writable open) and read-only. In
compatibility mode `open()`:

- never calls `_apply_schema()`, `create_all`, a rebuild, a backfill or a version stamp;
- never sets a persistent store setting: it reads `journal_mode` and refuses unless it is already
  `wal`, rather than setting it;
- sets connection-local settings only (`busy_timeout`, `foreign_keys = ON`, `synchronous`), and
  leaves SQLite's automatic PASSIVE checkpoint at its default; manual checkpoints refuse (D6);
- disposes the engine and leaves the object unopened when admission fails, so a failed open cannot be
  reused as a writer;
- keys any shared handle by URL, mode, profile and owner, so a native and a compatibility handle on
  one file, and two compatibility handles writing different owners, are never the same object.

Read paths that only read (status, list, detail) open read-only regardless of mode.

Why: P2. A separate path is the only place where "never migrate" holds by construction rather than by
a guard inside migration code.

### D2: admission by named profile

A compatibility profile is a versioned, named description of the part of a store an operation set
depends on, shipped with lionagi and selected explicitly in configuration. It declares:

- the structural manifest of its dependency closure: for each table, every column (type, nullability,
  default), primary and unique keys, check constraints, incoming and outgoing foreign keys grouped by
  constraint, and the triggers, views and virtual tables that touch the closure;
- for each column the store has and lionagi does not write, why its omission is correct (a default
  that is valid and means the right thing, or a nullable column with no reader meaning);
- the reader invariants its operations preserve (D4).

Admission requires an existing store, a selected profile, a structural match of the closure, and the
capabilities the caller requests. The store's version may narrow which profiles are tried; it never
admits on its own. An unknown trigger, generated column or virtual table on the closure refuses.

At the start of every write transaction, after the writer reservation, the connection compares
`PRAGMA schema_version` with the value it validated; a change re-inspects the closure or refuses
before any statement runs.

Why: P1 and P4. A version ceiling or column containment admits stores whose meaning differs.

### D3: enforcement at the connection and at the operation

Every compatibility connection, including pool growth and reconnects, receives a SQLite authorizer
before any application statement. It is default-deny:

- INSERT only into the profile's tables; UPDATE only of the profile's columns;
- DELETE, every DDL action, ATTACH, DETACH, temporary or virtual schema changes, and unlisted PRAGMAs
  deny;
- refusal uses `SQLITE_DENY`, never `SQLITE_IGNORE`.

The authorizer sees statements, not values or rows, so it is a ceiling and not the contract. The
contract is the operation layer: in compatibility mode `StateDB` serves only the operations the
profile names (D4); `execute`, `transaction`, `fetch_one` and `fetch_all` refuse any statement that
is not a read, and statement shapes such as `INSERT OR REPLACE`, `UPDATE OR REPLACE` and DML with
`RETURNING` refuse before execution.

A denial inside a transaction rolls that transaction back and forbids its commit, even if the caller
catches the error.

The dependency floor rises from `aiosqlite>=0.21.0` to `aiosqlite>=0.22.0`, the first release whose
`Connection` exposes `set_authorizer`; 0.21.0 does not. The engine's `connect` listener installs the
authorizer through SQLAlchemy's `AdaptedConnection.run_async`, which reaches the driver connection on
its worker thread and is present across the supported `sqlalchemy>=2.0.0` range. A connection whose
authorizer cannot be installed refuses to open. The authorizer is a SQLite facility, so a
compatibility open against any other dialect refuses.

Why: P3. Each layer alone has a known bypass: named-method checks miss generic SQL; the authorizer
cannot see owners or values; an adapter misses a second connection.

### D4: the write set is an operation manifest

The unit of the write set is an operation, and each entry records:

1. the entry points that reach it (CLI command, MCP tool, background handler, retry and cleanup
   paths);
2. every SQL effect it has, direct and transitive (lifecycle events, `message_types`, project
   registration, invocation counters);
3. the row predicates: which rows may already exist, how an existing id is treated, which parent rows
   must belong to the same owner;
4. its transaction boundaries, and what is committed if a later phase refuses;
5. the reader invariants it keeps (ordering, visibility in list and detail views, lifecycle
   consistency);
6. the tests that prove it (D8).

Tables are an index into the manifest. The manifest is built from the entry points down through
`StateDB` calls, lifecycle callbacks, hooks and separate connection factories, then confirmed by
traced runs on scratch stores; an effect a trace shows and the manifest lacks is a defect in the
manifest, and the authorizer denies it at runtime meanwhile.

The first profile admits only operations on rows the same run created: creating a session, branch
and progressions, persisting live messages, appending to progressions it created, recording that
run's lifecycle transitions and terminal state. Resuming or controlling a session that run did not
create is not in it.

### D5: ownership is explicit

When the profile's tables carry an owner column, compatibility mode writes the owner on every insert
from a principal resolved from configuration and verified to exist. A column default is never the
owner. Upserts and updates carry the owner predicate, and an existing id owned by another principal
refuses instead of updating. Links between rows (session to branch, branch to progression,
progression to message) are checked to share one owner, through the parent where a link table has
no owner column. Lookup tables without an owner (such as `message_types`) are named in the manifest
with their own rule.

### D6: refusals

In compatibility mode these refuse before any statement runs:

- every maintenance operation: migration, rebuild, `create_all`, backfill, version stamp, VACUUM,
  prune (including its preview, which runs real DML and rolls back), content erasure, manual
  checkpoint of any mode, import and repair;
- every DELETE path;
- replacing the collection of a progression the run did not create;
- workflow-definition writes, until revision semantics are in a profile;
- resume, control or adoption of rows the run did not create.

A refusal raises `StateCompatibilityRefused` carrying the operation, the profile, the reason and
whether anything was committed. The CLI exits nonzero; MCP returns a structured tool error. The live
persistence retry queue and setup-time persistence handlers re-raise it: a refused write never
becomes a run that succeeded without persistence. Where an operation spans transactions, the error
names the phases that committed.

### D7: lineage

Native stores carry a `schema_meta` row `lineage = 'lionagi'`, written only by native initialization
and by a native migration that has recognized the store. Native migration requires the lineage key, the
version and the expected shape together; a store without the key is adopted only if its shape is one
lionagi's own history produced. Compatibility mode reads the store's metadata and never writes
`version`, `lineage` or any backfill key. Non-numeric, missing and equal version values are
classified explicitly rather than falling through a numeric comparison.

### D8: acceptance

An operation enters a profile only with these tests passing on scratch stores built by the managing
application's own initializer and migrations, never the live store:

1. fresh and migrated store shapes;
2. the full run lifecycle through the CLI and MCP entry points, including failure, cancellation and
   retry, read back through the managing application's own list, detail and stream paths;
3. preservation: unrelated and foreign-owned rows, and the managing application's own invariants,
   unchanged after the run and after the managing application reopens the store;
4. negative enforcement: unknown table, forbidden column, generic DML, `RETURNING`, REPLACE forms,
   ATTACH, temporary schema, a new pool connection, a reconnect, a caught denial;
5. ownership with two principals, wrong-owner parents and id collisions;
6. a schema change between admission and a write, and between two writes;
7. concurrency with the managing application writing and checkpointing, with writer-hold, busy-rate
   and WAL-growth limits declared before the run;
8. the authorizer installed on every supported dependency version;
9. recovery: stop lionagi's writes and show the managing application still reads every row written.

Public tests use synthetic profiles. A deployment proves its own profile in its own integration suite.

## Consequences

- An older lionagi can append runs to a store a newer schema manages, within a declared profile, and
  the store's own migrations stay with its manager.
- The compatibility surface is a maintained public contract and narrower than native `StateDB`.
  Commands outside the profile refuse with a typed error instead of silently writing.
- Numeric schema parity stops being a prerequisite for sharing a store; per-operation semantic parity
  replaces it.
- The aiosqlite floor rises to 0.22.0 (D3).
- Turning compatibility writes off is the rollback. Restoring a backup over a store the managing
  application is still writing is not.

## Alternatives considered

- **Port every schema version and its consumers first, then share as a native writer.** It gives one
  schema and no guard. It loses as the first step because schema parity does not prove concurrent
  reader and writer compatibility, and the port is large; it remains the path for operations a profile
  cannot cover safely.
- **Allow writes when the version is higher but the columns fit.** It loses on P1 and P4.
- **Write to a separate store.** It isolates writes and fails the requirement that the managing
  application see the runs.
- **Guard named methods only.** It loses on P3.
- **Rely on column defaults for ownership.** It attributes every write to whichever principal the
  default names, and does not protect the conflict-update path.
- **Reserve the other lineage's version numbers.** The collision moves; it is not resolved.
- **An out-of-process writer broker with operating-system file restrictions.** It also protects
  against code outside lionagi. It is larger than the threat this record addresses (lionagi's own
  persistence drifting), and stays available if that threat model changes.

## Appendix A: operation manifest for the first profile

Traced at commit `5cab31a2` by reading each entry point down through `StateDB`, the lifecycle
service and the session observer. Entries name functions, not line numbers. The traced runs on
scratch stores that confirm the manifest (D4) and the tests in the last column (D8) land with the
implementation; until then this is the reading, and a trace that shows an effect missing here is a
defect in this appendix.

### A.0 Today

A writable open of a store stamped with a higher `schema_meta.version` raises `SchemaTooNewError`
before any data or DDL statement. Every step `_apply_schema()` runs ahead of its main version check
(`_reconcile_columns`, the legacy `sessions` rebuild, and the six legacy CHECK rebuilds of
`schedules`, `invocations`, `schedule_runs` and `definitions`) either finds nothing to do or checks
the version first inside its own `BEGIN IMMEDIATE`. The connection settings of P2 are applied before
any of it. Compatibility mode (D1) runs none of these steps.

### A.1 Entry points

The first profile admits one entry point: a fresh `li agent` run, reaching the store through
`setup_agent_persist`, the message-persistence hook with its retry queue, the session observer's
signal sink, and `teardown_persist`.

Outside the first profile, so these refuse with the D6 error:

- resuming or adopting an existing branch: `repair_session_progression`,
  `repair_branch_progression`, `create_progression` for a repaired id, and
  `_reopen_session_for_resume`, which calls `update_status`;
- `find_incomplete_session_for_run`, whose `update_status` marks a session failed after a setup
  failure, called from the Studio operator only;
- `attach_session_invocation`, orchestration and flow persistence, schedules, engines, MCP run
  submission, import, prune and every maintenance operation.

### A.2 Operations

Transaction labels are per operation; "after commit" means a separate transaction that runs only once
the previous one committed.

| Op | Statements today | Row predicate today | Transactions | Profile rule |
|----|------------------|---------------------|--------------|--------------|
| `create_progression` (session, then branch) | `INSERT INTO progressions (id, created_at, collection)` `ON CONFLICT (id) DO NOTHING` | an existing id is left as is and the run then appends to it | one per call | the id must not exist; an existing id refuses |
| `create_session` | `INSERT INTO sessions` (33 columns) `ON CONFLICT (id) DO NOTHING`; when a row was inserted with a declared initial status: `UPDATE sessions` reason columns, then the lifecycle initializer reads the row, checks no initial transition exists and `INSERT INTO status_transitions`; when `invocation_id` is set: `UPDATE invocations SET session_count = session_count + 1` | by id only | one transaction for all of these | the id must not exist; the owner written on the session and its transition (D5); the invocation must belong to the same owner, or the run carries no `invocation_id` |
| project registration (`register_project`, from `create_session`) | `INSERT INTO projects ... ON CONFLICT (name) DO UPDATE SET last_seen_at, updated_at, source, path, github` | by name; updates a project row the run did not create | after commit; its failure leaves the session committed | not admitted: the run's project stays on the session row, and registration is left to the managing application |
| `insert_message` (system message) and `_insert_message_in_tx` | `INSERT INTO message_types (lion_class) ON CONFLICT DO NOTHING`, a `SELECT` of the type id, then `INSERT INTO messages ... ON CONFLICT (id) DO UPDATE SET` every content column | by id only: an existing id is overwritten whatever its owner | one | `message_types`: insert-if-absent admitted, no owner; `messages`: the id must not exist or must already belong to this run's session through its progression, and the owner is written (D5) |
| `create_branch` | `INSERT INTO branches ... ON CONFLICT (id) DO NOTHING` | by id only | one | the id must not exist; session, progression and system message share the branch's owner |
| `_persist_live_message` (hook, retry queue, teardown flush) | `_insert_message_in_tx` as above; `_append_to_progression_in_tx` for the branch and session progressions; for a system message `UPDATE branches SET system_msg_id`; `UPDATE sessions SET last_message_at, updated_at` to the maximum of the stored and new values | by id only | one per message | appends only to progressions this run created; updates only this run's branch and session |
| `insert_session_signal` (observer sink) | `SELECT COALESCE(MAX(seq), 0)` for the session, then `INSERT INTO session_signals (id, session_id, seq, kind, op_id, ts, payload)` with no conflict clause | by session id; on Postgres a transaction-scoped lock keyed on the session first, on SQLite `BEGIN IMMEDIATE` | one per signal | only for this run's session; where the managed schema keeps `(session_id, seq)` unique, a concurrent writer's signal makes the insert raise, and the sink counts it dropped instead of retrying with a new `seq` |
| teardown `update_session` | `UPDATE sessions SET first_msg_id, last_msg_id, node_metadata, updated_at WHERE id` | by id; `node_metadata` is read, merged in Python and written whole | one | only this run's session; `node_metadata` written by key merge, never whole |
| teardown `update_artifact_verification` | `UPDATE sessions SET artifact_verification_json, updated_at WHERE id` | by id | one, after the previous commit | only this run's session |
| teardown `merge_session_node_metadata` (provider-error path only) | `UPDATE sessions SET node_metadata` by JSON patch | by id | one | only this run's session |
| teardown `update_status` (lifecycle transition) | a `SELECT` of the status columns (Postgres `FOR UPDATE`), `UPDATE sessions` guarded on the expected previous status, `INSERT INTO status_transitions`, and `INSERT INTO admin_events` on the rejected and override paths | compare-and-set on status; no owner predicate | one | only this run's session; owner on the transition and any admin event; override refuses in compatibility mode |
| terminal callbacks | after commit, in process: handlers registered on the terminal callback registry run; `li` registers one when `notify.on_terminal` is configured, and it runs an external command and records its outcome in the run directory | none | after commit, outside any transaction | no store statement; admitted unchanged |
| session and branch signals at setup | `SESSION_START` and `BRANCH_CREATE` are emitted before the observer's sink is bound, so neither reaches `session_signals` today | n/a | n/a | unchanged; the manifest does not add them |

### A.3 Reader invariants the profile keeps

- A progression lists its messages in arrival order with no duplicates.
- `last_message_at` and `updated_at` on a session never move backwards.
- A session's `status` and its newest `status_transitions` row change in one transaction, so a reader
  never sees one without the other.
- A run's session, branch, progressions and messages share one owner, so a reader filtering by owner
  sees the whole run or none of it.

### A.4 Tests (D8)

Each row of A.2 gets a test on a scratch store built to the managed schema: the statement set it
emits (a statement spy, as the progression tests use), the refusal for an existing id or a foreign
owner, and the rollback when a later statement in the same transaction is denied. The A.0 refusal and
the A.1 refusals each get a test that the store's bytes are unchanged after the call.
