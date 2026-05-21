# ADR-0020: Skill Invocations — Tracking the Orchestration Layer

**Status**: Proposed
**Date**: 2026-05-21
**Extends**: ADR-0009 (SQLite state layer), ADR-0012 (execution lineage), ADR-0019 (run lifecycle)

## Context

Lion Studio traces execution from sessions down to messages, but has no
record of what triggered those sessions. The causal chain today:

```
??? → Session → Branch → Messages → Tool Calls → Artifacts
```

The missing layer is **skill invocations**. When Ocean types `/show resolve
lionagi issues`, that single command produces:

- 6 `li play` sessions (one per play)
- 6 `li agent -a play-gate` sessions (one gate per play)
- 2+ `li agent -a reviewer` sessions (codex review rounds)
- Team coordination messages across plays
- Show-level artifacts (`_show.md`, verdicts, decisions log)

These 14+ sessions share a common origin — the `/show` invocation — but
nothing in the DB connects them. The `show_topic` and `show_play_name`
provenance columns (ADR-0012) partially group show-play sessions, but:

1. **No invocation record.** There's no "this `/show` started at 2:07 AM,
   produced 14 sessions, and completed at 8:45 AM" entity.
2. **Skills beyond `/show` are invisible.** `/codex-pr-review` fires 3
   rounds of `li agent -a reviewer` — those sessions have
   `invocation_kind='agent'` and no hint they're part of a review cycle.
3. **Marketplace plugins fire skills.** The `show` plugin invokes `/show`;
   the `devx` plugin invokes `/commit`, `/fmt`, `/ci`. These are
   higher-order compositions with no trace.
4. **`invocation_kind` is a closed enum.** Adding every skill to the CHECK
   constraint doesn't scale — there are 60+ skills, and users can create
   custom ones.

### Skill taxonomy (what produces runs)

Skills fall into three categories by how they interact with the run layer:

| Category | Examples | Run pattern |
|----------|----------|-------------|
| **Orchestrators** | `/show`, `/codex-pr-review`, `/reprompt` | Spawn N sessions over minutes-to-hours, with gating and adaptation |
| **Single-shot** | `/ci`, `/fmt`, `/commit`, `/pr` | Zero or one session; mostly shell commands, no `li play/agent` |
| **Read-only** | `/status`, `/memory-recall`, `/summarize` | No sessions; pure query or text generation |

Only **orchestrator** skills need invocation tracking. Single-shot and
read-only skills don't produce session trees worth grouping.

### Marketplace plugins as skill compositors

Marketplace plugins (ADR-0010) are Claude Code plugins that expose skills.
The `show` plugin contains the `/show` skill; `devx` contains `/commit`,
`/fmt`, `/ci`. Plugins are the packaging unit; skills are the invocation
unit. An invocation record should reference the skill, with the plugin
as optional metadata.

## Decision

### Add an `invocations` table

```sql
CREATE TABLE IF NOT EXISTS invocations (
  id              TEXT    PRIMARY KEY,
  skill           TEXT    NOT NULL,           -- skill name: "show", "codex-pr-review", "reprompt"
  plugin          TEXT,                       -- marketplace plugin: "show", "devx", NULL for user skills
  prompt          TEXT,                       -- the user's input (e.g., "resolve lionagi issues")
  started_at      REAL    NOT NULL,
  ended_at        REAL,
  status          TEXT    NOT NULL DEFAULT 'running' CHECK(
                    status IN ('running', 'completed', 'failed', 'aborted', 'timed_out', 'cancelled')
                  ),
  session_count   INTEGER NOT NULL DEFAULT 0, -- denormalized for list queries
  created_at      REAL    NOT NULL,
  updated_at      REAL    NOT NULL,
  node_metadata   JSON                        -- skill-specific state (show plan, review rounds, etc.)
);

CREATE INDEX IF NOT EXISTS idx_invocations_skill ON invocations(skill);
CREATE INDEX IF NOT EXISTS idx_invocations_status ON invocations(status);
CREATE INDEX IF NOT EXISTS idx_invocations_updated ON invocations(updated_at DESC);
```

### Link sessions to invocations

Add an optional FK on sessions:

```sql
ALTER TABLE sessions ADD COLUMN invocation_id TEXT
  REFERENCES invocations(id);

CREATE INDEX IF NOT EXISTS idx_sessions_invocation
  ON sessions(invocation_id) WHERE invocation_id IS NOT NULL;
```

