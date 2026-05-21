---
name: show
description: >
  Direct a multi-play DAG of `li play` invocations live — Claude is the director, each
  play is a 60-90 min auto-orchestrated subagent. Gates each play with `play-gate`
  (per-play) and `show-final-gate` (end-of-show), runs parallel plays in isolated
  worktrees, adapts the plan based on intermediate results. Suggest when: "run a show",
  "direct a show", "multi-play", "land an ADR end-to-end", "research → design →
  implement → review", or any goal spanning ≥3 plays where outputs cascade.
allowed-tools: [Bash, Read, Write, Edit, Glob, Grep]
---

# show

You are the director. A **play** is one `li play <playbook> "<prompt>"` invocation
running in its own worktree for 60-90 min — think of it as a higher-level
auto-orchestrated subagent. A **show** is a human-shaped DAG of plays.

This skill is for the *live* path: you decompose the goal, fire one (or a few
parallel) plays, **gate** each output, and decide what comes next *from what
you just saw* — not from a pre-authored script. If your plan would not change
based on intermediate results, you do not need this skill — author Play JSONs
and use the batch engine (`khive-internal/scripts/show.py`) instead.

## Mental model

```text
Show (this skill, you direct)
  ↓ fires
Play  = one `li play <playbook>` subprocess, 60-90 min, own worktree
  ↓ contains
FlowPlan = LLM-planned DAG inside the play (li play's own orchestrator)
  ↓ executes
FlowOp on a Branch (one agent turn inside the play)
```

A play sits at the granularity of a substantial subagent task — large enough
to justify its own LLM-planned sub-DAG, small enough that you can gate it
without losing context.

| Duration heuristic | Meaning |
|---|---|
| < 30 min | Should have been a single `Task()` subagent call, not a play. Don't use show. |
| 30-60 min | Borderline. OK if it produces non-trivial artifacts the next play depends on. |
| 60-90 min | Sweet spot. |
| 90-120 min | Acceptable. Watch for slippage. |
| > 120 min | Play is too big. Split it. Note in the decisions log so the next show learns. |

These are planning heuristics, not guarantees. Local history is better than
this table — track actual durations per play in `_meta.json` and use them
to recalibrate the next show.

## When to use

- Goal spans ≥3 plays where outputs cascade (research → design → impl → review)
- Each play deserves a gate (play-gate, Step 4) before the next fires
- The plan **should** adapt based on what each play produces

## When NOT to use

- Single play — just `li play X "..."` directly
- Sub-task DAG inside one play — that is FlowPlan's job (`li o flow`)
- Pre-decided pipeline with no adaptive decisions — author Play JSONs and use
  the batch engine (`khive-internal/scripts/show.py`) for concurrency + throttle
- < 3 plays — in-conversation sequencing is enough

## Workspace layout

One directory per show under a stable root. Recommended:

```text
$HOME/khive-work/shows/<topic>/
  _show.md                 director notes — plan, state, decisions log, cost
  _ABORT                   optional sentinel — director checks BEFORE fire, redo, AND merge
  _final_verdict.json      written after Step 7 (show-level gate)
  <play-name>/
    _intent.md             WHY this play exists (audience: director + resume)
    _prompt.md             WHAT goes to `li play` (audience: the play itself)
    _verdict.json          play-gate verdict (written after Step 4 gate)
    _meta.json             explicit schema (see below)
    .pid                   PID file (only present while subprocess is running)
    .log                   stdout+stderr capture
    <agent_id>/<file>      li play's per-agent artifact dirs (see Artifact path note)
```

Note: `$HOME/khive-work/...` is a show-local convention I (Ocean) adopted for
the `show` skill. It is intentionally separate from per-repo `khive-<layer>/`
worktrees used by long-lived sub-lambdas (foundation-work, platform-work,
etc.) which are layer-scoped, not topic-scoped.

### `_meta.json` schema (explicit)

```json
{
  "worktree": "/absolute/path/to/worktree",
  "branch": "show/<topic>/<play>",
  "attempt": 1,
  "started_at": "2026-05-19T19:00:00-04:00",
  "ended_at": "2026-05-19T20:25:00-04:00",
  "exit_code": 0,
  "merged_at": "2026-05-19T20:30:00-04:00",
  "status": "pending|running|gated|merged|escalated|aborted_after_finish",
  "team_missing": false,
  "model": "claude/claude-sonnet-4-6",
  "effort": "high"
}
```

Required at creation: `worktree`, `branch`, `attempt`, `started_at`.
Written by director at lifecycle transitions; never by the play itself.

### Artifact path note (read carefully)

`li play`'s internal agents write their files under `<save>/<agent_id>/`,
one subdir per agent in the FlowPlan. The orchestrator inside `li play`
chooses agent_ids dynamically — you do not know them ahead of time.

