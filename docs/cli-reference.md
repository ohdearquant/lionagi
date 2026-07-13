# CLI Reference

```bash
li agent MODEL PROMPT [flags]        # single-turn agent
li team SUBCMD [flags]               # persistent team messaging
li o fanout MODEL PROMPT [flags]     # parallel workers
li o flow   MODEL PROMPT [flags]     # auto-DAG pipeline
li play NAME [ARGS]                  # sugar for `li o flow -p NAME`
li skill NAME                        # print a CC-compatible skill body to stdout
```

Three reusable primitives live under `~/.lionagi/`:

| Primitive | Location | Invocation |
|-----------|----------|------------|
| Agent profile | `~/.lionagi/agents/<name>/<name>.md` | `li agent -a <name>` / `li o flow -a <name>` |
| Skill (static ref) | `~/.lionagi/skills/<name>/SKILL.md` | `li skill <name>` |
| Playbook (parametric flow) | `~/.lionagi/playbooks/<name>.playbook.yaml` | `li play <name>` |

See [`examples/`](../examples/) for minimal templates of each.

---

## Common flags

Available on `li agent`, `li o fanout`, `li o flow`. Source: `cli/_providers.py:263`

| Flag | Default | Notes |
|------|---------|-------|
| `--yolo` | false | Auto-approve all tool calls |
| `-v, --verbose` | false | Stream real-time output; suppresses final print |
| `--theme {light,dark}` | none | Terminal theme |
| `--effort LEVEL` | none | Override effort; claude: `low medium high xhigh max`; codex: `none minimal low medium high xhigh`; gemini: unsupported (`cli/_providers.py:24,44`) |
| `--cwd DIR` | none | Working directory for CLI endpoint |
| `--timeout SECONDS` | none | Hard wall-clock timeout; partial branches saved. Injects a `[DEADLINE]` preamble into the agent's first message so it can pace itself |

**Model spec**: `provider/model[-effort]` — e.g. `claude/opus-4-7-high`, `codex/gpt-5.4-xhigh`. Bare aliases: `claude` → `claude_code/sonnet`, `codex` → `codex/gpt-5.3-codex-spark`, `gemini-code` → `gemini_code/gemini-3.1-flash-lite-preview`. Source: `cli/_providers.py:72,145`

---

## `li agent`

One-shot agent turn or resumed conversation.

```bash
li agent [model] prompt [flags]
```

| Arg/Flag | Default | Notes |
|----------|---------|-------|
| `model` | — | Spec or alias. Omit with `-r` or `-c`. `cli/agent.py:156` |
| `prompt` | — | Message to send. `cli/agent.py:165` |
| `-a, --agent NAME` | none | Profile by name. Resolves `.lionagi/agents/<NAME>/<NAME>.md` first, then legacy `.lionagi/agents/<NAME>.md`. Sets model/effort/system/yolo. `cli/agent.py:167` |
| `-r, --resume BRANCH_ID` | none | Resume prior branch. `cli/agent.py:178` |
| `-c, --continue-last` | false | Resume most recent branch. `cli/agent.py:184` |
| `--context-from REF` | none | Inject distilled context from a prior session id, branch id, run id, or file path into the new branch's first instruction (above the prompt). Repeatable — refs concatenate in argv order, sharing one budget. `cli/_context_from.py` |
| `--context-budget N` | `8000` | Total token budget (~4 chars/token) for `--context-from` content, shared across all refs. |

`-r` and `-c` are mutually exclusive (`cli/agent.py:49`). `--context-from` is rejected together with `-r` / `-c` (resume already carries the source context). Common flags apply.

`--context-from` resolves the ref in order — session id, branch id, run id, then file path — erroring loudly on an unresolvable or ambiguous (2+ match) ref rather than spawning with silently-missing context. Distillation is mechanical (no LLM): a saved artifact/summary verbatim if it fits, else the initial instruction plus final assistant message, else a loudly-marked head/tail truncation.

```bash
li agent -a reviewer --bypass --context-from 20260420T110143-a1b2c3 --prompt-file review.md
```

```bash
li agent claude/sonnet "What does Branch.operate() do?"
```

```text
# output:
Branch.operate() is the universal structured operation entry point...

[to resume] li agent -r 20260420T110143-a1b2c3 "..."
```

