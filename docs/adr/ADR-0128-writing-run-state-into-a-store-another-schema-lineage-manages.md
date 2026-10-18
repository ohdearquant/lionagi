# ADR-0128: Writing run state into a store another schema lineage manages

- **Status**: Accepted
- **Kind**: Aspirational
- **Implementation-status**: partial: D1-D7 are implemented. D8 is not complete: no test covers
  item 7's writer-hold, busy-rate and WAL-growth limits declared before a run, or item 9's recovery
  after lionagi's writes stop, and item 8's authorizer tests run on each supported Python against
  one aiosqlite version.
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

Native initialization writes `lineage = 'lionagi'` in the transaction that applies its schema, and a
writable native open admits a store by its rows:

- A `lineage` row naming lionagi: the store is this package's. A version above `SCHEMA_VERSION` is
  refused with `SchemaTooNewError`; a version that is missing or not a whole number is replaced by
  the stamp, as before the row existed, since the row already says whose store it is.
- No `lineage` row, and a `schema_meta` table: every store lionagi created before the row holds a
  version from 1 to `SCHEMA_VERSION` and the `sessions` table, both created on its first open. Such
  a store is adopted and the migration writes the row into it; so is one that holds no table a
  lionagi component never creates, as a first open stopped before it created `sessions` leaves. A
  version above `SCHEMA_VERSION` is refused as too new; a missing version, or one no release writes
  (not a whole number, zero, padded, spaced or empty), raises `UnrecognizedStoreError`, since
  nothing then says what shape a migration would meet, and so does a store holding a table no
  lionagi component creates and no `sessions` table. A `schema_meta` table without `key` and
  `value` columns raises `UnrecognizedStoreError`.
- No `schema_meta` table: a store with no tables is new. A store whose tables are all ones a lionagi
  component creates in the state file is adopted: the `StateDB` schema's tables, among them Studio's
  approval ledger, and the Studio Operator store's, which those components create on their own and
  can create before any `StateDB` open. A store holding any other table raises
  `UnrecognizedStoreError`. A server database holds other applications' tables beside lionagi's,
  so on PostgreSQL a database with no `schema_meta` table is new, as before, and a database with
  one is admitted by its rows alone.

Tables are recognized by name, not by their columns: a table of another application that takes the
name of one lionagi creates reads as lionagi's, and the migration then meets it as it is.

On SQLite the version, the table names and the shape of `schema_meta` are read with the `lineage` row,
by the reads before any writable connection exists that the paragraphs below describe, so a store
refused on any of them keeps its bytes, its journal mode and its write-ahead log. When those reads
could not read the journal or the log beside the file, the file's own `lineage` row still refuses,
but its other rows and its tables can be part of a transaction, so they decide nothing there. Each
connection a handle opens while its open is in progress reads all of them again before its
connection settings; a connection opened after the open has applied the schema reads only the
`lineage` row, so `SchemaTooNewError` is raised by an open and never by an ordinary write. The
pre-rebuild backup reads them again before the checkpoint it takes. A refused open leaves no engine,
whichever of `ForeignLineageError`, `UnrecognizedStoreError`, `SchemaTooNewError` and the
compatibility refusal refused it. `UnrecognizedStoreError` is exported from `lionagi.state`.

A store another application manages carries a `lineage` row naming that application. The managing
application writes it when it creates the store, in the transaction that creates its schema, and the
row is the admission contract: compatibility mode admits only a store whose `lineage` row is present
and names an application other than lionagi. lionagi never writes the row into a store it does not
manage. A compatibility open that wrote it on first use would leave every native open before that
write, in any process, free to migrate the store.

