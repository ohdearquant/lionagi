# Procedure

Full 8-step show procedure with code examples.

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

---

## Step 1 — Plan

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

## Step 2 — Pick

Select the next ready play: status is `pending` and all `depends_on` plays are `merged`.
If multiple are ready, fire them in parallel (separate worktrees, separate processes).

## Step 3 — Worktree

Each play gets an isolated git worktree so plays don't interfere:

```bash
PLAY="backend-api"
BRANCH="show/$TOPIC/$PLAY"
WORKTREE="$HOME/.lionagi/worktrees/$TOPIC-$PLAY"

git worktree add -b "$BRANCH" "$WORKTREE" "show/$TOPIC/integration"
```

Write `_intent.md` and `_prompt.md` inside `$SHOW_DIR/$PLAY/` before firing.

## Step 4 — Fire

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

## Step 5 — Gate

See [gate-protocol.md](gate-protocol.md) for gate agent invocation and verdict schema.

## Step 6 — Decide

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

## Step 7 — Adapt

After each play merges or escalates, update `_show.md`:

- Record the decision under `## Decisions` with timestamp and outcome
- If the play's output changes what downstream plays need, update their `_intent.md`
- If a play is no longer needed (its goal was achieved as a side effect), mark it skipped
- If a new play becomes necessary, add it to `_show.md` and create its directory

## Step 8 — Final gate

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

---

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
