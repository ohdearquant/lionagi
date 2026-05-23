# Gate Protocol

Gate agents, verdict format, decision logic, and abort/resume protocols.

## Per-play gate agent

After the play completes (`running_complete`), run a gate agent to evaluate its output:

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

Write the result to `$PLAY_DIR/_verdict.json`.

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
