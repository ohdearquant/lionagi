# ADR-0053: Artifact Persistence in State Database

Status: proposed
Date: 2026-05-27
Decision owners: @governance-maintainers
Depends on: ADR-0009 (SQLite state layer), ADR-0029 (artifact contracts), ADR-0041 (immutable evidence nodes)
Related: ADR-0021 (structured artifacts), ADR-0050 (operation context), ADR-0055 (Studio artifact viewer), ADR-0059 (Postgres backend — consumes artifact columns defined here)

## Context

lionagi currently has a useful filesystem artifact protocol and a separate structured-artifact table, but the two do not form one evidence-grade artifact model.

The filesystem side is run-scoped. `RunDir` records an authoritative state root and a user-facing `artifact_root` (`lionagi/cli/_runs.py:70`), then derives agent artifact directories with explicit containment checks (`lionagi/cli/_runs.py:104`). `FlowAgent.id` and `FlowOp.id` are validated because they become path segments under `artifact_root` (`lionagi/cli/orchestrate/flow.py:193`). At runtime each worker is instructed to write files into its agent directory (`lionagi/cli/orchestrate/flow.py:1038`), `_record_result()` writes `{op_id}.md` files (`lionagi/cli/orchestrate/flow.py:1113`), and synthesis writes `synthesis.md` before finalization (`lionagi/cli/orchestrate/flow.py:1601`). This gives agents and users real files, which remains necessary.

The database side is structured-outcome oriented. The current `artifacts` table contains `id`, parent IDs, timestamps, `kind`, `name`, JSON `content`, and optional `file_path` (`lionagi/state/schema.sql:485`). It also has natural-key unique indexes for stable structured upserts (`lionagi/state/schema.sql:497`) and `StateDB.insert_artifact()` selects an existing natural key, updates it in place, and returns the stable ID (`lionagi/state/db.py:1658`). Studio exposes `GET /api/artifacts/{artifact_id}` and `GET /api/artifacts/by-session/{session_id}` over those rows (`apps/studio/server/routers/artifacts.py:22`).

Those two behaviors are both valid, but they must not be conflated. Stable upsert is appropriate for structured skill outcomes where "the current verdict for this invocation" is the object being addressed. It is not appropriate for file captures that may later become evidence. If a file row is overwritten because a path was scanned again, the evidence chain loses the previous bytes, hash, size, and preview body. The governance direction makes that unacceptable: the platform is explicitly built around immutable evidence nodes, SHA-256 hash chains, artifact contracts, and run-end certificates (`docs/governance/direction.md:40`, `docs/governance/direction.md:50`).

The prior ADR-0053 draft failed because it kept file artifacts mutable by natural key while ADR-0055 independently introduced a different file schema and versioning model. This rewrite makes ADR-0053 the single owner of artifact storage. ADR-0055 may render, resolve, and stream artifacts, but it must consume this schema without adding columns.

The current Studio auth middleware also matters to this ADR because artifact persistence creates a sensitive read surface. When `LIONAGI_STUDIO_AUTH_TOKEN` is set, the existing middleware gates admin GET requests and mutating API methods, but non-admin artifact GET routes are not covered by that rule (`apps/studio/server/app.py:52`). Persisted artifacts may contain source code, credentials accidentally written by agents, customer data, or private model outputs. Artifact reads must therefore be authenticated in the same release that makes file bodies readable from Studio.

## Decision

Persist artifacts through one canonical `artifacts` row shape owned by this ADR. The table supports two insertion paths with different identity semantics:

1. `insert_artifact()` for structured outcomes keeps ADR-0021 stable-upsert behavior.
2. `insert_file_artifact()` for file captures is append-only. It never natural-key upserts and never mutates an existing file artifact row.

The filesystem remains the working medium and user export target. The database becomes the durable metadata, hash, preview, and evidence index. Large bodies live in content-addressed blob storage, referenced by a relative blob key.

### Canonical Artifact Schema

ADR-0053 owns these columns. Other ADRs must not redefine or extend the artifact row shape without superseding this ADR.

