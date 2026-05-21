# ADR-0019: Teams DB Migration and Run Lifecycle Management

**Status**: Proposed
**Date**: 2026-05-21
**Extends**: ADR-0009 (SQLite state layer), ADR-0017 (session lifecycle)

## Context

Two operational gaps in Lion Studio:

### 1. Teams are file-only

`li team` stores all state in `~/.lionagi/teams/*.json` with `fcntl.flock`
for concurrent writes. The Studio teams page (`services/teams.py`) reads
these files directly — it has no DB backing, no history, no queryable
metadata. This means:

- No team activity timeline (who sent what, when).
- No cross-reference between teams and the sessions/runs they coordinate.
- No pruning, no archival — stale team files accumulate indefinitely.
- The `flock`-based concurrency model doesn't compose with async DB
  transactions that the rest of Studio uses.

### 2. Runs have no stale detection

ADR-0017 gave sessions a `status` column (`running`, `completed`, `failed`,
`aborted`), but status transitions depend entirely on the CLI writing them
at session close. When a process crashes, gets OOM-killed, or simply hangs
indefinitely, the session stays `status = 'running'` forever.

The admin `doctor` endpoint detects "phantom" sessions via PID checks and
`updated_at` staleness, but this is:

- **Reactive only** — requires an operator to hit `/api/admin/doctor`.
- **Not surfaced on the runs list** — a dead run shows green "running" pill.
- **Using a single staleness threshold** (`stale_hours`, default 1h) that
  doesn't account for run type. A flow with 10 agents legitimately runs for
  hours; a single-agent run with no message progression for 6 hours is dead.

Observable failure mode: single-agent runs that crash show as "running"
indefinitely. The signal is clear — no new messages for hours, process gone
— but nothing acts on it.

## Decision

### Part A: Teams table in state.db

Add a `teams` table and a `team_messages` table to the state schema:

```sql
CREATE TABLE IF NOT EXISTS teams (
  id              TEXT    PRIMARY KEY,
  name            TEXT    NOT NULL,
  created_at      REAL    NOT NULL,
  updated_at      REAL    NOT NULL,
  member_count    INTEGER NOT NULL DEFAULT 0,
  members         JSON    NOT NULL DEFAULT '[]',  -- array of member name strings
  node_metadata   JSON,                           -- team config, coordination mode
  status          TEXT    NOT NULL DEFAULT 'active' CHECK(
                    status IN ('active', 'archived')
                  )
);

CREATE INDEX IF NOT EXISTS idx_teams_name ON teams(name);
CREATE INDEX IF NOT EXISTS idx_teams_updated ON teams(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_teams_status ON teams(status);

CREATE TABLE IF NOT EXISTS team_messages (
  id              TEXT    PRIMARY KEY,
  team_id         TEXT    NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  created_at      REAL    NOT NULL,
  sender          TEXT    NOT NULL,                -- member name or "system"
  recipient       TEXT    NOT NULL DEFAULT 'all',  -- member name or "all"
  content         TEXT    NOT NULL,
  summary         TEXT,                            -- one-line distillation for collapsed display
  read_by         JSON    NOT NULL DEFAULT '[]',   -- array of member names
  session_id      TEXT    REFERENCES sessions(id)  -- optional link to coordinating session
  -- Note: the Studio teams page shows `summary` by default; full `content` is revealed
  -- on "Expand raw message". Long raw messages are collapsed by default (see ChatGPT
  -- frontend design review, section 9). Writers should populate `summary` when content
  -- exceeds ~200 chars.
);

CREATE INDEX IF NOT EXISTS idx_team_msgs_team ON team_messages(team_id);
CREATE INDEX IF NOT EXISTS idx_team_msgs_created ON team_messages(created_at);
CREATE INDEX IF NOT EXISTS idx_team_msgs_session ON team_messages(session_id)
  WHERE session_id IS NOT NULL;
```

#### Migration strategy

The CLI (`li team`) continues to write JSON files as the primary path —
the DB is populated via a dual-write layer. This preserves backward
compatibility during the transition:

1. **Phase 1 (this ADR)**: Add tables. Studio reads from DB. `li team`
   writes to both JSON and DB. The JSON write stays synchronous
   under `_locked_team`'s `fcntl.flock`; the DB write happens
   after the lock release via a sync SQLite helper (not async
   `StateDB`) to avoid mixing sync file locks with async DB.
   New `li state import-teams` backfills existing JSON files into the DB.
2. **Phase 2 (future)**: `li team` writes DB-only. JSON files become
   optional export (`li team export`). `fcntl.flock` replaced by DB
   transactions.

Phase 2 is gated on confirming no external tools depend on reading the
JSON files directly.

#### Team ↔ Session linkage

`team_messages.session_id` is an optional FK that connects a team
coordination message to the session that produced it. When `li team send`
is called from within a `li play --team-mode` session, the session ID is
available and recorded. This enables:

- "Show me all runs that coordinated through team X."
- "Show me the team inbox alongside the run's message timeline."

### Part B: Run lifecycle management

#### B1. Message-based staleness detection

Add a `last_message_at` column to sessions:

```sql
ALTER TABLE sessions ADD COLUMN last_message_at REAL;
```

The CLI updates `last_message_at` on every message INSERT (in the same
transaction as the progression append). This is cheaper than computing
`MAX(created_at)` from messages at query time.

#### B2. Staleness heuristic (computed, not stored)