So upstream-reference rules:
- **Inside a play's prompt**, when telling the play to write artifacts:
  request specific filenames — the orchestrator inside the play will pick
  which agent writes which file. The play's own FlowPlan handles routing.
- **When polling for completion**: use the PID, not file presence.
- **When the gate lists artifacts**: use `find $SHOW_DIR/<play> -maxdepth 3 -type f` to walk the agent subdirs.
- **When a downstream play needs upstream artifacts**: tell it the exact
  glob, e.g. `$SHOW_DIR/research/*/landscape.md` (the `*` matches the
  agent_id), and instruct the play to read whichever it finds.

### Intent vs prompt — keep them separate

Intent is for you and future-you; prompt is what the play receives. They
serve different audiences and rot at different rates.

`_intent.md` template (keep short — 1-3 paragraphs):

```markdown
# Intent: <play-name>

## Goal
What this play must produce to be considered passing.

## Why this matters
Why does this play exist in the show? What does it unblock?

## References
- ADR-053 (Sinkhorn attention)
- Issue #142, #145
- Upstream play outputs: research artifacts under `$SHOW_DIR/research/*/`

## Acceptance
- [ ] Concrete artifact 1 (filename, what it must contain)
- [ ] Concrete artifact 2
- [ ] All tests in <module> pass

## Out of scope
Items deliberately excluded so the play-gate does not flag them as missing.
```

**The Acceptance section is REQUIRED.** Do not fire a play whose `_intent.md`
lacks a `## Acceptance` section with at least one `- [ ]` item. The play-gate
will fail the play with feedback "missing Acceptance checklist" if you skip
this — see Step 4.

## Procedure

### Step 0 — Initial plan + integration branch + LI alias

Write `_show.md`:

```markdown
# Show: <topic>

## Goal
<one paragraph — what done looks like for the WHOLE show>

## Repository
- Repo: <path>
- Integration branch: show/<topic>/integration (branched off <base>)
- Base for final merge: <main|develop|other>

## Plays
1. **<name>** [<playbook>] [eff <low|medium|high>] — <one-line objective> · deps: []
2. **<name>** [<playbook>] [eff <…>] — … · deps: [<other names>]

## Cost & time
- Track actual `started_at`/`ended_at` in each `_meta.json`.
- If a play's actual time exceeds 120 min OR observed spend looks unusual
  vs peer plays, pause before firing the next and ask Ocean.

## Cleanup-owed (populated on abort or final cleanup)
- Remote branches still on origin: (list)

## Decisions log
- (append entries as you adapt the plan — WHEN and WHY)
```

Define the lionagi CLI path once at the top of your shell session — `uv run`
fails to resolve the venv from a worktree cwd, and the absolute path works
from any directory:

```bash
LI="$(command -v li)"
SHOW_DIR="$HOME/khive-work/shows/<topic>"
TOPIC="<topic>"
```

**Effort levels** (from lionagi `EFFORT_MAP`): `low | medium | high | xhigh`.
Do NOT use `quick` — it is not a valid value and will be rejected or ignored
unpredictably.

Create the integration branch ONCE at show start, off the base:

```bash
cd <repo>
git fetch origin
git checkout -B show/${TOPIC}/integration origin/<base>
git push -u origin show/${TOPIC}/integration
```

All play branches will branch off this integration branch. Final merge to
`<base>` happens only after the show-level final gate passes (Step 7).

### Step 1 — Pick the next ready play, write intent + prompt

A play is "ready" when its `depends_on` are all `merged` (not just `gated`)
AND no upstream play is `escalated`.

Before firing, check the abort sentinel:

```bash
if [ -f "$SHOW_DIR/_ABORT" ]; then
  echo "Show aborted; not firing more plays."
  exit 1
fi
```

Validate naming — topic and play names must each match the lionagi flow-id
regex so they're safe as team names + branch segments. Lowercase + dashes,
1-32 chars each:

```bash
# `show_${TOPIC}_${PLAY}` becomes the team name; must match ^[A-Za-z0-9_-]{1,64}$
[[ "$TOPIC" =~ ^[a-z0-9-]{1,32}$ ]] || { echo "TOPIC must be lowercase alnum+dash, 1-32 chars"; exit 1; }
[[ "$PLAY" =~ ^[a-z0-9-]{1,32}$ ]]  || { echo "PLAY must match same"; exit 1; }
TEAM="show_${TOPIC}_${PLAY}"
[ ${#TEAM} -le 64 ] || { echo "Combined team name '$TEAM' is ${#TEAM} chars; lionagi limit is 64"; exit 1; }
```

Write `_intent.md` and `_prompt.md`. The prompt MUST:
- Name concrete artifact filenames so the gate can verify them.
- Reference upstream artifacts by GLOB across the upstream play's
  per-agent subdirs (e.g., `$SHOW_DIR/research/*/landscape.md`).
- Include "Out of scope" items so the gate doesn't flag them.

Preflight the intent file before firing:

```bash
grep -q '^## Acceptance' "$SHOW_DIR/$PLAY/_intent.md" \
  && grep -q '^- \[ \]' "$SHOW_DIR/$PLAY/_intent.md" \
  || { echo "ERROR: $PLAY/_intent.md missing Acceptance checklist"; exit 1; }
