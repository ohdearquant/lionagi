# Gate Protocol

Gate agents, verdict format, decision logic, and abort/resume protocols.

## Per-play gate agent

After the play's run reaches a terminal state (`job.wait` returns `all_terminal: true`), read
its output first. Check every op's `ok` field:

```json
{"ops": [{"op": "job.output", "args": {"run_id": "<play_run_id>"}}]}
```

Then submit a gate agent with that output and the acceptance criteria in its prompt.
`agent.submit` is a spawn verb, so its op needs the current `schema_fingerprint`, from
`help='agent.submit'`:

```json
{"help": "agent.submit"}
```

```json
{
  "ops": [
    {
      "op": "agent.submit",
      "args": {
        "query": ["claude"],
        "cwd": "/absolute/path/to/<play_dir>",
        "prompt": "You are the gate agent for play '<play>' of show '<topic>'. Acceptance criteria from _intent.md: <contents of _intent.md>. Completed play output: <console and artifact summary from job.output>. Evaluate: did the play meet every acceptance criterion? Output ONLY valid JSON to stdout: {\"gate_passed\": true, \"feedback\": null, \"notes\": \"All criteria met.\"} or {\"gate_passed\": false, \"feedback\": \"Missing error handling for X.\", \"notes\": null}"
      },
      "schema_fingerprint": "<from the help call above>"
    }
  ]
}
```

Wait for the gate run and read its result. A wait is bounded, so check `ok` and
`all_terminal`; repeat the wait while it remains pending:

```json
{"ops": [{"op": "job.wait", "args": {"run_ids": ["<gate_run_id>"]}}]}
```

```json
{"ops": [{"op": "job.output", "args": {"run_id": "<gate_run_id>"}}]}
```

Write the parsed result to `$PLAY_DIR/_verdict.json`.

**Checkout-local alternative.** Inside a lionagi checkout,
`li agent claude --cwd "/absolute/path/to/<play_dir>" "<prompt>"` runs the same gate as a
foreground call and prints the JSON verdict to stdout directly, without a `run_id`
round-trip.

## Verdict JSON schema

```json
{"gate_passed": true,  "feedback": null,  "notes": "Criteria met."}
{"gate_passed": false, "feedback": "...", "notes": null}
```

| Field | Type | Meaning |
|---|---|---|
| `gate_passed` | bool | Whether all acceptance criteria were met |
| `feedback` | string \| null | Concrete feedback for the redo prompt (null when passed) |
| `notes` | string \| null | Human-readable summary (null when failed) |

## Decision logic

```
gate_passed = true            → merge to integration (procedure.md Step 6a)
gate_passed = false, attempt 1 → redo with feedback injected (procedure.md Step 6b)
gate_passed = false, attempt 2 → escalate (procedure.md Step 6c)
```

Gate is mandatory on both attempts. Never escalate without running the gate on attempt 2.

## Final-show verdict schema

Written to `$SHOW_DIR/_final_verdict.json`:

```json
{"show_passed": true,  "summary": "...", "blockers": []}
{"show_passed": false, "summary": "...", "blockers": ["play_name: reason"]}
```

## Grouping runs under one show

Invocation records can group related runs in Fleet through `li invoke start`/`li invoke end`.
The retired `/invocations` route redirects to Fleet. Opening and closing a record is CLI-only:
the MCP catalog names `invoke.start` and `invoke.end` as verbs it declines, because the
surface cannot tell that the caller who opened a record is the one closing it. Reading is
available over MCP — `invoke.list` returns what the CLI wrote. If you fire plays through the
`li` CLI directly inside a checkout, you can still open and close records:

```bash
# At show start
INV_ID=$(li invoke start --skill show --prompt "$TOPIC" 2>/dev/null || echo "")

# Each play (pass-through — li play forwards to a flow run)
li play <name> "<prompt>" --cwd "$WORKTREE" --yolo --bypass --invocation "$INV_ID"

# At show end
[ -n "$INV_ID" ] && li invoke end "$INV_ID" --status completed
# or: --status failed / --status aborted
```

Plays fired through `play.submit` are not grouped this way, and don't need to be: every
play's `run_id` is already recoverable from its own `_meta.json`, and `job.status`/
`job.output`/`job.wait` key on `run_id` directly rather than on an invocation.

Source: `lionagi/cli/invoke.py`.

---

## Abort protocol

**Soft abort** — blocks new play launches; running plays finish naturally:

```bash
touch "$SHOW_DIR/_ABORT"
```

Before firing any new play, check:

```bash
[ -f "$SHOW_DIR/_ABORT" ] && { echo "Show aborted — not launching $PLAY"; exit 0; }
```

**Hard abort** — stop running plays immediately:

```json
{"ops": [{"op": "job.kill", "args": {"run_id": "<run_id>"}}]}
```

Do this once per play directory with a non-terminal `_meta.json` status. Checkout-local
alternative: `li kill` stops in-progress runs, sessions, plays and shows from a terminal.
It resolves ids against the local state store rather than against this server's job records,
so use `job.kill` with the `run_id` you were given for a play you submitted over MCP.

Worktrees are preserved after abort for forensic review. Clean up manually:

```bash
git worktree list | grep "$TOPIC" | awk '{print $1}' | xargs -I{} git worktree remove --force {}
```

## Resume protocol

On resuming a show, classify each play directory and act:

| Play status | Action |
|---|---|
| `merged` | Skip — already in integration branch |
| `running` | Check the run: `job.status` for its `run_id`. Live → wait. Terminal but ungated → re-gate. |
| `running_complete` | Run gate (Step 5) — play finished but was not yet evaluated |
| `gate_failed` attempt 1 | Redo with feedback (Step 6b) |
| `gate_failed` attempt 2 | Escalate (Step 6c) |
| `escalated` | Log in `_show.md`, continue with other plays |
| `pending` | Check deps — if all deps merged, fire (Step 4) |

Resume check: `job.status` takes one `run_id` per call, but a single request's `ops` array
can carry several `job.status` entries — or one `job.wait` with a `run_ids` list — instead of
looping separate requests:

```json
{
  "ops": [
    {"op": "job.status", "args": {"run_id": "<run_id from play A's _meta.json>"}},
    {"op": "job.status", "args": {"run_id": "<run_id from play B's _meta.json>"}}
  ]
}
```

For any play whose `_meta.json` predates `play.submit` adoption and still has a `.pid` file
instead of a `run_id`, fall back to `kill -0 "$(cat "$play_dir/.pid")"` to check liveness.
