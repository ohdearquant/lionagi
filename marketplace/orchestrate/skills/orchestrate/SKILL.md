---
name: orchestrate
description: >
  Plan and execute multi-agent workflows using lionagi's CLI: li o flow (DAG pipelines),
  li o fanout (parallel workers), and li play (playbook invocations). Use when a task
  needs multiple agents working in parallel or staged phases.
allowed-tools: [Bash, Read, Write, Glob, Grep]
---

# orchestrate

Plan and execute multi-agent workflows using lionagi's CLI.

## When to use which command

| Situation | Command |
|---|---|
| Single task, one agent | `li agent MODEL PROMPT` |
| Same prompt to N independent workers | `li o fanout MODEL PROMPT -n N` |
| Staged pipeline with dependencies | `li o flow MODEL PROMPT` |
| Pre-saved parametric workflow | `li play NAME [PROMPT]` |

Rule of thumb: if all subtasks are independent (no output feeds another), use `fanout`.
If any subtask depends on the output of another, use `flow`.

---

## CLI Reference

### `li agent [MODEL] PROMPT` — single agent

Spawn one blocking agent turn. Prints the final response to stdout.

```
li agent claude "Write unit tests for auth.py"
li agent claude/opus-4-6-high "Produce a security audit"
li agent codex "Fix the failing test" --yolo --bypass
li agent -r <branch-id> "Follow-up question"
li agent -c "Continue the previous conversation"
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `MODEL` | (positional, optional) | Provider/model spec, e.g. `claude`, `codex`, `claude/opus-4-6-high` |
| `PROMPT` | (positional, required) | Task text |
| `-a / --agent NAME` | — | Load agent profile from `.lionagi/agents/<NAME>.md` |
| `-r / --resume BRANCH_ID` | — | Resume a previous branch by ID |
| `-c / --continue-last` | false | Continue the most recently used branch |
| `--yolo` | false | Auto-approve all tool calls |
| `--bypass` | false | Bypass all codex approvals and sandbox |
| `--effort LEVEL` | — | Override reasoning effort: `low\|medium\|high\|xhigh\|max` (claude); `none\|minimal\|low\|medium\|high\|xhigh` (codex) |
| `--cwd DIR` | — | Working directory for CLI provider |
| `--timeout SECONDS` | — | Kill after N seconds |
| `--invocation ID` | — | Parent invocation id (from `li invoke start`) |
| `--project NAME` | — | Explicit project name; overrides auto-detection |
| `-v / --verbose` | false | Stream real-time output |
| `--theme light\|dark` | — | Terminal display theme |
| `--fast` | false | Codex priority service tier (lower latency) |

Exit codes: `0` completed, `1` failed, `124` timed out, `130` aborted (Ctrl-C), `143` cancelled.

---

### `li o fanout [MODEL] PROMPT` — parallel workers

Orchestrator decomposes the task into N subtasks, fans out to workers in parallel,
optionally synthesizes results into a final summary.

```
li o fanout claude "Review this codebase for security issues" -n 4
li o fanout claude/sonnet "Suggest API design approaches" -n 3 \
    --with-synthesis claude/opus-4-6-high
li o fanout codex "Implement the parser" --workers codex,codex,codex \
    --with-synthesis --save ./out
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `MODEL` | (positional, optional) | Orchestrator model; also default worker model |
| `PROMPT` | (positional, required) | Task for the orchestrator to decompose |
| `-a / --agent NAME` | — | Load orchestrator profile |
| `-n / --num-workers N` | 3 | Number of workers (ignored if `--workers` set) |
| `--workers M1,M2,...` | — | Explicit comma-separated worker model specs |
| `--max-concurrent N` | 0 (all) | Max workers running at once |
| `--with-synthesis [MODEL]` | false | Enable synthesis. Bare flag uses orchestrator model; `--with-synthesis claude/opus-4-6` uses that model |
| `--synthesis-prompt TEXT` | — | Custom synthesis instruction |
| `--save DIR` | — | Save all outputs to directory |
| `--team-mode [NAME]` | — | Create a team for inter-worker messaging. Bare flag uses name `"fanout"` |
| `--output text\|json` | text | Output format |
| `--yolo` | false | Auto-approve tool calls for all workers |
| `--bypass` | false | Bypass approvals for all workers |
| `--effort LEVEL` | — | Effort level for all workers |
| `--cwd DIR` | — | Working directory |
| `--timeout SECONDS` | — | Kill after N seconds |
| `--invocation ID` | — | Parent invocation id |
| `--project NAME` | — | Explicit project name |