```

### Step 2 — Worktree per play (mandatory)

Every play runs in its own worktree on its own branch. There is no
"artifact-only skips worktree" exception — even research/design-doc plays
get a worktree. If they don't touch the repo, Step 5a's merge will be a
no-op (`git diff` on BR vs integration is empty) and produces a trivial
merge commit. The uniform lifecycle simplifies the state machine.

If a play genuinely should not affect the repo, write its artifacts only
into `$SHOW_DIR/$PLAY/` (NOT inside `$WT`) and the merge will be empty.

```bash
PLAY=<play-name>
WT="$HOME/khive-work/worktrees/${TOPIC}-${PLAY}"
BR="show/${TOPIC}/${PLAY}"

cd <repo>
git worktree add -b "$BR" "$WT" "show/${TOPIC}/integration"

# Initialize _meta.json with required fields
jq -n \
  --arg wt "$WT" \
  --arg br "$BR" \
  --argjson attempt 1 \
  --arg t "$(date -Iseconds)" \
  '{worktree:$wt, branch:$br, attempt:$attempt, started_at:$t, status:"pending"}' \
  > "$SHOW_DIR/$PLAY/_meta.json"
```

### Step 3 — Fire the play

#### Foreground (single play, blocks until done)

```bash
"$LI" play <playbook> "$(cat $SHOW_DIR/$PLAY/_prompt.md)" \
  --save "$SHOW_DIR/$PLAY" \
  --cwd "$WT" \
  --yolo \
  --bypass \
  --effort <low|medium|high> \
  --team-mode "show_${TOPIC}_${PLAY}"
EC=$?

# Record exit + ended_at
tmp=$(mktemp)
jq --argjson ec "$EC" --arg t "$(date -Iseconds)" \
  '.exit_code=$ec | .ended_at=$t | .status="running_complete"' \
  "$SHOW_DIR/$PLAY/_meta.json" > "$tmp" && mv "$tmp" "$SHOW_DIR/$PLAY/_meta.json"
```

#### Background (parallel independent plays — max 3 concurrent)

The bg wrapper owns the subprocess lifecycle so exit code + timestamps are
always recorded, even if the director's shell crashes between fire and wait.

```bash
(
  "$LI" play <playbook> "$(cat $SHOW_DIR/$PLAY/_prompt.md)" \
    --save "$SHOW_DIR/$PLAY" \
    --cwd "$WT" \
    --yolo \
    --bypass \
    --effort <low|medium|high> \
    --team-mode "show_${TOPIC}_${PLAY}"
  ec=$?
  tmp=$(mktemp)
  jq --argjson ec "$ec" --arg t "$(date -Iseconds)" \
    '.exit_code=$ec | .ended_at=$t | .status="running_complete"' \
    "$SHOW_DIR/$PLAY/_meta.json" > "$tmp" && mv "$tmp" "$SHOW_DIR/$PLAY/_meta.json"
  rm -f "$SHOW_DIR/$PLAY/.pid"
) > "$SHOW_DIR/$PLAY/.log" 2>&1 &
echo $! > "$SHOW_DIR/$PLAY/.pid"

# Stagger before firing the next bg play to avoid API rate-limit bursts
sleep 120
```

Stagger justification: primarily for API rate-limiting. For plays on
different repos with no shared resources, 30-60s may suffice. Same repo
with concurrent workers can also help avoid worktree contention.

Poll via PID + verify it's still our process (avoid PID-recycle races):

```bash
while ps -p "$(cat $SHOW_DIR/$PLAY/.pid)" -o command= 2>/dev/null | grep -q "li play"; do
  sleep 30
done
```

Bash `run_in_background=true` notifications fire when the *wrapper* exits,
instantly — NOT when the play completes. Do not rely on them.

### Step 4 — Gate the play

Check the subprocess exit code FIRST. Non-zero → diagnose before any redo;
do not feed a crash trace to the gate as "play output".

```bash
EC=$(jq -r '.exit_code // empty' "$SHOW_DIR/$PLAY/_meta.json")
if [ "$EC" != "0" ]; then
  echo "Subprocess exited $EC; diagnose before redoing."
  # Inspect .log; decide whether to retry, fix the prompt, or escalate.
