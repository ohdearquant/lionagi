# ADR-0077: Studio state and filesystem boundary

- **Status**: Proposed
- **Kind**: Retrospective
- **Area**: studio
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0011, v0-0012, v0-0016, v0-0019, v0-0020, v0-0021, v0-0022, v0-0055

## Context

Studio does not own an independent datastore. Its daemon, the CLI, and runtime all read
and write the StateDB at `DEFAULT_DB_PATH = ~/.lionagi/state.db`. Studio services use two
access idioms against that physical store: typed `StateDB` methods and hand-written SQL
through a Studio-local `aiosqlite` connection helper. Authored definitions and show
material also remain operationally significant files.

This ADR answers six problems created by that hybrid boundary.

**P1 — Duplicating runtime truth would split lifecycle state.** Schedule, workflow,
launch, invocation, artifact, session, and status records already originate in StateDB.
A separate Studio database would force synchronization before a UI could answer whether a
run is current or terminal (`lionagi/state/schema.sql`; `lionagi/studio/services/*.py`).

**P2 — “Definition” names two different persistence contracts.** Agent and playbook
definitions are current files with append-only snapshots in `definitions`; workflow
definitions are database rows whose `spec_json` is current. A generic definition API that
erases this difference would falsely imply one save, rollback, and conflict model
(`definitions.py`; `workflow_defs.py`).

**P3 — A file plus a database row cannot be committed by one SQLite transaction.** The
agent/playbook save path allocates a database version before writing the file. A disk error
after commit advances history without updating the canonical current file, while direct
editor writes update the file without creating a version (`definitions.py`; `state/db.py`).

**P4 — Schema ownership is not consistently mediated.** Many services use `StateDB`, but
sessions, runs, shows, statistics, definition history, and projects also issue SQL directly.
`projects.py` even repeats the `projects` table and index DDL already present in
`lionagi/state/schema.sql`, then runs it lazily from requests. A schema change can therefore
break code outside typed StateDB methods.

**P5 — Shows and artifacts cross the state/file boundary differently.** Shows merge rows
with `_show.md`, `_intent.md`, play directories, and file-change events. Artifacts store
queryable JSON plus an optional `file_path`. A path in state is metadata, not authorization
to return arbitrary host bytes (`shows.py`; `invocations.py`; `state/schema.sql`).

**P6 — The scheduler has a narrow persistence seam, not a Studio-wide repository.**
`SchedulerStateService` makes scheduler engine I/O replaceable in tests, but it does not
abstract raw SQL in unrelated Studio services and does not transfer scheduler execution
semantics into this area (`scheduler_state.py`).

| Concern | Decision |
|---|---|
| Operational truth | D1: Reuse StateDB as the sole queryable operational store. |
| File-backed definitions | D2: Keep agent/playbook files canonical and use `definitions` as append-only history. |
| Workflow definitions | D3: Keep database-backed workflow definitions as a distinct current-state contract. |
| Access and schema ownership | D4: Record the shipped mix of StateDB calls and raw SQL, including duplicated project DDL. |
| Shows and artifacts | D5: Treat files as authored/blob content and StateDB as structural and queryable metadata. |
| Scheduler persistence | D6: Keep `SchedulerStateService` as the scheduler-specific injectable seam. |

Out of scope:

- A replacement application-service architecture is specified by ADR-0078, not described
  as shipped here.
- Scheduler firing, budget, lease, and retry policy remain scheduling-control-plane
  decisions.
- UI artifact rendering and the unified execution workspace target are in ADR-0081.
- General file synchronization, distributed locking, and multi-process editing are not
  provided by the present implementation.
- This ADR does not ratify every raw SQL query as ideal; it records the current boundary.

## Decision

### D1 — StateDB is the shared operational store

StateDB's default physical boundary is:

```python
DEFAULT_DB_PATH = LIONAGI_HOME / "state.db"

class StateDB:
    async def __aenter__(self) -> StateDB: ...
    async def __aexit__(self, ...): ...
```