This replaces the need to expand the `invocation_kind` CHECK constraint
for every new skill. `invocation_kind` stays as-is (it describes the
CLI primitive: `agent`, `play`, `flow`, `fanout`, `show-play`).
`invocation_id` answers the higher-order question: "which skill
orchestration spawned this session?"

The two are orthogonal:
- `invocation_kind = 'play'` — this session was created by `li play`
- `invocation_id = 'abc123'` — it was spawned as part of the `/show`
  invocation `abc123`

### Invocation lifecycle

| Event | Who writes | What changes |
|-------|-----------|--------------|
| Skill starts | Skill runner (Claude Code) | INSERT invocation with `status='running'` |
| Session spawned | CLI (`li play`, `li agent`) | INSERT session with `invocation_id`; UPDATE invocation `session_count += 1` |
| Skill completes | Skill runner | UPDATE `status='completed'`, `ended_at=now()` |
| Skill fails | Skill runner | UPDATE `status='failed'`, `ended_at=now()` |
| Skill interrupted | Signal handler | UPDATE `status='aborted'`, `ended_at=now()` |
| Timeout | Timeout handler | UPDATE `status='timed_out'`, `ended_at=now()` |
| Cancelled | Orchestrator / admin | UPDATE `status='cancelled'`, `ended_at=now()` |

Note: `stale` is a **health indicator** on invocations (derived, same as
sessions — see ADR-0024), not a stored status. An invocation whose child
sessions are all stale/failed surfaces as health `stale` in the dashboard.

### Write path: who creates invocation records?

The skill runner is Claude Code itself (the LLM + tool loop). Skills are
markdown files loaded into context — they don't have their own process.
The invocation record must be created by the **first CLI command** the
skill fires, or by a lightweight `li invoke start` command that the skill
calls before spawning sessions.

Recommended approach — explicit `li invoke` lifecycle commands:

```bash
# Skill calls this at start
INV_ID=$(li invoke start --skill show --prompt "resolve lionagi issues")

# Each li play/agent gets the invocation ID
li play feature "..." --invocation "$INV_ID" ...

# Skill calls this at end
li invoke end "$INV_ID" --status completed
```

This keeps invocation tracking opt-in and non-breaking. Skills that don't
call `li invoke start` simply produce sessions with `invocation_id = NULL`
— the same behavior as today.

### Studio UI impact

#### Runs list

The runs list gains a **grouping mode**: when invocations exist, sessions
can be grouped under their parent invocation. Default view is flat (current
behavior); grouped view nests sessions under invocation headers:

```
▼ /show "resolve lionagi issues" — 14 sessions, 6h 38m, completed
    play:backend — completed, 87 min
    play-gate:backend — completed, 3 min
    play:frontend — completed, 64 min
    ...
▼ /codex-pr-review PR #1039 — 3 sessions, 45 min, completed
    reviewer round 1 — completed, 15 min
    reviewer round 2 — completed, 12 min
    reviewer round 3 (APPROVE) — completed, 8 min
  agent "quick fix" — completed, 2 min        ← no invocation (standalone)
```

#### Grouped invocation row design (Runs page)

The Runs page (`/runs/invocations`) uses a `View: Invocations | Sessions` toggle
(see ChatGPT frontend design review, sections 1 and 5). The default view is grouped
by invocation. Each parent row shows:

```
▾ /show lionagi-issue-sweep                              worst: stale
  6 plays · 14 sessions · 9h elapsed · 7 artifacts · updated 10m ago
  Models: codex/gpt-5.5 ×10 · claude-sonnet-4-6 ×4
  Status: running 6 · completed 8 · failed 0
```

The `worst: stale` badge on the parent row reflects the worst **health level**
among all child sessions — not the worst reported status. This prevents a
grouped row from showing a clean "running" state when one child is stale.

Expanded child rows show two columns — **Status** (reported) and **Health** (derived):

```
Run          Agent        Model             Status       Health     Dur
f82488be     reviewer     codex/gpt-5.5     running      stale      1h 34m
462bb5ed     play-gate    claude-sonnet     running      stale      9h 46m
c3e69388     reviewer     codex/gpt-5.5     running      stale      17h 56m
e37ff231     reviewer     codex/gpt-5.5     completed    healthy    5m 10s
```

Do not show a blue Running pill alone when the child's health is stale.
Compact representation: `[stale running]` as a compound state pill.

Standalone sessions without an `invocation_id` appear at the bottom under
"Ungrouped sessions" when the toggle is set to Invocations view.

#### Invocations page (new)

A dedicated `/invocations` page showing skill-level orchestration history:

| Skill | Prompt | Sessions | Duration | Status |
|-------|--------|----------|----------|--------|
| show | resolve lionagi issues | 14 | 6h 38m | completed |
| codex-pr-review | PR #1039 | 3 | 45m | completed |
| show | lattice kernel fusion | 8 | 3h 12m | running |

Click-through to the invocation detail shows the session tree, timeline,
and skill-specific metadata (show plan, review rounds, etc.).

#### Dashboard

New card: **Active skills** — count of invocations with `status = 'running'`.

### Skill metadata conventions

The `node_metadata` JSON on invocations carries skill-specific state.
Each orchestrator skill defines its own schema:

| Skill | Metadata shape |
|-------|---------------|
| `/show` | `{topic, goal, plays: [{name, status, attempt}], waves: [...]}` |
| `/codex-pr-review` | `{pr_number, rounds: [{round, verdict, resume_id}]}` |
| `/reprompt` | `{original_prompt, refined_prompt, agents_planned}` |

This is unstructured JSON — no schema enforcement. Skills write what they
need for resume and display. Studio renders it as best-effort JSON view
on the invocation detail page.

### Relationship to `invocation_kind`

`invocation_kind` on sessions is NOT deprecated. It remains the CLI-level
primitive type (`agent`, `play`, `flow`, `fanout`, `show-play`). The new
`invocation_id` is the higher-order grouping.

The two compose:

```
invocation (skill="show", prompt="resolve lionagi issues")
  ├── session (invocation_kind="play", show_play_name="backend")
  ├── session (invocation_kind="agent", agent_name="play-gate")
  ├── session (invocation_kind="play", show_play_name="frontend")
  ├── session (invocation_kind="agent", agent_name="play-gate")
  └── session (invocation_kind="agent", agent_name="reviewer")
```

### Custom skills

User-created skills (files in `~/.lionagi/skills/`) work the same way.
`li invoke start --skill my-custom-skill` creates an invocation with
`skill='my-custom-skill'` and `plugin=NULL`. No registration required.

## Consequences

**Positive**
- The full execution tree is traceable: skill → invocation → sessions →
  branches → messages.
- Orchestrator skills get first-class lifecycle tracking without expanding
  the `invocation_kind` enum.
- Invocations page gives Ocean a single view of "what did my system do
  overnight" at the right granularity — skill-level, not session-level.
- Grouped runs view reduces visual noise from multi-session orchestrations.
- `li invoke` is opt-in — zero breakage for existing skills and CLI usage.
- Marketplace plugins get attribution via the `plugin` column.

**Negative**
- Skills must explicitly call `li invoke start/end` to get tracking. Skills
  that don't are invisible at the invocation layer (sessions still tracked).
- `node_metadata` is unstructured — no schema validation means skill-specific
  metadata can drift or rot. Acceptable because it's display-only.
- Adds a table and an FK column — migration in `_reconcile_columns()`.
- Grouped runs view is a frontend complexity increase (collapsible groups,
  mixed flat + grouped entries).

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Expand `invocation_kind` for each skill | Doesn't scale — 60+ skills, users can create custom ones. CHECK constraint becomes unwieldy |
| Use `show_topic` for all skill grouping | Show-specific; doesn't generalize to codex-pr-review, reprompt, or custom skills |
| Derive invocations from session timestamps | Fragile heuristic; overlapping skills would be mis-grouped |
| Store invocation state in skill-local files | Same problem as teams — no queryable history, no cross-referencing |
| Auto-create invocations from session patterns | Requires pattern recognition that's skill-specific; explicit is better than magic |
| Make invocations a subclass of sessions | Category error — an invocation is not a conversation with an LLM; it's a coordination record |

## References

- [ADR-0009](ADR-0009-sqlite-state-layer.md) — SQLite state layer
- [ADR-0010](ADR-0010-plugin-aware-studio.md) — Plugin system (marketplace)
- [ADR-0012](ADR-0012-studio-execution-lineage.md) — Execution lineage
- [ADR-0017](ADR-0017-session-lifecycle-status.md) — Session lifecycle
- [ADR-0019](ADR-0019-teams-db-and-run-lifecycle.md) — Teams DB + run staleness
- [ChatGPT frontend design review](.khive/workspaces/20260521/chatgpt-frontend-review.md) — Nav restructure with Runs/Invocations/Sessions/Artifacts sub-tabs (section 1), grouped invocation row design with worst-health aggregation (section 5)
- `lionagi/cli/agent.py` — Session creation with invocation_kind
- `~/.lionagi/skills/show/SKILL.md` — Show skill (orchestrator pattern)
- `~/.lionagi/skills/codex-pr-review/SKILL.md` — Review skill (round pattern)
