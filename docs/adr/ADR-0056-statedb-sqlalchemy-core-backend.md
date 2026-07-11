# ADR-0056: StateDB SQLAlchemy Core backend

- **Status**: Accepted
- **Kind**: Retrospective
- **Area**: persistence-state
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0009, v0-0059, v0-0086

## Context

The persistence layer began as a SQLite-specific implementation and later acquired a PostgreSQL
target. Maintaining separate query and schema implementations would make every operational record,
migration, and lifecycle update a parity problem. The current code instead routes both dialects
through SQLAlchemy Core and one `StateDB` method surface.

This ADR answers six concrete problems:

**P1 — Callers need one backend-neutral entry point.** A CLI, scheduler, or runtime component
should not select a SQLite repository class versus a PostgreSQL repository class or learn driver
URL variants. Existing callers already construct `StateDB`, use it as an async context manager, or
obtain a shared instance.

**P2 — Schema authority must not fork by backend.** Fresh SQLite and PostgreSQL databases must be
created from the same table and constraint declarations. Existing databases additionally require
additive reconciliation and a small set of SQLite table rebuilds.

**P3 — SQLite needs explicit single-writer coordination.** SQLite WAL permits concurrent readers,
but write acquisition still contends. Coroutines sharing an engine must not race each other into
`BEGIN IMMEDIATE`, and independent connections must wait for a bounded period rather than hang.

**P4 — PostgreSQL must use database concurrency primitives.** A process-local SQLite lock would
unnecessarily serialize PostgreSQL. Read-modify-write paths instead need transactions, row locks,
or advisory locks with the database as the coordination authority.

**P5 — Portability is semantic, not textual.** JSON append, activity timestamps, maintenance
commands, lock syntax, connection configuration, and some migrations differ by dialect. A string
replacement layer cannot make those operations equivalent.

**P6 — Lifecycle and read-only modes must be explicit.** Ordinary open may create a SQLite parent
directory and apply schema. Read-only SQLite must never create a missing file, run schema DDL, take
a write reservation, or apply persistent PRAGMAs. Process-wide shared instances must also have
defined open and teardown behavior.

| Concern | Decision |
|---------|----------|
| Backend selection and caller surface | D1: Normalize path/URL inputs behind one asynchronous `StateDB` facade. |
| Schema creation and compatibility | D2: SQLAlchemy `MetaData` is runtime authority; reconcile existing schemas before `create_all()`. |
| SQLite concurrency | D3: Apply WAL-oriented PRAGMAs, serialize writes per instance, and begin with `BEGIN IMMEDIATE`. |
| PostgreSQL concurrency | D4: Use ordinary database transactions plus targeted row/advisory locks. |
| Portable queries and dialect seams | D5: Share Core/SQL text paths and branch only where semantics differ. |
| Read-only and shared lifecycle | D6: Give read-only engines and process-shared instances explicit, separate contracts. |

This ADR deliberately does **not** decide:

- Which records belong in the database versus files. ADR-0055 owns that boundary.
- Per-entity lifecycle vocabularies or transition policy. ADR-0057 and ADR-0058 own those rules,
  even though `StateDB` currently implements the writes.
- Database provisioning, replication, backup scheduling, or server topology. `StateDB` consumes a
  URL; it does not operate a PostgreSQL service.
- Repository extraction from the large facade. The current-vs-ideal delta records that possible
  refactor while preserving the public surface.

## Decision

### D1 — One normalized asynchronous facade

`StateDB` is the compatibility facade over one asynchronous SQLAlchemy Core implementation. A
normalized URL selects the driver; callers do not select a separate repository implementation.

**The contract.**

```python
DEFAULT_DB_PATH = LIONAGI_HOME / "state.db"

def normalize_state_db_url(value: str | Path | None) -> str: ...
def dialect_of(url: str) -> str: ...
def make_engine(url: str, **overrides): ...
def make_readonly_engine(url: str, **overrides): ...

class StateDB:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        url: str | None = None,
        readonly: bool = False,
    ): ...

    @property
    def path(self) -> Path | None: ...

    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def __aenter__(self) -> StateDB: ...
    async def __aexit__(self, *exc: Any) -> None: ...
```

Resolution order in the constructor is exact:

```text
explicit url keyword
  else path positional/keyword value
  else settings.LIONAGI_STATE_DB_URL
  else LIONAGI_HOME / "state.db"
```

URL normalization is:

