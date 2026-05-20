# ADR-0011: Shows Data Model — Hybrid SQLite + Filesystem

**Status**: Accepted
**Date**: 2026-05-20
**Extends**: ADR-0009 (SQLite state layer), ADR-0010 (plugin-aware Studio)

## Context

Shows (multi-play DAGs orchestrated by the `show` skill) currently exist only
as filesystem trees under `~/khive-work/shows/{topic}/`. Each play writes
`_meta.json`, `_verdict.json`, `_intent.md`, `_prompt.md`, and agent artifacts.
Studio reads these files on every request.

This creates three problems:

1. **No cross-reference to sessions.** Each play IS a `li play` invocation that
   creates a session in SQLite (via ADR-0009 hooks). But nothing links the show
   DAG to those sessions. You can't drill from show → play → session → messages.

2. **No query performance.** Listing shows requires `readdir` + parsing N JSON
   files per show. With 2 shows × 12 plays average, this is ~24 file reads per
   list request. Scales poorly.

3. **No structural queries.** "Which plays are blocked?" "What's the critical
   path?" "Show me all escalated plays across all shows" — impossible without
   loading everything into memory.

Meanwhile, the long-form content (`_show.md`, `_intent.md`, `_prompt.md`) is
authored markdown that belongs on disk — it's edited by the director mid-show,
versioned by git, and doesn't benefit from being in a database column.

## Decision

Add two tables to `~/.lionagi/state.db` for show-level orchestration state.
Filesystem remains source of truth for markdown content; SQLite stores
structural/status data and the critical `session_id` foreign key.

### Schema

```sql
-- ── Shows ────────────────────────────────────────────────────────────────
-- One row per show (multi-play DAG).

CREATE TABLE IF NOT EXISTS shows (
  id                  TEXT    PRIMARY KEY,
  topic               TEXT    NOT NULL UNIQUE,
  goal                TEXT,                       -- one-line summary from _show.md
  repo                TEXT,                       -- absolute path to repo
  base_branch         TEXT,                       -- e.g. 'main'
  integration_branch  TEXT,                       -- e.g. 'show/topic/integration'
  status              TEXT    NOT NULL DEFAULT 'active',  -- active|completed|aborted
  show_dir            TEXT    NOT NULL,           -- absolute path to show directory
  created_at          REAL    NOT NULL,
  updated_at          REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_shows_topic ON shows(topic);
CREATE INDEX IF NOT EXISTS idx_shows_status ON shows(status);
CREATE INDEX IF NOT EXISTS idx_shows_updated ON shows(updated_at DESC);

-- ── Plays ────────────────────────────────────────────────────────────────
-- One row per play within a show. Links to sessions table via session_id.

CREATE TABLE IF NOT EXISTS plays (
  id              TEXT    PRIMARY KEY,
  show_id         TEXT    NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
  name            TEXT    NOT NULL,
  playbook        TEXT,                           -- which playbook was used
  effort          TEXT,                           -- low|medium|high|xhigh
  status          TEXT    NOT NULL DEFAULT 'pending',
                  -- pending|prepared|running|running_complete|
                  -- gated|gate_failed|redoing|merged|escalated|
                  -- blocked|aborted_after_finish
  attempt         INTEGER NOT NULL DEFAULT 1,
  session_id      TEXT    REFERENCES sessions(id), -- THE KEY LINK
  started_at      REAL,
  ended_at        REAL,
  exit_code       INTEGER,
  worktree        TEXT,                           -- absolute path
  branch          TEXT,                           -- git branch name
  merge_sha       TEXT,
  merged_at       REAL,
  gate_passed     INTEGER,                        -- 0|1|NULL (not yet gated)
  gate_feedback   TEXT,                           -- from _verdict.json
  depends_on      JSON    DEFAULT '[]',           -- array of play names
  sort_order      INTEGER NOT NULL DEFAULT 0,
  created_at      REAL    NOT NULL,
  updated_at      REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plays_show ON plays(show_id);
CREATE INDEX IF NOT EXISTS idx_plays_status ON plays(status);
CREATE INDEX IF NOT EXISTS idx_plays_session ON plays(session_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_plays_show_name ON plays(show_id, name);
```