Studio's local raw-SQL helper opens the same path:

```python
@asynccontextmanager
async def open_db(path: str) -> AsyncIterator[aiosqlite.Connection]:
    # per connection:
    # PRAGMA journal_mode = WAL
    # PRAGMA busy_timeout = 5000
    # PRAGMA foreign_keys = ON
    # row_factory = aiosqlite.Row
    ...
```

`get_active_connection_count() -> int` reports connections opened through this helper,
not every StateDB/SQLAlchemy connection. The 5,000 ms SQLite busy timeout is intended to
absorb modest concurrent-reader/single-writer contention. The exact value is inherited;
the source records no measurement selecting five seconds.

The shared schema contains the operational nouns Studio projects: messages,
progressions, sessions, branches, definitions, shows, plays, invocations, schedules,
schedule runs/task applications, artifacts, status transitions, session signals, engine
runs/definitions, workflow definitions, projects, and related indexes
(`lionagi/state/schema.sql`).

Exact semantics:

- If `DEFAULT_DB_PATH` does not exist, list operations commonly return empty collections
  and get operations return `None`; they do not create a shadow database merely to read.
- StateDB writes use its transaction helpers; raw Studio queries use their explicit
  connection and commit behavior. Sharing a path does not make multiple service calls one
  transaction.
- WAL and foreign-key pragmas are applied to Studio-local raw connections. StateDB applies
  its own schema/connection setup.
- The CLI, runtime, daemon, web client projections, and VS Code projections converge on
  the same rows. Studio does not copy lifecycle truth into a UI database.

Why this way: the runtime is the producer of session and invocation truth. Reusing its
store gives local tools one record without a replication protocol. The cost is that Studio
queries are coupled to StateDB schema unless mediated through typed methods.

### D2 — Agent and playbook files are current; `definitions` is history

The editable set and file roots are closed:

```python
AGENTS_DIR = LIONAGI_HOME / "agents"
PLAYBOOKS_DIR = LIONAGI_HOME / "playbooks"
KIND_DIRS = {"agent": AGENTS_DIR, "playbook": PLAYBOOKS_DIR}
_EXTENSIONS = (".md", ".playbook.yaml", ".yaml")
```

The history table is:

```sql
CREATE TABLE definitions (
  id         TEXT PRIMARY KEY,
  kind       TEXT NOT NULL CHECK(kind IN ('agent', 'playbook')),
  name       TEXT NOT NULL,
  path       TEXT NOT NULL,
  content    TEXT NOT NULL,
  version    INTEGER NOT NULL,
  created_at REAL NOT NULL,
  message    TEXT
);
CREATE UNIQUE INDEX idx_def_unique_version
  ON definitions(kind, name, version);
```

The service contract is:

```python
async def list_definitions(kind: str | None = None) -> list[dict[str, Any]]: ...
async def get_definition(kind: str, name: str) -> dict[str, Any] | None: ...
async def get_version(kind: str, name: str, version: int) -> dict[str, Any] | None: ...
async def save_definition(
    kind: str,
    name: str,
    content: str,
    message: str | None = None,
) -> dict[str, Any]: ...
async def rollback_definition(
    kind: str, name: str, target_version: int
) -> dict[str, Any] | None: ...
async def snapshot_current(kind: str | None = None) -> int: ...

class SaveBody(BaseModel):
    content: str
    message: str | None = None
```

Exact save semantics:

1. Validate `kind` and `name` before filesystem operations. Unknown kinds fail with
   `ValueError`; the HTTP adapter maps that to 422.
2. Resolve or create a per-process `(kind, name)` `asyncio.Lock`; different definitions
   may save concurrently, while the same definition is serialized within one daemon.
3. Locate a direct file, a `<name>/<name><ext>` file, or a literal candidate one directory
   deep. Symlinked definitions may intentionally resolve outside the root.
4. Call `StateDB.save_definition()` first. It selects `MAX(version)+1`, inserts a UUID row,
   and retries an integrity collision up to five times under a StateDB lock.
