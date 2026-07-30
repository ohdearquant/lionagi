# Procedure

Full 8-step show procedure with the exact calls.

Firing a play and reading its state go through the plugin's MCP server, tool
`mcp__plugin_orchestrate_lion__request`. Ask for the shape before relying on
it — `help=true` in its own call returns the verb catalog, and `help='<verb>'`
returns that verb's parameter schema — because a catalog request and an `ops`
request are different reply shapes and the server refuses a request that asks
for both. Everything about git (worktrees, branches, merges) stays plain git
run as Bash; there is no MCP verb for it.

If you are working inside a lionagi checkout with `li` on PATH, each MCP call
below has a `li` equivalent, noted at the point it applies. Treat it as a
local convenience for that situation, not the path most readers will use.

## Workspace layout

```
${LIONAGI_SHOWS_ROOT:-$HOME/.lionagi/shows}/<topic>/
├── _show.md              # Living plan: goal, plays, decisions, status
├── _final_verdict.json   # Show-level gate result
├── _ABORT                # Soft-abort sentinel — touch to stop new plays
└── <play_name>/
    ├── _intent.md        # What this play must accomplish
    ├── _prompt.md        # Exact prompt passed to play.submit
    ├── _verdict.json     # Gate result: {gate_passed, feedback, notes}
    ├── _meta.json        # worktree, branch, attempt, run_id, timestamps
    └── .log              # Local copy of the run's console tail (job.output)
```

Worktrees live at `$HOME/.lionagi/worktrees/<topic>-<play_name>[-attempt<N>]`.

A play fired through `play.submit` runs as a background job the MCP server
tracks by `run_id` — there is no PID file to manage by hand. `job.status`,
`job.output` and `job.wait` read that job record; use them instead of reading
a process table or tailing a log file directly.

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
If multiple are ready, fire them in parallel (separate worktrees, separate `play.submit` calls).

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

Ask what `play.submit` takes, once per play — it is a spawn verb, so its op must carry the
current `schema_fingerprint`, and that fingerprint depends on which `playbook` you name:

```json
{"help": {"verb": "play.submit", "playbook": "<playbook_name>"}}
```

Then fire the play. **Pass `cwd` explicitly, as an absolute path.** A play runs
in whatever directory `cwd` names, and its default is the directory the MCP
server itself is running in, which has nothing to do with yours. Your own
working directory does not reach the run, so a show whose whole premise is one
worktree per play must say which worktree each play gets. For the same reason
any path in the request is absolute: `prompt_file` is documented as requiring
one, and a relative path would resolve against the server's directory.

```json
{
  "ops": [
    {
      "op": "play.submit",
      "args": {
        "playbook": "<playbook_name>",
        "prompt": "<contents of _prompt.md>",
        "cwd": "/absolute/path/to/worktree"
      },
      "schema_fingerprint": "<from the help call above>"
    }
  ]
}
```

The reply carries the allocated `run_id` (never a bare success flag — check
`ok` on the op before trusting it). Write it into `_meta.json` immediately:

```json
{
  "worktree": "/path/to/worktree",
  "branch": "show/topic/play-name",
  "attempt": 1,
  "run_id": "<run_id from the reply>",
  "started_at": 1748000000.0,
  "status": "running"
}
```

**Checkout-local alternative.** Inside a lionagi checkout, `li play <playbook> "<prompt>"
--yolo --bypass --save "$PLAY_DIR"` does the same thing as a foreground/backgrounded shell
process instead of a server-tracked job; if you use it, note that `--invocation`-based
grouping under Studio's `/invocations` page (`li invoke start`/`li invoke end`) is a CLI-only
feature — there is no MCP verb for it, since the server has no caller identity to attach an
invocation record to. `job.status`/`job.output`/`job.wait` do not need it: they key on
`run_id`, not on an invocation.

## Step 5 — Gate

See [gate-protocol.md](gate-protocol.md) for the gate call and verdict schema.

## Step 6 — Decide

Poll or block on the run, then read `_verdict.json` and branch:

```json
{"ops": [{"op": "job.wait", "args": {"run_ids": ["<run_id>"]}}]}
```

```
gate_passed = true            → merge to integration (Step 6a)
gate_passed = false, attempt 1 → redo with feedback injected (Step 6b)
gate_passed = false, attempt 2 → escalate (Step 6c)
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
Prepend the gate feedback to the play prompt and re-fire with a new `play.submit` call.
Record `attempt: 2` and the new `run_id` in `_meta.json`.

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

When all plays have reached a terminal status (merged, escalated, or skipped),
submit the final gate as an agent run. `agent.submit` is a spawn verb, so ask for its current
`schema_fingerprint` first:

```json
{"help": "agent.submit"}
```

```json
{
  "ops": [
    {
      "op": "agent.submit",
      "args": {
        "agent": "reviewer",
        "prompt": "You are the final gate agent for show '<topic>'. Read _show.md for the original goal. For each play, read its _verdict.json and _meta.json. Determine: does the integration branch now satisfy the show goal? Output ONLY valid JSON: {\"show_passed\": true, \"summary\": \"...\", \"blockers\": []} or {\"show_passed\": false, \"summary\": \"...\", \"blockers\": [\"play_name: reason\"]}"
      },
      "schema_fingerprint": "<from the help call above>"
    }
  ]
}
```

Wait for it and read its output:

```json
{"ops": [{"op": "job.wait", "args": {"run_ids": ["<run_id>"]}}]}
```

```json
{"ops": [{"op": "job.output", "args": {"run_id": "<run_id>"}}]}
```

Write the parsed result to `$SHOW_DIR/_final_verdict.json`. If `show_passed = true`, open
a PR from `show/$TOPIC/integration` into `base_branch`. If `show_passed = false`, address
blockers or escalate the whole show.

---

## Quick reference

```json
// 1. Ask what play.submit takes for this playbook (schema_fingerprint depends on it)
{"help": {"verb": "play.submit", "playbook": "<name>"}}

// 2. Fire a play
{"ops": [{"op": "play.submit", "args": {"playbook": "<name>", "prompt": "<from _prompt.md>"}, "schema_fingerprint": "<from help>"}]}
// -> record the returned run_id in _meta.json

// 3. Wait for it, then read its output
{"ops": [{"op": "job.wait", "args": {"run_ids": ["<run_id>"]}}]}
{"ops": [{"op": "job.output", "args": {"run_id": "<run_id>"}}]}

// 4. Gate verdict -> _verdict.json; merge, redo, or escalate (Step 6)

// 5. Final gate (Step 8) -> _final_verdict.json
```

Local git steps (integration branch, worktree per play, merge on pass) run as plain Bash
throughout — see Steps 1, 3 and 6a above.
