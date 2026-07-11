# ADR-0055: Operational state persistence boundary

- **Status**: Accepted
- **Kind**: Retrospective
- **Area**: persistence-state
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0004

## Context

LionAGI has two materially different persistence needs. Operational records must support indexed,
cross-record queries and atomic updates, while authored definitions and produced files must remain
usable by editors, version-control tools, and external processes. Treating either medium as the
exclusive store would discard capabilities the other supplies.

This ADR answers five concrete problems in the shipped implementation:

**P1 — Operational state needs relational identity and transactions.** Sessions, branches,
messages, progressions, invocations, shows, plays, schedules, schedule runs, artifacts, signals,
controls, dispatches, and audit records are queried and changed across records. Reconstructing that
state by scanning files would lose the indexed lookups, foreign keys, uniqueness rules, and guarded
updates already provided by `StateDB`.

**P2 — Authored definitions remain independently useful files.** Agent and playbook definitions
are edited and reviewed outside the running process. The database records their path, text, and
version history, but that record does not make the source file an internal database implementation
detail.

**P3 — Conversation order must round-trip without a second ordering model.** The runtime already
represents a progression as an ordered collection of message identifiers. The persistence layer
must retain that ordering and make repeated appends idempotent without introducing a separate
join-table abstraction.

**P4 — The current artifact row is mutable structured state, not immutable evidence.** An artifact
has structured JSON content and an optional external path. Re-inserting the same natural key
updates the existing row. Treating that row as content-addressed, append-only, or byte-preserving
would promise guarantees the implementation does not provide.

**P5 — File verification observes files without capturing them.** Artifact-contract verification
checks safe relative paths, file existence, and non-zero size, then stores the result on the
session. It does not read file bytes into an artifact row, compute a content hash, verify MIME type,
or retain a copy after the external file changes or disappears.

The boundary is backend-neutral. SQLite is the default relational deployment, while PostgreSQL is
implemented through the same persistence facade as recorded by ADR-0056.

| Concern | Decision |
|---------|----------|
| Operational authority | D1: `StateDB` owns queryable operational records; files are not a parallel serialization of those rows. |
| Authored definitions | D2: Definitions retain both versioned database content and an explicit source path. |
| Conversation ordering | D3: A progression stores its ordered message identifiers as one JSON-array text value. |
| Structured artifacts | D4: Artifact insertion is a mutable natural-key upsert with a stable row identifier. |
| Produced-file verification | D5: Verification records observations about external files; it does not capture their bytes. |

This ADR deliberately does **not** decide:

- The SQL engine, transaction, migration, or connection mechanics. ADR-0056 owns those backend
  choices.
- Lifecycle vocabularies and audited status writes. ADR-0057 records the current contract and
  ADR-0058 defines its target consolidation.
- An immutable file-evidence or blob-retention design. No such contract is shipped; the first delta
  below identifies the required follow-up.
- Filesystem run-layout persistence in `lionagi/cli/_runs.py`. That layout is a CLI concern and is
  not an alternate implementation of `StateDB`.

## Decision

### D1 — Relational authority for operational records

`StateDB` is the persistence authority for the operational records it owns. Output files are not an
alternate relational representation of those rows, and a stored path is not proof that external
bytes were captured.

**The contract.** The runtime schema is the SQLAlchemy `MetaData` in
`lionagi/state/schema_meta.py`. Its operational tables are grouped as follows:

```text
conversation:  message_types, messages, progressions, sessions, branches
execution:     invocations, shows, plays, teams, team_messages
scheduling:    schedules, schedule_runs, workers
definitions:   definitions, engine_defs, workflow_defs
state/control: schema_meta, projects, session_signals, engine_runs,
               session_controls, run_tags, approvals, approval_evidence
evidence:      artifacts, status_transitions, admin_events
delivery:      dispatch_outbox
```

Representative public persistence calls use plain Python mappings rather than file objects:

```python
class StateDB:
    async def create_session(self, session: dict[str, Any]) -> None: ...
    async def get_session(self, session_id: str) -> dict[str, Any] | None: ...
    async def insert_message(self, msg: dict[str, Any]) -> None: ...
    async def create_branch(self, branch: dict[str, Any]) -> None: ...
    async def create_invocation(self, invocation: dict[str, Any]) -> None: ...
    async def create_schedule_run(self, run: dict[str, Any]) -> None: ...
    async def insert_admin_event(
        self,
        *,
        action: str,
        details: dict[str, Any],
        target_id: str | None = None,
        actor: str = "admin",
    ) -> str: ...
```

