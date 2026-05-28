# ADR-0055: Studio Artifact Viewer and File Reference Resolution

Status: proposed
Date: 2026-05-27
Decision owners: @governance-maintainers
Depends on: ADR-0053 (artifact persistence), ADR-0059 (StateStore protocol)
Related: ADR-0012 (Studio execution lineage), ADR-0021 (structured artifacts API), ADR-0027 (Studio architecture), ADR-0029 (artifact contracts), ADR-0056 (play control API)

## Context

Studio is the operator surface for governed orchestration. It already shows sessions, branches, messages, graph shape, invocation outcomes, and run status, but it does not reliably show the files produced by a run. That weakens the governance story: operators can see that work happened, but not inspect the work products that should feed evidence and task certificates.

The CLI writes useful artifacts today. `RunDir` separates authoritative state from user-facing artifacts (`lionagi/cli/_runs.py:70`), workers receive agent artifact directories in their operation context (`lionagi/cli/orchestrate/flow.py:1038`), operation outputs are written to `{agent_id}/{op_id}.md` (`lionagi/cli/orchestrate/flow.py:1113`), and synthesis writes `synthesis.md` (`lionagi/cli/orchestrate/flow.py:1601`). Live persistence stores `sessions.artifacts_path` and artifact contract JSON (`lionagi/cli/orchestrate/_orchestration.py:532`) and verifies required artifacts at teardown (`lionagi/cli/orchestrate/_orchestration.py:804`). These facts are enough to know where files should exist, but not enough for a remote or audited Studio viewer.

The current Studio artifacts API is intentionally narrow. It exposes structured artifact rows by ID and by session (`apps/studio/server/routers/artifacts.py:22`), serializes the current row fields from `apps/studio/server/services/invocations.py:113`, and the frontend type mirrors those fields (`apps/studio/frontend/lib/api.ts:462`). The run page separately extracts file-looking values from tool-call arguments (`apps/studio/frontend/app/runs/[id]/page.tsx:697`). Those paths are shown as text, not resolved into a DB-backed artifact.

ADR-0053 now owns the artifact schema and insertion semantics. This ADR does not add columns, change file identity, or define persistence. It consumes ADR-0053's canonical fields:

```text
id, session_id, invocation_id, op_id, kind, name, content, file_path,
sha256, size_bytes, media_type, rel_path, source_kind, created_at, updated_at
```

The prior ADR-0055 draft mixed viewer needs with storage decisions. It required `rel_path`, `media_type`, `source_kind`, and `insert_file_artifact()` while ADR-0053 still said file artifacts upserted by path. That split left implementers unable to tell which contract was authoritative. This rewrite makes the boundary explicit: ADR-0053 owns schema and writes; ADR-0055 owns authenticated API routes, content streaming, reference resolution, and frontend viewer behavior.

The security baseline is also non-optional. Current middleware requires the bearer token for admin GET routes and mutating API methods, but not all non-admin GETs (`apps/studio/server/app.py:52`). Artifact routes read sensitive content, including logs and arbitrary agent-written files. They must require bearer auth when `LIONAGI_STUDIO_AUTH_TOKEN` is set.

## Decision

Build a DB-first Studio artifact viewer over ADR-0053 artifacts. Studio reads artifact metadata and preview bodies from the state store, streams blob-backed content through authenticated routes, and resolves file references found in messages or manifests to artifact rows.

ADR-0055 adds no artifact table columns. It may add service functions, Pydantic API models, routes, frontend types, and viewer components. Any future storage-field change belongs in a revision of ADR-0053 and ADR-0059, not this ADR.

### Trust Model

Phase 0 Studio remains a single-admin surface protected by `LIONAGI_STUDIO_AUTH_TOKEN`. When the token is set, every artifact route requires `Authorization: Bearer <token>`, including GET routes.

The supported OSS configuration is a single-admin deployment protected by a bearer token. In this model, a valid token is treated as permission to read all artifacts visible to that Studio instance.

Multi-user Studio deployments require project/session authorization before enabling artifact reads. Until that authorization layer exists, multi-user deployments must either disable artifact routes or put Studio behind an equivalent authenticated administrative boundary.

### API Response Models

API models mirror ADR-0053 fields but do not expose absolute paths. `file_path` is returned only when it is a relative blob key or a safe public relative display path. Legacy absolute paths are omitted from responses and may still be used internally after containment checks.