| Input | Normalized result |
|-------|-------------------|
| `Path` or string without `://` | absolute `sqlite+aiosqlite:///...` |
| `:memory:` | `sqlite+aiosqlite:///:memory:` |
| `sqlite:///...` | same path under `sqlite+aiosqlite:` |
| `sqlite+aiosqlite://...` | unchanged |
| `postgres://...` or `postgresql://...` | `postgresql+asyncpg://...` |
| `postgresql+asyncpg://...` | unchanged |
| another scheme | returned unchanged; `dialect_of()` derives its prefix |

Code anchors: `lionagi/state/db.py`; `lionagi/state/engine.py`;
`lionagi/config.py`.

**Exact semantics.**

- `url` wins over `path` even when both are supplied. The constructor does not reject the unused
  path.
- `StateDB.dialect` is the value returned by `dialect_of()`, normally `sqlite` or `postgresql`.
- `path` returns an on-disk `Path` or `Path(":memory:")` for SQLite and `None` for PostgreSQL.
- `open()` is idempotent while `_engine` is non-`None`; it returns without rebuilding the engine.
- Writable SQLite creates missing parent directories. Read-only SQLite requires the file to exist
  and raises `FileNotFoundError` rather than silently creating it.
- Writable open constructs the engine and applies schema. Read-only open constructs the restricted
  engine and skips schema application entirely.
- `close()` disposes the engine and resets `_engine`; repeated close is a no-op.
- Context-manager exit closes even when the body raises; it does not suppress the exception.
- The facade exposes mappings and primitive values, not ORM entity instances. SQLAlchemy Core is
  the query/construction layer; there is no declarative ORM model graph.

**Why this way.** One facade preserves the established import and call surface while allowing a
deployment to move from an embedded file to PostgreSQL through configuration. Async driver
normalization also prevents callers from needing to know `aiosqlite` or `asyncpg` URL spelling.

### D2 — `MetaData` is runtime schema authority

SQLAlchemy `MetaData` in `lionagi/state/schema_meta.py` is the authoritative runtime schema for
both dialects. `StateDB.open()` reconciles compatible existing databases before creating missing
objects.

**The contract.** The state package is divided as follows:

```text
lionagi/state/
├── db.py                 StateDB facade, queries, schema-open orchestration
├── engine.py             URL normalization and AsyncEngine factories
├── schema_meta.py        authoritative SQLAlchemy MetaData and indexes
├── schema_migrations.py  additive ALTER TABLE column declarations
├── schema.sql            compatibility DDL snapshot and parity input
├── reasons.py            status reason vocabulary
├── transitions.py        smaller guarded transition adapter
├── health.py             derived session-health classifier
├── staleness.py          kind-aware stale threshold helper
└── artifact_verifier.py  produced-file observation contract
```

The open sequence is:

```text
StateDB.open()
  -> make_engine(normalized_url)
  -> SQLite only: install BEGIN IMMEDIATE begin hook
  -> _apply_schema()
       1. _reconcile_columns()
       2. SQLite: rebuild legacy sessions status CHECK if present
       3. SQLite: rebuild legacy schedules action-kind CHECK if present
       4. SQLite: rebuild legacy schedules action-kind CHECK to admit `command` if present
       5. SQLite: rebuild legacy invocations status CHECK if present
       6. SQLite: rebuild legacy schedule_runs CHECK/nullability if present
       7. metadata.create_all()
       8. seed schema_meta version/created_at and message_types rows
```

`schema_meta.py` currently declares these tables from one `MetaData` object:

```text
schema_meta, message_types, messages, progressions, projects, invocations,
sessions, branches, definitions, shows, plays, teams, team_messages, schedules,
schedule_runs, workers, admin_events, artifacts, status_transitions,
session_signals, engine_runs, engine_defs, workflow_defs, session_controls,
dispatch_outbox, run_tags, approvals, approval_evidence
```

Code anchors: `lionagi/state/db.py`; `lionagi/state/schema_meta.py`;
`lionagi/state/schema_migrations.py`; `lionagi/state/schema.sql`.

**Exact semantics.**

- `_reconcile_columns()` inspects only tables listed in `MIGRATION_COLUMNS`. Missing tables are
  skipped for later `create_all()`; missing listed columns are added with `ALTER TABLE`.
- Inspection exceptions in additive reconciliation are caught and that table is skipped. A later
  schema operation may still surface the underlying database error.
- `metadata.create_all()` is additive. It creates absent tables and indexes but does not alter an
  incompatible existing constraint, which is why the four SQLite rebuild paths run first.
- The legacy rebuilds are SQLite-only because SQLite cannot widen those CHECK/nullability rules in
  place. PostgreSQL uses ordinary DDL compatibility and additive columns on this path.