Code anchors: `lionagi/state/schema_meta.py`; `lionagi/state/db.py`.

**Exact semantics.**

- A successful relational write is durable according to the selected database transaction; it
  does not imply that any referenced external file exists or is durable.
- `get_*` methods return a row mapping on a hit and generally return `None` on a missing identifier.
  Domain-specific collection methods return lists, not filesystem iterators.
- Foreign keys and uniqueness constraints apply to relational identifiers. They do not constrain
  the contents behind a stored `path` or `file_path` string.
- JSON-bearing columns store structured values; callers may observe decoded native values or a JSON
  string on lower-level portable query paths and must use the documented decoder behavior of the
  facade.
- No database operation scans output directories to synthesize missing operational rows.
- No filesystem operation is treated as a transaction participant in a `StateDB` write.

**Why this way.** Operational records require cross-record queries and guarded changes, while files
must remain directly usable by tools that do not import LionAGI. Keeping the boundary explicit
avoids a false single-source claim: relational state is authoritative for the records it owns, and
external content remains authoritative for its bytes.

### D2 — Versioned definitions retain source path and content

Agent and playbook definitions are versioned in the database without erasing their filesystem
identity.

**The contract.** The table and facade are:

```text
definitions
  id          TEXT PRIMARY KEY
  kind        TEXT NOT NULL CHECK kind IN ('agent', 'playbook')
  name        TEXT NOT NULL
  path        TEXT NOT NULL
  content     TEXT NOT NULL
  version     INTEGER NOT NULL
  created_at  FLOAT NOT NULL
  message     TEXT NULL
  UNIQUE(kind, name, version)
```

```python
async def save_definition(
    self,
    *,
    kind: str,
    name: str,
    path: str,
    content: str,
    message: str | None = None,
) -> int: ...

async def get_definition(
    self,
    kind: str,
    name: str,
    *,
    version: int | None = None,
) -> dict[str, Any] | None: ...

async def list_definition_versions(
    self,
    kind: str,
    name: str,
) -> list[dict[str, Any]]: ...
```

Code anchors: `lionagi/state/schema_meta.py`; `lionagi/state/db.py`.

**Exact semantics.**

- Only `agent` and `playbook` are accepted as definition kinds; any other value raises
  `ValueError` before mutation.
- Version numbering is scoped to `(kind, name)` and starts at 1. `save_definition()` reads the
  current maximum and inserts `max + 1`.
- Calls sharing one `StateDB` instance serialize version allocation per `(kind, name)`. A database
  uniqueness race is retried up to five times; the value five is inherited and has no recorded
  tuning rationale. Exhaustion raises `RuntimeError` and includes the final integrity error.
- A requested version returns that exact row or `None`. Omitting `version` returns the highest
  version or `None`.
- `list_definition_versions()` returns metadata newest first and deliberately omits `path` and
  `content`; a caller retrieves a selected version separately.
- Saving database content does not write, move, or validate the file named by `path`.
- Updating the authored file does not automatically create a database version. A caller must invoke
  `save_definition()`.

**Why this way.** The database history makes definition versions queryable and stable enough for
run provenance. Retaining `path` and `content` keeps the authored representation inspectable and
does not require editors or version-control systems to become database clients.

### D3 — Progression order is a JSON collection

Message order remains a JSON collection on a progression rather than a message-order join table.

**The contract.**

```text
progressions
  id          TEXT PRIMARY KEY
  created_at  FLOAT NOT NULL
  collection  TEXT NOT NULL DEFAULT '[]'  # JSON array of message-id strings
```

```python
async def create_progression(
    self,
    progression_id: str,
    collection: list[str] | None = None,
) -> None: ...

async def get_progression(self, progression_id: str) -> list[str]: ...

async def append_to_progression(
    self,
    progression_id: str,
    message_id: str,
) -> None: ...
```

Code anchors: `lionagi/state/schema_meta.py`; `lionagi/state/db.py`.

**Exact semantics.**

- `create_progression()` serializes `collection or []`; therefore `None` and an empty list both
  store `[]`.
- Creation uses `ON CONFLICT (id) DO NOTHING`. Re-creating an existing identifier does not replace
  or merge its collection.
- `get_progression()` returns `[]` both for a missing row and for an existing empty collection.
  The API intentionally does not distinguish those cases.
- `append_to_progression()` appends only if the message identifier is not already present. A
  duplicate is a no-op, preserving the position of its first occurrence.
- SQLite uses `json_insert(..., '$[#]', ...)` plus `json_each`; PostgreSQL casts the text value to
  `jsonb`, appends with `||`, and casts back to text. Both paths preserve insertion order.