5. Only after the DB commit, create parent directories and write the file in a worker
   thread.
6. Return `{kind, name, version, saved_at, message}`.

Five retries bound collisions from concurrent writers; the implementation records no
measurement selecting exactly five. Exhaustion raises `RuntimeError` and does not return a
version.

Failure and conflict semantics:

- A database failure prevents the file write.
- A file failure after the database commit propagates, but the history row remains. There
  is no compensation row or automatic reconciliation.
- The in-process lock does not coordinate another daemon or a direct editor.
- Listing scans disk first, then enriches matching entries with max DB version. A history
  row without a current file is absent from the current-definition list.
- Current content is read from disk; version content is read from StateDB.
- Rollback reads an old row and calls the normal save path, creating version `N+1`; it
  never rewinds or overwrites history. Missing target version returns `None`/HTTP 404.
- Snapshot creates a version only when the current disk content differs from latest
  history. Empty or missing roots produce zero snapshots.

This is append-only history around a file source of truth, not an atomic cross-medium
transaction.

### D3 — Workflow definitions are database-current and separately typed

Workflow definitions do not use D2's file/history path. Their current table is:

```sql
CREATE TABLE workflow_defs (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  description TEXT,
  spec_json   JSON,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
);
```

The HTTP request models are:

```python
class CreateWorkflowDefRequest(BaseModel):
    name: str
    description: str | None = None
    spec_json: dict[str, Any] | None = None

class UpdateWorkflowDefRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    spec_json: dict[str, Any] | None = None

class RunWorkflowDefRequest(BaseModel):
    inputs: dict[str, Any] | None = None
    base_dir: str | None = None
```

`spec_json.version` must equal `1`; `base_dir` is forbidden inside the authored spec and
is supplied only at run time. Nodes and edges are arrays capped at 200 and 400
respectively. Valid node kinds are `input`, `chat`, `parse`, `fanout`, and `engine`.
Node ids and edge ids are non-empty and unique; every node has numeric `pos.x`/`pos.y`;
edge endpoints must exist; optional conditions are non-empty strings. Chat nodes require
`config.prompt: str`; an optional model must be a provider-prefixed string. Inputs and
outputs are arrays of strings.

The node/edge caps bound Designer payload and validation work. No recorded measurement
selects exactly 200 or 400; they are inherited shipped limits.

Exact semantics:

- Create strips the name, requires 1–120 characters, validates the graph, allocates a
  12-hex-character id, and maps uniqueness failure to HTTP 409.
- Invalid graph or name maps to HTTP 422. A removed `gate` node is rejected on writes and
  also causes an actionable 422 when loading a legacy row.
- Update first checks existence. An empty patch succeeds without changing `updated_at`;
  non-empty patches validate only supplied fields and map a name conflict to 409.
- Delete returns 404 when no row was removed.
- List defaults to 100 rows, accepts 1–200, orders by `updated_at DESC`, and returns an
  empty list when the DB file does not exist.
- Run compilation returns HTTP 202 with `{run_id, status}`; `run_id` is the Session id.
  Missing definitions map to 404 and compile errors to structured 422 detail.
- There is no append-only workflow version history in this contract.

The word “definition” therefore does not imply identical persistence. Consumers must
select file-definition or workflow-definition operations explicitly.

### D4 — Mixed typed and raw access is shipped, but not a complete adapter

The current module-level boundary is:

```text
StateDB methods:
  workflow_defs, schedules, schedule_runs, invocations, artifacts,
  status transitions, definition-version inserts, scheduler state

Studio-local SQL:
  sessions and message hydration, run aggregates, definition history reads,
  shows and plays, statistics, signals, projects, selected maintenance reads
```

`projects.py` declares this request-time DDL:

```sql
CREATE TABLE IF NOT EXISTS projects (
  name TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  path TEXT,
  github TEXT,
  description TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  last_seen_at REAL
);
CREATE INDEX IF NOT EXISTS idx_projects_source ON projects(source);
CREATE INDEX IF NOT EXISTS idx_projects_updated ON projects(updated_at DESC);
```

