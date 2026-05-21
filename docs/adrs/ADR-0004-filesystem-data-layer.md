# ADR-0004: Data Layer — Filesystem + SQLite Hybrid

**Status**: Accepted
**Date**: 2026-05-19 (revised 2026-05-20)

## Context

Lion Studio's backend serves runs, agents, playbooks, plugins, shows, and sessions.
Data exists in two forms: authored files on the local filesystem (agent definitions,
playbook YAML, show plans, skill markdown) and operational state in SQLite (sessions,
branches, messages, shows, plays).

The original design read everything from filesystem. As the product evolved, SQLite
became necessary for live monitoring, cross-session queries, and execution lineage
(see ADR-0009, ADR-0011, ADR-0012).

## Decision

Lion Studio uses a **hybrid data layer**: filesystem for authored definitions and
content, SQLite for operational/query state.

### Data authority matrix

| Data | Location | Why |
|------|----------|-----|
| Agent definitions (`*.md`) | Filesystem `~/.lionagi/agents/` | Edited by humans, git-versioned, read by CLI/Claude Code |
| Playbook definitions (`*.yaml`) | Filesystem `~/.lionagi/playbooks/` | Same — authored content |
| Skill content (`SKILL.md`) | Filesystem (plugin dirs) | Read-only from disk, part of plugin packages |
| Plugin structure | Filesystem (marketplace + cache) | Scanned by plugin discovery |
| Show plans (`_show.md`, `_intent.md`) | Filesystem `~/khive-work/shows/` | Authored markdown, git-versioned, edited mid-show |
| Artifacts (agent output files) | Filesystem (play dirs, worktrees) | Binary/large files, git-tracked |
| Sessions, branches, messages | SQLite `~/.lionagi/state.db` | Queryable, FK-linked, live-updatable |
| Shows, plays (structural state) | SQLite `~/.lionagi/state.db` | Queryable, cross-referenced to sessions |
| Definition versions (edit history) | SQLite `definitions` table | Disk is source of truth; SQLite tracks edit history |

### Route mapping

| Route | Data source |
|-------|-------------|
| `/api/runs` | SQLite `sessions` (enriched with provenance) |
| `/api/sessions/{id}` | SQLite sessions + branches + messages |
| `/api/agents` | Filesystem scan + definitions API for versions |
| `/api/playbooks` | Filesystem scan + definitions API for versions |
| `/api/plugins` | Filesystem scan (marketplace + third-party cache) |
| `/api/shows` | SQLite `shows` + `plays`, filesystem fallback |
| `/api/shows/{topic}` | SQLite + filesystem (`_show.md`, `_intent.md`) |
| `/api/stats` | Mixed — sessions from SQLite, definitions from filesystem |

Note: the browser route is `/runs/{id}` (user-facing 'Runs' label per ADR-0012), while the API route is `/api/sessions/{id}` (matching the SQLite table). The frontend API client translates between these.

### Run persistence: SQLite only (no JSON snapshot)

New runs write sessions, branches, and messages to SQLite via live hooks during
execution. The CLI no longer writes a post-run `run.json` manifest or
`branches/*.json` snapshot to `~/.lionagi/runs/`. Rationale: aiosqlite is now a
mandatory dependency (not optional), and the live hooks provide richer data than
the end-of-run JSON dump (e.g., messages appear as they're produced, not after
the run completes).

> **Stream artifacts are a narrow exception.** `stream_persist=True` writes
> `~/.lionagi/runs/<id>/stream/<branch>.json` plus a `<branch>.buffer.jsonl`
> incremental chunk log. These are **not** canonical state — SQLite is — they
> exist as a transient resume/debug artifact for the streaming providers (the
> JSON file is the last-known branch snapshot; the JSONL buffer is the
> chunk-by-chunk stream). Downstream tooling MUST NOT treat them as the
> source of truth, and Studio query routes MUST NOT read them.

Historical `~/.lionagi/runs/` JSON directories remain on disk as a read-only
archive. `li state import` brings them into SQLite for querying.

> **Studio query rewiring — current state.** As of the feat/studio-apps-audit
> PR (commit eaf3f3c28), the Studio API routes have been rewired as follows:
>
> | Route | Backing | Notes |
> |-------|---------|-------|
> | `GET /api/runs` (list) | SQLite `sessions` table | `services/runs.py list_runs()` reads SQLite via `list_sessions()` |
> | `GET /api/runs/{id}` (detail) | **Filesystem** `RUNS_ROOT/<id>/run.json` | Still filesystem-backed; see design note below |
> | `GET /api/sessions/{id}` | SQLite sessions + branches + messages | `services/sessions.py list_sessions()` |
> | `GET /api/shows` (list) | SQLite `shows` + `plays`, filesystem fallback | `services/shows.py _list_shows_db()`, falls back to directory scan if DB empty |
> | `/api/runs/{id}/events` | **REMOVED** | This SSE route was removed in the same PR; the session stream (`/api/sessions/{id}/stream`) is the correct live-data path |
>
> **Design note — `GET /api/runs/{id}` remains filesystem-backed**: the run
> detail endpoint reads `RUNS_ROOT/<id>/run.json` for the full manifest. New
> runs do not write `run.json` (SQLite-only persistence), so this endpoint
> only serves historical filesystem-originated runs. Whether to rewire the
> detail endpoint to SQLite (and what to return for SQLite-only sessions
> lacking a `run.json`) is an open design question tracked separately.
> Until then, the detail page for new runs may 404 or return partial data.

For human-readable export, use `li state export <session-id> --format json`
(future command).

### Sync and drift

For authored content (agents, playbooks, show plans), filesystem is canonical
and SQLite tracks edit history. The two can drift if a write fails mid-operation.
Mitigation: `li state import` and `li state import-shows` re-sync from filesystem
into SQLite at any time.

## Consequences

**Positive**
- Authored content stays in git-friendly files editable by any tool.
- Operational queries are fast (SQLite indexes, JOINs, aggregates).
- No external database process — SQLite is embedded.
- CLI and Studio share the same data without coordination protocol.

**Negative**
- Two data sources means two things to keep in sync.
- Import commands needed for historical data migration.
- Contributors must know which data lives where.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|-------------|
| Filesystem only | Cannot support live monitoring, cross-session queries, or execution lineage |
| SQLite only | Agent/playbook definitions are authored markdown/YAML — git versioning and editor access matter more than query performance on content |
| External database (Postgres, etc.) | Overkill for single-user local workload; adds ops burden |