---

### `li o flow [MODEL] [PROMPT]` — DAG orchestration

Orchestrator analyzes the task, plans a DAG of agents with dependency edges,
and executes with automatic parallelism where dependencies allow.

```
li o flow claude "Audit and harden the authentication module" \
    --with-synthesis --save ./audit-out --yolo --bypass

li o flow claude "Implement feature X" \
    --max-ops 8 --effort high --dry-run

li o flow -f ./my-spec.yaml "Custom task prompt" --save ./out

li o flow -p security-audit "JWT middleware" --save ./sec-out --yolo --bypass
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `MODEL` | (positional, optional) | Orchestrator model spec |
| `PROMPT` | (positional, optional) | Task; can also come from spec file's `prompt:` key |
| `-f / --file PATH` | — | Load flow spec from YAML/JSON. File values are defaults; CLI flags override |
| `-p / --playbook NAME` | — | Load playbook from `~/.lionagi/playbooks/<NAME>.playbook.yaml` |
| `-a / --agent NAME` | — | Load orchestrator profile |
| `--with-synthesis [MODEL]` | false | Final synthesis after all ops complete |
| `--max-concurrent N` | 0 (all) | Max agents running in parallel within a phase |
| `--save DIR` | — | Save outputs to directory (required with `--background`) |
| `--team-mode [NAME]` | — | Create a fresh team per invocation. Bare flag uses `"flow"` |
| `--team-attach NAME` | — | Attach to existing team by name (upsert: load or create). Mutually exclusive with `--team-mode` |
| `--dry-run` | false | Plan the DAG but do not execute; shows agents, ops, deps, model resolution |
| `--show-graph` | false | Render DAG as matplotlib visualization; saves PNG if `--save` set |
| `--background` | false | Fork into background subprocess (requires `--save`); monitor via `tail -f <save>/flow.log` |
| `--bare` | false | Ignore agent profiles; all workers use the CLI model. Roles define behavior only |
| `--max-ops N` | 0 (unlimited) | Cap total ops (DAG nodes). Plans exceeding cap are truncated |
| `--output text\|json` | text | Output format |
| `--yolo` | false | Auto-approve tool calls |
| `--bypass` | false | Bypass approvals |
| `--effort LEVEL` | — | Effort level |
| `--cwd DIR` | — | Working directory |
| `--timeout SECONDS` | — | Kill after N seconds |
| `--invocation ID` | — | Parent invocation id |
| `--project NAME` | — | Explicit project name |

`--max-agents` is a deprecated alias for `--max-ops`.

---

### `li play NAME [PROMPT] [ARGS...]` — playbook sugar

Sugar for `li o flow -p NAME`. Playbooks live at `~/.lionagi/playbooks/<NAME>.playbook.yaml`.

```
li play security-audit "Audit the JWT middleware"
li play refactor-module --module payments --save ./out
li play list                    # list available playbooks
li play security-audit --help   # show playbook description and args
```

All flags accepted by `li o flow` (except `-p`) work with `li play`.

---

## Flow YAML Spec Format

A spec file pre-configures a flow. CLI flags override spec values.
Useful for reusable pipelines committed to the repo.

```yaml
# my-audit.yaml
model: claude/opus-4-6-high
effort: high
max_ops: 10
with_synthesis: true
save: ./audit-output
prompt: |
  Perform a full security and correctness audit of the codebase.
  Focus on: authentication, input validation, secret handling, error handling.
```

Run it:

```bash
li o flow -f my-audit.yaml --yolo --bypass
# Override prompt inline:
li o flow -f my-audit.yaml "Focus only on JWT handling" --yolo --bypass
```

Spec keys mirror CLI flag names (`max_ops`, `with_synthesis`, `bare`, `dry_run`,
`show_graph`, `team_mode`, `team_attach`, `agent`, `save`). Both dashed and
underscored forms are accepted (`max-ops` and `max_ops` both work).

### Playbook spec (with template args)

```yaml
# ~/.lionagi/playbooks/code-review.playbook.yaml
description: "Multi-agent code review with critic checkpoint"
argument-hint: "[--target FILE] [--depth N]"
model: claude/sonnet
effort: high
with_synthesis: true
prompt: |
  Perform a {depth}-pass code review of {target}.
  {input}