```python
from typing import Any, Literal

from pydantic import BaseModel

SourceKind = Literal["structured", "inline", "blob", "filesystem"]

class ArtifactListItem(BaseModel):
    id: str
    session_id: str | None
    invocation_id: str | None
    op_id: str | None
    kind: str
    name: str
    rel_path: str | None
    file_path: str | None
    sha256: str | None
    size_bytes: int | None
    media_type: str | None
    source_kind: SourceKind
    created_at: float
    updated_at: float
    raw_url: str | None = None

class ArtifactContentResponse(ArtifactListItem):
    content: dict[str, Any] | None
    body: str | None
    preview_lines: list[str] | None
    truncated: bool

class FileReferenceResolution(BaseModel):
    status: Literal["resolved", "unavailable", "ambiguous", "forbidden"]
    reference: str
    normalized_reference: str | None = None
    artifact: ArtifactContentResponse | None = None
    candidates: list[ArtifactListItem] = []
    reason: str | None = None
```

### Backend Service Contract

Add `apps/studio/server/services/artifacts.py` as the only backend service that decides how artifact content is listed, previewed, streamed, and resolved:

```python
async def list_session_artifacts(
    session_id: str,
    *,
    include_content: bool = False,
) -> list[ArtifactListItem]:
    ...

async def list_play_artifacts(
    play_id: str,
    *,
    include_content: bool = False,
) -> list[ArtifactListItem]:
    ...

async def get_artifact_content(
    artifact_id: str,
    *,
    preview_lines: int,
    max_response_bytes: int,
) -> ArtifactContentResponse | None:
    ...

async def open_artifact_blob(
    artifact_id: str,
    *,
    disposition: Literal["attachment", "inline"] = "attachment",
) -> tuple[BinaryIO, str, str, int] | None:
    """Return stream, media_type, download_name, size_bytes."""

async def resolve_file_reference(
    *,
    reference: str,
    session_id: str | None = None,
    invocation_id: str | None = None,
    play_id: str | None = None,
    run_id: str | None = None,
) -> FileReferenceResolution:
    ...
```

The service uses the ADR-0059 state store. It must not query SQLite directly once the `StateStore` artifact methods exist.

### API Routes

Update `apps/studio/server/routers/artifacts.py` and add thin route handlers elsewhere only where the URL belongs to a parent resource:

```text
GET  /api/artifacts/{artifact_id}
GET  /api/artifacts/{artifact_id}/blob
GET  /api/sessions/{session_id}/artifacts
GET  /api/plays/{play_id}/artifacts
POST /api/artifact-references/resolve
```

Keep `GET /api/artifacts/by-session/{session_id}` for one release as a compatibility shim that delegates to `GET /api/sessions/{session_id}/artifacts`.

`GET /api/artifacts/{artifact_id}` returns metadata and bounded preview content. It does not inline unbounded file bodies. `GET /api/artifacts/{artifact_id}/blob` streams the captured bytes for inline and blob-backed file artifacts. Structured artifacts without file bytes return `404` for the blob route unless `content.body` is explicitly treated as downloadable text.

Raw/blob responses must set:

```text
Content-Type: <artifact.media_type or application/octet-stream>
Content-Disposition: attachment; filename="<safe name>"
X-Content-Type-Options: nosniff
Cache-Control: no-store
```

Inline display is allowed only for safe text media types when the client requests it, for example `?disposition=inline`. HTML, SVG, executable content, unknown binary content, and mismatched hashes must not be rendered inline. A SHA-256 mismatch returns `409 Conflict`.

Use `POST /api/artifact-references/resolve` rather than putting arbitrary paths in URL segments:

```json
{
  "reference": "r1/research.md",
  "session_id": "session-uuid",
  "invocation_id": null,
  "play_id": null,
  "run_id": null
}
```

### Reference Resolution

Resolution is deterministic and DB-first:

1. If `reference` is an artifact ID, return that artifact.
2. If `invocation_id` is known, match normalized `rel_path` within that invocation and return the newest row by `created_at`.
3. If `session_id` is known, match normalized `rel_path` within that session and return the newest row by `created_at`.
4. If `play_id` is known, resolve the play's `session_id`, then use session lookup.
5. If the reference is an absolute path, never open it directly. Convert it to a relative path only if it is under the session artifact root, configured project root, or an explicitly allowed local fallback root.
6. If filesystem fallback is disabled or the converted path is outside allowed roots, return `forbidden`.
7. If multiple DB rows are plausible and none is clearly newest within the requested scope, return `ambiguous` with candidates.
8. If no source can satisfy the reference, return `unavailable`.