```sql
CREATE TABLE IF NOT EXISTS artifacts (
  id             TEXT    PRIMARY KEY,
  session_id     TEXT    REFERENCES sessions(id),
  invocation_id  TEXT    REFERENCES invocations(id) ON DELETE CASCADE,
  op_id          TEXT,
  kind           TEXT    NOT NULL,
  name           TEXT    NOT NULL,
  content        JSON    NOT NULL,
  file_path      TEXT,
  sha256         TEXT,
  size_bytes     INTEGER,
  media_type     TEXT,
  rel_path       TEXT,
  source_kind    TEXT    NOT NULL DEFAULT 'structured'
                  CHECK(source_kind IN ('structured', 'inline', 'blob', 'filesystem')),
  created_at     REAL    NOT NULL,
  updated_at     REAL    NOT NULL
);
```

Column meanings:

- `id`: opaque row ID. Evidence and certificates reference this value plus `sha256`.
- `session_id`: session/run owner when known.
- `invocation_id`: skill or orchestration invocation owner when known.
- `op_id`: Flow operation that produced a file capture, when known.
- `kind`: renderer and policy discriminator, for example `doc`, `research`, `diff`, `log`, `data`, `verdict`, or `unknown`.
- `name`: display and natural-key name. Structured outcomes keep their existing names. File captures use `{rel_path}@{sha256[:12]}`.
- `content`: JSON metadata and optional inline body.
- `file_path`: relative content-addressed blob key for blob-backed artifacts, never a host absolute path for new rows.
- `sha256`: SHA-256 of the captured file bytes.
- `size_bytes`: captured byte count.
- `media_type`: detected MIME type, for example `text/markdown` or `application/json`.
- `rel_path`: normalized POSIX artifact path relative to the run artifact root.
- `source_kind`: `structured` for ADR-0021 outcomes, `inline` for file rows with body in `content`, `blob` for file rows backed by `file_path`, and `filesystem` only for explicit legacy imports.
- `created_at`: row creation time.
- `updated_at`: equals `created_at` for file artifact rows; changes only for structured outcome upserts.

No `source_path` column is added. If provenance needs to record where a source file came from, it may place `project_rel_path` inside `content.source` only when the path can be expressed relative to the detected project root. Absolute paths are server-internal only and must not be stored in new artifact rows or returned by API responses.

### Identity And Immutability

File artifact rows are immutable. `insert_file_artifact()` always mints a new `id`, inserts a row, and returns that ID. It does not call `_find_artifact_id()`, does not update rows by `(session_id, kind, name)`, and does not treat repeated scans as idempotent. If the same file bytes are captured twice, the rows may share `rel_path`, `name`, `sha256`, and `file_path`; row identity still records that two captures happened.

The version display key for file artifacts is:

```python
name = f"{rel_path}@{sha256[:12]}"
```

The append-only invariant is the evidence property. A later evidence node or certificate can cite `(artifact_id, sha256, size_bytes)` without worrying that a path-based rescan replaced the row body.

Structured outcomes keep stable upsert semantics for compatibility with ADR-0021. `insert_artifact()` may update `content`, `file_path`, and `updated_at` for `source_kind='structured'` rows because those rows represent the current structured result, not a captured file version. Structured rows must not be used as immutable file evidence unless first copied through `insert_file_artifact()`.

### Indexes

The existing natural-key unique indexes must become structured-only. File artifact rows need lookup indexes, not uniqueness constraints.

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_struct_inv_only
  ON artifacts(invocation_id, kind, name)
  WHERE source_kind = 'structured'
    AND invocation_id IS NOT NULL
    AND session_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_struct_ses_only
  ON artifacts(session_id, kind, name)
  WHERE source_kind = 'structured'
    AND session_id IS NOT NULL
    AND invocation_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_struct_both
  ON artifacts(invocation_id, session_id, kind, name)
  WHERE source_kind = 'structured'
    AND invocation_id IS NOT NULL
    AND session_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_struct_unattached
  ON artifacts(kind, name)
  WHERE source_kind = 'structured'
    AND invocation_id IS NULL
    AND session_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_artifacts_session_rel_path_time
  ON artifacts(session_id, rel_path, created_at DESC)
  WHERE session_id IS NOT NULL AND rel_path IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_artifacts_invocation_rel_path_time
  ON artifacts(invocation_id, rel_path, created_at DESC)
  WHERE invocation_id IS NOT NULL AND rel_path IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_artifacts_op_id
  ON artifacts(op_id) WHERE op_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_artifacts_sha256
  ON artifacts(sha256) WHERE sha256 IS NOT NULL;