The same table and indexes also appear in `lionagi/state/schema.sql`. This is duplicated
ownership, not a second database. Each project request calls `_ensure_table()` before its
query or mutation.

Project exact semantics include:

- List returns `{projects: [], unassigned_count: 0}` when the DB file is absent.
- Project list/detail left-join sessions and derive counts. `editable` is true only for
  sources `studio` and `global_override`.
- Create trims a required name and inserts source `studio`; HTTP maps invalid name to 400
  and other insertion failures, including conflict, to 409.
- Update accepts only `description`, `github`, and `path`; no accepted fields or missing row
  yields false and the route returns 404 “not found or no changes.”
- Assignment updates named sessions or all unassigned rows, marks `project_source='manual'`,
  and upserts the project. With neither selector it returns zero.
- Delete removes only source `studio`; a missing or non-Studio row maps to HTTP 403.

These details show why a generic statement that “Studio uses StateDB” is insufficient:
schema and behavior also live in HTTP-service SQL today.

### D5 — Shows and artifacts preserve a structural/content split

Shows combine StateDB rows with filesystem content beneath `SHOWS_ROOT`. The key storage
shapes are:

```text
shows: id, topic, goal, repo, base_branch, integration_branch, status,
       show_dir, status_source, created_at, updated_at, status_reason_*
plays: id, show_id, name, playbook, effort, status, attempt, session_id,
       timing/exit/worktree/branch/merge fields, gate fields, depends_on,
       sort_order, timestamps, status_reason_*
```

Exact show semantics:

- List prefers the DB projection. A query failure or an empty DB result falls back to a
  directory scan.
- Detail may combine a DB show and play rows with `_show.md`, `_intent.md`, and verdict
  files. If neither directory nor row exists, it returns `None`/404.
- Filesystem paths are projected with `public_path()` rather than treated as content.
- Import is a POST mutation. A missing shows root returns zero counts; existing folders are
  imported into StateDB using runtime state methods and status transitions.
- The file watcher contract is recorded in ADR-0076 D5.

Artifacts are DB-first metadata:

```sql
artifacts(
  id, invocation_id, session_id, created_at, updated_at,
  kind, name, content JSON, file_path
)
```

Four partial unique indexes cover invocation-only, session-only, both, and unattached
natural keys because SQLite treats NULLs as distinct. `StateDB.insert_artifact()` updates
the stable existing id for a matching natural key or creates a 12-hex id. It rejects empty
`kind` or `name`. Studio serializes JSON content into an object and returns `file_path` as
metadata; `GET /api/artifacts/{id}` does not read and return the referenced file.

This preserves queryable outcomes while avoiding large blob insertion. It does not yet
provide an authenticated, containment-checked content download contract.

### D6 — `SchedulerStateService` is the narrow scheduler seam

The protocol in `lionagi/studio/services/scheduler_state.py` contains:

```python
class SchedulerStateService(Protocol):
    async def get_schedule(self, schedule_id: str) -> dict[str, Any] | None: ...
    async def list_schedules(self, *, enabled: bool | None = None) -> list[dict[str, Any]]: ...
    async def update_schedule(self, schedule_id: str, **fields: Any) -> None: ...
    async def count_schedule_runs(self, schedule_id: str, *, chain_depth: int = 0) -> int: ...
    async def sum_schedule_spend(self, schedule_id: str) -> dict[str, Any]: ...
    async def metric_value(self, metric: str, window_start: float) -> float: ...
    async def create_schedule_run(self, run: dict[str, Any]) -> None: ...
    async def update_schedule_run(self, run_id: str, **fields: Any) -> None: ...
    async def create_invocation(self, invocation: dict[str, Any]) -> None: ...
    async def update_invocation(self, inv_id: str, **fields: Any) -> None: ...
    async def update_status(..., expected_statuses: set[str | None] | frozenset[str | None] | None = None) -> bool: ...
    async def list_sessions_for_invocation(self, invocation_id: str) -> list[dict[str, Any]]: ...
```

