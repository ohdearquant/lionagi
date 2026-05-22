# show Plugin

Multi-play DAG orchestration with quality gates and isolated workspaces — you are the director. Each play is a 60-90 minute auto-orchestrated `li play` subagent running in its own git worktree. The show skill connects plays into a human-shaped DAG, gates each output, and adapts the plan based on intermediate results.

**Source**: `marketplace/show/`  
**Install**: `claude /plugin install show@lionagi`  
**Version**: 0.1.0 (Apache-2.0)

!!! note "Live orchestration only"
    This plugin is for the *live* path where the plan adapts based on what each play produces. If your pipeline would not change based on intermediate results, author Play JSONs and use the batch engine (`khive-internal/scripts/show.py`) instead.

## Skills

| Skill | Description |
|---|---|
| [`/show`](#show) | Direct a multi-play DAG of `li play` invocations live — gate each play, adapt the plan, run parallel plays in isolated worktrees |

### `/show`

> **Source**: `marketplace/show/skills/show/SKILL.md`

You decompose a goal into plays, fire one (or a few parallel) plays, gate each output with `play-gate`, and decide what comes next from what you just saw.

**When to use**: goal spans ≥3 plays where outputs cascade (research → design → impl → review); each play deserves a gate before the next fires; the plan should adapt based on what each play produces.

**When NOT to use**: single play (just `li play X "..."` directly); sub-task DAG inside one play (use `li o flow`); pre-decided pipeline with no adaptive decisions; fewer than 3 plays.

---

#### Mental model

```text
Show  (this skill — you direct)
  ↓ fires
Play  = one `li play <playbook>` subprocess, 60–90 min, own worktree
  ↓ contains
FlowPlan = LLM-planned DAG inside the play (li play's orchestrator)
  ↓ executes
FlowOp on a Branch (one agent turn inside the play)
```

| Duration heuristic | Meaning |
|---|---|
| < 30 min | Should have been a single `Task()` subagent call — don't use show |
| 30–60 min | Borderline; OK if it produces non-trivial artifacts the next play depends on |
| 60–90 min | Sweet spot |
| 90–120 min | Acceptable — watch for slippage |
| > 120 min | Play is too big — split it |

---

#### Workspace layout

```text
$HOME/khive-work/shows/<topic>/
  _show.md                 director notes — plan, state, decisions log, cost
  _ABORT                   optional sentinel — director checks before fire, redo, and merge
  _final_verdict.json      written after show-level gate (Step 7)
  <play-name>/
    _intent.md             WHY this play exists (audience: director + resume)
    _prompt.md             WHAT goes to `li play`
    _verdict.json          play-gate verdict (written after Step 4 gate)
    _meta.json             lifecycle metadata
    .pid                   PID file (only present while subprocess is running)
    .log                   stdout+stderr capture
    <agent_id>/<file>      per-agent artifact dirs
```

`_meta.json` schema:

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

!!! tip "Artifact path note"
    `li play`'s internal agents write files under `<save>/<agent_id>/`. Agent IDs are chosen dynamically by the play's orchestrator — you don't know them ahead of time. Use `find $SHOW_DIR/<play> -maxdepth 3 -type f` to walk artifacts; downstream plays reference upstream files with globs like `$SHOW_DIR/research/*/landscape.md`.

---

#### Intent vs prompt

Keep them separate — they serve different audiences and rot at different rates.

`_intent.md` template:

```markdown
# Intent: <play-name>

## Goal
What this play must produce to be considered passing.

## Why this matters
Why does this play exist in the show? What does it unblock?

## References
- ADR-053
- Upstream play outputs: $SHOW_DIR/research/*/

## Acceptance
- [ ] Concrete artifact 1 (filename, what it must contain)
- [ ] Concrete artifact 2

## Out of scope
Items deliberately excluded so the play-gate does not flag them as missing.
```

!!! warning "Acceptance checklist is required"
    Do not fire a play whose `_intent.md` lacks a `## Acceptance` section with at least one `- [ ]` item. The `play-gate` will fail the play with feedback "missing Acceptance checklist".

---

#### Procedure

=== "Step 0 — Plan + integration branch"

    Write `_show.md` with goal, repo, play list, cost tracking, and decisions log section.

    Define the CLI path once:

    ```bash
    LI="$(command -v li)"
    SHOW_DIR="$HOME/khive-work/shows/<topic>"
    TOPIC="<topic>"
    ```

    Create the integration branch off the base:

    ```bash
    cd <repo>
    git fetch origin
    git checkout -B show/${TOPIC}/integration origin/<base>
    git push -u origin show/${TOPIC}/integration
    ```

    Validate topic and play name format:

    ```bash
    [[ "$TOPIC" =~ ^[a-z0-9-]{1,32}$ ]] || { echo "TOPIC must be lowercase alnum+dash"; exit 1; }
    [[ "$PLAY"  =~ ^[a-z0-9-]{1,32}$ ]] || { echo "PLAY must match same"; exit 1; }
    TEAM="show_${TOPIC}_${PLAY}"
    [ ${#TEAM} -le 64 ] || { echo "Team name too long"; exit 1; }
    ```

=== "Step 1 — Pick next ready play"

    A play is ready when all its `depends_on` plays have status `merged` (not just `gated`) and no upstream play is `escalated`.

    Check abort sentinel before firing:

    ```bash
    [ -f "$SHOW_DIR/_ABORT" ] && { echo "Show aborted."; exit 1; }
    ```

    Preflight intent file before firing:

    ```bash
    grep -q '^## Acceptance' "$SHOW_DIR/$PLAY/_intent.md" \
      && grep -q '^- \[ \]' "$SHOW_DIR/$PLAY/_intent.md" \
      || { echo "ERROR: missing Acceptance checklist"; exit 1; }
    ```

=== "Step 2 — Worktree per play (mandatory)"

    Every play runs in its own worktree on its own branch — no exceptions, even for research/design-doc plays.

    ```bash
    PLAY=<play-name>
    WT="$HOME/khive-work/worktrees/${TOPIC}-${PLAY}"
    BR="show/${TOPIC}/${PLAY}"

    cd <repo>
    git worktree add -b "$BR" "$WT" "show/${TOPIC}/integration"

    jq -n \
      --arg wt "$WT" --arg br "$BR" --argjson attempt 1 \
      --arg t "$(date -Iseconds)" \
      '{worktree:$wt,branch:$br,attempt:$attempt,started_at:$t,status:"pending"}' \
      > "$SHOW_DIR/$PLAY/_meta.json"
    ```

=== "Step 3 — Fire the play"

    Foreground (single play, blocks until done):

    ```bash
    "$LI" play <playbook> "$(cat $SHOW_DIR/$PLAY/_prompt.md)" \
      --save "$SHOW_DIR/$PLAY" \
      --cwd "$WT" \
      --yolo \
      --bypass \
      --effort <low|medium|high> \
      --team-mode "show_${TOPIC}_${PLAY}"
    EC=$?
    ```

    Background (parallel independent plays — max 3 concurrent):

    ```bash
    (
      "$LI" play <playbook> "$(cat $SHOW_DIR/$PLAY/_prompt.md)" \
        --save "$SHOW_DIR/$PLAY" \
        --cwd "$WT" \
        --yolo --bypass \
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
    sleep 120  # stagger for API rate-limit
    ```

    Poll via PID:

    ```bash
    while ps -p "$(cat $SHOW_DIR/$PLAY/.pid)" -o command= 2>/dev/null | grep -q "li play"; do
      sleep 30
    done
    ```

=== "Step 4 — Gate the play"

    Check subprocess exit code first — non-zero means diagnose before any redo.

    ```bash
    EC=$(jq -r '.exit_code // empty' "$SHOW_DIR/$PLAY/_meta.json")
    if [ "$EC" != "0" ]; then
      echo "Subprocess exited $EC; diagnose before redoing."
    fi
    ```

    Run `play-gate`:

    ```bash
    ARTIFACT_TREE="$(cd $SHOW_DIR/$PLAY && find . -maxdepth 3 -type f \
      ! -name '.*' ! -name '_intent.md' ! -name '_prompt.md' \
      ! -name '_verdict.json' ! -name '_meta.json' | sort)"

    "$LI" agent -a play-gate --cwd "$WT" --yolo --bypass "$(cat <<EOF
    Gate this play.
    Intent: $(cat $SHOW_DIR/$PLAY/_intent.md)
    Prompt: $(cat $SHOW_DIR/$PLAY/_prompt.md)
    Exit code: ${EC:-unknown}
    Artifact tree: $ARTIFACT_TREE
    Respond JSON ONLY:
    {"gate_passed": <true|false>, "feedback": "...", "notes": "..."}
    EOF
    )" > "$SHOW_DIR/$PLAY/_verdict.json"
    ```

    !!! warning "Never use `jq -e '.gate_passed'`"
        The `-e` flag returns failure for falsy values, misclassifying valid `false` verdicts. Always validate with `jq -e 'has("gate_passed") and (.gate_passed | type == "boolean")'`.

=== "Step 5 — Decide"

    | Verdict | Action |
    |---|---|
    | `gate_passed: true` | Merge play branch → integration (Step 5a) |
    | Failed, attempt == 1 | Redo with `--team-attach` (Step 5b) |
    | Failed, attempt == 2 | ESCALATE (Step 5c) |
    | Non-zero subprocess exit | Diagnose `.log` (Step 5d) |

    Redo uses `--team-attach` (NOT `--team-mode`) to preserve team state from the first attempt. These flags are mutually exclusive — never pass both.

    On second failure, escalate and present options: widen the prompt and retry; director writes the missing piece directly then re-gates; accept partial result; abort the show. Do not auto-fire any option.

=== "Step 6 — Adapt the plan"

    Before firing the next play, re-read `_show.md` and ask: did the prior play change what the next play should do? Update `_show.md` BEFORE firing. Log changes in the decisions log with WHEN and WHY.

    This adaptive step is the reason to use show instead of a static runner. If you never update `_show.md` between plays, you should not be using this skill.

=== "Step 7 — Show-level final gate"

    After every play has merged, run `show-final-gate`:

    ```bash
    "$LI" agent -a show-final-gate --effort high --cwd "$SHOW_DIR" --yolo --bypass "$(cat <<EOF
    Final review of show "${TOPIC}".
    Show dir: $SHOW_DIR
    Original goal: $GOAL
    Play directories: $PLAY_DIRS
    All artifacts: $PLAY_ARTIFACTS
    Decisions log: [...]
    Respond JSON ONLY:
    {"show_passed": <bool>, "blockers": [...], "recommendations": [...],
     "goal_assessment": "...", "cross_play_findings": [...]}
    EOF
    )" > "$SHOW_DIR/_final_verdict.json"
    ```

    If `show_passed: false`, treat each blocker as a new play or roll back specific play merges.

=== "Step 8 — Final merge + cleanup"

    Only after `show_passed: true`:

    ```bash
    cd <repo>
    git checkout <base>
    git pull origin <base>
    git merge --no-ff "show/${TOPIC}/integration" -m "Show ${TOPIC}: integrate"
    # Inspect diff before pushing — DO NOT auto-push
    ```

    Then prune worktrees and branches after the user confirms forensics complete.

---

#### Resume protocol

When picking up a show mid-flight (after Claude session ended or context compacted), rehydrate shell variables and read state before acting:

1. `cat $SHOW_DIR/_show.md` — original plan + decisions log
2. `ls $SHOW_DIR/` — which plays exist, check for `_ABORT` and `_final_verdict.json`
3. For each play dir: read `_intent.md`, `_meta.json`, `_verdict.json`, `.pid`

Classify each play by state (running, gated-pending-merge, needs-redo, escalated, completed, etc.) using the precedence rules in the skill source before taking any action.

#### Abort protocol

```bash
# Soft abort — no more launches, no more integration mutations
touch "$SHOW_DIR/_ABORT"

# Hard abort — kill in-flight plays now
for pidfile in $SHOW_DIR/*/.pid; do
  pid=$(cat "$pidfile" 2>/dev/null) || continue
  kill -TERM "$pid" 2>/dev/null
done
```

Worktrees stay intact after abort — forensics first, cleanup later.

---

#### End-to-end example

Show: "Land ADR-053 (Sinkhorn attention) in lattice"

```text
research        → gate pass → merge
                  ↓
                  decisions log: OT-LR surfaced as stronger baseline
                  Update _show.md: design emphasizes OT-LR; add play 2.5
literature_verify → gate pass → merge
design            → gate fail 1st (missing ablation) → redo → pass → merge
implement         → gate fail 2x (test coverage) → ESCALATE
                  ↓
                  User picks: director writes missing tests; re-gate passes; merge
review            → gate pass → merge
Step 7 show-final-gate → show_passed:true → final merge to main
```

---

## Agent: `critic`

> **Source**: `marketplace/show/agents/critic.md`

| Field | Value |
|---|---|
| Model | `claude/claude-opus-4-6` |
| Effort | `xhigh` |
| Yolo | `true` |

**Role**: Adversarial quality gate. Assumes broken until proven working. Produces formal verdicts: `APPROVE`, `APPROVE-WITH-FIXES`, or `REJECT`. Runs AFTER all main agents complete — never in parallel with producers.

**Distinct from play-gate and show-final-gate**: critic does adversarial logic attack, grounds findings in named frameworks (OWASP, CAP theorem, SOLID, etc.), and produces severity-tiered prose. play-gate and show-final-gate produce JSON for programmatic consumption.

**Verdict logic**:

| Condition | Verdict |
|---|---|
| Zero CRIT, zero MAJ | `APPROVE` |
| Zero CRIT, MAJ with clear fix path | `APPROVE-WITH-FIXES` |
| Any CRIT, or blocking MAJ | `REJECT` |

**Severity taxonomy**:

| Severity | Conditions | Action |
|---|---|---|
| CRITICAL | auth_bypass, injection, data_loss, crash, corrupt | BLOCK |
| MAJOR | missing error handling, perf 2×, edge case wrong, integration fail | FIX_BEFORE_PROD |
| MINOR | docs, duplication, suboptimal code | OPTIONAL |

Every finding requires: location (file:line), severity, evidence, and `blast_radius` ∈ `{local, module, cross_module, global}`.

**Output format**: severity-tiered progressive disclosure.

```
CRIT:N | MAJ:N | MIN:N | PASS:N
```

- CRIT findings: full detail (location, evidence, blast_radius, remediation path)
- MAJ findings: summary + location
- MIN findings: count + file list only

**Skills loaded before acting**:

```bash
li skill review           # standard correctness/quality rubric
li skill security-review  # threat-model rubric (when auth/crypto touched)
li skill pr-review        # multi-perspective methodology (for PR reviews)
```

---

## Agent: `play-gate`

> **Source**: `marketplace/show/agents/play-gate.md`

| Field | Value |
|---|---|
| Model | `claude/claude-sonnet-4-6` |
| Effort | `medium` |
| Yolo | `true` |

**Role**: Per-play acceptance gatekeeper inside the `show` skill (Step 4). Intentionally narrower and cheaper than `critic` — no adversarial logic attack, no multi-agent synthesis.

**Inputs**: the play's `_intent.md` (Acceptance checklist), `_prompt.md`, subprocess exit code, and `find`-walked artifact tree.

**Decision logic**:

```text
exit_code ≠ 0                   → gate_passed: false
_intent.md missing Acceptance   → gate_passed: false (fail closed)
∀ acceptance_item satisfied     → gate_passed: true
∃ acceptance_item not satisfied → gate_passed: false
Stubs/TODOs in code             → gate_passed: false
```

**Output contract**: JSON ONLY — no prose, no preceding paragraphs.

```json
{
  "gate_passed": true,
  "feedback": null,
  "notes": null
}
```

If failing, `feedback` names each missing acceptance item by line. `notes` is advisory only — never used for failure conditions.

---

## Agent: `show-final-gate`

> **Source**: `marketplace/show/agents/show-final-gate.md`

| Field | Value |
|---|---|
| Model | `claude/claude-sonnet-4-6` |
| Effort | `high` |
| Yolo | `true` |

**Role**: End-of-show synthesis gate (Step 7). Runs after every play has passed its per-play `play-gate` check. Verifies the show as a whole achieved the original goal — catching cross-play inconsistencies that per-play gates miss.

**What it checks**:

- Did the show achieve the original goal (not just per-play acceptance)?
- Cross-play contradictions: Play A's claim contradicted by Play B's evidence
- Per-play gates that were too narrow to catch overall goal misses
- Tests that pass per-play but fail when integrated
- Goal drift in decisions log without documented intentional re-scoping

**Scope boundary**: does NOT re-grade per-play work that already passed `play-gate`. If a per-play verdict was generous, names it as a `recommendations` entry to "re-gate with stricter criteria" — does not override the prior pass.

**Output contract**: JSON ONLY. `show_passed: false` requires at least one entry in `blockers`.

```json
{
  "show_passed": true,
  "blockers": [],
  "recommendations": ["one advisory item"],
  "goal_assessment": "One paragraph assessing whether the show achieved the original goal.",
  "cross_play_findings": []
}
```