```

Usage:

```bash
li play code-review "Focus on error handling" --target src/auth.py --depth 3
```

---

## DAG Planning: FlowPlan Models

When running `li o flow`, an orchestrator LLM produces a `FlowPlan` before execution.
The plan has two levels:

**FlowPlan**

```
agents:     list[FlowAgent]   # who exists
operations: list[FlowOp]      # what happens and in what order
synthesis:  bool              # request a final consolidation pass
```

**FlowAgent**

```
id:        str        # short unique id, e.g. "r1", "impl-1" (^[A-Za-z0-9_-]{1,64}$)
role:      str        # role from the available-agents roster (e.g. "researcher", "implementer", "critic")
model:     str|None   # optional model override, e.g. "codex/gpt-5.4-xhigh"
guidance:  str|None   # default behavioral framing for all ops on this agent
```

**FlowOp**

```
id:         str         # short unique op id, e.g. "o1", "review1" (same regex as agent id)
agent_id:   str         # references a FlowAgent.id
instruction: str        # concrete task text for this invocation
guidance:   str|None    # per-op override (replaces agent.guidance when set)
depends_on: list[str]   # upstream FlowOp ids this op waits on
control:    bool        # True = critic checkpoint; produces FlowControlVerdict
```

**FlowControlVerdict** (produced by `control=True` ops)

```
should_continue: bool   # False = flow ends; True = orchestrator re-plans
reason:          str    # justification, grounded in specific op outputs
next_steps:      str    # specific gaps to address (used by re-planner)
```

When `should_continue=True`, the orchestrator produces a new `FlowPlan` targeting only the
gaps. Up to 3 re-plan rounds are allowed. Existing agents are reused (memory persists).

---

## DAG Planning Principles

**Identify independence first.** Two ops are independent when neither needs the other's output.
Independent ops run in the same phase (parallel). Dependent ops are sequential.

```
Phase 1 (parallel): [research-1, research-2, context-fetch]
Phase 2 (parallel): [implement]  ← depends on research-1 and research-2
Phase 3 (parallel): [write-tests] ← depends on implement
Phase 4 (serial):   [critic] ← control op, runs last
```

**Agent reuse is cheaper than spawning.** An agent is a Branch with persistent memory.
Reusing the same `agent_id` across ops means the agent remembers its prior turns —
no re-injection of context needed. Prefer 2-4 agents running multiple ops over
8 agents with one op each.

**Critic ops run last, never in parallel with producers.** Set `control=True` only
on an op that genuinely reviews completed work. At most one control op per round.
The control op must declare `depends_on` referencing the ops it reviews.

**Artifact handoff.** Each agent writes to `{save_dir}/{agent_id}/`. Op results are
also persisted there as `{op_id}.md`. Downstream ops (different agent) read from
`../{dep_agent_id}/{filename}`. Same-agent deps need no file read — the branch
already has memory of the prior turn.

**Role-to-model guidance.**

| Role | Recommended model |
|---|---|
| researcher, analyst | high-reasoning (claude/opus-4-6-high, codex/gpt-5.4-xhigh) |
| implementer | code model (codex, claude/sonnet) |
| critic, reviewer | highest reasoning (claude/opus-4-6-xhigh) |
| writer, documenter | claude/sonnet or similar |

---

## Standard Workflow

### 1. Simple parallel exploration

```bash
# Three independent researchers, synthesized at the end
li o fanout claude/sonnet "What are the security risks in this codebase?" \
    -n 3 \
    --with-synthesis claude/opus-4-6-high \
    --save ./fanout-out \
    --yolo --bypass
```

### 2. Staged pipeline (dry-run first)

```bash
# Preview the DAG the orchestrator would plan
li o flow claude/opus-4-6-high \
    "Audit auth.py, implement fixes, verify with tests" \
    --dry-run --effort high

# Execute once the plan looks right
li o flow claude/opus-4-6-high \
    "Audit auth.py, implement fixes, verify with tests" \
    --with-synthesis \
    --save ./flow-out \
    --max-ops 8 \
    --effort high \
    --yolo --bypass
```

### 3. Background flow with monitoring

```bash
li o flow claude/sonnet "Full codebase migration to async" \
    --save ./migration-out \
    --background \
    --yolo --bypass

tail -f ./migration-out/flow.log
```

### 4. Spec file for a repeatable pipeline

```yaml
# security-review.yaml
model: claude/opus-4-6-high
effort: xhigh
max_ops: 12
with_synthesis: true
save: ./security-review-out
```

```bash
li o flow -f security-review.yaml "Focus on the payments module" --yolo --bypass
```

### 5. Graph visualization

```bash
li o flow claude "Plan and implement feature X" \
    --dry-run --show-graph --save ./viz-out
