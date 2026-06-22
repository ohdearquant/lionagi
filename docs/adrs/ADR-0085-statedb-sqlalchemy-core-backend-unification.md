# ADR-0085: StateDB SQLAlchemy-Core backend unification

**Status**: Accepted
**Date**: 2026-06-22

## Context

`lionagi/state/db.py` (`StateDB`, ~2485 lines) is the async persistence layer for sessions,
branches, messages, progressions, signals, shows, plays, schedules, invocations, artifacts,
status transitions, and engine runs (21 tables). It is written directly against `aiosqlite`:
it holds a single `aiosqlite.Connection`, applies SQLite `PRAGMA`s, runs `schema.sql` via
`executescript`, serializes every write through one `anyio.Lock` plus `BEGIN IMMEDIATE`
(aiosqlite routes all statements through one background thread, so concurrent `BEGIN`s would
raise), and uses SQLite-only SQL (`INSERT OR IGNORE`, `json_insert`/`json_each`,
`PRAGMA table_info`, `strftime`). A parallel raw path, `studio/services/_db.py::open_db()`,
opens its own `aiosqlite.connect()` and is used by nine Studio services.

Issue #1557 requires StateDB to run on PostgreSQL as well as SQLite, including remote Postgres
(TLS / `sslmode`, secrets from environment), so deployments can share state across processes and
hosts instead of being pinned to one local file. The codebase already commits to
SQLAlchemy-async + asyncpg for relational Postgres: the `postgres` optional-dependency
(`pydapter[postgres]`, `asyncpg`, `sqlalchemy[asyncio]`) is declared, and
`lionagi/adapters/async_postgres_adapter.py` already drives Postgres through
`create_async_engine`. StateDB is the one persistence surface that has not adopted that stack.

## Decision

Unify both backends on **one SQLAlchemy Core async engine and one query layer**. A single
`AsyncEngine` is created per resolved database URL (`sqlite+aiosqlite://…` or
`postgresql+asyncpg://…`). SQLAlchemy handles paramstyle, type mapping (JSON↔JSONB,
`REAL`→double precision, `BLOB`→bytea), and dialect-specific upserts. Default behavior is
**unchanged**: with no configuration, StateDB resolves to the same local SQLite file and the
existing SQLite test suite must stay green (the regression gate).

### Configuration & URL resolution

- New field `LIONAGI_STATE_DB_URL: str | None = None` on `AppSettings` (`config.py`).
- `StateDB` resolution order: explicit `url=` arg → `settings.LIONAGI_STATE_DB_URL` →
  default `sqlite+aiosqlite:///{LIONAGI_HOME}/state.db`.
- Backward compatibility: `StateDB(path=…)` and a bare filesystem path normalize to
  `sqlite+aiosqlite:///{abspath}`. `sqlite:///x` → `sqlite+aiosqlite:///x`;
  `postgres://` / `postgresql://` → `postgresql+asyncpg://`.
- The shared-instance registry (`_SHARED`, `get_shared_db`, `register_shared_db`,
  `close_shared_db`, `_SHARED_CLOSE_GEN`) rekeys from `dict[Path, StateDB]` to
  `dict[str, StateDB]` keyed by the normalized URL string (a `Path("postgresql://…")`
  mis-parses today).
- The URL may carry a password; it must never be logged. Mask as `first6…[N chars]`.

### Schema is SQLAlchemy `MetaData` (single source of truth)

`lionagi/state/schema_meta.py` defines a `MetaData` with all 21 `Table`s, reproducing
`schema.sql` exactly (columns, types, PK/FK, indexes, CHECK constraints, defaults). Schema
application becomes `metadata.create_all(engine)` — dialect-correct DDL for both backends.
Partial indexes use `Index(…, sqlite_where=…, postgresql_where=…)`; enum CHECKs use
`CheckConstraint`. Column reconciliation (`_reconcile_columns`, driven by `MIGRATION_COLUMNS`)
moves from `PRAGMA table_info` to `sqlalchemy.inspect(conn).get_columns(...)` + `ALTER TABLE
ADD COLUMN`. The legacy CHECK-drop table-rebuilds (`_drop_legacy_*_check`) stay SQLite-only and
are no-ops on Postgres. `schema.sql` is retired once MetaData is authoritative (after a grep
confirms no other readers). JSON columns use SA `JSON` type (→ `jsonb` on PG, JSON-text on
SQLite); method bodies pass/read native dicts instead of hand-serializing, except the
progression-array path below.

### Concurrency model is dialect-conditional

The `_write_lock` + `BEGIN IMMEDIATE` design exists solely because aiosqlite is single-threaded.
A `_tx()` helper abstracts the transaction window:

- **SQLite**: keep `_write_lock` and emit `BEGIN IMMEDIATE` (preserve exact single-writer
  semantics; readers may use the pool). PRAGMAs applied via a `connect` event listener.