Filesystem fallback is transitional and disabled by default. It exists only for old local runs without ADR-0053 rows. It never inserts rows implicitly and never returns absolute paths.

### Frontend Behavior

Add artifact-aware UI without changing the storage contract:

- `apps/studio/frontend/lib/api.ts`: add `ArtifactListItem`, `ArtifactContentResponse`, `FileReferenceResolution`, `getArtifactContent()`, `getArtifactBlobUrl()`, `listSessionArtifacts()`, `listPlayArtifacts()`, and `resolveFileReference()`.
- `apps/studio/frontend/components/artifacts/ArtifactLink.tsx`: wraps file-looking text and resolves on click or explicit hover intent.
- `apps/studio/frontend/components/artifacts/ArtifactViewer.tsx`: renders Markdown, Python, YAML, JSON, TOML, shell, plain text, and unified diff previews. Unknown or binary media shows metadata and a download action.
- `apps/studio/frontend/components/artifacts/ArtifactTimeline.tsx`: groups artifacts by `rel_path`, orders versions by `created_at`, and offers text diffs only when both bodies are available.
- `apps/studio/frontend/app/runs/[id]/page.tsx`: replace the current extracted-file text list with artifact links and a compact artifact timeline.
- `apps/studio/frontend/app/invocations/[id]/page.tsx`: keep structured outcome rendering and link attached artifact blobs where present.

Unavailable references render as inert labels with a short reason. Forbidden references do not echo absolute paths. Ambiguous references show a small chooser ordered newest first.

## Implementation

### Phase 0: Auth And API Foundations (220-340 LOC)

Files:

- `apps/studio/server/app.py`: ensure artifact routes require bearer auth for all methods when `LIONAGI_STUDIO_AUTH_TOKEN` is set, or add a shared route dependency applied to every artifact route. Estimated 40-70 LOC.
- `apps/studio/server/config.py`: add artifact preview, response-size, and fallback settings. Estimated 40-60 LOC.
- `apps/studio/server/services/artifacts.py`: define response models and path redaction helpers. Estimated 100-160 LOC.
- Tests for auth gating and response redaction. Estimated 40-50 LOC.

Exit criteria:

- All artifact routes return `401` without the bearer token when configured.
- Metadata responses omit legacy absolute paths.
- Settings default to DB-first and filesystem fallback disabled.

### Phase 1: Backend Listing, Preview, Blob Streaming, Resolution (520-780 LOC)

Files:

- `apps/studio/server/services/artifacts.py`: implement list, get, preview extraction, blob open, hash verification, and reference resolution. Estimated 300-460 LOC.
- `apps/studio/server/routers/artifacts.py`: implement artifact ID, blob, compatibility by-session, and resolve routes. Estimated 100-140 LOC.
- `apps/studio/server/routers/sessions.py` and play/show route surface: add session and play artifact list routes. Estimated 60-90 LOC.
- Backend tests for list, preview, blob, hash mismatch, ambiguous, unavailable, forbidden, compatibility route, and max-size behavior. Estimated 60-90 LOC.

Exit criteria:

- DB rows from ADR-0053 are listable and previewable.
- Blob route streams with correct `Content-Type` and `Content-Disposition`.
- SHA-256 mismatch returns `409`.
- Resolve never opens arbitrary absolute paths.

### Phase 2: Frontend Viewer MVP (620-940 LOC)

Files:

- `apps/studio/frontend/lib/api.ts`: artifact API types and client functions. Estimated 80-120 LOC.
- `apps/studio/frontend/components/artifacts/ArtifactLink.tsx`: link and resolve states. Estimated 120-180 LOC.
- `apps/studio/frontend/components/artifacts/ArtifactViewer.tsx`: preview, metadata, download controls, and error states. Estimated 220-340 LOC.
- `apps/studio/frontend/app/runs/[id]/page.tsx` and invocation page integration. Estimated 120-180 LOC.
- Component tests for resolved, unavailable, forbidden, ambiguous, text preview, and binary download states. Estimated 80-120 LOC.

Exit criteria:

- Run pages expose clickable artifacts without leaving Studio.
- Existing structured outcome cards still render.
- Long text and binary files do not break layout or inline response limits.

### Phase 3: Timeline And Diffs (320-520 LOC)

Files:

- `ArtifactTimeline.tsx`: grouping by `rel_path`, version order, operation metadata display, and adjacent diff affordance. Estimated 160-260 LOC.
- Server or client diff helper for bounded text artifacts. Estimated 80-140 LOC.
- Tests for grouping, ordering, and missing-body diff states. Estimated 80-120 LOC.