# Saves DAG as PNG to ./viz-out/
```

---

## Team Coordination

Teams enable inter-agent messaging during a flow or fanout. Agents can broadcast
findings or ask peers for clarification.

**Fresh team per invocation** (`--team-mode`): creates a new team UUID each run.
Good for isolated pipelines.

```bash
li o flow claude "Multi-agent code review" \
    --team-mode review-session --save ./out --yolo --bypass
```

**Persistent team across invocations** (`--team-attach`): loads existing team
(preserving message history) or creates it if absent. Good for long-running
iterative workflows.

```bash
# First run: creates the team
li o flow claude "Start the migration plan" \
    --team-attach project-alpha --save ./out --yolo --bypass

# Later runs: reuse the same team, history preserved
li o flow claude "Continue the migration" \
    --team-attach project-alpha --save ./out --yolo --bypass
```

Direct team operations:

```bash
li team create "my-team" -m "researcher,writer,reviewer"
li team send "Found a critical bug in JWT handling" --team my-team --to all
li team receive --team my-team --as reviewer
li team show my-team
```

`--team-mode` and `--team-attach` are mutually exclusive.

---

## Invocation Tracking

Group multiple sessions spawned by a skill into one parent record, visible
in Studio's /invocations page.

```bash
# 1. Open an invocation
INV=$(li invoke start --skill orchestrate --prompt "Full security audit")

# 2. Run flows under that invocation
li o flow claude "Audit authentication" --save ./auth-out \
    --invocation "$INV" --yolo --bypass

li o fanout claude "Audit input validation" -n 3 \
    --invocation "$INV" --save ./val-out --yolo --bypass

# 3. Close the invocation
li invoke end "$INV" --status completed

# List recent invocations
li invoke list --skill orchestrate --limit 10
```

`--invocation` is accepted by `li agent`, `li o fanout`, and `li o flow`.

---

## Scheduling (ADR-0027)

The Studio scheduler engine fires `li agent`, `li o flow`, and `li play` as subprocesses
on a schedule. Manage schedules via the Studio UI at `/schedules` or the REST API.

Trigger types: `cron`, `interval`, `github_poll`.

DAG chains: each schedule entry can declare `on_success` and `on_fail` to form
conditional follow-up actions.

---

## Source Code Reference

| Component | Path |
|---|---|
| CLI entrypoint | `lionagi/cli/main.py` |
| `li agent` | `lionagi/cli/agent.py` |
| `li o flow` — FlowPlan/FlowOp/FlowAgent/FlowControlVerdict | `lionagi/cli/orchestrate/flow.py` |
| `li o fanout` | `lionagi/cli/orchestrate/fanout.py` |
| Orchestrate subparser (argparse) | `lionagi/cli/orchestrate/__init__.py` |
| Common CLI flags (add_common_cli_args) | `lionagi/cli/_providers.py` |
| `li team` | `lionagi/cli/team.py` |
| `li invoke` | `lionagi/cli/invoke.py` |
| State DB | `lionagi/state/db.py` + `lionagi/state/schema.sql` |
| Studio scheduler | `apps/studio/server/scheduler/engine.py` |
| Studio backend | `apps/studio/server/` |

---

## Common Mistakes

**Using fanout when ops depend on each other.** Fanout workers are independent by design.
If worker B needs worker A's output, use `flow` instead.

**Putting the critic in parallel.** A `control=True` op reviews completed work.
It must come after the ops it reviews via `depends_on`, never in the same parallel phase.

**Too many agents.** An agent is a Branch. Reusing `agent_id` across ops is cheaper
than spawning a new branch. Design for 2-4 agents running multiple ops each.

**Omitting `--save` for multi-agent flows.** Without `--save`, artifact files are
written to the temporary run directory under `~/.lionagi/runs/`. Downstream ops that
read files from peer agents will still work (paths are absolute), but the outputs
are harder to find after the run. Pass `--save ./out` for any flow you care about.

**Using `--background` without `--save`.** `--background` forks a subprocess and
returns immediately. It requires `--save` so the log and artifacts have a stable path.

**Inventing role names.** `FlowAgent.role` must come from the available-agents roster
(use `--dry-run` to see which roles the planner selects). The planner rejects
unrecognized roles at plan validation time.
