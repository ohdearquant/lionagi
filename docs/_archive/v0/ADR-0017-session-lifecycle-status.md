# ADR-0017: Session Lifecycle and Status Derivation

**Status**: Partially superseded by [ADR-0033](ADR-0033-unified-entity-state-model.md) — see Supersession Notice
**Date**: 2026-05-20
**Extends**: ADR-0009 (SQLite state layer), ADR-0012 (execution lineage)

---

> **Supersession notice**: [ADR-0033](ADR-0033-unified-entity-state-model.md) supersedes ADR-0017's "Status vocabulary" section (replaced by the unified `NormalizedState.lifecycle` axis) and the single-axis status derivation. ADR-0017's lifecycle enum is preserved as one of three orthogonal axes in NormalizedState (lifecycle × health × delivery). Read this ADR for historical context; treat [ADR-0033](ADR-0033-unified-entity-state-model.md) as authoritative for status semantics going forward.

---

## Context

The `sessions` table (ADR-0009) stores identity, metadata, progression references,
and provenance hints (ADR-0012). But it has no lifecycle columns — no `status`,
no `started_at`, no `ended_at`. Yet the UI depends on session lifecycle:

- **Runs list** (ADR-0015) requires a Status column per row.
- **Dashboard** (ADR-0012 §10) requires cards for running, failed, slow, needs-review.
- **Display mapping** (ADR-0012 §3) translates raw statuses to UI vocabulary — but
  doesn't specify where the raw status *comes from* for sessions.

For show-play sessions, status can be derived from `plays.status` via the
`plays.session_id` FK. For standalone sessions (`li agent`, `li play` without
show context), there is no external status source.

Without a lifecycle contract, implementers must invent derivation logic — leading
to inconsistent status computation across the runs list, dashboard, and detail page.

## Decision

### Add lifecycle columns to sessions

```sql
ALTER TABLE sessions ADD COLUMN status     TEXT;  -- NULL for existing rows
  -- running|completed|failed|aborted
ALTER TABLE sessions ADD COLUMN started_at REAL;
ALTER TABLE sessions ADD COLUMN ended_at   REAL;

-- Backfill: existing sessions are historical (all completed or imported).
-- New sessions get status='running' at INSERT time from the CLI.
UPDATE sessions SET status = 'completed'
  WHERE status IS NULL AND source_kind = 'imported_fs';
UPDATE sessions SET status = 'completed'
  WHERE status IS NULL;
```

Migration: these columns are part of the collapsed v1 schema (see ADR-0009
Migration Protocol). For a pre-release `state.db` that pre-dates these
columns, `StateDB._reconcile_columns()` `ALTER TABLE ADD COLUMN`s them on
open; existing rows therefore have `status IS NULL` and the conservative
backfill statements above apply. New sessions get `status='running'` at
INSERT time from the CLI, not from a column DEFAULT.

### Status vocabulary (sessions)

Sessions use a minimal lifecycle — four terminal-capable states:

| Status | Meaning | Set by |
|--------|---------|--------|
| `running` | Session is active, branch(es) in progress | CLI at session creation |
| `completed` | Session finished normally | CLI at session close (exit code 0) |
| `failed` | Session terminated with error | CLI at session close (exit code != 0) |
| `aborted` | Session was interrupted or cancelled | CLI on SIGINT/SIGTERM or user abort |

This is deliberately simpler than the play status vocabulary (ADR-0011 has 11 play
statuses). Sessions don't need gate/merge/redo states — those belong to the play
layer. A show-play session is just `completed` or `failed`; the richer lifecycle
lives on `plays.status`.

### Write points

| Event | Who writes | What changes |
|-------|-----------|--------------|
| `li agent` / `li play` start | CLI session init | INSERT session with `status='running'`, `started_at=now()` |
| Session close (success) | CLI session finalize | UPDATE `status='completed'`, `ended_at=now()` |
| Session close (error) | CLI session finalize | UPDATE `status='failed'`, `ended_at=now()` |
| Session interrupt | CLI signal handler | UPDATE `status='aborted'`, `ended_at=now()` |
| `li state import` | Import command | INSERT with status derived from run.json manifest |
| `li state prune` | Operator | DELETE old sessions (cascades branches); see ADR-0009 §"Operational commands". |
| Show play links session | Show skill Step 3 | Session already created by `li play`; play links via `plays.session_id` |

### Import status derivation

For filesystem imports (`source_kind='imported_fs'`), status is derived from:

1. If `run.json` has `"status"` field → use it (mapped to session vocabulary).
2. If `run.json` has `"exit_code": 0` → `completed`.
3. If `run.json` has `"exit_code"` != 0 → `failed`.
4. If neither → `completed` (conservative default for legacy runs that finished
   writing `run.json`).

`started_at` and `ended_at` come from `run.json` timestamps or filesystem
`ctime`/`mtime` as fallback.

### Duration computation

Duration is computed, not stored:

```text
duration_ms = (ended_at - started_at) * 1000   -- if both present
duration_ms = NULL                               -- if session still running
```

The API returns `duration_ms` as a computed field. No `duration_ms` column.

### Dashboard status queries

With an explicit `status` column, dashboard queries become simple aggregates:

