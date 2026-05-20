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

### Sync and drift

SQLite and filesystem can drift if a write to one fails before the other completes.
Mitigation: `li state import` and `li state import-shows` re-sync from filesystem
into SQLite at any time. Filesystem is recoverable source; SQLite is the query cache.

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