The real implementation opens a fresh StateDB context per protocol method. Scheduler helper
functions accept the protocol so tests can substitute a fake. This seam covers persistence
needed by scheduler execution; it does not own HTTP adaptation or unrelated Studio reads.

## Consequences

- CLI, runtime, daemon, web, and editor projections share one operational record.
- Direct file editing, symlinks, and ordinary filesystem tools remain viable for agent and
  playbook authoring. Rollback is auditable because it appends rather than rewrites.
- Contributors must know which “definition” contract they are changing. File definitions
  have history but a cross-medium failure window; workflows have DB-current state but no
  built-in versions.
- Raw SQL gives efficient projections but makes schema changes visible beyond StateDB's
  typed method surface. Reversing D4 requires migrating each query and removing duplicated
  DDL, not swapping one repository object.
- Show availability depends on both DB and filesystem. Artifact metadata can survive without
  readable blob content, and current APIs must not claim otherwise.
- The scheduler can be tested against a fake state service without implying that all Studio
  persistence is abstracted.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|---|---|---|
| 1 | Remove the projects table/index DDL from the HTTP service, make StateDB schema and migrations the sole owner, and move project/session query methods behind typed StateDB operations while preserving the six project endpoints. | M | (filled at issue-open time) |
| 2 | Add a definition-save reconciliation contract with content hashes, persisted file-write state, recovery for DB-first/file-second failures, and explicit multi-process edit behavior. | M | (filled at issue-open time) |
| 3 | Replace repeated raw `aiosqlite` access with typed StateDB query methods or bounded read repositories, and add dialect-compatible tests for every retained raw query. | L | (filled at issue-open time) |
| 4 | Complete the scheduler persistence seam by routing remaining scheduler-engine state access through `SchedulerStateService` or documenting each intentional exception in the scheduling-control-plane ADR. | S | (filled at issue-open time) |
| 5 | Make artifact file references relative, authenticated, containment-checked, and hash-verifiable before the web client offers inline preview or download. | M | (filled at issue-open time) |

## Alternatives considered

### A Studio-owned database

This would isolate UI schema changes and allow aggressive read-model denormalization. It
lost because lifecycle, schedules, invocations, and artifacts originate in runtime state;
Studio would need a replication and conflict protocol before it could display authoritative
status. One local source is simpler and already shipped.

### Move all authored content into StateDB

Database-current agents and playbooks would make save and history atomic and simplify remote
editing. It lost because direct editors, symlinked definition trees, filesystem discovery,
and version-control workflows are established behavior. D2 makes the non-atomic cost
explicit instead of erasing those workflows.

### Keep all current content on disk, including workflow graphs

This would unify the meaning of definition and make workflows editor-friendly. It lost
because the shipped Designer contract already treats `workflow_defs.spec_json` as current
DB state with name uniqueness and typed graph validation. Moving it is a migration decision,
not a harmless repository consolidation.

### Treat DB-first/file-second save as transactional

The per-process lock makes concurrent same-process saves appear serialized and could tempt
documentation to call the operation atomic. It lost because a disk failure after the DB
commit and a direct external edit are observable counterexamples. Honest failure semantics
are more useful than a false transaction label.

### Generic repository over every table

A uniform CRUD interface could hide SQLAlchemy versus aiosqlite and make tests easy. It lost
because StateDB has behavior-rich methods—status transitions, artifact natural-key upserts,
definition version allocation—that generic CRUD would obscure. ADR-0078 instead calls for
narrow command/query ports around changed operations.

### Inline all artifact bytes in SQLite

This would make artifact reads transactional and avoid missing files. It lost because logs,
diffs, and generated outputs can be large, while structured outcome cards benefit from JSON
queries. The current split stores queryable content and references blobs, with a required
future safe-resolution boundary.