```sql
-- Running sessions
SELECT COUNT(*) FROM sessions WHERE status = 'running';

-- Failed sessions (last 24h)
SELECT COUNT(*) FROM sessions WHERE status = 'failed'
  AND ended_at > unixepoch() - 86400;

-- Slow sessions (running > 30 min)
SELECT COUNT(*) FROM sessions WHERE status = 'running'
  AND started_at < unixepoch() - 1800;

-- Needs review (sessions linked to gated/escalated/blocked plays)
SELECT COUNT(DISTINCT s.id) FROM sessions s
  JOIN plays p ON p.session_id = s.id
  WHERE p.status IN ('gated', 'escalated', 'blocked');
```

### Relationship to play status

For sessions created by show plays, both the session and the play have status.
They are independent:

- **Session status**: did the CLI process complete? (`completed` / `failed`)
- **Play status**: what happened in the show lifecycle? (`running_complete` →
  `gated` → `merged` or `gate_failed` → `redoing`)

A session can be `completed` while its play is `gate_failed` — the CLI process
succeeded, but the gate reviewer rejected the output. The session status answers
"did it run?" The play status answers "was the output accepted?"

The display mapping (ADR-0012 §3) applies to sessions on the runs list. Play
status uses the richer vocabulary on the shows detail page (ADR-0011).

### "Completed with errors" — no separate status

Per ADR-0012 §3, tool errors are diagnostic, not status-changing. A session with
intermediate tool failures is `completed`, not `completed_with_errors`. Error
counts are surfaced on the run detail page, not in the session status.

Error counts are NOT precomputed on the sessions table. Computing
`COUNT(*) FROM messages WHERE role='tool' AND content LIKE '%error%'` is
expensive at list-query time. Instead:

- **Runs list**: all completed sessions show green `completed` pill. No error
  distinction until error counts are precomputed (deferred optimization).
- **Run detail**: error count computed on page load from the session's messages.
- **Dashboard**: intermediate tool errors do not feed any dashboard card.

### Pruning gaps (deliberate, today)

`li state prune` deletes session rows; ON DELETE CASCADE drops branches.
Two layers are NOT yet cleaned up:

1. **Orphan progressions.** `sessions.progression_id` and
   `branches.progression_id` reference `progressions(id)` without
   `ON DELETE CASCADE`, so the progression rows survive the parent
   delete. The orphan-message sweep (`DELETE FROM messages WHERE id
   NOT IN (SELECT value FROM progressions, json_each(...))`) therefore
   still sees those messages as referenced and leaves them in place.
2. **Plays still referencing deleted sessions.** `plays.session_id`
   has no cascade and no SET NULL — SQLite REJECTS a delete of a
   session that a play still references. This protects play history
   from dangling pointers (ADR-0012) but means show-play sessions
   can only be pruned after their owning show is pruned too.

These are not bugs — they're conservative behavior pending an
explicit decision on what cleanup the operator wants. A future
`li state prune --orphan-progressions` would close (1) by sweeping
progression rows referenced only by deleted sessions and then
re-running the message sweep.

## Consequences

**Positive**

- Runs list and dashboard can query session status directly — no derivation logic.
- Four-status vocabulary is simple and unambiguous.
- Duration is computable from two timestamps without a stored column.
- Import status derivation is well-defined for legacy filesystem runs.
- Clean separation: session status = "did it run?", play status = "was output accepted?"

**Negative**

- Three new columns on the sessions table (part of the collapsed v1 schema; reconciled into pre-release DBs by `StateDB._reconcile_columns()`).
- CLI session init and finalize must write status — requires hooks or explicit calls.
- Imported sessions may have imprecise timestamps if run.json is sparse.
- Error counts remain expensive to compute at list-query time.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|-------------|
| Derive status from messages (no column) | Every list query scans messages; expensive and fragile (message patterns vary by provider) |
| Derive from plays.status | Only works for show-play sessions; standalone sessions have no play |
| Store error_count on sessions | Premature optimization; requires scanning all messages at session close; add when the runs list needs error distinction |
| Rich session status (mirror play vocabulary) | Sessions don't have gates, merges, or redo cycles; forcing play lifecycle onto sessions is a category error |
| Compute duration and store it | Derived from two timestamps; storing adds a column that can drift if ended_at is corrected |

## Implementation note — DISPLAY_MAP

`apps/studio/server/services/status_mapping.py` exports `DISPLAY_MAP`: a
dict that translates raw DB status tokens (`running`, `completed`, `failed`,
`aborted`, and the play statuses from ADR-0011) into UI-friendly display
strings. Key constraints:

- **DISPLAY_MAP is a display mapper, NOT a lifecycle gate.** It maps tokens
  for UI rendering; it is not authoritative for session state transitions.
  Session writes are validated at the DB layer (CHECK constraints on the
  `status` column) and at the CLI call sites that write `status=`.
- Only values present in the ADR-0011 or ADR-0017 CHECK vocabularies appear
  in the map. Tokens outside the closed vocabularies (`done`, `success`,
  `cancelled`, `error`, `finished`) are intentionally absent.
- The `running` key appears in both vocabularies (play and session) and maps
  to `"running"` in both cases — no conflict.

## References

- [ADR-0009](ADR-0009-sqlite-state-layer.md) — sessions schema (extended by this ADR)
- [ADR-0011](ADR-0011-shows-data-model.md) — play status vocabulary (richer, independent)
- [ADR-0012](ADR-0012-studio-execution-lineage.md) — display mapping, dashboard cards, runs list
- [ADR-0015](ADR-0015-runs-list-design.md) — runs list Status column (consumes this ADR)
