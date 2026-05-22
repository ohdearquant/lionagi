# Studio Features

This page walks through each section of Lion Studio. All API paths and service references below trace to actual source files.

## Dashboard (`/`)

The dashboard (`frontend/app/page.tsx:58`) calls `GET /api/stats` and displays aggregate counts for the entire state database.

Counts shown (`services/stats.py:20`):

- Messages, progressions, sessions, branches
- Definitions (agent + playbook)
- Shows, plays

Links on the dashboard navigate directly to the Runs, Playbooks, Agents, Shows, and Admin pages.

---

## Runs (`/runs`, `/runs/{id}`)

> Backend: `routers/runs.py` + `services/runs.py` (522 lines)

Runs are the top-level execution units — each `li agent` or `li o flow` invocation creates a run. The Runs page (`frontend/app/runs/page.tsx:539`) lists run summaries with status filtering and session/invocation grouping.

### Run lifecycle

Runs read session state from `~/.lionagi/state.db`. Status is normalised from raw values through a status alias table (`services/runs.py:14–24`):

| Canonical status | Raw aliases |
|---|---|
| `done` | `done`, `completed`, `success`, `finished` |
| `cancelled` | `cancelled`, `canceled` |
| `aborted` | `aborted`, `aborted_after_finish` |
| `timed_out` | `timed_out`, `timeout` |
| `pending` | `pending`, `prepared` |

### List endpoint

```
GET /api/runs/?page=1&per_page=20&status=running&playbook=my-worker
```

Supports pagination (`page`, `per_page`) and repeated `status` and `playbook` filter parameters.

### Run detail

```
GET /api/runs/{run_id}
```

