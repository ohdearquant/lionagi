---
name: show
description: >
  Orchestrate multi-play shows: decompose a complex goal into sequential plays
  (each a li play invocation), gate each output for quality, adapt the plan based
  on results, and merge work into an integration branch. Shows are first-class
  entities in Lion Studio with dedicated UI at /shows.
allowed-tools: [Bash, Read, Write, Glob, Grep]
---

# show

Orchestrate a complex goal as a sequence of gated plays. Each play is one `li play`
invocation running in its own git worktree. The show coordinates: plan → fire →
gate → merge → adapt → repeat.

## Shows vs flows — pick the right tool

| Dimension | `li o flow` | show (this skill) |
|---|---|---|
| Duration | 5-30 min | 1-4 hours |
| Unit of work | Single agent op | Full `li play` invocation |
| Branching | Single DAG | Each play gets its own worktree + branch |
| Human gate | Optional critic node | Between every play |
| Replanning | Control-node triggered | After every play based on verdict |
| Studio UI | `/runs` | `/shows` (dedicated PlayDag view) |

Use a show when: the goal requires multiple independent bodies of work, each needing
its own branch and quality gate, with a plan that adapts based on intermediate results.

Use a flow when: a single orchestrator can plan and execute everything in one session.

## Data model

Shows and plays are first-class entities in `state.db` (SQLite):

```
shows table:
  id, topic, goal, repo, base_branch, integration_branch
  status: active | completed | aborted | imported
  show_dir, created_at, updated_at

plays table:
  id, show_id, name, playbook, effort
  status: pending | prepared | running | running_complete |
          gated | gate_failed | redoing | merged |
          escalated | blocked | aborted_after_finish
  attempt (1 or 2), session_id, started_at, ended_at, exit_code
  worktree, branch, merge_sha, merged_at
  gate_passed, gate_feedback, depends_on (JSON array), sort_order
```

Studio pages:
- `/shows` — list all shows with status, play count, last update
- `/shows/<topic>` — PlayDag component: dependency graph with per-play status colors
- Each play links to its session in `/runs`

The show directory is controlled by `LIONAGI_SHOWS_ROOT`. Set it to any path you prefer.
If unset, the skill uses `$HOME/.lionagi/shows` as its default.

## Workspace layout

```
${LIONAGI_SHOWS_ROOT:-$HOME/.lionagi/shows}/<topic>/
├── _show.md              # Living plan: goal, plays, decisions, status
├── _final_verdict.json   # Show-level gate result
├── _ABORT                # Soft-abort sentinel — touch to stop new plays
└── <play_name>/
    ├── _intent.md        # What this play must accomplish
    ├── _prompt.md        # Exact prompt passed to li play
    ├── _verdict.json     # Gate result: {gate_passed, feedback, notes}
    ├── _meta.json        # worktree, branch, attempt, timestamps, exit_code
    ├── .pid              # PID of running li play process
    └── .log              # Captured stdout/stderr
```

Worktrees live at `$HOME/.lionagi/worktrees/<topic>-<play_name>[-attempt<N>]`.

## _show.md format

The parser in `shows.py` reads specific patterns from `_show.md`. Use this structure
so Studio can display goal, repo, and branches correctly:

```markdown
# Show: <topic>

## Goal
<one paragraph: what done looks like>

## Context
- Repo: <path or remote URL>
- Base: <branch to merge integration back into>
- Integration: <integration-branch-name> (created before any plays fire)

## Plays

**<play_name>**
- Intent: <what this play produces>
- deps: [<other_play_name>, ...]   ← PlayDag reads this for edges
- Acceptance: <1-3 concrete criteria>

**<play_name_2>**
- Intent: ...
- deps: [<play_name>]
- Acceptance: ...

## Decisions
<!-- Updated after each play completes -->
```

The `deps: [...]` syntax on the line after a play name drives the PlayDag visualization.
Keep the `**play_name**` / `deps:` pattern intact — the frontend parser is line-oriented.

## The 8-step procedure

### Step 1 — Plan

Write `_show.md` (use the format above). Create the integration branch:

```bash
TOPIC="my-feature"
SHOW_DIR="${LIONAGI_SHOWS_ROOT:-$HOME/.lionagi/shows}/$TOPIC"
mkdir -p "$SHOW_DIR"

# Create integration branch (rebased on latest main)
git fetch origin
git checkout -b "show/$TOPIC/integration" origin/main
git push -u origin "show/$TOPIC/integration"
```

Write `_show.md` with goal, repo, branches, and the initial play list. Dependencies
between plays go in the `deps: [...]` line — the PlayDag renders them as edges.

### Step 2 — Pick

Select the next ready play: status is `pending` and all `depends_on` plays are `merged`.
If multiple are ready, fire them in parallel (separate worktrees, separate processes).

### Step 3 — Worktree

Each play gets an isolated git worktree so plays don't interfere:

```bash
PLAY="backend-api"
BRANCH="show/$TOPIC/$PLAY"
WORKTREE="$HOME/.lionagi/worktrees/$TOPIC-$PLAY"

git worktree add -b "$BRANCH" "$WORKTREE" "show/$TOPIC/integration"
```

Write `_intent.md` and `_prompt.md` inside `$SHOW_DIR/$PLAY/` before firing.

### Step 4 — Fire

Run the play inside the worktree. Pass `--invocation` so Studio groups it with the show:

```bash
PLAY_DIR="$SHOW_DIR/$PLAY"
mkdir -p "$PLAY_DIR"

# Start invocation tracking
INV_ID=$(li invoke start --skill show --prompt "$TOPIC/$PLAY" 2>/dev/null || echo "")

# Fire the play (runs li o flow internally)
(
  cd "$WORKTREE"
  li play <playbook_name> "$(cat $PLAY_DIR/_prompt.md)" \
    --yolo --bypass \
    --save "$PLAY_DIR" \
    ${INV_ID:+--invocation "$INV_ID"} \
    > "$PLAY_DIR/.log" 2>&1 &
  echo $! > "$PLAY_DIR/.pid"
  wait $!
  echo $? > "$PLAY_DIR/.exit_code"
)
```

Write `_meta.json` after the process exits:

```json
{
  "worktree": "/path/to/worktree",
  "branch": "show/topic/play-name",
  "attempt": 1,
  "started_at": 1748000000.0,
  "ended_at":   1748003600.0,
  "exit_code":  0,
  "status":     "running_complete"
}
```

### Step 5 — Gate

After the play completes, run a gate agent to evaluate its output:

```bash
li agent -a reviewer "$(cat <<'EOF'
You are the gate agent for play '$PLAY' of show '$TOPIC'.

Read the play output in $PLAY_DIR/.log and any artifacts saved to $PLAY_DIR/.

Acceptance criteria from _intent.md:
$(cat $PLAY_DIR/_intent.md)

Evaluate: did the play meet every acceptance criterion?

Output ONLY valid JSON to stdout:
{"gate_passed": true,  "feedback": null,    "notes": "All criteria met."}
{"gate_passed": false, "feedback": "Missing error handling for X.", "notes": null}
EOF
)"
```

Write the result to `$PLAY_DIR/_verdict.json`. The schema the DB reads:

```json
{"gate_passed": true,  "feedback": null,    "notes": "Criteria met."}
{"gate_passed": false, "feedback": "...",   "notes": null}
```

### Step 6 — Decide

Read `_verdict.json` and branch:

```
gate_passed = true  → merge to integration branch (Step 6a)
gate_passed = false, attempt = 1 → redo with feedback injected (Step 6b)
gate_passed = false, attempt = 2 → escalate (Step 6c)
```

**6a — Merge:**

```bash
git checkout "show/$TOPIC/integration"
git merge --no-ff "$BRANCH" -m "play($PLAY): merge attempt $ATTEMPT"
MERGE_SHA=$(git rev-parse HEAD)
git push origin "show/$TOPIC/integration"
# Update _meta.json: status=merged, merge_sha, merged_at
```

**6b — Redo (attempt 2):**

Create a fresh worktree from integration at `$HOME/.lionagi/worktrees/$TOPIC-$PLAY-attempt2`.
Prepend the gate feedback to the play prompt and re-fire. Record `attempt: 2` in `_meta.json`.

**6c — Escalate:**

Write `status: escalated` to `_meta.json`. Log a clear human-readable summary of both
attempts and the gate feedback into `$PLAY_DIR/_escalation.md`. Stop this play — do not
retry a third time. Continue with other ready plays.

### Step 7 — Adapt

After each play merges or escalates, update `_show.md`:

- Record the decision under `## Decisions` with timestamp and outcome
- If the play's output changes what downstream plays need, update their `_intent.md`
- If a play is no longer needed (its goal was achieved as a side effect), mark it skipped
- If a new play becomes necessary, add it to `_show.md` and create its directory

### Step 8 — Final gate

When all plays have reached a terminal status (merged, escalated, or skipped):

```bash
li agent -a reviewer "$(cat <<'EOF'
You are the final gate agent for show '$TOPIC'.
Read _show.md for the original goal.
For each play, read its _verdict.json and _meta.json.
Determine: does the integration branch now satisfy the show goal?
Output ONLY valid JSON:
{"show_passed": true,  "summary": "...", "blockers": []}
{"show_passed": false, "summary": "...", "blockers": ["play_name: reason"]}
EOF
)"
# Write result to $SHOW_DIR/_final_verdict.json
```

If `show_passed = true`, open a PR from `show/$TOPIC/integration` into `base_branch`.
If `show_passed = false`, address blockers or escalate the whole show.

Close the invocation:

```bash
[ -n "$INV_ID" ] && li invoke end "$INV_ID" --status completed
```

## Abort protocol

**Soft abort** — blocks new play launches; running plays finish naturally:

```bash
touch "$SHOW_DIR/_ABORT"
```

Before firing any new play, check:

```bash
[ -f "$SHOW_DIR/_ABORT" ] && { echo "Show aborted — not launching $PLAY"; exit 0; }
```

**Hard abort** — kill running plays immediately:

```bash
for pid_file in "$SHOW_DIR"/*/.pid; do
  PID=$(cat "$pid_file" 2>/dev/null)
  [ -n "$PID" ] && kill "$PID" 2>/dev/null
done
```

Worktrees are preserved after abort for forensic review. Clean up manually:

```bash
git worktree list | grep "$TOPIC" | awk '{print $1}' | xargs -I{} git worktree remove --force {}
```

## Resume protocol

On resuming a show, classify each play directory and act:

| Play status | Action |
|---|---|
| `merged` | Skip — already in integration branch |
| `running` | Check `.pid`. If PID alive: wait. If dead: mark `exit_code` unknown, re-gate. |
| `running_complete` | Run gate (Step 5) — play finished but was not yet evaluated |
| `gate_failed` attempt 1 | Redo with feedback (Step 6b) |
| `gate_failed` attempt 2 | Escalate (Step 6c) |
| `escalated` | Log in `_show.md`, continue with other plays |
| `pending` | Check deps — if all deps merged, fire (Step 4) |

Resume bash check:

```bash
for play_dir in "$SHOW_DIR"/*/; do
  play=$(basename "$play_dir")
  status=$(python3 -c "import json; d=json.load(open('$play_dir/_meta.json')); print(d.get('status','pending'))" 2>/dev/null || echo "pending")
  echo "$play: $status"
done
```

## Invocation tracking

Studio's `/invocations` page groups all sessions fired by a show under one parent record.
Every `li play` call in the show should carry `--invocation "$INV_ID"`.

```bash
# At show start
INV_ID=$(li invoke start --skill show --prompt "$TOPIC" 2>/dev/null || echo "")

# Each play (pass-through — li play forwards to li o flow)
li play <name> "<prompt>" --yolo --bypass --invocation "$INV_ID"

# At show end
[ -n "$INV_ID" ] && li invoke end "$INV_ID" --status completed
# or: --status failed / --status aborted
```

If `li invoke` is unavailable (older install), omit the `--invocation` flag — plays still
run and appear in `/runs`; they just won't be grouped under a show parent in `/invocations`.

## Source code reference

| File | Purpose |
|---|---|
| `lionagi/state/schema.sql` line ~218 | `shows` and `plays` table DDL |
| `apps/studio/server/services/shows.py` | List, detail, import, SSE watcher |
| `apps/studio/server/routers/shows.py` | REST + SSE endpoints |
| `apps/studio/frontend/app/shows/page.tsx` | Show list page |
| `apps/studio/frontend/app/shows/[topic]/page.tsx` | Show detail page |
| `apps/studio/frontend/app/shows/[topic]/components/PlayDag.tsx` | DAG visualization |
| `apps/studio/server/config.py` | `LIONAGI_SHOWS_ROOT` env var |
| `lionagi/cli/invoke.py` | `li invoke start/end/list` |

## Common mistakes

```
❌ Creating a show for a task one flow can finish — overkill, use li o flow
❌ Forgetting --yolo --bypass on li play — interactive prompts stall the play
❌ Using git merge without --no-ff — loses play boundary in git history
❌ Skipping gate on attempt 2 — gate is mandatory before escalate decision
❌ Firing plays before integration branch exists — plays have nowhere to merge
❌ Touching _ABORT after all plays have launched — it won't stop them (use kill)
❌ Keeping play worktrees after merge — they accumulate; remove after merge
❌ Not updating _show.md decisions after each play — Studio shows stale plan
```

## Quick reference

```bash
TOPIC="my-feature"
SHOW_DIR="${LIONAGI_SHOWS_ROOT:-$HOME/.lionagi/shows}/$TOPIC"
WORKTREES="$HOME/.lionagi/worktrees"

# Bootstrap
mkdir -p "$SHOW_DIR"
INV_ID=$(li invoke start --skill show --prompt "$TOPIC" 2>/dev/null || echo "")

# Per play
PLAY="backend-api"
BRANCH="show/$TOPIC/$PLAY"
git worktree add -b "$BRANCH" "$WORKTREES/$TOPIC-$PLAY" "show/$TOPIC/integration"
li play <name> "$(cat $SHOW_DIR/$PLAY/_prompt.md)" --yolo --bypass \
  --save "$SHOW_DIR/$PLAY" ${INV_ID:+--invocation "$INV_ID"} \
  > "$SHOW_DIR/$PLAY/.log" 2>&1

# Gate verdict → _verdict.json
# Merge or redo or escalate based on gate_passed

# Final gate → _final_verdict.json
# Close
[ -n "$INV_ID" ] && li invoke end "$INV_ID" --status completed
```