### What lives where

| Data | Location | Why |
|------|----------|-----|
| Show goal summary, status, timestamps | SQLite `shows` | Queryable, fast list |
| Play status, attempt, exit_code, gate result | SQLite `plays` | Queryable, cross-ref to sessions |
| `plays.session_id` → `sessions.id` | SQLite FK | **Drill-down**: show → play → session → branches → messages |
| `_show.md` (full plan, decisions log) | Filesystem | Long-form markdown, git-versioned, edited mid-show |
| `_intent.md`, `_prompt.md` | Filesystem | Authored content, not status |
| `_verdict.json` (full) | Filesystem | Gate feedback archived on disk; summary in `gate_feedback` column |
| Agent artifacts (`{play}/{agent_id}/*`) | Filesystem | Binary/large files, git-tracked |

### Write path

The show skill's existing Steps already write `_meta.json` at lifecycle
transitions (Step 0, 3, 4, 5). Each write point also upserts the SQLite row:

| Show skill step | Filesystem write | SQLite write |
|-----------------|-----------------|--------------|
| Step 0 (plan) | `_show.md` | INSERT `shows` row |
| Step 2 (worktree) | `_meta.json` {status:pending} | INSERT `plays` row |
| Step 3 (fire) | `_meta.json` {status:running} | UPDATE `plays` status, started_at |
| Step 3 (complete) | `_meta.json` {exit_code, ended_at} | UPDATE `plays` exit_code, ended_at, session_id |
| Step 4 (gate) | `_verdict.json` | UPDATE `plays` gate_passed, gate_feedback |
| Step 5a (merge) | `_meta.json` {merged_at, merge_sha} | UPDATE `plays` merged_at, merge_sha, status=merged |
| Step 5b (redo) | `_meta.json` {attempt:2} | UPDATE `plays` attempt, status=redoing |
| Step 5c (escalate) | `_meta.json` {status:escalated} | UPDATE `plays` status=escalated |
| Step 7 (final gate) | `_final_verdict.json` | UPDATE `shows` status=completed\|aborted |

### session_id resolution

When a play fires via `li play ... --team-mode show_topic_play`, the session
created by `li play` gets a deterministic name: `show_{topic}_{play}`. After
the play subprocess exits, the director queries:

```sql
SELECT id FROM sessions WHERE name = ? ORDER BY created_at DESC LIMIT 1
```

This links the play row to the session. From there, the full message tree is
accessible: `plays.session_id` → `sessions` → `branches` → `progressions` →
`messages`.

### Read path (Studio API)

```
GET /api/shows/           → SELECT from shows table (fast, no filesystem)
GET /api/shows/{topic}    → shows row + plays rows + _show.md from disk
GET /api/shows/{topic}/plays/{name}
                          → play row + _intent.md + _verdict.json from disk
                          → play.session_id for drill-down link to /runs/{session_id}
```

### Migration: `li state import-shows`

One-time import of existing filesystem shows into SQLite:

```python
for show_dir in SHOWS_ROOT.iterdir():
    # Parse _show.md for goal (first paragraph under ## Goal)
    # Insert shows row
    for play_dir in show_dir subdirs:
        # Parse _meta.json → plays row
        # Parse _verdict.json → gate_passed, gate_feedback
        # Resolve session_id by name lookup
        # Insert plays row
```

### Play detail accordion and session drill-down

The `plays.session_id` FK enables the most important UI connection. The
shows detail page surfaces this via **inline accordion** (not a drawer —
a drawer competes with the DAG for horizontal space):

1. **Play row click** expands the row vertically to show:
   - Session link: `Open Session →` routes to `/runs/{session_id}` (first element)
   - Intent (`_intent.md`)
   - Duration, exit code, attempt count
   - Gate verdict and feedback
   - Raw data (meta JSON, verdict JSON) collapsed by default