fi
```

Then fire the per-play gate via `play-gate`. Use `find` to walk the
per-agent artifact tree (artifacts are nested):

```bash
ARTIFACT_TREE="$(cd $SHOW_DIR/$PLAY && find . -maxdepth 3 -type f \
  ! -name '.*' ! -name '_intent.md' ! -name '_prompt.md' \
  ! -name '_verdict.json' ! -name '_meta.json' | sort)"

"$LI" agent -a play-gate --cwd "$WT" --yolo --bypass "$(cat <<EOF
Gate this play.

Intent (why this play exists):
$(cat $SHOW_DIR/$PLAY/_intent.md)

Original prompt:
$(cat $SHOW_DIR/$PLAY/_prompt.md)

Subprocess exit code: ${EC:-unknown}

Artifact tree under $SHOW_DIR/$PLAY/ (relative paths):
$ARTIFACT_TREE

Read the relevant files from those paths. Evaluate strictly against the
Acceptance checklist in the intent. Items in "Out of scope" are forbidden
grounds for failing.

Respond as JSON ONLY:
{"gate_passed": <true|false>, "feedback": "<actionable items if failed, null if passed>", "notes": "<optional advisory, null otherwise>"}
EOF
)" > "$SHOW_DIR/$PLAY/_verdict.json"
```

Validate the verdict — but accept boolean `false` as a valid value
(do NOT use `jq -e '.gate_passed'`; `-e` returns failure for falsy values).

```bash
jq -e 'has("gate_passed") and (.gate_passed | type == "boolean")' \
  "$SHOW_DIR/$PLAY/_verdict.json" >/dev/null || {
  echo "Gate returned malformed JSON — treating as failed gate"
  printf '%s\n' '{"gate_passed":false,"feedback":"gate output not JSON; manual review needed","notes":null}' \
    > "$SHOW_DIR/$PLAY/_verdict.json"
}

# Mark status
PASSED=$(jq -r '.gate_passed' "$SHOW_DIR/$PLAY/_verdict.json")
tmp=$(mktemp)
jq --arg s "$( [ "$PASSED" = "true" ] && echo "gated" || echo "gate_failed" )" \
  '.status=$s' "$SHOW_DIR/$PLAY/_meta.json" > "$tmp" && mv "$tmp" "$SHOW_DIR/$PLAY/_meta.json"
```

### Step 5 — Decide

| Verdict | Action |
|---|---|
| `gate_passed: true` | Merge play branch → integration (Step 5a). |
| Failed, attempt == 1 | Redo (Step 5b). |
| Failed, attempt == 2 | ESCALATE (Step 5c). |
| Non-zero subprocess exit | Step 5d (diagnose). |

#### 5a — Merge on pass

First, refresh the play branch against current integration (other plays may
have merged ahead of you):

```bash
# Re-check abort sentinel before mutating integration
[ -f "$SHOW_DIR/_ABORT" ] && {
  tmp=$(mktemp "$SHOW_DIR/$PLAY/.meta.XXXXXX")
  jq '.status="aborted_after_finish"' "$SHOW_DIR/$PLAY/_meta.json" > "$tmp" \
    && mv "$tmp" "$SHOW_DIR/$PLAY/_meta.json"
  echo "Show aborted; play $PLAY passed but not merged."
  exit 0
}

cd "$WT"
git fetch origin
git merge --no-ff "show/${TOPIC}/integration" \
  -m "Show ${TOPIC}: refresh ${PLAY} from integration"
# Resolve only objectively trivial conflicts (see Conflict policy below).
# Re-run acceptance fast-checks (cargo test for the affected scope, etc.).
# If any test fails post-refresh → ESCALATE, do not merge into integration.

# Then merge play branch INTO integration with a stable, grep-able message:
cd <repo>  # main checkout
git checkout "show/${TOPIC}/integration"
git merge --no-ff "${BR}" -m "Show ${TOPIC}: integrate ${PLAY}"
MERGE_SHA=$(git rev-parse HEAD)
git push origin "show/${TOPIC}/integration"

# Record merge time + SHA (Step 7 rollback uses this SHA, not a grep)
tmp=$(mktemp "$SHOW_DIR/$PLAY/.meta.XXXXXX")
jq --arg t "$(date -Iseconds)" --arg sha "$MERGE_SHA" \
  '.merged_at=$t | .merge_sha=$sha | .status="merged"' \
  "$SHOW_DIR/$PLAY/_meta.json" > "$tmp" && mv "$tmp" "$SHOW_DIR/$PLAY/_meta.json"