A writable native `StateDB` open refuses a store whose `lineage` row names another application,
raising `ForeignLineageError`; this is what refuses a writable `StateDB` open in a second process of
a store this process holds in compatibility mode. On SQLite the row is read before any writable
connection exists, from the file SQLAlchemy opens for the URL however the URL spells the path, and
those reads write no file of the store: the file, its rollback journal, its write-ahead log and the
log's `-shm` index keep their bytes and modification times, whether or not another process has the
store open. The first read opens the file immutable, so SQLite takes no lock and reads no journal,
log or index; a table an interrupted transaction left half-written is skipped rather than failing
the read. Beside a rollback journal that begins with a journal header, as one an interrupted
transaction leaves does, the file can hold part of that transaction, a row it deleted or one it
wrote, so the row is read instead from a private copy of the file and the journal, which SQLite rolls
back. When a non-empty write-ahead log is beside the file, whatever row the file shows, the row is
read again read-only with `readonly_shm`, which reads the log through its existing `-shm` index and
never writes to it; when there is no index, or that read fails, the row is read from a private copy
of the file and the log. A private copy is made in a new directory under the system's temporary
directory (Python's `tempfile`), which only the user running lionagi can read, and the directory is
removed before the read returns; a process killed while it copies leaves the copy in that directory.
The file is copied byte for byte, whatever its pages hold, through a descriptor this process keeps
open for each file it copies and never closes, since closing any descriptor of a file drops every
lock this process holds on it, which another connection here may hold; two costs follow from that:
each distinct store copied keeps one descriptor open for the life of the process, and a store deleted
after it was copied keeps its disk space allocated until the process exits. On Windows, where a lock
belongs to the handle that took it, the file is copied as usual. A copy the store committed or
checkpointed under, seen as a changed journal or log header, is taken again from the first read, a
bounded number of times; when every copy sees the store move, the file's own row stands and the
writable connection decides the rest. A copy costs a read of the whole file, and is made only beside
such a journal or a log without a readable index.

A read that fails decides nothing; the writable connection then decides. Each writable connection
reads the row again right after its busy timeout, before the connection settings of P2 and before
any statement of `_apply_schema()`, which refuses a row written between the first reads and the
writable connection. A connection that refuses is closed. That read is the writable connection's,
and it is the case in which a refused open can change a file of the store: by the time it refuses,
the connection may have rolled back a hot rollback journal, as on any writable open, built the
`-shm` index or recorded its read in it, and, if no other connection has the store open when it
closes, SQLite folds the log into the file. It happens only when the row was written after the reads
before the writable connection, or when those reads could not read it (a file SQLite cannot open
read-only, a copy that cannot be made or read, or a store that moved under every copy). A refused
open, and an open whose first connection never read the store, leaves no engine. A connection a
native handle opened before the row was written is not asked again; only its later connections read
the row. Every transaction of `_apply_schema()` that writes reads the row again under its write
lock, ahead of the version, so a store whose `lineage` names another application and whose version
is above `SCHEMA_VERSION` is refused on its lineage by any of these checks.

While a writable native handle in this process has a file open, compatibility mode does not admit
that file: the handle's connections write without the profile, and those already open would never
read the row. The file is the one the handle's connection opened, by its device and inode, however
the handle's URL spells the path. A native handle in another process is not visible to that check. These refusals cover
writable opens through `StateDB`. A read-only `StateDB` open applies nothing and is not checked, and
Studio's `open_db` and other code that connects to the file natively are not covered.

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
- No production caller opened a store in compatibility mode before the native refusal of D7 shipped,
  since until then a native open in another process could migrate a store this process was writing.
  None does yet: the mode is reached only by passing a profile to `StateDB`, and no command, server
  or configuration passes one.

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
service and the session observer; the four rows for the default hook handlers and the steer
tombstone sweep's row were traced at `54132f85`. Entries name functions, not line numbers. The
traced runs on scratch stores that confirm the manifest (D4) and the tests in the last column (D8)
land with the implementation; until then this is the reading, and a trace that shows an effect
missing here is a defect in this appendix.

### A.0 Today

A writable open of a store stamped with a higher `schema_meta.version` raises `SchemaTooNewError`
before any data or DDL statement; on SQLite the version is read with the `lineage` row, before any
writable connection exists and again by each connection the open makes before its connection
settings of P2 (D7), and the refused open leaves no engine. Every step `_apply_schema()` runs ahead
of its main version check (`_reconcile_columns`, the legacy `sessions` rebuild, and the six legacy
CHECK rebuilds of `schedules`, `invocations`, `schedule_runs` and `definitions`) either finds
nothing to do or checks the version first inside its own `BEGIN IMMEDIATE`, and the backup a
rebuild takes reads it again before its checkpoint. Compatibility mode (D1) runs none of these
steps.

A writable native `StateDB` open of a store whose `lineage` row names another application raises
`ForeignLineageError` (D7) before any writable connection exists, leaving the store file, its
journal, its log and its `-shm` index as they were: beside a journal an interrupted transaction left,
or when the row is only in the log and the log has no readable index, the row is read from a private
copy in a temporary directory that is removed before the read returns, and otherwise a row only in
the log is read through the index without writing it. A row written after those reads, or one they
could not read, is refused by the writable connection's first read, before the connection settings
of P2 and before any statement of `_apply_schema()`; by then that connection may have rolled back a
hot journal, written the index, or, closing as the store's last connection, folded the log into the
file. Wherever the lineage and the version are both read, the lineage is checked
first. While this process holds a store open in compatibility mode, a writable native open of that
file from this process is refused before any writable connection exists, whatever the row says and
however the URL spells the path: by the URL before an engine exists, and by the file's device and
inode on the first read. While a writable native handle in this process has a file open,
compatibility admission of that file is refused, the file being the one that handle's connection
opened, however its URL spells the path. These refusals cover writable `StateDB` opens; a
read-only open applies nothing and is not checked, and Studio's `open_db` and other code that
connects to the file natively are not covered.

A writable native open of a store with no `lineage` row raises `UnrecognizedStoreError` (D7) when
its `schema_meta` version is missing or one no release writes, or, on SQLite, when its
`schema_meta` table has no `key` and `value` columns, when it has no `schema_meta` table and holds a
table no lionagi component creates, or when it records a version and holds such a table but no
`sessions` table. On SQLite each of these is read with the `lineage` row, before any writable
connection exists, so a store those reads can read keeps its bytes, its journal mode and its log
when it is refused; otherwise the open's first writable connection refuses it before its connection
settings.

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
| `persist_session_start` (the default `SESSION_START` handler) | nothing when the run hands its handle to the emit (the agent setup does: the session row it created carries every column below); otherwise a `get_session` read, and nothing more when the session is missing or terminal; when its reason is not yet `run.started.ok`: the `update_status` transition to `running` as above, then `UPDATE sessions SET` the provenance columns the event carries (`model`, `provider`, `effort`, `agent_name`, `agent_hash`, `invocation_id`), `started_at = COALESCE(started_at, :now)` and `updated_at`; when it already is, only that `UPDATE` | by id; `started_at` keeps the first value written; no owner predicate | the transition in one, the `UPDATE` after its commit | only this run's session; `started_at` written only when null; the owner on the transition (D5); `invocation_id` only when that invocation belongs to the same owner |
| `persist_branch_provenance` (the default `BRANCH_CREATE` handler) | nothing when the run hands its handle to the emit (the agent setup does: the branch row it created carries these columns); otherwise `UPDATE branches SET model, provider, agent_name WHERE id`, each as the event carries it, a missing one as `NULL` | by id only | one | only this run's branch |
| `persist_branch_end` (the default `BRANCH_END` handler, through `finalize_branch`) | for a terminal status only: `UPDATE branches SET status, ended_at WHERE id AND (status IS NULL OR status = 'running')` | by id, guarded on a status that is not yet terminal; with the owner predicate in compatibility mode | one | only this run's branch; the guard kept; in compatibility mode the branch must be one this run created and the UPDATE carries the owner predicate (D5) |
| `persist_session_end` (the default `SESSION_END` handler) | a `get_session` read; when the session is not terminal: the `update_status` transition to the event's status as above, carrying `ended_at`, `duration_ms` and, when the event has an error, `node_metadata` read, merged with `error` in Python and written whole; then `UPDATE sessions SET` the usage columns the event carries (`input_tokens`, `output_tokens`, `total_cost_usd`, `num_turns`) and `updated_at`; when the teardown handed its handle to the emit, only `UPDATE sessions SET` those usage columns (the teardown owns the terminal status and writes it once the emits have run); when the session is already terminal and no handle was handed over, the same usage-only `UPDATE` | by id; no owner predicate | the transition in one, the `UPDATE` after its commit | only this run's session; `node_metadata` written by key merge, never whole; the owner on the transition (D5) |
| steer tombstone sweep (`li agent`, after teardown) | `list_pending_session_controls` for the session, then for each control no consumer claimed `UPDATE session_controls SET applied_at, result WHERE id AND result IS NULL` | by id, guarded on an unclaimed control | one per control | only this run's session's controls; the guard kept |

The four default hook handlers take their store from `_db()` in `lionagi/hooks/builtins.py`. When a
run emits a lifecycle event it hands its own open handle to the handler on the event, so the handler
writes through the run's handle and meets whatever the profile requires of that run; only an emit
that carries no handle falls back to the process's shared handle looked up by the store's URL. The
agent setup hands its handle to the `SESSION_START` and `BRANCH_CREATE` emits it makes, and those
two handlers then write nothing, since the rows the setup created carry every column they write; a
teardown hands its handle to the `BRANCH_END` and `SESSION_END` emits it makes. So the default
handlers never open or reach a second handle for that run, and a branch row another principal owns
is left untouched. A handler that does fall back to a URL lookup whose store the process holds in
compatibility mode meets A.0's refusal before any statement. The steer tombstone sweep is a
different class: it opens its own native `StateDB()` on the store instead of taking the run's
handle, so its row is admitted on the same terms.

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