```

Migration must drop the old four natural-key indexes before creating the structured-only replacements. New file rows must not be blocked by a uniqueness constraint on `(session_id, kind, name)`.

### Python Interfaces

Add `lionagi/state/artifacts.py` for collection and payload construction:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

ArtifactKind = Literal[
    "code",
    "test",
    "doc",
    "research",
    "verdict",
    "synthesis",
    "config",
    "diff",
    "log",
    "data",
    "unknown",
]

SourceKind = Literal["structured", "inline", "blob", "filesystem"]

class ArtifactContent(TypedDict, total=False):
    body: str
    frontmatter: dict[str, Any]
    encoding: str
    line_count: int
    truncated: bool
    language: str
    source: dict[str, str]

@dataclass(frozen=True, slots=True)
class FileArtifactCapture:
    session_id: str
    invocation_id: str | None
    op_id: str | None
    kind: ArtifactKind
    name: str
    content: ArtifactContent
    file_path: str | None
    sha256: str
    size_bytes: int
    media_type: str
    rel_path: str
    source_kind: Literal["inline", "blob", "filesystem"]

@dataclass(frozen=True, slots=True)
class ArtifactPersistenceConfig:
    inline_max_bytes: int = 1_048_576
    capture_mode: Literal["off", "expected", "all"] = "all"
    blob_root: Path | None = None
    include_globs: tuple[str, ...] = ("**/*",)
    exclude_globs: tuple[str, ...] = (
        "**/.git/**",
        "**/__pycache__/**",
        "**/.DS_Store",
        "**/*.pyc",
    )

def build_file_artifact(
    path: Path,
    *,
    artifact_root: Path,
    project_root: Path | None,
    session_id: str,
    invocation_id: str | None,
    op_id: str | None,
    config: ArtifactPersistenceConfig,
) -> FileArtifactCapture:
    """Validate, hash, classify, inline or blob-route one regular file."""

def collect_file_artifacts(
    *,
    artifact_root: Path,
    project_root: Path | None,
    session_id: str,
    invocation_id: str | None,
    op_id: str | None,
    config: ArtifactPersistenceConfig,
    changed_paths: list[Path] | None = None,
) -> list[FileArtifactCapture]:
    """Return safe file captures under artifact_root."""
```

Extend `StateDB` and the ADR-0059 `StateStore` protocol with two explicit write paths:

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
) -> str:
    """Upsert one structured outcome and return its stable id."""

async def insert_file_artifact(
    self,
    *,
    session_id: str,
    invocation_id: str | None,
    op_id: str | None,
    kind: str,
    rel_path: str,
    content: dict[str, Any],
    file_path: str | None,
    sha256: str,
    size_bytes: int,
    media_type: str,
    source_kind: Literal["inline", "blob", "filesystem"],
) -> str:
    """Append one immutable file artifact row and return its new id."""
```

`insert_file_artifact()` validates `rel_path`, computes `name`, sets `created_at == updated_at`, and rejects absolute `file_path` values. Blob-backed rows store only a relative blob key. The blob root is resolved by server-side configuration and is not part of the public row contract.

### Orchestration Integration

Add a narrow helper around the live-persist context:

```python
async def persist_run_artifacts(
    env: OrchestrationEnv,
    *,
    op_id: str | None = None,
    changed_paths: list[Path] | None = None,
) -> list[str]:
    """Collect safe files under env.run.artifact_root and append file rows."""