```

**Conflict policy (objective)**. Trivial conflicts are limited to:
- Non-overlapping additions in different files.
- Pure formatting conflicts where `git diff -w` is empty.
- Generated files where the regenerator produces a clean diff from source.

Everything else is semantic and escalates:
- Same-hunk source/test/config/docs conflicts.
- Delete-vs-edit, renames, public API changes.
- Dependency lockfiles (unless regenerated from source and re-tested).

#### 5b — Redo (after first failure)

Re-check abort sentinel, verify team exists (`--team-attach` upserts; if the
prior team is missing, the redo silently loses memory):

```bash
[ -f "$SHOW_DIR/_ABORT" ] && { echo "Show aborted; not redoing"; exit 0; }

# Team existence check (resolve by name — team files are UUID-named, not name-named)
TEAM="show_${TOPIC}_${PLAY}"
if ! "$LI" team show "$TEAM" >/dev/null 2>&1; then
  echo "WARN: prior team '$TEAM' missing; redo will start without memory."
  tmp=$(mktemp "$SHOW_DIR/$PLAY/.meta.XXXXXX")
  jq '.team_missing=true' "$SHOW_DIR/$PLAY/_meta.json" > "$tmp" \
    && mv "$tmp" "$SHOW_DIR/$PLAY/_meta.json"
fi

# Bump attempt
tmp=$(mktemp); jq '.attempt=2 | .status="redoing" | del(.exit_code, .ended_at)' \
  "$SHOW_DIR/$PLAY/_meta.json" > "$tmp" && mv "$tmp" "$SHOW_DIR/$PLAY/_meta.json"

# Build redo prompt in a temp file and pass contents as one quoted positional arg.
# Never interpolate feedback text directly into a command string.
REDO_PROMPT_FILE=$(mktemp "$SHOW_DIR/$PLAY/.redo_prompt.XXXXXX")
FEEDBACK=$(jq -r '.feedback // ""' "$SHOW_DIR/$PLAY/_verdict.json")
printf '## Previous attempt feedback (fix these):\n%s\n\n---\n\n%s' \
  "$FEEDBACK" "$(cat "$SHOW_DIR/$PLAY/_prompt.md")" > "$REDO_PROMPT_FILE"

# Re-fire (same as Step 3 foreground), but with --team-attach (NOT --team-mode)
"$LI" play <playbook> "$(cat "$REDO_PROMPT_FILE")" \
  --save "$SHOW_DIR/$PLAY" \
  --cwd "$WT" \
  --yolo \
  --bypass \
  --effort <low|medium|high> \
  --team-attach "show_${TOPIC}_${PLAY}"
EC=$?
rm -f "$REDO_PROMPT_FILE"

# Record + re-gate (same as Step 4)
tmp=$(mktemp); jq --argjson ec "$EC" --arg t "$(date -Iseconds)" \
  '.exit_code=$ec | .ended_at=$t' \
  "$SHOW_DIR/$PLAY/_meta.json" > "$tmp" && mv "$tmp" "$SHOW_DIR/$PLAY/_meta.json"
# (then run Step 4 again — same jq validation, same JSON parsing)
```

**Never pass both `--team-mode` and `--team-attach`** on the same invocation
— lionagi rejects them as mutually exclusive. First fire uses `--team-mode`;
redos use `--team-attach`.

#### 5c — Escalate (after second failure)

```bash
tmp=$(mktemp); jq '.status="escalated"' "$SHOW_DIR/$PLAY/_meta.json" > "$tmp" \
  && mv "$tmp" "$SHOW_DIR/$PLAY/_meta.json"
```

Mark all downstream plays (transitively dependent on this one) as `blocked`.
Stop firing new plays. Surface to Ocean:

```text
ESCALATED: <play-name>
Two attempts failed. Critic feedback: <verdict.feedback>
Downstream blocked: <list>

Options:
  (a) Widen the prompt and try again — name the change you'd make.
  (b) Director writes the missing piece directly, then re-runs Step 4
      on the now-updated artifacts. The play is not complete until that
      re-gate passes — manual fixes do NOT bypass the gate.
  (c) Accept partial result; continue with downstreams that don't depend on this play.
  (d) Abort the show.