- The `schedule_runs` rebuild checkpoints and copies the database to a timestamped backup before
  replacement. The older session, schedule, and invocation rebuilds do not all use that same
  backup helper.
- The rebuilds preserve known columns and recreate captured indexes; the `schedule_runs` rebuild
  also captures triggers and explicitly creates its new queue indexes.
- Fresh schema creation seeds `schema_meta.version = "1"`, an immutable initial `created_at`, and
  six stable message-type rows with `ON CONFLICT DO NOTHING`.
- `schema.sql` is not executed by ordinary `StateDB.open()`. It remains a hand-maintained
  compatibility DDL whose column sets and enum CHECK values are compared with `MetaData` in tests.
- `schema_version()` reads the `schema_meta` row and returns its string value or `None`.

**Why this way.** Shared metadata makes fresh-database behavior a single authored contract. The
pre-create reconciliation path preserves existing local state without forcing every change into a
full migration framework. Retaining `schema.sql` is compatibility debt, not a second runtime
authority.

### D3 — SQLite WAL and per-instance write serialization

SQLite is the zero-configuration default. Writable connections receive a fixed PRAGMA profile, and
every `StateDB` write transaction begins with `BEGIN IMMEDIATE` while holding the instance's write
lock.

**The contract.**

```text
PRAGMA busy_timeout = 5000
PRAGMA journal_mode = WAL
PRAGMA synchronous = NORMAL
PRAGMA foreign_keys = ON
PRAGMA cache_size = -64000
PRAGMA wal_autocheckpoint = 1000
```

```python
@asynccontextmanager
async def StateDB._tx(self):
    if self.dialect == "sqlite":
        async with self._write_lock:
            async with self._engine.begin() as conn:  # begin hook emits BEGIN IMMEDIATE
                yield conn
    else:
        async with self._engine.begin() as conn:
            yield conn

async def vacuum(self) -> None: ...
async def checkpoint(self, mode: str = "PASSIVE") -> tuple[int, int, int] | None: ...
```

Code anchors: `lionagi/state/engine.py`; `lionagi/state/db.py`.

**Exact semantics.**

- The SQLAlchemy SQLite driver is placed in driver autocommit mode, and the engine `begin` event
  emits `BEGIN IMMEDIATE`. Write reservation therefore happens at transaction start, not at the
  first modifying statement.
- `_write_lock` serializes coroutines using the same `StateDB` instance. It does not coordinate
  independent instances or processes; SQLite file locking plus `busy_timeout` governs those.
- The 5,000 ms busy timeout is a bounded contention window and is tunable in tests. The repository
  records the bounded-wait intent but no workload-derived rationale for exactly five seconds.
- WAL, `NORMAL` synchronous mode, the `-64000` cache setting, and the 1,000-page autocheckpoint are
  inherited operational defaults; no benchmark or capacity rationale is recorded alongside them.
- Foreign-key enforcement is enabled on every writable connection. Selected raw rebuild paths
  temporarily disable it while replacing referenced tables, then re-enable it.
- `vacuum()` and `checkpoint()` use the raw aiosqlite connection because the adapter's implicit
  transaction interferes with those maintenance commands.
- Checkpoint accepts only `PASSIVE`, `FULL`, `RESTART`, or `TRUNCATE`; any other value raises
  `ValueError`. It returns SQLite's three-value result or `None` on PostgreSQL.
- An independent writer holding a lock beyond `busy_timeout` surfaces a database operational error;
  `StateDB` does not retry an arbitrary transaction automatically.

**Why this way.** `BEGIN IMMEDIATE` and one in-process lock make write ownership explicit and avoid
same-engine coroutine races. WAL keeps reads available during ordinary writes. Database locking
remains the authority across processes because a Python lock cannot protect the file globally.

### D4 — PostgreSQL transactions and targeted locks

PostgreSQL does not use the process-local SQLite write lock. It relies on the database transaction
manager and adds locks only to read-modify-write operations that need them.

**The contract.**

```text
ordinary write:  async with AsyncEngine.begin()
status update:   SELECT status ... FOR UPDATE, then guarded UPDATE
signal sequence: pg_advisory_xact_lock(hashtextextended(session_id, 0)),
                 then MAX(seq) + 1 and INSERT
engine health:   pool_pre_ping = True
```

PostgreSQL URL query handling recognizes `sslmode`:

```text
require     -> asyncpg SSL context without hostname/certificate verification
verify-ca   -> default SSL context
verify-full -> default SSL context
disable     -> ssl=False
```

The `sslmode` query parameter is removed before the URL is passed to asyncpg.