Python equivalent: `branch.operate(instruction="...")` → [`api/branch.md#operate`](api/branch.md#operate)

---

## `li team`

Persistent inbox messaging. Teams stored at `~/.lionagi/teams/{team_id}.json` under `fcntl.flock` (`cli/team.py:50`).

```bash
li team create NAME -m MEMBERS
li team list     [alias: ls]
li team show TEAM
li team send CONTENT -t TEAM --to RECIPIENTS [--from NAME] [--from-op OP]
li team receive  -t TEAM [--as MEMBER]   [alias: recv]
```

### `li team create`

| Arg/Flag | Required | Notes |
|----------|----------|-------|
| `name` | yes | Team name |
| `-m, --members` | yes | Comma-separated member names |

Source: `cli/team.py:284`

```bash
li team create "docs-team" -m "researcher,writer,reviewer"
```

```text
# output:
Created team 'docs-team' (7fa0d9abbf5b)
  Members: researcher, writer, reviewer
  File: ~/.lionagi/teams/7fa0d9abbf5b.json
```

**list** — sorted by mtime; shows ID, name, members, msg count (`cli/team.py:294`). **show TEAM** — all messages with timestamps and `read_by` (`cli/team.py:297`). `TEAM` = ID, prefix, or name.

### `li team send`

| Arg/Flag | Required | Default | Notes |
|----------|----------|---------|-------|
| `content` | yes | — | Message text (positional) |
| `--team, -t` | yes | — | Team ID or name |
| `--to` | yes | — | `all` or comma-separated names |
| `--from` | no | `_cli` | Sender name |
| `--from-op` | no | none | Op id; ties signal to a specific flow invocation |

Source: `cli/team.py:301`

```bash
li team send "Research done — see research.md" \
  --team 7fa0d9abbf5b --to writer --from researcher --from-op o1
```

### `li team receive`

| Flag | Required | Default | Notes |
|------|----------|---------|-------|
| `--team, -t` | yes | — | Team ID or name |
| `--as` | no | none | Mark as read for this member; omit = see all |

Source: `cli/team.py:322`

```bash
li team receive --team 7fa0d9abbf5b --as writer
```

Python equivalent: `session.send()` / `session.receive()` → [`api/team.md`](api/team.md)

---

## `li o fanout`

Three-phase: orchestrator decomposes → N workers in parallel → optional synthesis.

```bash
li o fanout [model] prompt [flags]
```

| Flag | Default | Notes |
|------|---------|-------|
| `-a, --agent NAME` | none | Orchestrator profile. `cli/orchestrate/__init__.py:49` |
| `-n, --num-workers N` | 3 | Worker count; ignored when `--workers` set |
| `--workers M1,M2,...` | none | Per-worker model specs (each can include effort suffix) |
| `--max-concurrent N` | 0 | Max concurrent (0 = all) |
| `--with-synthesis [MODEL]` | false | Enable synthesis; bare = orchestrator model |
| `--synthesis-prompt TEXT` | none | Override synthesis instruction |
| `--output {text,json}` | text | Output format |
| `--save DIR` | none | Write artifacts here |
| `--team-mode [NAME]` | none | Create persistent team; bare = `"fanout"` |

Source: `cli/orchestrate/__init__.py:29–119`. Common flags apply.

```bash
li o fanout claude/opus-high "Audit lionagi/session/ for stale API surface" \
  -n 3 --with-synthesis --save ./audit-out
```

```text
# output:
Phase 1: Orchestrator decomposing task into 3 agent requests...
Phase 1 done (3.2s): 3 requests generated.
Phase 2: Fanning out to 3 workers: [claude/opus, claude/opus, claude/opus]
Phase 2 done (14.1s).
Saved 3 worker results to /Users/ocean/audit-out
Phase 3: Synthesis [claude/opus]...
Saved to /Users/ocean/audit-out
```

Worker outputs: `worker_1.md … worker_N.md` in artifact root (`fanout.py:269`). Synthesis: `synthesis.md` (`fanout.py:317`). Resume cancelled workers with `li agent -r BRANCH_ID`.

---

## `li o flow`

Auto-DAG pipeline. Orchestrator plans a `FlowPlan` (agents + ops with `depends_on` edges); engine executes with dependency-aware parallelism. Control ops trigger re-planning up to 3 rounds (`flow.py:705`).

```bash
li o flow [model] prompt [flags]
```

| Flag | Default | Notes |
|------|---------|-------|
| `-a, --agent NAME` | none | Orchestrator profile. Resolves `.lionagi/agents/<NAME>/<NAME>.md` first, then legacy `.lionagi/agents/<NAME>.md`. |
| `-f, --file PATH` | none | Load flow spec from YAML/JSON file. File values are defaults; CLI flags override. |
| `-p, --playbook NAME` | none | Load playbook from `~/.lionagi/playbooks/<NAME>.playbook.yaml`. Playbook's declared args are injected as additional flags. |
| `--with-synthesis [MODEL]` | false | Final synthesis after all ops |
| `--max-concurrent N` | 0 | Max concurrent agents per phase (0 = all) |
| `--max-agents N` | 0 | Cap total ops (0 = unlimited) |
| `--dry-run` | false | Plan DAG and print; no execution |
| `--show-graph` | false | Render DAG as matplotlib PNG into `--save` dir |
| `--bare` | false | Ignore agent profiles; all workers use CLI model |
| `--background` | false | Subprocess run; requires `--save`; monitor `tail -f <save>/flow.log`; child inherits `LIONAGI_RUN_ID` (`cli/_runs.py:57`) |
| `--output {text,json}` | text | Output format |
| `--save DIR` | none | Artifact dir; required for `--background` |
| `--team-mode [NAME]` | none | Create a FRESH team every invocation (new UUID). Bare = `"flow"`. |
| `--team-attach NAME` | none | Upsert: attach to existing team by NAME (preserving message history) or create if missing. Mutex with `--team-mode`. |

`-f` and `-p` are mutually exclusive. `--team-mode` and `--team-attach` are mutually exclusive. Source: `cli/orchestrate/__init__.py:122–209`. `--background` re-invokes `python -m lionagi.cli` without itself (`cli/orchestrate/__init__.py:265`). Common flags apply.

### Team lifecycle summary

| Goal | Flag | Behavior |
|------|------|----------|
| One-off parallel workers, no shared history | `--team-mode [NAME]` | New UUID every invocation. Messages posted; team discarded conceptually. |
| Persistent thread across invocations | `--team-attach NAME` | First call creates; subsequent calls attach to the same team (same UUID, same history). No pre-step required — you never have to `li team create` first. |
| Strict attach (error if missing) | `li team create NAME -m ...` first, then `--team-attach NAME` | Explicit human-in-the-loop for shared, long-lived teams. |

```bash
li o flow claude/opus "Write and test a CLI arg parser for a new subcommand" \
  --save ./parser-work --with-synthesis
```

```text
# output:
Planning DAG...
Plan done (4.1s): 3 agents, 4 ops — o1:r1 | o2:i1←o1 | o3:t1←o2 | o4:r1←o3
Executing DAG: 3 agents / 4 ops...
  ▶ researcher started
  ✓ researcher done (8.2s)
  ▶ implementer started
  ✓ implementer done (22.1s)
  ▶ tester started
  ✓ tester done (18.4s)
Synthesis [claude/opus]...
Saved to ./parser-work/
Total: 55.8s
```

Use `--dry-run` to inspect the plan before running. Artifact dirs per agent: `<save>/{agent_id}/`. Python equivalent: `Builder` + `Session.flow()` → [`api/flow.md`](api/flow.md)

---

## Playbooks (`-f`, `-p`, `li play`)

A **playbook** is a YAML file that declares a reusable, parametric flow
invocation: model, agent, effort, prompt template, and typed CLI args.
Source of truth: `~/.lionagi/playbooks/<NAME>.playbook.yaml`.

### Playbook YAML shape

```yaml
name: audit
description: Parametric audit pattern
argument-hint: '[--mode MODE] [--workers N]'   # CC-compatible display string

model: claude-code/opus-4-7
agent: orchestrator
effort: high

args:                       # explicit, typed schema (preferred)
  mode:
    type: str               # str | int | float | bool
    default: dry
    help: "audit mode: dry | security | dead-code"
  workers:
    type: int
    default: 8
  strict:
    type: bool
    default: false

prompt: |
  Run a {mode} audit with {workers} parallel workers. Strict: {strict}.

  Target: {input}
```

All playbook fields map to `li o flow` flags. If both `args:` and
`argument-hint:` are present, `args:` wins. If only `argument-hint:` is
present, it's parsed as CC does — `[--flag VALUE]` → string arg, `[--flag]`
→ bool arg, no type coercion.

### Template interpolation

Inside `prompt:`, three rules:

1. `{input}` → the positional prompt text passed on the CLI.
2. `{arg_name}` → a declared arg (CLI override > playbook default).
3. If the template has **no** `{...}` placeholders, the positional text is
   **appended** with a blank line — same convention as Claude Code slash
   commands.

### Invocation

```bash
# Long form
li o flow -p audit --mode security "the auth service"

# Sugar
li play audit --mode security "the auth service"
li play list                        # list playbooks in ~/.lionagi/playbooks/
li play audit --help                # show playbook description, args, and usage
```

### `li play list`

Lists all installed playbooks in `~/.lionagi/playbooks/`.

```bash
li play list
```

```text
# output:
audit        Parametric audit pattern              [--mode MODE] [--workers N]
refactor     Multi-step refactor with review       [--scope SCOPE]
```

### `li play NAME --help`

Shows the playbook's `description`, its declared arguments with types and defaults, and a
generated usage line. Does not execute the flow.

```bash
li play audit --help
```

```text
# output:
audit — Parametric audit pattern

Usage: li play audit [--mode MODE] [--workers N] [--strict] PROMPT

Arguments:
  --mode MODE    str    default: dry    audit mode: dry | security | dead-code
  --workers N    int    default: 8
  --strict       bool   default: false

Prompt template:
  Run a {mode} audit with {workers} parallel workers. Strict: {strict}.

  Target: {input}
```

`--help` is checked before any flags are forwarded to `li o flow`, so it never starts execution.

### Ad-hoc specs (`-f`)

For one-off specs not worth installing globally:

```bash
li o flow -f ./my-spec.yaml "target"
```

`-f` takes an absolute/relative path; `-p` takes a bare name resolved under
`~/.lionagi/playbooks/`. They are mutually exclusive.

See [`examples/playbooks/`](../examples/playbooks/) for ready-to-install
playbooks with different shapes.

---

## Skills (`li skill`)

A **skill** is static reference content the agent pulls on demand. Format
is identical to Claude Code skills — you can symlink one source file into
both `~/.claude/skills/<name>/SKILL.md` and
`~/.lionagi/skills/<name>/SKILL.md`.

```text
~/.lionagi/skills/commit/SKILL.md
```

```markdown
---
name: commit
description: Conventional Commits style guide + safety rules.
---

# Commit conventions

... body ...
```

### Commands

```bash
li skill NAME          # print body (post-frontmatter) to stdout
li skill list          # list installed skills
li skill show NAME     # print full file (frontmatter + body)
```

An orchestrator agent can shell out to `li skill <name>`, capture stdout,
and inject the result into its own context — no extra protocol required.

See [`examples/skills/`](../examples/skills/) for templates.

---

## `li monitor`

Observe play/agent/run progress in real time. Replaces fragile file-polling and
log-tailing with a single surface. Source: `cli/monitor.py` (`add_monitor_subparser`).
Alias: `li mon`.

```bash
li monitor                      # table of all running entities
li monitor <id>                 # detail view for one run/play/agent/invocation
li monitor --watch              # live-refresh table
li monitor --watch <id>         # live-refresh detail view
li monitor --since 1h           # entities updated in the last hour
li monitor --type session       # filter table by entity type
li monitor --project myproject  # filter sessions by project
```

| Arg/Flag | Default | Notes |
|----------|---------|-------|
| `id` | none | Entity ID or prefix; omit for the table view |
| `-w, --watch` | false | Live-refresh every `--refresh` seconds |
| `--refresh SECS` | 2 | Refresh interval for `--watch` |
| `--since WINDOW` | all | Time window: `30m`, `1h`, `2d` |
| `-t, --type` | none | One of `session`, `invocation`, `show`, `play` |
| `-p, --project` | none | Filter sessions by project name |

### Orphan detection

Every `li agent` / `li o flow` / `li o fanout` session records its own launcher
process's `pid` and `pid_create_time` into `node_metadata` at session-creation
time. If that process dies without running its own teardown — the terminal
that held it closes, the parent harness restarts, a session gets compacted
out from under it — the session's row is left at `status=running` with no
live process behind it and no natural way to reach a terminal state on its
own.

`li monitor` sweeps for this on every table and detail render: it scans
`running` sessions, confirms (kill-0 plus a process-creation-time check, to
rule out an unrelated process that has since reused the same pid) that the
recorded launcher pid is actually gone, and — only for a *confirmed* dead
pid, never an unrecorded or unreadable one — transitions the row to `failed`
with reason code `run.failed.orphaned_parent` through the same guarded
transition path every other status change uses. The table and detail views
then display that row as `orphaned` rather than `failed`; the persisted
status and reason code are unchanged; anything reading the database directly
sees the honest `failed` / `run.failed.orphaned_parent` pair. A session that
is still alive, or whose liveness can't be determined (no pid recorded), is
left alone — orphan detection only ever acts on positive evidence of death.

Re-arming an orphaned row depends on what it was running:

- A flow that reached a checkpoint can be resumed from where it left off:
  `li o flow --resume <session_id>`. `li monitor <session_id>` on an orphaned
  row shows the resume command when one applies.
- Everything else (a bare agent turn, a fanout leg, a flow with no
  checkpoint) has no resume frontier — re-arming means re-running the
  original command. The sweep does not attempt this automatically; it only
  clears the row out of `running` so downstream consumers (`li monitor`,
  anything polling for completion) stop waiting on a session nothing is ever
  going to finish.

Play-level rows are not covered by this sweep — see the play reaper gap
tracked separately.

### Detached launch (surviving the launcher)

A run that must keep going after the shell/terminal/harness that started it
exits needs to be launched detached, not just backgrounded in the same
process group:

```bash
li o flow claude/opus "long-running task" --save ./work --background
```

`--background` re-spawns the flow in its own session (`start_new_session`,
the `setsid` equivalent) and returns immediately, printing the child's PID
and a ready-to-use `li monitor <session_id>` pointer; the detached child logs
to `<save>/flow.log`. Because the child is the process that actually records
`node_metadata.pid`, it is exactly the process orphan detection watches —
closing the original terminal has no effect on it, and if the machine itself
loses the child (a reboot, an OOM kill), the row still self-heals via the
sweep above instead of hanging at `running` forever.

`li agent` has no built-in `--background` flag; for a long agent leg that
must outlive its launcher, wrap it the same way (`nohup`/`setsid`, redirect
output to a file, record the printed PID) and track it with `li monitor`
plus the artifact/log path rather than holding a foreground shell open.

---

## `li invoke`

Group the sessions a skill spawns (e.g. `/show`, `/codex-pr-review`) into one parent
invocation record, so the runs list and Studio dashboard collapse "14 sessions" into a
single row. Opt-in — sessions spawned without `--invocation` behave exactly as before.
See [ADR-0020](_archive/v0/ADR-0020-skill-invocations.md). Source: `cli/invoke.py`.

```bash
INV=$(li invoke start --skill show --prompt "resolve lionagi issues")
li play backend  ... --invocation "$INV"
li play frontend ... --invocation "$INV"
li invoke end "$INV" --status completed
```

| Subcommand | Flags | Notes |
|------------|-------|-------|
| `start` | `--skill` (required), `--plugin`, `--prompt`, `--metadata` | Opens an invocation; prints its id to stdout |
| `end ID` | `--status` (default `completed`), `--metadata` | Closes it; status from the [ADR-0025](_archive/v0/ADR-0025-session-status-vocabulary.md) vocabulary |
| `list` | `--skill`, `--status`, `--limit` (default 20) | Lists recent invocations |

---

## `li engine run`

Run a domain-specific multi-agent engine pipeline without writing Python. Progress
events stream to stderr; the final result is emitted as JSON on stdout for piping.
Run records persist in the StateDB `engine_runs` table. Source: `cli/engine.py`.

```bash
li engine run research 'What are the latest advances in GQA?'
li engine run review   'See artifact.py' --model claude/sonnet
li engine run coding   'Implement a BFS traversal' --test-cmd 'pytest'
li engine run hypothesis 'Finding: X causes Y' --export-dir ./out
li engine run planning 'Build a REST API'
```

| Arg/Flag | Default | Notes |
|----------|---------|-------|
| `kind` | — | Engine kind (e.g. `research`, `review`, `coding`, `hypothesis`, `planning`) |
| `spec` | — | Main input: topic / artifact / spec / findings / prompt |
| `--test-cmd CMD` | none | Validation command; required for the `coding` kind |
| `--export-dir DIR` | none | Output directory (`coding`, `hypothesis`) |
| `--model MODEL` | default | Provider/model override |
| `--max-depth N` | kind default | Max recursion/expansion depth |
| `--max-agents N` | none | Cap on spawned sub-agents |
| `--session-id ID` | none | Associate with an existing StateDB session |
| `--no-persist` | false | Skip writing the run record to StateDB |

---

## Agent profile layout

A profile is resolved by name. Two layouts are supported:

```text
~/.lionagi/agents/
    orchestrator/                      # preferred — directory layout
        orchestrator.md                # main profile
        patterns/                      # optional supplementary references
            empaco.md
        refs/
            commit-conventions.md
    legacy.md                          # flat layout — backward compat
```

`li agent -a NAME` and `li o flow -a NAME` check for
`<NAME>/<NAME>.md` first and fall back to `<NAME>.md`. Supplementary files
beside the main profile are **not** injected into the initial system prompt
— the agent reads them on demand (via direct file reads or `li skill`).

Project-local `.lionagi/agents/` takes precedence over `~/.lionagi/agents/`.

See [`examples/agents/`](../examples/agents/) for `minimal/` and `with-refs/`
templates.

### Profile format

A profile is YAML frontmatter followed by a markdown body (the system prompt).
Source: `cli/_agents.py` (`AgentProfile`).

```markdown
---
model: claude_code/opus
effort: high
yolo: true
---

You are an implementer. Write production code, not stubs...
```

All frontmatter fields are optional; matching CLI flags override them at invocation.

| Field | Notes |
|-------|-------|
| `model` | Provider/model spec (e.g. `claude_code/opus`, `codex/gpt-5.4-xhigh`) |
| `effort` | Reasoning effort level (e.g. `high`, `xhigh`) |
| `yolo` | Auto-approve tool calls |
| `fast_mode` | Route via the OpenAI priority tier (codex only) |
| `lion_system` | Prepend `LION_SYSTEM_MESSAGE` to the body (default: `true`) |
| `artifact_defaults` | Expected-artifact defaults; see [ADR-0029](_archive/v0/ADR-0029-artifact-contract.md) |

When `lion_system: true`, the global Lion system preamble is prepended to the body
to form the system prompt. Set it to `false` for a verbatim body (e.g. when the
profile already carries its own complete system prompt).

---

## Run-ID and persistence

Every CLI invocation allocates a run directory. Source: `cli/_runs.py:14`. Run ID format: `YYYYMMDDTHHMMSS-{6hex}` (`cli/_runs.py:61`).

```text
~/.lionagi/runs/{run_id}/
  run.json                        manifest (command, branches, artifact_root)
  branches/{branch_id}.json       branch snapshot — resumable via -r / -c
  stream/{branch_id}.buffer.jsonl live chunk buffer during streaming
  artifacts/                      deliverables — only when --save was NOT given
```

Authoritative state always lives under `~/.lionagi/runs/{run_id}/`, so any branch is
resumable from anywhere. User-facing artifacts (per-agent working dirs, `synthesis.md`,
`flow.log`, `flow_dag.png`) land in the `--save` directory when one is provided,
otherwise in `artifacts/` under the run dir. The `--save` directory is **not**
authoritative state — deleting it does not break `-r`.

Pre-run-scoped sessions (legacy `~/.lionagi/logs/agents/{provider}/{branch_id}`) are
still read as a fallback on resume.

Resume any prior branch:

```bash
li agent -r 20260420T110143-a1b2c3 "follow up"
li agent -c "continue most recent"
```

### Env Vars

| Variable | Purpose | Source |
|----------|---------|--------|
| `LIONAGI_RUN_ID` | Child inherits parent run ID (background flows) | `cli/_runs.py:57` |
| `LIONAGI_HOME` | Override `~/.lionagi/` base dir | `lionagi/utils.py` |
| `LIONAGI_WORKER_LIVENESS_TIMEOUT` | Seconds `run()` waits for a CLI worker's first stream chunk before retrying once, then raising `WorkerLivenessError`; default `120`, `0` disables. Applied by default only to endpoints that stream output early (`claude_code`, `codex`) — buffered endpoints (`gemini-cli`, `pi`) are unaffected unless `liveness_timeout` is passed explicitly to `run()` | `lionagi/operations/run/run.py` |
| `OPENAI_API_KEY` | OpenAI REST API key (for `iModel`, not for `codex` CLI alias) | `lionagi/config.py` |
| `ANTHROPIC_API_KEY` | Anthropic REST API key (for `iModel`; `claude` alias uses `claude login` instead) | `lionagi/config.py` |
| `GOOGLE_API_KEY` | Gemini key | `lionagi/config.py` |
| `GROQ_API_KEY` | Groq key | `lionagi/config.py` |

---

*Sources: `cli/agent.py` · `cli/team.py` · `cli/orchestrate/__init__.py` · `cli/orchestrate/fanout.py` · `cli/orchestrate/flow.py` · `cli/_providers.py` · `cli/_runs.py`*

Next: [Python API reference](api/index.md)