```

Do not auto-fire any option. Wait for Ocean.

#### 5d — Subprocess crash (non-zero exit)

Diagnose `.log` first. Common causes: API rate-limit / model timeout / OOM /
prompt size / playbook arg error. Do not redo blindly. If the cause is
transient (rate-limit), wait + retry as Step 5b (uses `--team-attach`,
preserving any partial team state). If the cause is the prompt or playbook,
fix the prompt and redo. If unknown, escalate.

### Step 6 — Adapt the plan (between plays)

Before firing the next play, re-read `_show.md` and ask:

- Did the prior play change WHAT the next play should do?
- New plays to add? Old ones to drop?
- Is the goal still right?

Update `_show.md` BEFORE firing. Log in the decisions log:

```markdown
## Decisions log
- 2026-05-19 18:42: research/*/landscape.md surfaced OT-LR as stronger baseline
  than Sinkhorn alone. Updated design play to emphasize OT-LR; added a new
  play 2.5 (literature_verify) before design fires.
```

This adaptive step is THE reason to use show instead of a static runner. If
you never update `_show.md` between plays, you should not be using this skill.

### Step 7 — Show-level final gate

After every play has merged (not just gated), run the end-to-end gate using
the `show-final-gate` profile (purpose-built for JSON output across plays;
see "Custom agent profiles" below):

Extract the goal precisely (the simpler awk range can terminate on the same
line and lose the body):

```bash
GOAL=$(awk '
  /^## Goal$/ {in_goal=1; print; next}
  in_goal && /^## / {exit}
  in_goal {print}
' "$SHOW_DIR/_show.md")

PLAY_DIRS=$(ls -d "$SHOW_DIR"/*/ 2>/dev/null | sort)
PLAY_ARTIFACTS=$(find "$SHOW_DIR" -maxdepth 4 -type f \
  ! -name '.*' ! -name '_show.md' ! -name '_ABORT' ! -name '_final_verdict.json' \
  | sort)

"$LI" agent -a show-final-gate --effort high --cwd "$SHOW_DIR" --yolo --bypass "$(cat <<EOF
Final review of show "${TOPIC}".

Show dir: $SHOW_DIR
Repo: <repo>
Integration branch: show/${TOPIC}/integration

Original goal:
$GOAL

Play directories (absolute paths — read each play's _intent.md, _verdict.json, _meta.json, and artifact subdirs):
$PLAY_DIRS

All artifact files across all plays:
$PLAY_ARTIFACTS

Decisions log:
$(awk '/^## Decisions log$/,0' $SHOW_DIR/_show.md)

Evaluate:
1. Did the SHOW achieve the original goal — not just per-play gates?
2. Cross-play inconsistencies that per-play gates missed?
3. Tests that pass per-play but fail when integrated?
4. Is integration branch safe to merge to <base>?

Respond JSON ONLY:
{"show_passed": <bool>, "blockers": [...], "recommendations": [...], "goal_assessment": "<one paragraph>", "cross_play_findings": [...]}
EOF
)" > "$SHOW_DIR/_final_verdict.json"

# Validate — tighter than Step 4. Requires blockers/recommendations/cross_play_findings to be
# arrays, goal_assessment to be a string, and any failure to name at least one blocker.
jq -e '
  has("show_passed") and (.show_passed | type == "boolean") and
  (.blockers | type == "array") and
  (.recommendations | type == "array") and
  (.cross_play_findings | type == "array") and
  (.goal_assessment | type == "string") and
  ((.show_passed == true) or ((.blockers | length) > 0))