```

`flow.py` should call this helper after `_record_result()` writes the operation Markdown file, after synthesis writes `synthesis.md`, and once during teardown as a catch-up scan. The helper is best-effort: failures are logged and do not override run status. ADR-0029 remains the authority for failing a run because required artifacts are missing.

The helper should use the open live-persist database connection when available. If live persistence is disabled, artifact DB capture is disabled for that run rather than opening an unrelated state connection with partial context.

## Implementation

### Phase 0: Schema Contract And Migration (180-260 LOC)

Files:

- `lionagi/state/schema.sql`: add canonical columns, structured-only unique indexes, and file lookup indexes. Estimated 60-90 LOC.
- `lionagi/state/db.py`: add migration columns, drop/replace old artifact natural-key indexes, and include new fields in row serialization. Estimated 90-130 LOC.
- ADR-0059 follow-up: mirror the same columns and method signatures in the `StateStore` protocol and Postgres DDL. Estimated 30-40 LOC in that ADR's implementation surface.

Exit criteria:

- Fresh SQLite databases contain exactly the ADR-0053 artifact columns.
- Existing databases open successfully and gain nullable columns.
- Old structured artifacts remain readable and keep stable upsert behavior.
- File rows are not constrained by the old natural-key unique indexes.

### Phase 1: Collector And Blob Routing (420-620 LOC)

Files:

- `lionagi/state/artifacts.py`: path validation, symlink containment, hashing, MIME detection, frontmatter parsing, inline thresholding, relative blob-key creation, and payload construction. Estimated 260-380 LOC.
- `tests/state/test_artifact_collector.py`: unit coverage for safe paths, excluded files, binary/text behavior, deterministic hashing, and project-relative provenance. Estimated 160-240 LOC.

Exit criteria:

- Absolute paths, parent traversal, symlink escapes, device files, sockets, directories, `.git`, and excluded files are not captured.
- Text files at or below the inline threshold get `content.body`.
- Large or binary files get a relative blob key and no body.
- No absolute host path appears in `content`, `file_path`, or serialized rows.

### Phase 2: StateDB Insert Paths (220-340 LOC)

Files:

- `lionagi/state/db.py`: implement `insert_file_artifact()`, keep `insert_artifact()` structured-only, and add lookup helpers used by Studio. Estimated 140-220 LOC.
- `tests/state/test_db_artifacts.py`: verify structured upsert, append-only file insertion, repeated same-content scans, timestamp semantics, and row serialization. Estimated 80-120 LOC.

Exit criteria:

- `insert_artifact()` updates a structured row and preserves its ID.
- `insert_file_artifact()` always inserts a new row.
- Repeated file captures with the same `rel_path` and `sha256` produce distinct IDs.
- File row `updated_at` equals `created_at`.

### Phase 3: Flow Capture (220-340 LOC)

Files:

- `lionagi/cli/orchestrate/_orchestration.py`: add `persist_run_artifacts()` and config loading. Estimated 90-140 LOC.
- `lionagi/cli/orchestrate/flow.py`: call persistence after operation result writes, synthesis writes, and final catch-up. Estimated 50-80 LOC.
- `tests/cli/test_flow_artifact_persistence.py`: run a small flow and assert disk file, DB row, hash, size, and inline body. Estimated 80-120 LOC.

Exit criteria:

- Flow still writes the same files to `artifact_root`.
- New runs append DB file artifact rows for operation Markdown and synthesis files.
- Capture failure logs a warning without masking ADR-0029 missing-artifact failures.
- `LIONAGI_ARTIFACT_CAPTURE_MODE=off` disables DB file capture.

### Phase 4: Backfill And Governance References (300-460 LOC)

Files:

- `lionagi/cli/state.py` or the existing state CLI surface: add `li state artifacts backfill --run <id>` and `--all`. Estimated 120-180 LOC.
- `lionagi/state/artifact_verifier.py`: include matching `artifact_id`, `sha256`, and `size_bytes` in produced artifact verification results. Estimated 60-90 LOC.
- Future evidence/certificate modules after ADR-0041/ADR-0042: consume artifact refs without changing this schema. Estimated 60-90 LOC.
- Tests for backfill and verifier linkage. Estimated 60-100 LOC.

Exit criteria:

- Backfill inserts append-only rows and never mutates historical rows.
- Artifact verification can cite durable artifact IDs for present files.
- Evidence code has stable inputs: `artifact_id`, `sha256`, and `size_bytes`.

## Security

Artifact persistence copies agent-produced data into durable storage, so the security floor is part of this ADR, not a later UI concern.

All artifact routes require bearer auth when `LIONAGI_STUDIO_AUTH_TOKEN` is set. This includes metadata, list, preview, raw/blob download, and reference-resolution routes. The current middleware's admin-GET and mutating-method rule is insufficient for artifacts because `GET /api/artifacts/{id}` exposes sensitive run data.

API responses must never expose host absolute paths. New rows store `rel_path` and relative blob keys only. Legacy absolute `file_path` values may exist from older structured artifacts; Studio must redact or omit them unless it can transform them into a safe public relative display path.

Path safety is mandatory during collection and backfill. The collector persists only regular files whose resolved path remains under `artifact_root`. It rejects absolute candidate paths, parent traversal, symlink escapes, NUL bytes, glob metacharacters where relevant, hidden control directories, and non-regular files. This follows the containment posture already used by `RunDir.agent_artifact_dir()` and Studio's path safety utilities.

Blob integrity is checked with SHA-256. Blob-backed reads must verify the blob bytes against `sha256`; mismatch is a corruption error, not a partial success. Integrity is not the same as tamper-proof storage: local users can still edit SQLite or blobs. ADR-0041 evidence chains and later certificate signing provide tamper-evident packaging. This ADR supplies immutable file rows and content hashes for those systems.

Secret detection is out of scope for Phase 0. Agents may write secrets or customer data into artifacts. Capture can be disabled or narrowed, but if artifact capture is enabled, the resulting DB and blob store must be treated as sensitive run output.

Availability is bounded by best-effort writes. Hashing and blob copy failures log warnings and preserve normal run finalization. Required-artifact failure remains owned by ADR-0029 verification.

## Migration

1. Add nullable columns for `op_id`, `sha256`, `size_bytes`, `media_type`, `rel_path`, and `source_kind`; add `updated_at` where missing.
2. Backfill `source_kind='structured'` for existing rows.
3. Drop the existing four natural-key unique indexes on all artifacts and recreate them with `source_kind='structured'` predicates.
4. Add file lookup indexes on `(session_id, rel_path, created_at DESC)`, `(invocation_id, rel_path, created_at DESC)`, `op_id`, and `sha256`.
5. Keep `insert_artifact()` compatible for ADR-0021 callers.
6. Add `insert_file_artifact()` and route all file captures through it.
7. New runs dual-write files to disk and append file rows to `artifacts`.
8. Historical runs are imported with an explicit backfill command. Backfill is append-only and may create multiple rows for the same path and hash.
9. Studio consumes this schema through ADR-0055 and must not add artifact columns.

## Alternatives Considered

### Alternative 1: Keep Files Filesystem-Only

This keeps implementation small and preserves current CLI behavior. It fails remote Studio, search, audit, and certificate use cases because evidence would point at mutable local paths. Rejected.

### Alternative 2: Upsert File Artifacts By Relative Path

This avoids duplicate rows and makes "latest by path" trivial. It destroys history when a file changes and contradicts the evidence model. Rejected.

### Alternative 3: Store Every File Body Inline

This simplifies reads but bloats `state.db`, increases WAL pressure, and makes logs or binary outputs expensive. Rejected in favor of inline previews plus content-addressed blobs.

### Alternative 4: Let ADR-0055 Own Viewer-Specific Columns

This was the prior cross-ADR failure mode. It gives implementers two schemas and makes Postgres parity unclear. Rejected. ADR-0053 owns storage; ADR-0055 owns routes and UI only.

## Consequences

- Artifact schema ownership is unambiguous.
- File captures become evidence-safe append-only rows.
- Structured outcome compatibility is preserved.
- Studio can render and stream artifacts without inventing storage columns.
- SQLite and Postgres implementations must both implement the same artifact contract through ADR-0059.
- Storage grows with each scan, but that is the cost of preserving evidence history; retention and cleanup policy must operate on explicit retention rules, not hidden mutation.