Exit criteria:

- Multiple immutable file rows for one path are visible as versions.
- Diff is offered only when both versions have bounded text bodies.
- Timeline behavior relies on `rel_path` and `created_at`, not on schema additions.

### Phase 4: Hardening And Migration Support (360-560 LOC)

Files:

- Backend tests for local fallback disabled/enabled, symlink escapes, URL-decoded traversal, cache headers, and no absolute path echo. Estimated 140-220 LOC.
- Playwright smoke test on a seeded run with inline and blob artifacts. Estimated 80-120 LOC.
- Admin health counters or diagnostics for missing blobs, hash mismatches, unavailable references, and fallback usage. Estimated 100-160 LOC.
- Documentation updates for Studio deployment auth expectations. Estimated 40-60 LOC.

Exit criteria:

- Artifact viewer passes security regression tests.
- Local fallback is visibly off by default and auditable when enabled.
- Seeded run pages load artifact previews and downloads through the authenticated API.

## Security

All artifact routes require bearer auth when `LIONAGI_STUDIO_AUTH_TOKEN` is set. This includes `GET /api/artifacts/{id}`, `GET /api/artifacts/{id}/blob`, session and play listing routes, the by-session compatibility route, and `POST /api/artifact-references/resolve`.

No artifact API response may expose host absolute paths. `rel_path` is the primary UI label. `file_path` is returned only as a relative blob key or safe public relative path. Forbidden and unavailable responses must not include the rejected absolute path in `reason`.

Blob responses set `Content-Type`, `Content-Disposition`, `X-Content-Type-Options: nosniff`, and `Cache-Control: no-store`. Unknown, binary, HTML, SVG, and executable content defaults to attachment. Inline display is limited to safe text media types and bounded previews.

The service verifies `sha256` for blob-backed reads. A mismatch returns `409 Conflict` and should be surfaced in admin diagnostics.

Filesystem fallback is disabled by default and is not part of the production evidence path. If enabled for local migration, it may read only under configured allowed roots and the session artifact root. It rejects parent traversal, NUL bytes, glob metacharacters, directories, symlink escapes, and absolute references that cannot be converted to an allowed relative path.

The viewer does not redact content. Artifact producers remain responsible for not writing secrets; Studio is responsible for not broadening access and not caching sensitive reads.

Multi-user Studio deployments need resource-level authorization before artifact routes are enabled. The Phase 0 single-admin bearer-token model is the supported OSS configuration.

## Migration

1. Implement ADR-0053 first so new runs write canonical artifact rows.
2. Add authenticated Studio routes over the ADR-0053 schema. Do not add artifact columns in this ADR.
3. Preserve existing `/api/artifacts/{id}` and `/api/artifacts/by-session/{session_id}` for one release, adding fields where available.
4. Add session and play artifact list routes, then move frontend callers to those routes.
5. Replace run-page path text with `ArtifactLink` resolution. Existing file-looking strings remain visible when resolution is unavailable.
6. Keep filesystem fallback disabled by default. Enable it only for local migration after explicit configuration.
7. Use ADR-0053 backfill to make old runs DB-visible. Studio does not implicitly backfill during reads.
8. Deprecate the by-session compatibility route after frontend and documented consumers use `/api/sessions/{session_id}/artifacts`.

## Alternatives Considered

### Alternative 1: Serve Files Directly From `artifact_root`

This is quick for local runs but makes Studio depend on host filesystem layout, leaks absolute paths, and does not support remote runs or evidence references. Rejected.

### Alternative 2: Let The Viewer Define Extra Columns

This was the prior schema-conflict failure. Viewer-specific storage columns would drift from ADR-0053 and ADR-0059. Rejected.

### Alternative 3: Frontend-Only Path Detection

Copy buttons or local editor links improve convenience but do not solve authenticated content reads, remote execution, hash verification, or version timelines. Rejected.

### Alternative 4: DB-Only With No Local Fallback

This is the clean steady state. It is too abrupt during the migration window because old local runs have only filesystem artifacts. Accepted as the long-term target, with disabled-by-default fallback for explicit local migration.

## Consequences

- Studio gains a real artifact viewer without owning storage.
- Artifact rows remain canonical under ADR-0053 and backend-neutral under ADR-0059.
- All artifact reads become authenticated sensitive reads.
- Blob downloads have predictable content headers and hash verification.
- File references in messages can resolve to durable artifact rows instead of dead text.
- The UI can show file versions because ADR-0053 file rows are append-only.