' "$SHOW_DIR/_final_verdict.json" >/dev/null || {
  echo "Final gate returned non-JSON or violated schema; treating as needs-review."
  printf '%s\n' '{"show_passed":false,"blockers":["final-gate output invalid; manual review needed"],"recommendations":[],"goal_assessment":"","cross_play_findings":[]}' \
    > "$SHOW_DIR/_final_verdict.json"
}
```

If `show_passed: false`:

```text
Options:
  - Treat each blocker as a new play (re-enter Step 1).
  - Roll back specific play merges (see rollback below) and re-decompose.
  - Accept partial + ship anyway (with Ocean's approval).
```

**Rollback** (after merge, when final gate finds an irreparable cross-play issue):

```bash
# The play's merge SHA was recorded in _meta.json at Step 5a
MERGE_SHA=$(jq -r '.merge_sha // empty' "$SHOW_DIR/$PLAY/_meta.json")
[ -n "$MERGE_SHA" ] || MERGE_SHA=$(git log --oneline "show/${TOPIC}/integration" \
  | grep "integrate ${PLAY}" | head -1 | awk '{print $1}')
git revert -m 1 "$MERGE_SHA"
```

Reverted merges cannot be re-merged cleanly via `git merge`. If multiple
rollbacks are needed, prefer to `git reset --hard <last-known-good>` on a
fresh integration branch and re-merge only the valid plays. Note this in
the decisions log.

### Step 8 — Final merge + cleanup

Only after `show_passed: true`:

```bash
cd <repo>
git checkout <base>
git pull origin <base>
git merge --no-ff "show/${TOPIC}/integration" -m "Show ${TOPIC}: integrate"
# Resolve conflicts if base moved. Inspect the diff manually.
# DO NOT auto-push — surface the diff to Ocean before pushing.
```

Then prune worktrees and branches (after Ocean confirms forensics complete):

```bash
for wt in $HOME/khive-work/worktrees/${TOPIC}-*; do
  git worktree remove "$wt"
done
git branch -d show/${TOPIC}/integration
git push origin --delete show/${TOPIC}/integration
# Optional: delete play branches on remote after 1 week or per Ocean's policy.
```

## Resume protocol (first-class — multi-hour shows survive compaction)

When picking up a show mid-flight (after Claude session ended or context
compacted), do these reads + rehydration BEFORE doing anything else.

### Rehydrate shell variables

```bash
LI="$(command -v li)"
SHOW_DIR="$HOME/khive-work/shows/<topic>"  # use the show dir the resume targets
TOPIC=$(basename "$SHOW_DIR")
REPO="$(awk -F': ' '/^- Repo: / {print $2; exit}' $SHOW_DIR/_show.md)"

# Per-play variables (set inside a loop over plays):
# PLAY=$(basename "$play_dir")
# WT=$(jq -r '.worktree // empty' "$play_dir/_meta.json")
# BR=$(jq -r '.branch // empty' "$play_dir/_meta.json")
```

### Read state

1. `cat $SHOW_DIR/_show.md` — original plan + decisions log
2. `ls $SHOW_DIR/` — see which plays exist + check `_ABORT` + check `_final_verdict.json`
3. For each play dir: read `_intent.md`, `_meta.json`, `_verdict.json` (if exists), `.pid` (if exists)

Then classify each play by **ordered precedence** — apply in order, first
match wins:

```text
1. `$SHOW_DIR/_ABORT` exists
   → ALL plays: aborted_pending_cleanup. Do not fire, redo, or merge without Ocean.

2. play's `.pid` file exists AND `ps -p $(cat .pid) -o command=` contains "li play"
   → state: running. Poll until exit, then re-gate.

3. play's `.pid` file exists but PID dead OR command doesn't match
   → state: state_corrupt_manual_review. Inspect `.log`; the wrapper crashed
     before recording exit code. Surface to Ocean.

4. `_verdict.json gate_passed:true` AND `_meta.merged_at` present
   → state: completed. Skip.

5. `_verdict.json gate_passed:true` AND `_meta.merged_at` absent AND no `.pid` alive
   → state: gated_passed_pending_merge. Verify with `git log --oneline
     show/${TOPIC}/integration | grep -q "integrate ${PLAY}"`. If absent,
     proceed to Step 5a (merge). If present (race: merge succeeded but
     meta not updated), patch `_meta.merged_at` from git log timestamp.

6. `_verdict.json gate_passed:false` AND `_meta.attempt == 1`
   → state: needs_redo. Proceed to Step 5b.

7. `_verdict.json gate_passed:false` AND `_meta.attempt == 2`
   → state: escalated. Surface to Ocean.

8. `_verdict.json gate_passed:false` AND `_meta.attempt` missing
   → state: state_corrupt_manual_review. Surface to Ocean.

9. `_verdict.json` present AND `_meta.json` missing
   → state: state_corrupt_manual_review. Surface to Ocean.

10. no `.pid` AND `_meta.exit_code` present (or `_meta.status == "running_complete"`)
    AND no `_verdict.json`
    → state: run_complete_pending_gate. Subprocess finished but gate was never run.
       Proceed to Step 4 — do NOT re-fire the play.

11. `_intent.md` + `_meta.json` present AND no `.pid` AND no `_verdict.json`
    AND no `_meta.exit_code` (and `_meta.status == "pending"`)
    → state: prepared_not_fired. Proceed to Step 3.

12. `_intent.md` present, no `_meta.json`, no `.pid`, no `_verdict.json`
    → state: pending. Proceed to Step 2.
```

`state_corrupt_manual_review` always escalates. Do not attempt to repair
corrupt state without Ocean.

## Abort protocol

**Soft abort** (no more launches, no more integration mutations; in-flight
plays finish but their gate results are not auto-merged):

```bash
touch "$SHOW_DIR/_ABORT"
```

The director checks this sentinel at:
- Step 1 (before firing next play)
- Step 5a (before merging a passed play)
- Step 5b (before redoing a failed play)

If a play finishes after `_ABORT` is set, Step 5a records
`status:"aborted_after_finish"` instead of merging.

**Hard abort** (kill in-flight plays now):

```bash
for pidfile in $SHOW_DIR/*/.pid; do
  pid=$(cat "$pidfile" 2>/dev/null) || continue
  kill -TERM "$pid" 2>/dev/null
done
sleep 5
# Best-effort orphan cleanup (PID-based kill above is primary):
pkill -9 -f "li play.*show_${TOPIC}_" 2>/dev/null || true
```

Worktrees stay intact after abort — forensics first, cleanup later. The
remote `show/${TOPIC}/integration` branch is left on origin; record it in
`_show.md`'s "Cleanup-owed" section for Ocean to delete later (or via the
`/clean` skill).

## Custom agent profiles

If a default profile doesn't fit, write a new one. Canonical location:
`~/.lionagi/agents/<name>.md`. Format:

```markdown
---
model: claude/claude-sonnet-4-6
effort: medium
yolo: true
---

# α[Name]

Mission and behavior body here. Plain markdown after frontmatter.
```

**Profiles the show skill ships with**:

- `play-gate` — per-play gatekeeper (Step 4). Sonnet, effort:medium,
  JSON-only output. Checks Acceptance checklist against artifacts.
- `show-final-gate` — show-level synthesis gate (Step 7). Sonnet, effort:high,
  JSON-only output. Checks cross-play coherence + original goal.

Both are separate from the existing `critic.md` profile because that
profile's contract is severity-tiered prose, incompatible with the JSON
parsing the show skill needs. We isolate the new behavior in new profiles
rather than mutating the existing one.

## Cost & time awareness

A play at high effort is typically 60-90 min wall clock. Actual cost varies
significantly with model + effort + how many sub-agents the FlowPlan
allocates inside the play. Lionagi does not yet expose spend per run, so
treat cost as observed-only: investigate if one play takes substantially
longer or feels more expensive than peer plays.

Recalibration loop: at the end of each show, append a one-line "actual time
per play" note to `_show.md` so future shows can decompose better.

## Failure cascade rules

When a play is escalated:

1. Director sets `status: escalated` in that play's `_meta.json`.
2. All plays in the escalated play's **transitive downstream cone** are
   marked `blocked` — they do NOT auto-fire.
3. Plays whose dependency cone does NOT include the escalated play MAY
   continue if their deps are met (parallel branches of the show DAG).
4. The director surfaces options to Ocean (5c list) and waits.

"Downstream cone of P": all plays that transitively depend on P. Compute
by walking `depends_on` forward from P.

## Common gotchas

- **`run_in_background` ≠ play completion.** Notification fires on wrapper
  exit, instantly. Use PID polling + command-line verification.
- **`uv run li` from worktree cwd fails.** Use `$LI` (set in Step 0 via `command -v li`).
- **Foreground play blocks 60-90 min.** Use bg + polling unless you want
  to wait synchronously.
- **Each play gets its own `--save` subdir.** Sharing dirs clobbers artifacts.
- **`li play` agents write to `<save>/<agent_id>/`, not `<save>/`.** Use
  `find` to discover artifacts; don't poll fixed top-level filenames.
- **`--team-attach` and `--team-mode` are mutually exclusive.** First fire =
  mode; redos = attach. Verify team file exists before attaching.
- **Gate validation: never use `jq -e '.gate_passed'`** — `-e` returns
  failure for falsy values, misclassifying valid `false` verdicts.
  Always check `has("gate_passed") and (.gate_passed | type == "boolean")`.
- **Don't auto-push final base merge.** Inspect diff first.
- **Manual fixes after escalation MUST re-gate.** Do not mark a play
  complete by hand; re-run Step 4 on the updated artifacts.

## End-to-end example

Show: "Land ADR-053 (Sinkhorn attention) in lattice"

Initial `_show.md`:

```markdown
# Show: adr-053-sinkhorn

## Goal
Implement, test, and review the Sinkhorn-attention path in lattice-inference per ADR-053.

## Repository
Repo: <path-to-repo>
Integration: show/adr-053-sinkhorn/integration
Base: main

## Plays
1. **research**  [research]  [eff high]    — survey OT-attention literature · deps: []
2. **design**    [feature]   [eff high]    — write ADR-053 draft · deps: [research]
3. **implement** [feature]   [eff high]    — impl Sinkhorn solver + tests · deps: [design]
4. **review**    [pr-review] [eff medium]  — multi-perspective gate · deps: [implement]
```

Run:

```text
research        → gate pass → merge → adapt: surfaces OT-LR as stronger baseline
                              ↓
                              Update _show.md: design emphasizes OT-LR; add play 2.5 literature_verify
literature_verify → gate pass → merge
design            → gate fail 1st (missing ablation) → redo (--team-attach) → pass → merge
implement         → gate fail 2x (test coverage) → ESCALATE
                              ↓
                              Director surfaces options.
                              Ocean picks "(b) director writes missing tests".
                              Director writes tests in worktree, re-runs Step 4.
                              Re-gate passes. Merge.
review            → gate pass → merge
Step 7 show-final-gate → show_passed:true → final merge to main (manual push by Ocean)
                       → cleanup worktrees + delete play branches per policy
```

The decisions log + final summary feed the next show's planning so
decomposition gets better over time.