- Appending to a missing progression updates zero rows and returns normally because the method does
  not inspect row count.
- The table stores message identifiers only. Message content remains in `messages`, and referential
  membership is not represented by a progression-message join constraint.

**Why this way.** The stored shape matches the runtime collection and makes ordered reconstruction
one row read. A join table would improve relational inspection of individual positions but would
introduce another ordering identity and migration surface without changing the runtime abstraction.

### D4 — Artifact rows are mutable structured outcomes

The current artifact API is a mutable structured-outcome upsert. An artifact row makes no implicit
immutability, content-addressability, byte-retention, or integrity guarantee.

**The contract.**

```text
artifacts
  id             TEXT PRIMARY KEY
  invocation_id  TEXT NULL REFERENCES invocations(id) ON DELETE CASCADE
  session_id     TEXT NULL REFERENCES sessions(id)
  created_at     FLOAT NOT NULL
  updated_at     FLOAT NOT NULL
  kind           TEXT NOT NULL
  name           TEXT NOT NULL
  content        JSON NOT NULL
  file_path      TEXT NULL
```

The natural key has four nullable-parent shapes, each enforced by a partial unique index:

```text
(invocation_id, kind, name)             where invocation_id IS NOT NULL and session_id IS NULL
(session_id, kind, name)                where session_id IS NOT NULL and invocation_id IS NULL
(invocation_id, session_id, kind, name) where both parent ids are non-NULL
(kind, name)                            where both parent ids are NULL
```

```python
async def insert_artifact(
    self,
    *,
    kind: str,
    name: str,
    content: dict[str, Any],
    invocation_id: str | None = None,
    session_id: str | None = None,
    file_path: str | None = None,
) -> str: ...
```

Code anchors: `lionagi/state/schema_meta.py`; `lionagi/state/db.py`.

**Exact semantics.**

- Empty `kind` or `name` raises `ValueError` before a write.
- On a natural-key miss, the method creates a 12-hex-character row id, stores the structured
  content, and sets `created_at == updated_at`.
- On a natural-key hit, it updates `content`, `file_path`, and `updated_at`, preserves the id and
  `created_at`, and returns the existing id.
- Passing `file_path=None` on an update clears the previous path; omission and explicit `None` are
  the same because the parameter default is `None`.
- The lookup occurs before the write transaction. The unique indexes remain the authority if two
  independent writers race to insert the same natural key; the method has no artifact-specific
  retry or merge path for that race.
- `content` must be a mapping at the Python signature. The database does not derive it from the
  external file.
- Deleting an invocation cascades artifacts linked to that invocation. A session reference has no
  declared `ON DELETE CASCADE` on this table.
- `file_path` is an uninterpreted nullable text reference. No hash, size, MIME type, capture time,
  blob identifier, or retention policy is stored with it.

**Why this way.** The natural key models a named latest outcome for an invocation/session context.
That makes repeated production idempotent for ordinary callers and gives readers a stable row id.
It is intentionally not an evidence ledger; immutable capture needs different identity and
retention rules.

### D5 — File verification records observations, not bytes

`lionagi/state/artifact_verifier.py` verifies an expected-file contract and persists the resulting
observation on the session. It does not ingest produced files into the artifacts table.

**The contract.**

```python
class ExpectedArtifact(TypedDict, total=False):
    id: str
    path: str
    required: bool
    description: str
    source: str

class ProducedArtifact(TypedDict):
    id: str
    path: str
    size: int
    present: bool

class VerificationResult(TypedDict):
    status: Literal["passed", "failed", "warning", "skipped"]
    checked_at: float
    missing_required: list[ExpectedArtifact]
    missing_optional: list[ExpectedArtifact]
    produced: list[ProducedArtifact]

def verify_artifact_contract(
    contract: dict[str, Any] | None,
    *,
    artifacts_root: str | None,
) -> VerificationResult | None: ...

async def StateDB.update_artifact_verification(
    self,
    session_id: str,
    verification: dict[str, Any] | None,
) -> None: ...
```

Code anchors: `lionagi/state/artifact_verifier.py`; `lionagi/state/db.py`;
`lionagi/state/schema_meta.py`.

**Exact semantics.**

- A `None` contract returns `None` and performs no filesystem check.
- Contract entries require a unique alphanumeric/underscore/hyphen id and a non-empty safe
  relative path. Absolute paths, NULs, glob characters, `..` segments, and paths escaping the root
  raise `ArtifactPathError`.