- **Postgres**: real MVCC + asyncpg pool, no global write lock; transactions via `conn.begin()`,
  with targeted locking only where a read-modify-write requires it.

The five `BEGIN IMMEDIATE` windows map as follows:

| Window (method) | SQLite | Postgres |
|---|---|---|
| `update_status` CAS + `status_transitions` | write-lock + `BEGIN IMMEDIATE` | tx + `SELECT status … FOR UPDATE` on the entity row |
| `write_provenance` (session + project upsert) | write-lock + `BEGIN IMMEDIATE` | tx (single-row UPDATE + `ON CONFLICT` upsert) |
| `append_to_progression` (idempotent array append) | write-lock + `BEGIN IMMEDIATE` | tx + jsonb append guarded by membership test |
| `insert_session_signal` (`MAX(seq)+1`) | write-lock + `BEGIN IMMEDIATE` | tx + `pg_advisory_xact_lock(hashtextextended(session_id,0))` *or* unique `(session_id,seq)` + retry |
| `insert_engine_run` / `update_engine_run` | write-lock + `BEGIN IMMEDIATE` | tx (single-row; PK conflict handles dup) |

### Dialect translation catalog

| SQLite-ism | Unified form |
|---|---|
| `INSERT OR IGNORE` | `INSERT … ON CONFLICT DO NOTHING` (valid in **both** dialects as plain text) |
| `json_insert(col,'$[#]',?)` | SQLite: keep; PG: `col \|\| to_jsonb(:v)` (dialect-conditional) |
| `json_each(col)` membership | SQLite: keep; PG: `EXISTS(SELECT 1 FROM jsonb_array_elements_text(col) WHERE value=:v)` |
| `?` positional params | SA Core expression-language (dicts) or `text(:name)` named binds |
| big multi-column `INSERT` | `insert(table).values(**row)` + `.on_conflict_do_nothing()` |
| `strftime('%s','now')` | app-level `time.time()` default (already app-set in live paths) |
| `PRAGMA *` | SQLite-only `connect` event; skipped on PG |
| `PRAGMA table_info` (reconcile) | `inspect(conn).get_columns(...)` |
| `VACUUM` / `wal_checkpoint` (CLI/stats) | dialect branch; PG `VACUUM (ANALYZE)` / autovacuum, WAL stats reported N/A |

### Rollout (phased, each phase keeps SQLite green and adds PG coverage)

1. **Foundation** — config field, `engine.py` (URL normalize + engine factory + pragmas + `_tx`),
   `schema_meta.py` (MetaData), port `open()`/schema/reconcile + singleton rekey, port the full
   `StateDB` method surface onto SA Core, dual-backend parity test on the live container.
2. Studio `_db.py` + the nine Studio services onto the shared engine.
3. CLI dialect branches (`cli/state.py`, `cli/kill.py`, `cli/monitor.py`): `VACUUM`, `json_each`,
   WAL stats.
4. CI dual-backend matrix (real Postgres service container) + docs + Makefile target.

## Consequences

**Positive**

- One query layer; SQLite/Postgres dialect drift is eliminated by construction.
- Remote/shared Postgres state becomes possible (multi-process, multi-host deployments).
- Matches the SQLAlchemy-async + asyncpg stack already used by `async_postgres_adapter.py`.
- MetaData becomes the single schema source; DDL is generated, not hand-maintained per dialect.

**Negative**

- Large rewrite of a hand-tuned, well-tested layer; risk of regressing SQLite concurrency
  fixes (WAL-promotion deadlock, CAS races, singleton lifecycle). Mitigated by the full SQLite
  suite as a hard regression gate plus a dual-backend parity test on a live container.
- Two concurrency strategies coexist (write-lock+IMMEDIATE for SQLite, MVCC+targeted-locks for
  PG); the `MAX(seq)+1` signal path needs explicit PG-correct locking.
- Adds SQLAlchemy to the StateDB hot path (already a declared dependency).

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Additive backend interface (SQLite stays raw aiosqlite; separate PG backend) | Two full implementations to keep in parity forever; perpetuates the studio raw path. Lower risk but loses the "one code path" goal Ocean chose. |
| Hand-written dialect layer over raw drivers (aiosqlite + asyncpg, no SQLAlchemy) | Reinvents paramstyle/JSON/DDL translation that SA already does; leaves the declared `sqlalchemy` dependency unused. |
| Phased dialect-shim that string-rewrites SQLite SQL to PG | Fragile (regex-rewriting `json_each` etc.); a too-clever compatibility layer that review would rightly flag. |

## References

- Issue #1557 — StateDB backend-pluggable storage + Postgres support
- `lionagi/adapters/async_postgres_adapter.py` — existing SA-async + asyncpg precedent
- `pyproject.toml` `[project.optional-dependencies].postgres`
- `lionagi/state/db.py`, `lionagi/state/schema.sql`, `lionagi/state/schema_migrations.py`
- `lionagi/studio/services/_db.py` — parallel raw-aiosqlite path (phase 2)