Code anchors: `lionagi/state/engine.py`; `lionagi/state/db.py`.

**Exact semantics.**

- `_tx()` does not acquire `_write_lock` for PostgreSQL, so independent operations can run
  concurrently subject to database isolation and row locks.
- `update_status()` selects its entity row `FOR UPDATE`; the subsequent update also reasserts the
  previous status and optional timestamp guard.
- Session-signal sequence allocation takes a transaction-scoped advisory lock derived from the
  session id before computing `MAX(seq) + 1`. Different session ids do not share that logical lock.
- No global advisory lock serializes all state writes.
- PostgreSQL `VACUUM` is executed through a read/autocommit connection path; `checkpoint()` returns
  `None` because the SQLite WAL checkpoint contract has no PostgreSQL analogue here.
- Unsupported or already driver-qualified URL forms pass through normalization. Engine creation,
  rather than the normalizer, reports an unusable driver or scheme.

**Why this way.** PostgreSQL supplies multi-writer concurrency and durable lock primitives. Reusing
the SQLite Python lock would reduce throughput without protecting other processes. Targeted locks
make the read-modify-write invariant visible at the operation that needs it.

### D5 — Shared queries with explicit dialect seams

Dialect branches are confined to behavior that is genuinely different. Ordinary reads and writes
use SQLAlchemy connections, named parameters, portable SQL, and JSON bind types.

**The contract.**

```python
async def fetch_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]: ...
async def fetch_one(self, sql: str, params: Any = None) -> dict[str, Any] | None: ...
async def execute(self, sql: str, params: Any = None) -> None: ...
def transaction(self): ...
```

Legacy qmark parameters are converted to named `:pN` parameters before SQLAlchemy execution. A
parameter-count mismatch raises `ValueError`.

Known semantic branches include:

| Operation | SQLite | PostgreSQL |
|-----------|--------|------------|
| Progression append | `json_insert` + `json_each` | text-to-`jsonb`, append, cast to text |
| Monotonic activity | scalar `MAX` expression | `GREATEST` expression |
| Status row lock | transaction begins immediate | `FOR UPDATE` |
| Signal sequence | immediate write transaction | advisory transaction lock |
| Checkpoint | `PRAGMA wal_checkpoint` | no-op result `None` |
| Legacy CHECK rebuilds | table replacement | not run |

Code anchors: `lionagi/state/db.py`; `lionagi/state/engine.py`.

**Exact semantics.**

- Read helpers use an autocommit execution option and return plain dictionaries.
- `fetch_one()` returns `None` on no row; `fetch_all()` returns an empty list.
- `execute()` wraps one SQL statement in the normal transaction contract and returns no row count.
- `transaction()` exposes the same internal transaction context for callers that must group
  multiple statements.
- JSON is bound with SQLAlchemy's `JSON` type where the method declares it. Lower-level raw query
  results can differ by driver, so higher-level row decoding handles string JSON defensively.
- Dynamic identifiers are used only from internal allowlists/table maps. Caller values remain bound
  parameters.

**Why this way.** Most record operations are structurally identical and benefit from one testable
implementation. Explicit branches remain necessary where database functions, lock behavior, or DDL
capability differ. Keeping those branches local is clearer than pretending textual SQL equivalence.

### D6 — Read-only engines and shared instances have separate lifecycle rules

Read-only access and process-wide reuse are explicit adaptations of `StateDB`, not flags that weaken
ordinary write invariants invisibly.

**The contract.**

```python
async def get_shared_db(path: str | Path | None = None) -> StateDB: ...
async def register_shared_db(db: StateDB) -> None: ...
def unregister_shared_db(db: StateDB) -> None: ...
async def close_shared_db() -> None: ...
```

Read-only SQLite opens an existing file using:

```text
sqlite+aiosqlite:///file:<path>?mode=ro&uri=true
PRAGMA busy_timeout = 5000
PRAGMA query_only = 1
```

Code anchors: `lionagi/state/engine.py`; `lionagi/state/db.py`.

**Exact semantics.**

- Read-only mode supports SQLite files only. PostgreSQL callers must use a database role with
  read-only permissions; `make_readonly_engine()` raises `ValueError` for that dialect.
- Read-only mode rejects `:memory:` and missing files, applies no schema, and skips persistent
  connection PRAGMAs and `BEGIN IMMEDIATE` installation.
- `get_shared_db()` keys instances by the fully normalized URL, opens once under a lazily-created
  lock, and uses a double check after acquiring it.
- `register_shared_db()` adopts the caller's instance for its URL and closes a different previously
  registered instance.
- `unregister_shared_db()` removes only when object identity matches the registered value; it does
  not close the object.