- The v1 entry keys are `id`, `path`, `required`, `description`, and `source`. Unknown keys are
  warned and ignored rather than enforced.
- Agent defaults are applied first and playbook entries with the same id replace them. Omitted
  `required` defaults to `True`; omitted `description` defaults to an empty string.
- A produced file is present only when it is a regular file and its size is greater than zero.
- A missing or non-directory artifact root classifies every expected entry as missing. Required
  misses produce `failed`; optional-only misses produce `warning`; an empty contract produces
  `passed`.
- A valid root with any required miss produces `failed`; with optional misses only it produces
  `warning`; with no misses it produces `passed`.
- Verification records relative path, observed size, presence, and check time. It does not read or
  store file bytes and does not calculate a digest.
- `update_artifact_verification()` writes the JSON result and updates the session timestamp. A
  missing session updates zero rows and is not reported as an error.

**Why this way.** The verifier answers whether declared output exists at teardown while leaving the
output itself available to ordinary file tooling. Observation and capture are different contracts;
the current code implements only observation.

## Consequences

- Operational queries and atomic state changes use one relational model without forcing authored
  or large produced content into database rows.
- Definitions and outputs remain accessible to ordinary file tooling, and progression ordering
  stays compatible with the runtime data model.
- Contributors must distinguish an operational row, a versioned definition, a mutable structured
  artifact, a verification observation, and an external file. They are not interchangeable forms
  of one datum.
- A database backup does not, by itself, back up referenced definition or artifact files.
- A filesystem backup does not, by itself, preserve operational identity, transition history, or
  dispatch state.
- Reversing D1 or D2 would require a migration and synchronization protocol between files and rows.
  Reversing D3 would require progression data migration. Reversing D4 requires a new identity model,
  because existing rows have already been updated in place.
- Current artifact rows cannot safely be cited as immutable evidence without a separate capture
  contract.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Define and implement the artifact persistence contract as either mutable structured outcomes only or a separate immutable file-capture path; acceptance requires documented identity, mutability, hash, path, and retention semantics with tests that enforce the selected contract. | M | (filled at issue-open time) |
| 2 | Move profile-path resolution out of `lionagi.cli` and make `lionagi/state/provenance.py` depend on a neutral configuration API or an injected resolved path; acceptance requires no import from the state package into the CLI package. | S | (filled at issue-open time) |

## Alternatives considered

### Filesystem-only operational state

Store sessions, messages, schedules, transitions, and dispatches as files and derive indexes by
scanning. This would make every record human-readable and reduce the number of persistence
technologies. It lost because cross-record queries, uniqueness, foreign keys, and guarded atomic
updates would have to be rebuilt above the filesystem. The current status and dispatch paths rely
on database compare-and-set behavior that directory scans cannot supply.

### Database-only authored and produced content

Store definition text and all produced bytes exclusively in relational rows. This would make a
database backup more self-contained and could enable transactional metadata-plus-content writes.
It lost because definitions and outputs are independently consumed by editors, version control,
shell tools, and external processes. The shipped schema also has no blob identity, hash, or
retention contract for output bytes.

### Progression-message join table

Represent each message position as `(progression_id, position, message_id)`. This would make
position-level SQL queries and foreign-key validation easier. It lost because the runtime consumes
an ordered identifier collection as a unit, and the existing dual-backend append contract already
preserves order and idempotency in one row. Moving would add an ordering identity and migration
without a current query requirement.

### Append-only artifact rows

Insert a new artifact row for every production attempt. This would preserve historical structured
outcomes and avoid overwriting earlier values. It lost for the current `artifacts` name because the
natural key and API model a stable latest outcome, and existing callers expect the returned id to
remain stable. Append-only evidence remains viable as a separately named capture model with hashes
and retention.

### Content-addressed immutable file capture

Hash bytes, store a blob or durable object reference, and attach immutable evidence records. This
would provide integrity and deduplication that the current path reference cannot. It was not
selected because byte ownership, storage location, hash algorithm, garbage collection, retention,
and failure atomicity are all undecided. Claiming it through the current artifact API would be
misleading; it remains the explicit follow-up in delta 1.

### Mirror every definition-file edit automatically

Watch the filesystem and create database versions on change. This would reduce manual drift between
the authored file and database history. It lost because the repository has no authoritative watcher
or save boundary, and transient editor writes could become versions. Explicit `save_definition()`
keeps version creation a deliberate operation.

## Notes

The `path` and `file_path` names are references, not integrity claims. Future immutable capture
should use a distinct type or operation so maintainers cannot confuse mutable structured outcomes
with retained evidence.