Returns the full run manifest including branches, step count, and timing. The run detail page (`frontend/app/runs/[id]/page.tsx:497`) shows: overview, branches, errors, and file artifacts. Session messages stream live via SSE (see [Sessions](#sessions-apisessions)).

!!! note "ADR reference"
    Run list design is documented in ADR-0015. Run step provenance (how sessions link to runs) is in ADR-0022.

---

## Sessions (`/api/sessions/`)

> Backend: `routers/sessions.py` + `services/sessions.py`

Sessions are the SQLite-backed record of a `li agent` or `li play` execution. Each session has branches; each branch has messages.

### Live streaming

```
GET /api/sessions/{session_id}/stream
```

This is an SSE endpoint. The response is a stream of newline-delimited JSON events:

- `data: {"type": "message", ...}` — new message appended to a branch
- `data: {"type": "heartbeat"}` — keep-alive every few seconds
- `data: {"type": "done"}` — session closed

The run detail page (`/runs/[id]`) uses this stream to show messages as they arrive.

!!! note "ADR reference"
    SSE streaming design is in ADR-0006.

---

## Shows (`/shows`, `/shows/{topic}`)

> Backend: `routers/shows.py` + `services/shows.py` (76 lines)

Shows are multi-play DAGs orchestrated by the `show` skill. Each show has a `topic` and contains one or more plays. The data model is a hybrid: SQLite stores structural state (play status, session foreign keys); the filesystem holds authored markdown (`_show.md`, `_intent.md`, `_prompt.md`).

The show detail page (`frontend/app/shows/[topic]/page.tsx:60`) renders:

- A `PlayDag` component — ReactFlow DAG of all plays with their statuses
- A summary panel with show-level metadata
- Live file change stream (see below)

### Importing shows

Shows that exist only on disk must be imported into SQLite before they appear in the list:

```
POST /api/shows/import
```

### Live show stream

```
GET /api/shows/{topic}/stream
```

SSE stream of filesystem changes under the show directory. The frontend polls this to refresh the play DAG as plays complete.

!!! note "ADR reference"
    Shows data model (hybrid SQLite + filesystem) is in ADR-0011.

---

## Agents (`/agents`, `/agents/{name}`)

> Backend: `routers/agents.py` + `services/agents.py` (51 lines)

Agent profiles are markdown files with YAML frontmatter stored under `~/.lionagi/agents/`. Studio lets you browse, edit, and validate them.

### Agent profile format

Each agent file has frontmatter fields including `name`, `provider`, `model`, `effort`, and `permission`. The service normalises legacy `reasoning_effort` to `effort` on read.

### Editing

```
PUT /api/agents/{name}
```

Updates the agent profile on disk. Requires `Authorization` header when `LIONAGI_STUDIO_AUTH_TOKEN` is set.

### Validation

```
POST /api/agents/{name}/validate
```

Validates the `name`, `provider`, and `model` fields of a payload without writing to disk. Returns validation errors as structured JSON.

!!! note "Create and delete stubs"
    `POST /api/agents/{name}` (create) and `DELETE /api/agents/{name}` return HTTP 501. These are placeholders; agent creation is handled through definitions versioning (`/api/definitions/`).

---

## Definitions & Versioning (`/api/definitions/`)

> Backend: `routers/definitions.py` + `services/definitions.py`

The definitions API provides versioned storage for agent and playbook files. Both kinds are stored under `~/.lionagi/{agents,playbooks}/` on disk; version metadata is persisted in the SQLite `definitions` table.

### Endpoints

| Endpoint | Description |
|---|---|
| `GET /api/definitions/?kind=agent` | List current definitions; optional `kind` filter (`agent` or `playbook`) |
| `GET /api/definitions/{kind}/{name}` | Current content + version list |
| `GET /api/definitions/{kind}/{name}/versions/{version}` | Historical version content |
| `POST /api/definitions/{kind}/{name}` | Save new version with optional commit message |
| `POST /api/definitions/{kind}/{name}/rollback?version=3` | Roll back to a previous version |
| `POST /api/definitions/snapshot` | Snapshot all current disk files into the DB; optional `kind` filter |

### Concurrency safety

Each `(kind, name)` pair is protected by an `asyncio.Lock` (`services/definitions.py:21–28`). The lock spans both the DB write and the disk write, preventing races when concurrent requests save the same definition.

!!! note "ADR reference"
    Definitions write path and race-condition fix are in ADR-0016.

---

## Playbooks (`/playbooks`, `/playbooks/{name}`)

> Backend: `routers/playbooks.py` + `services/playbooks.py` (101 lines)

Playbooks are YAML workflow definitions stored under `~/.lionagi/playbooks/`. Studio supports two playbook formats:

| Format | Editor component | Description |
|---|---|---|
| Declarative YAML | `DeclarativePlaybookForm` | Linear step-by-step playbook |
| Graph DAG | `GraphPlaybookEditor` + `WorkerCanvas` | ReactFlow DAG with conditional edges |

The edit page (`/playbooks/[name]/edit`) auto-detects the format and renders the appropriate editor.

### WorkerCanvas

`WorkerCanvas` (`frontend/components/canvas/WorkerCanvas.tsx:123`) is a ReactFlow-based DAG editor with:

- Auto-layout via `dagre` (`components/canvas/useLayout.ts`)
- Condition edges (`ConditionEdge` component)
- Step node editing via `StepEditor` and `LinkEditor`
- Side panel for node/edge/execution details

### Validation

```
POST /api/playbooks/{name}/validate
```

Validates playbook YAML structure and returns errors without writing.

!!! note "Run stub"
    `POST /api/playbooks/{name}/run` returns HTTP 501. Playbook execution is triggered from the CLI (`li play`), not the Studio UI.

---

## Skills (`/skills`, `/skills/{name}`)

> Backend: `routers/skills.py` + `services/skills.py` (46 lines)

Skills are markdown files with YAML frontmatter under `~/.lionagi/skills/`. Each skill has a `name`, `description`, and body content describing how to invoke it.

The Skills page lists all skills with their names and descriptions. Clicking a skill shows the full markdown rendered via the `Markdown` component (`react-markdown` + `remark-gfm`).

Skills are read-only in Studio — authoring is done on the filesystem.

!!! note "ADR reference"
    Skill invocation tracking (how skill runs are linked to sessions) is in ADR-0020.

---

## Invocations (`/invocations`, `/invocations/{id}`)

> Backend: `routers/invocations.py` + `services/invocations.py`

Invocations represent the top-level triggering event for a skill — the causal layer above sessions. A single `/show` invocation can produce 14+ sessions; the invocations page connects them.

### List

```
GET /api/invocations/?skill=show&status=running&limit=50&offset=0
```

Filters by `skill` name and `status`. Returns paginated invocation summaries.

### Detail

```
GET /api/invocations/{invocation_id}
```

Returns the invocation record with its child sessions and artifacts. The detail page links out to individual runs and session streams.

!!! note "ADR reference"
    Invocations data model is in ADR-0020.

---

## Plugins / Marketplace (`/plugins`)

> Backend: `routers/plugins.py` + `services/plugins.py` (286 lines)

Studio scans two plugin sources:

| Source | Path | Notes |
|---|---|---|
| Marketplace (built-in) | `marketplace/` in the repo root | Versioned with lionagi; includes `devx`, `show`, `orchestrate`, `hookify`, etc. |
| Third-party cache | `~/.claude/plugins/cache/` | Installed by users via Claude Code |

The Plugins page (`frontend/app/plugins/page.tsx:472`) shows:

- Plugin list with description and metadata
- Skills subpane — lists skills bundled with each plugin
- Agents subpane — lists agent profiles bundled with each plugin

### Endpoints

```
GET /api/plugins                              # list all plugins
GET /api/plugins/{name}                       # plugin detail
GET /api/plugins/{plugin_name}/skills/{skill_name}   # skill content
GET /api/plugins/{plugin_name}/agents/{agent_name}   # agent content
```

!!! note "ADR reference"
    Plugin discovery and marketplace design are in ADR-0003 (marketplace), ADR-0007 (auto-discovery), and ADR-0010 (plugin-aware Studio UI).

---

## Teams (`/teams`, `/teams/{id}`)

> Backend: `routers/teams.py` + `services/teams.py` (21 lines)

Teams are JSON files under `~/.lionagi/teams/` written by the `li team` coordination commands. Studio provides a read-only view.

```
GET /api/teams/?limit=50&offset=0    # paginated list
GET /api/teams/{team_id}             # team detail JSON
```

The team detail page shows the raw team JSON and a message timeline view.

---

## Artifacts (`/api/artifacts/`)

> Backend: `routers/artifacts.py` (19 lines) + `services/invocations.py`

Artifacts are structured output files produced by agent runs (CI results, review verdicts, gate verdicts). Studio renders them with typed renderers:

| Artifact kind | Renderer component |
|---|---|
| CI result | `CIResultCard` |
| Gate verdict | `GateVerdictCard` |
| Review verdict | `ReviewVerdictCard` |
| Unknown | `OutcomeRenderer` fallback |

```
GET /api/artifacts/{artifact_id}
GET /api/artifacts/by-session/{session_id}
```

---

## Admin (`/admin`)

> Backend: `routers/admin.py` + `services/admin.py` (136 lines)

!!! warning "Auth required"
    All `/api/admin/*` endpoints require `Authorization: Bearer <token>` regardless of HTTP method, even GET requests. This is enforced in `app.py:44–46`.

### Doctor

```
GET /api/admin/doctor?stale_hours=1
```

Scans all sessions with `status='running'` and classifies them as phantom when:

- The associated process PID is dead (checked via `os.kill(pid, 0)`)
- Artifact files are missing (`missing_artifacts`)
- No activity for `stale_hours` hours (`stale_lock`)

Returns a list of phantom sessions with their reason codes (`process_dead`, `missing_artifacts`, `stale_lock`).

### Health

```
GET /api/admin/health
```

Composite health report: session health summary plus DB file size and WAL size (`_db_health()` in `services/admin.py`).

### Transition

```
POST /api/admin/transition
```

Force-transitions running sessions to `failed`, `aborted`, or `cancelled`. Requires `reason` and `actor` in the request body.

### Prune

```
POST /api/admin/prune
```

Prune phantom sessions. Pass explicit `session_ids` to prune specific sessions, or omit to prune all detected phantoms.

### Events

```
GET /api/admin/events?action=prune&target_id=abc&limit=100
```

Admin event log. Queryable by `action`, `target_id`, and `limit`.

!!! note "ADR reference"
    Session health monitoring and the admin surface are documented in ADR-0024. Session status vocabulary (`running`, `completed`, `failed`, `aborted`, `cancelled`) is in ADR-0025.