2. **Play DAG node click** scrolls to and expands the corresponding play row.

3. **Reverse lookup**: run detail page queries
   `SELECT show_id, name FROM plays WHERE session_id = ?` to show a
   "Source: Show {topic} / Play {name}" backlink in the Overview section.

Note: the UI route is `/runs/{session_id}` (user-facing "Runs" label per
ADR-0012), not `/sessions/{session_id}`.

### Play status display: structured multi-badge State cell

The plays table uses a single **State** column with a primary lifecycle pill
plus optional secondary badges for gate and integration:

```
[completed] [passed] [merged]      ← full provenance
[completed]                        ← no gate, no merge
[awaiting gate]                    ← running_complete
[pending]                          ← no activity yet
[failed] [gate failed]             ← gate rejection
```

This avoids three separate columns (too wide for the dense table) and avoids
a single compound pill (hard to scan because dimensions aren't visually
separable).

**Badge colors**:
- Lifecycle: pending (amber), running (blue), awaiting_gate (amber),
  completed (green), failed (red), aborted (gray)
- Gate: passed (green outline), failed (red outline), skipped (gray outline)
- Integration: merged (green outline), local (gray outline)

Raw status appears in expanded accordion details and tooltips.

The raw `plays.status` values (`running_complete`, `gated`, `gate_failed`,
`redoing`, `escalated`, `blocked`, `aborted_after_finish`) map to normalized
lifecycle + gate + review badges per ADR-0012's status vocabulary.

### PlayDag placement: compact strip above plays table

The PlayDag renders as a **compact dependency graph strip above the plays
table**, not a competing primary view:

```
Dependency graph                                        [Hide graph]
┌────────────────────────────────────────────────────────────────────┐
│  compact PlayDag, fitView, 150–170px tall                          │
│  node hover → highlight table row                                  │
│  row hover → highlight graph node                                  │
│  node click → scroll to and expand play row                        │
└────────────────────────────────────────────────────────────────────┘
```

Height scales with play count:
- ≤4 plays: 112px
- 5–20 plays: 160px
- 21+ plays: 220px with scroll/pan

The graph is visible by default (not behind a toggle) because it is the only
visual representation of dependency structure. Hiding it collapses shows into
a plain status table and loses the orchestration identity of the page.

### Show status provenance (added post-review)

Show status includes provenance to address trust concerns:
- `active` — at least one play running or pending
- `completed` — all plays merged and final gate passed (or inferred from filesystem)
- `aborted` — `_ABORT` file exists
- `imported` — status inferred during `import_shows`, may not reflect `_show.md` decisions log

The API includes `status_source: "sqlite" | "filesystem" | "imported"` to
indicate confidence level.

## Consequences

**Positive**
- Show → play → session → message drill-down via foreign keys.
- Fast list/filter queries without filesystem scanning.
- Structural queries: blocked plays, critical path, cross-show stats.
- Filesystem content unchanged — git workflow, director editing, resume
  protocol all work identically.
- Play inline accordion provides full context without leaving the shows page.
- Status normalization eliminates contradictory displays across pages.

**Negative**
- Dual write: every `_meta.json` update must also update SQLite. If the
  show skill crashes between writes, they can drift. Mitigation: the
  `li state import-shows` command can re-sync from filesystem at any time.
- Schema migration: existing `state.db` instances need ALTER TABLE or
  recreation. Use schema_meta version bump (1 → 2) with migration.
- Status mapping adds a translation layer between raw play statuses and
  normalized display vocabulary.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|-------------|
| Pure filesystem (status quo) | No session cross-reference, no query performance, no structural queries |
| Full SQLite (move all content to DB) | `_show.md` and `_intent.md` are authored markdown — git versioning and editor access matter more than query performance on content |
| Separate shows.db | Unnecessary complexity — `state.db` already exists and the FK to sessions is the whole point |