- `close_shared_db()` closes and clears all registered instances. A generation counter causes an
  open/register call that waited across teardown to raise rather than resurrect a just-closed
  singleton.
- Shared-instance lifetime is process-local. It does not make separate processes share a connection
  or Python lock.

**Why this way.** Read-only inspection must be safe even when pointed at valuable state, while live
services benefit from avoiding redundant engines. Separating those lifecycle contracts prevents an
inspection command from mutating schema and prevents teardown races from silently reopening state.

## Consequences

- SQLite and PostgreSQL share schema objects, query construction, and the public persistence API.
- Backend parity is testable without maintaining two complete stores, and local users retain the
  embedded default.
- The implementation still carries two transaction strategies, driver-specific engine setup, and
  SQLite-specific rebuild code. Contributors must test both dialects for portable changes.
- `StateDB` concentrates schema lifecycle, migrations, and many unrelated record domains in one
  large module. The stable facade lowers caller coupling but raises internal change coupling.
- Reversing D1 would migrate every caller. Reversing D2 would require a new schema authority and
  compatibility plan. D3 and D4 can evolve independently behind `_tx()` so long as their conflict
  and durability contracts remain stable.
- Read-only correctness depends on using the dedicated constructor path, not merely promising that
  a writable engine will issue no writes.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Make SQLAlchemy `MetaData` the only maintained schema definition by generating `schema.sql` as a compatibility snapshot or removing it after all consumers migrate; acceptance requires parity tests to have exactly one authored schema source. | S | (filled at issue-open time) |
| 2 | Move SQLite rebuild plans and additive migration definitions into a dedicated schema-migration module; acceptance requires `StateDB` open to delegate migration planning and retain backup, rollback, and existing-database compatibility tests. | M | (filled at issue-open time) |
| 3 | Extract record repositories by persistence concern behind the existing `StateDB` facade; acceptance requires no caller migration, preserved SQLite/PostgreSQL contract tests, and a smaller change surface for conversation, execution, scheduling, studio, and artifact records. | M | (filled at issue-open time) |

## Alternatives considered

### Separate raw-driver implementations

Maintain one aiosqlite store and one asyncpg store. This would expose every backend feature directly
and avoid SQLAlchemy's abstraction cost. It lost because schema, query, JSON, lifecycle, and
migration behavior would be duplicated, making parity a continuous two-implementation obligation.

### SQLite-only persistence

Keep the embedded store as the sole supported backend. This would minimize operational surface and
retain simple local deployment. It lost because a PostgreSQL target is already implemented and the
shared facade demonstrates that callers need not absorb the additional backend complexity.

### PostgreSQL-only persistence

Require a server database for all use. This would provide uniform multi-writer concurrency and
remove SQLite rebuild and PRAGMA logic. It lost because zero-configuration local state is a core
shipped behavior, and requiring service provisioning would be a disproportionate migration for
single-user workflows.

### String-rewriting dialect shim

Write SQLite SQL and translate tokens for PostgreSQL. This would appear lighter than SQLAlchemy.
It lost because JSON functions, NULL-safe comparisons, locks, maintenance commands, DDL changes,
and driver configuration differ semantically rather than lexically.

### Declarative ORM entities

Represent every table as an ORM class with relationships and unit-of-work sessions. This would
offer object navigation and change tracking. It lost because the public contract is mapping-based,
many operations are explicit guarded SQL, and adopting ORM identity would add a second state model
without a caller requirement.

### Migration framework as the only open path

Require every database to run a versioned external migration command before `StateDB.open()`. This
would make history and rollback steps more explicit. It lost for the current implementation because
local databases already rely on idempotent open-time reconciliation. A dedicated internal migration
planner remains desirable, but breaking automatic open compatibility is not required to obtain it.

### Process-local lock for both dialects

Acquire `_write_lock` on PostgreSQL as well as SQLite. This would simplify reasoning inside one
process. It lost because it cannot coordinate other processes and would serialize independent rows
that PostgreSQL can safely update concurrently. Database locks are the correct shared authority.

### Treat `schema.sql` as runtime authority

Execute raw DDL for SQLite and derive PostgreSQL separately. This would preserve a directly readable
schema script. It lost because dialect-neutral `MetaData` already creates both backends, while a raw
SQLite script cannot be the unmodified PostgreSQL schema. The remaining script is compatibility
input, not runtime authority.

## Notes

The current schema comment and parity tests should be kept synchronized with the actual table set.
Any new dialect branch must name the semantic difference it addresses; backend conditionals are not
a substitute for a shared contract.