Staleness is a **derived status** computed at read time, not a stored
column. The runs list and dashboard compute `effective_health` from:

```python
STALE_THRESHOLDS = {
    "agent": 6 * 3600,       # 6h — single-agent runs
    "play": 6 * 3600,        # 6h — single-play runs
    "flow": 12 * 3600,       # 12h — multi-agent flows
    "fanout": 12 * 3600,     # 12h — parallel fan-out
    "show-play": 12 * 3600,  # 12h — show-managed plays
}
DEFAULT_STALE_THRESHOLD = 6 * 3600  # 6h fallback

def staleness_check(session: dict, *, now: float) -> str | None:
    """Return 'stale' if a running session exceeds its activity threshold.

    Returns None for terminal sessions (health classification is
    handled by ADR-0024's classify_session_health). This function
    only answers one question: is this running session still active?
    """
    if session["status"] != "running":
        return None  # terminal — defer to ADR-0024 health classifier

    threshold = STALE_THRESHOLDS.get(
        session.get("invocation_kind"), DEFAULT_STALE_THRESHOLD
    )
    last_activity = session.get("last_message_at") or session.get("updated_at") or 0
    if now - last_activity > threshold:
        return "stale"

    return None
```

Key properties:

- **Non-destructive**: the DB `status` column stays `running`. The `stale`
  label is display-only, computed per-read. No data mutation on read path.
- **Kind-aware**: single-agent runs get a 6h threshold; flows get 12h.
  Thresholds are tunable constants, not config.
- **Message-based**: uses `last_message_at` (progression activity), not
  `updated_at` (which can be bumped by metadata writes). A session with
  0 messages for 6 hours and no process is dead.

#### B3. Admin transition (destructive, explicit)

The admin `doctor` endpoint gains a `transition_stale` option that
**writes** `status = 'failed'` for stale sessions after confirming the
process is dead:

```python
async def transition_stale_sessions(
    *, stale_hours: float = 6.0
) -> list[dict]:
    """Mark stale running sessions as failed.

    Only transitions sessions where:
    1. effective_health == 'stale' (no message activity past threshold)
    2. Process is confirmed dead (PID check + ps scan)

    Returns list of transitioned session IDs with reason.
    """
```

This is an explicit admin action, not automatic. The runs list shows
`stale` as a warning; the admin page lets the operator confirm and
transition.

#### B4. Dashboard cards

The dashboard gains two new cards from these signals:

| Card | Query |
|------|-------|
| **Stale** | `status = 'running' AND last_message_at < now() - threshold` |
| **Teams active** | `SELECT COUNT(*) FROM teams WHERE status = 'active'` |

### Status vocabulary update

This ADR does not modify the session status vocabulary or CHECK
constraint. `stale` is NOT a stored status — it is a derived health
indicator computed at read time (see ADR-0024). The session status
vocabulary and its validation strategy (including CHECK removal) are
governed entirely by ADR-0025.

The runs list API response shape adds:

```json
{
  "status": "running",
  "effective_health": "stale",
  "last_message_at": 1716300000.0,
  "message_count": 42
}
```

Frontend renders `effective_health` for the pill color. Tooltip shows raw
`status` for disambiguation.

## Consequences

**Positive**
- Teams become queryable, linkable to sessions, and prunable.
- Dead runs are visually distinguishable from active runs without operator
  intervention.
- Message-based staleness is a robust signal — no false positives from
  legitimate long-running processes that are actively producing messages.
- Non-destructive by default: stale is a display label, not a data mutation.
- Admin retains explicit control over status transitions.

**Negative**
- Dual-write period (Phase 1) means two sources of truth for teams. Risk
  of drift if a write path is missed. Mitigated by keeping JSON as
  fallback read source during transition.
- `last_message_at` requires a write on every message INSERT. At our scale
  (< 500 messages/session) this is negligible, but it's an extra UPDATE
  per message.
- Staleness thresholds are heuristic. A legitimately idle agent (waiting
  for user input for 7 hours) would show as stale. Acceptable because the
  operator can inspect and dismiss.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Auto-transition stale → failed without admin | Risk of killing legitimately paused sessions; operator should confirm |
| Store `stale` as a DB status | Mixes display concern with lifecycle state; `stale` is a view, not a transition |
| Heartbeat column (process writes every N seconds) | Requires CLI changes to all run types; message activity is already a natural heartbeat |
| Keep teams file-only, add DB index | Half-measure; still no history, no cross-referencing, no async-safe writes |
| Merge team messages into the main `messages` table | Teams use a different addressing model (member names, not UUIDs); forcing them into the Element-based message table adds complexity for no query benefit |
| Single staleness threshold for all run types | 6h is too aggressive for flows, too lenient for single agents. Kind-aware thresholds are simple and correct |

## References

- [ADR-0009](ADR-0009-sqlite-state-layer.md) — SQLite state layer (teams table extends this)
- [ADR-0017](ADR-0017-session-lifecycle-status.md) — Session lifecycle (staleness extends this)
- [ADR-0012](ADR-0012-studio-execution-lineage.md) — Execution lineage (team linkage extends this)
- ChatGPT frontend design review (external, not in repo) — Teams page redesign (section 9), member sidebar + activity timeline, message summary/collapse, linked sessions; 60m threshold confirmation (section 2)
- `lionagi/cli/team.py` — Current file-based team implementation
- `apps/studio/server/services/admin.py` — Current phantom session detection
- `apps/studio/server/services/sessions.py` — Current session queries
