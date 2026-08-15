# CLI Reference

The CLI has two jobs: run agent work and operate the durable lifecycle around it.
Start with `li agent`; move to fan-out or flow only when the work needs more than one
worker.

## Command map

### Run work

| Command | Purpose |
|---------|---------|
| `li agent [MODEL] PROMPT` | Run or resume one worker |
| `li o fanout [MODEL] PROMPT` | Decompose into independent workers, optionally synthesize |
| `li o flow [MODEL] PROMPT` | Plan and execute a dependency-aware, reactive graph |
| `li play NAME [ARGS]` | Run a reusable playbook (`li o flow -p NAME`) |
| `li engine run KIND SPEC` | Run a built-in coding, hypothesis, planning, research, or review engine |

### Observe and control

| Command | Purpose |
|---------|---------|
| `li monitor` / `li mon` | List or watch sessions, invocations, plays, shows, and runs |
| `li wait ID...` | Block until any mix of durable entity IDs reaches a terminal state |
| `li monitor run ID...` | Wait for scheduled runs and their chains; optionally keep watching |
| `li agent status [ID]` | Read stable session/invocation status, optionally as JSON |
| `li o ctl {status,pause,resume,msg}` | Inspect or steer a live flow by ID |
| `li kill ID` | Terminate one running entity or sweep stale processes; play kills cannot reach their workers and exit non-zero saying so; show ids are not directly killable ([details](#li-kill)) |

### Reuse, coordination, and operation

| Command | Purpose |
|---------|---------|
| `li team {create,list,show,send,receive}` | Durable team inboxes across processes |
| `li casts [NAME]` | Inspect built-in roles or modes |
| `li skill {NAME,list,show}` | Read installed static skill instructions |
| `li plugin {list,info,trust,enable,disable}` | Inspect and activate trusted plugin bundles |
| `li hooks {import,trust}` | Import Claude Code / Codex hook configs and trust the imported commands |
| `li invoke {start,end,list}` | Group sessions under one higher-level invocation |
| `li studio [start]` | Start the Studio backend and selected frontend mode |
| `li schedule {list,get,limits,create,enable,disable,trigger,delete,runs}` | Manage schedules through the Studio API |
| `li state {import,import-teams,ls,stats,checkpoint,vacuum,prune,doctor}` | Inspect and maintain StateDB |
| `li dispatch {ls,show,ack,retry,purge}` | Operate the durable dispatch outbox |
| `li stats runs` | Aggregate run reporting from StateDB |
| `li mirror` | Mirror Claude Code transcripts into StateDB/Studio |
| `li mcp` | Serve an MCP server that submits `li` runs as background jobs ([details](#li-mcp)) |
| `li doctor` | Check installation, dependencies, Studio reachability, and writable state |

`play`, `skill`, and `wait` are compatibility-friendly top-level conveniences handled
before the normal argparse registry, so they may not appear in the command list printed
by `li --help`. They are supported surfaces and are documented here.

Reusable definitions can be project-local, user-global, or supplied by a trusted
plugin:

| Primitive | Location | Invocation |
|-----------|----------|------------|
| Agent profile | `.lionagi/agents/<name>/<name>.md` | `li agent -a <name>` / `li o flow -a <name>` |
| Skill (static ref) | `~/.lionagi/skills/<name>/SKILL.md` | `li skill <name>` |
| Playbook (parametric flow) | `.lionagi/playbooks/`, `~/.lionagi/playbooks/`, or a trusted plugin | `li play <name>` |
| Plugin bundle | `.lionagi/plugins/<name>/plugin.yaml` | `li plugin info <name>` |

See the [repository examples](https://github.com/ohdearquant/lionagi/tree/main/examples)
for minimal templates of each.

---

## Shared run flags

Available on `li agent`, `li o fanout`, `li o flow`. Source: `cli/_providers.py`

| Flag | Default | Notes |
|------|---------|-------|
| `--yolo` | false | Auto-approve provider tool calls |
| `--bypass` | false | Bypass Codex approvals and sandboxing; intended for already-isolated environments |
| `--fast` | false | Use Codex priority service tier when the account supports it |
| `-v, --verbose` | false | Stream real-time output; suppresses final print |
| `--theme {light,dark}` | none | Terminal theme |
| `--effort LEVEL` | none | Override effort; provider-specific limits are normalized or clamped. Gemini CLI folds effort into its resolved model tier; direct `gemini` API has no effort setting |
| `--cwd DIR` | none | Working directory for CLI endpoint |
| `--timeout SECONDS` | none | Hard wall-clock timeout; partial branches saved. Injects a `[DEADLINE]` preamble into the agent's first message so it can pace itself |
| `--invocation ID` | none | Group the session under an ID from `li invoke start` |
| `--project NAME` | auto | Override project detection from config/git metadata |

**Model spec**: `provider/model[-effort]` — for example
`claude/opus-4-7-high` or `codex/gpt-5.4-xhigh`. Current bare aliases include
`claude` → `claude_code/sonnet`, `codex` → `codex/gpt-5.3-codex-spark`,
`gemini-code` → `gemini_code/gemini-3.5-flash`, and
`pi` → `pi/gemini-2.5-flash`. Use `gemini`, without `-code`, for the direct Google
API provider rather than the Gemini CLI backend.

---

## `li agent`

One-shot agent turn or resumed conversation.

```bash
li agent [model] prompt [flags]
```

| Arg/Flag | Default | Notes |
|----------|---------|-------|
| `model` | — | Spec or alias. Omit with `-r` or `-c`. |
| `prompt` | — | Message to send. |
| `--prompt TEXT` | none | Prompt flag alternative to positionals |
| `--prompt-file PATH` | none | Read the prompt from a file; `-` reads stdin |
| `-a, --agent NAME` | none | Profile by name. Resolves `.lionagi/agents/<NAME>/<NAME>.md` first, then legacy `.lionagi/agents/<NAME>.md`. Sets model/effort/system/yolo. |
| `-r, --resume BRANCH_ID` | none | Resume prior branch. |
| `-c, --continue-last` | false | Resume most recent branch. |
| `--preset coding` | none | Wire the coding toolkit, path guards, and coding prompt; cwd defaults to the invocation directory |
| `--form SPEC` | none | Validate a YAML/JSON work-form before making any model call, then inject its typed values |
| `--context-from REF` | none | Inject distilled context from a prior session id, branch id, run id, or file path into the new branch's first instruction (above the prompt). Repeatable — refs concatenate in argv order, sharing one budget. `cli/_context_from.py` |
| `--context-budget N` | `8000` | Total token budget (~4 chars/token) for `--context-from` content, shared across all refs. |
| `--resume-on-timeout` | false | Resume a timed-out agent session once with a bounded continuation |

`-r` and `-c` are mutually exclusive. `--context-from` is rejected together with `-r` / `-c` (resume already carries the source context). Common flags apply.

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

Python equivalent: `branch.operate(instruction="...")` → [`Branch` API](api/branch.md)

Read status without starting a worker:

```bash
li agent status                         # latest agent-kind session in this project
li agent status SESSION_OR_INVOCATION   # full ID or unique prefix
li agent status SESSION_OR_INVOCATION --json
```

---

## `li team`

Persistent inbox messaging. Teams are stored at `~/.lionagi/teams/{team_id}.json` under `fcntl.flock`.

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

Source: `cli/team.py`

```bash
li team create "docs-team" -m "researcher,writer,reviewer"
```

```text
# output:
Created team 'docs-team' (7fa0d9abbf5b)
  Members: researcher, writer, reviewer
  File: ~/.lionagi/teams/7fa0d9abbf5b.json
```

**list** — sorted by mtime; shows ID, name, members, and message count. **show TEAM** — all messages with timestamps and `read_by`. `TEAM` = ID, prefix, or name.

### `li team send`

| Arg/Flag | Required | Default | Notes |
|----------|----------|---------|-------|
| `content` | yes | — | Message text (positional) |
| `--team, -t` | yes | — | Team ID or name |
| `--to` | yes | — | `all` or comma-separated names |
| `--from` | no | `_cli` | Sender name |
| `--from-op` | no | none | Op id; ties signal to a specific flow invocation |

Source: `cli/team.py`

```bash
li team send "Research done — see research.md" \
  --team 7fa0d9abbf5b --to writer --from researcher --from-op o1
```

### `li team receive`

| Flag | Required | Default | Notes |
|------|----------|---------|-------|
| `--team, -t` | yes | — | Team ID or name |
| `--as` | no | none | Mark as read for this member; omit = see all |

Source: `cli/team.py`

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
| `-a, --agent NAME` | none | Orchestrator profile. |
| `-n, --num-workers N` | 3 | Maximum number of assignments generated |
| `--workers M1,M2,...` | none | Worker model pool, assigned round-robin; excess specs warn when the pool is larger than `-n` |
| `--max-concurrent N` | 0 | Max concurrent (0 = all) |
| `--with-synthesis [MODEL]` | false | Enable synthesis; bare = orchestrator model |
| `--synthesis-prompt TEXT` | none | Override synthesis instruction |
| `--output {text,json}` | text | Output format |
| `--save DIR` | none | Write artifacts here |
| `--team-mode [NAME]` | none | Create persistent team; bare = `"fanout"` |

Source: `cli/orchestrate/__init__.py`. Common flags apply.

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
Saved 3 worker results to /path/to/audit-out
Phase 3: Synthesis [claude/opus]...
Saved to /path/to/audit-out
```

Worker outputs are `worker_1.md … worker_N.md` in the artifact root. Synthesis is written to `synthesis.md`. Resume cancelled workers with `li agent -r BRANCH_ID`.

---

## `li o flow`

Auto-DAG pipeline. The orchestrator plans an initial list of `TaskAssignment`
entries with assignees, dependencies, and execution modes; the engine executes
them with dependency-aware parallelism. When reactivity is enabled, workers can
emit `SpawnRequest` follow-up work without re-running the initial planner.

```bash
li o flow [model] prompt [flags]
```

| Flag | Default | Notes |
|------|---------|-------|
| `-a, --agent NAME` | none | Orchestrator profile. Resolves `.lionagi/agents/<NAME>/<NAME>.md` first, then legacy `.lionagi/agents/<NAME>.md`. |
| `-f, --file PATH` | none | Load flow spec from YAML/JSON file. File values are defaults; CLI flags override. |
| `-p, --playbook NAME` | none | Resolve a project-local, user-global, or trusted-plugin playbook. Declared args are injected as additional flags. |
| `--with-synthesis [MODEL]` | false | Final synthesis after all ops |
| `--max-concurrent N` | 0 | Max concurrent agents per phase (0 = all) |
| `--max-agents N` | 0 | Shared cap for planned and reactive ops (`0` = no shared ceiling; reactive spawns are capped at 20) |
| `--dry-run` | false | Plan DAG and print; no execution |
| `--show-graph` | false | Render DAG as matplotlib PNG into `--save` dir |
| `--bare` | false | Ignore agent profiles; all workers use CLI model |
| `--background` | false | Subprocess run; requires `--save`; writes `<save>/flow.log` and prints the monitorable session ID |
| `--output {text,json}` | text | Output format |
| `--save DIR` | none | Artifact dir; required for `--background` |
| `--team-mode [NAME]` | none | Create a FRESH team every invocation (new UUID). Bare = `"flow"`. |
| `--team-attach NAME` | none | Upsert: attach to existing team by NAME (preserving message history) or create if missing. Mutex with `--team-mode`. |
| `--team-max-rounds N` | `2` | Extra reactive wake-up rounds for unread teammate messages after active workers finish |
| `--workers M1,M2,...` | none | Mixed worker model pool; preserves each role's profile and overrides model routing |
| `--pack PATH` | none | Per-role routing pack used when `--workers` is absent |
| `--max-ops N` | `0` | Shared cap for planned and reactive ops (`0` = no shared ceiling; reactive spawns are capped at 20); `--max-agents` is deprecated |
| `--reactive MODE` | `all` | Roles allowed to emit `SpawnRequest`: `all`, `off`, or a comma-separated role list |
| `--resume ID` | none | Restart a checkpointed flow without re-planning; does not read other planning flags |
| `--allow-degraded-context` | false | Permit resumed inherited-context operations to run with empty predecessor history |
| `--notify CMD` | none | Run a terminal callback template with status/invocation payload values |

`-f` and `-p` are mutually exclusive. `--team-mode` and `--team-attach` are mutually exclusive. Source: `cli/orchestrate/__init__.py`. `--background` re-invokes `python -m lionagi.cli` without itself. Common flags apply.

### Team lifecycle summary

| Goal | Flag | Behavior |
|------|------|----------|
| One-off parallel workers, no shared history | `--team-mode [NAME]` | New UUID every invocation. Messages posted; team discarded conceptually. |
| Persistent thread across invocations | `--team-attach NAME` | First call creates; subsequent calls attach to the same team (same UUID, same history). No pre-step required — you never have to `li team create` first. |

```bash
li o flow claude/opus "Write and test a CLI arg parser for a new subcommand" \
  --save ./parser-work --with-synthesis
```

Use `--dry-run` to inspect assignments, dependencies, and resolved model/mode
routing before running. Artifact directories are `<save>/{agent_id}/`. Python
equivalent: `Builder` + `Session.flow()` → [`api/flow.md`](api/flow.md)

Checkpoint resume and live control are intentionally separate:

```bash
li o flow --resume RUN_OR_SESSION_ID      # prior process ended; replay checkpoint
li o ctl resume RUN_OR_SESSION_ID         # process is alive but paused
```

### `li o ctl`

Address read/control operations to a durable ID:

```bash
li o ctl status ID
li o ctl pause ID
li o ctl resume ID
li o ctl msg ID "Prioritize correctness over breadth"
```

`status` reads sessions, invocations, plays, and branch-backed sessions. `pause`,
`resume`, and `msg` queue control for a running flow; `msg` is available for flows
using context-mode operator steering. Use `li o ctl SUBCOMMAND --help` for the
command-specific ID and JSON options.

---

## Playbooks (`-f`, `-p`, `li play`)

A **playbook** is the declarative tier of orchestration: a reusable YAML input
to the flow planner. It is not the plan itself. Each invocation combines the
playbook with that run's input, the orchestrator produces a run-specific
FlowPlan, and each plan assignment becomes a branch-bound FlowOp. Source:
`lionagi/orchestration/patterns.py` and `cli/orchestrate/flow.py`.

Bare names resolve project-local `.lionagi/playbooks/` first, then user-global
`~/.lionagi/playbooks/`, then active trusted plugins. Use `<plugin>/<name>` to
select a plugin playbook explicitly.

### Playbook YAML shape

```yaml
description: Parametric audit pattern    # shown by `li play NAME --help`
argument-hint: '[--mode MODE]'           # used only when `args:` is absent

model: claude-code/opus-4-7
agent: orchestrator
effort: high
max_ops: 6
reactive: "off"

args:                       # typed schema; becomes real CLI flags
  mode:
    type: str               # str | int | float | bool
    default: dry
    help: "audit mode: dry | security | dead-code"

prompt: |
  Run a {mode} audit of {input}. Cite source evidence for every finding.
```

A playbook carries two kinds of key, read by different code.

**Runtime fields** configure the flow: `model`, `agent`, `effort`, `prompt`,
`workers`, `max_ops`, deprecated `max_agents`, `with_synthesis`, `bare`,
`dry_run`, `show_graph`, `save`, `team_mode`, `team_attach`, `reactive`, and
`artifacts`. `_validate_spec_fields` in `cli/orchestrate/__init__.py` checks
the type and bounds of whichever of these are present. It is a type check, not
a field list: it does not enumerate a closed set and does not reject unknown
keys. See the [orchestration guide](guides/orchestration.md#playbook-field-reference)
for types, bounds, and runtime effects. Quote `reactive: "off"`; bare YAML
`off` is a boolean, but the loader requires this field to be a string.

**Declaration fields** describe the playbook's own interface: `description`,
`args`, and `argument-hint`. They are read on a separate path. `args` is
checked by `_validate_args_schema`, which fails the run on a malformed schema,
and is turned into parser flags by `inject_playbook_schema_into_parser`. When
`args` is absent, `argument-hint` is parsed into the same schema shape by
`_parse_argument_hint`. The discovered playbook name comes from the filename.

### Template interpolation

Inside `prompt:`, three rules:

1. `{input}` → the positional prompt text passed on the CLI.
2. `{arg_name}` → a declared arg (CLI override beats playbook default).
3. If the template has no `{...}` placeholders, the positional text is
   appended after a blank line.

### Invocation

```bash
# Long form
li o flow -p audit --mode security "the auth service"

# Sugar
li play audit --mode security "the auth service"
li play list                        # list all discovered playbooks
li play audit --help                # show description, declared args, and usage
li play check audit                 # validate declared playbook artifacts/dependencies
li play status [ID]                 # latest play/flow status, or one durable ID
li play --resume ID                 # resume a checkpointed flow
```

### `li play list`

Lists project-local, user-global, and active trusted-plugin playbooks. Plugin
entries are namespaced as `<plugin>/<name>`.

```bash
li play list
```

### `li play NAME --help`

Prints the playbook's `description`, a usage line built from `argument-hint`
when one is declared, and an `Arguments:` block listing each declared arg with
its type and default.

```bash
li play audit --help
```

`--help` is checked before any flags are forwarded to `li o flow`, so it never starts execution.

### Submission is not validation

`play.submit` returns an allocated run ID after spawning the CLI child. The
child then loads the playbook and calls `_validate_spec_fields`; a run ID is
therefore a handle, not proof that the YAML is valid or that a result exists.
Read it with `job.status` and `job.output`. Source: `lionagi/mcp/jobs.py` and
`cli/orchestrate/__init__.py`.

### Ad-hoc specs (`-f`)

For one-off specs not worth installing globally:

```bash
li o flow -f ./my-spec.yaml "target"
```

`-f` takes an absolute or relative path. `-p` takes a discovered bare name or
an explicit `<plugin>/<name>` token. They are mutually exclusive.

The [orchestration guide](guides/orchestration.md#a-complete-example) follows one
playbook through its FlowPlan and a single FlowOp.

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

See the [skill examples](https://github.com/ohdearquant/lionagi/tree/main/examples/skills)
for templates.

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

A session detail view includes a `khive injection` block when an opted-in profile recorded
non-zero recall, injection, failure, or writeback counters. The block contains aggregate counts,
not injected source text.

For scripts, use a waiter instead of scraping the watch display:

```bash
li wait SESSION_ID PLAY_ID                 # any durable entity kinds; mixed IDs allowed
li wait ID_A,ID_B --interval 2

li monitor run SCHEDULE_RUN_ID             # follows on_success/on_fail children by default
li monitor run SCHEDULE_RUN_ID --no-chain  # wait for only the literal ID
li monitor run SCHEDULE_RUN_ID --follow    # keep watching for later schedule runs
li monitor run SCHEDULE_RUN_ID --max-wait 0
```

`li wait` accepts run, session, play, flow-invocation, and scheduled-run IDs or
unique prefixes. `li monitor run` follows a watched run's scheduler chain by
default; `--no-chain` disables that behavior. After the initial set drains,
`--follow` keeps the monitor open and prints newly created schedule runs. The
initial wait defaults to a bounded 900 seconds.

---

## `li kill`

Terminate a running entity by id, or sweep stale entities whose OS process is
already dead. Source: `cli/kill.py` (`add_kill_subparser`).

```bash
li kill abc123                        # kill by id prefix
li kill <session-id>                  # stop a worker process
li kill abc123 --reason 'stuck'
li kill abc123 --recursive            # kill + direct children (session -> invocation)
li kill --all-stale                   # sweep dead-PID sessions/invocations
li kill --all-stale --threshold 3600  # only rows older than 1h
li kill --all-stale --dry-run
```

| Arg/Flag | Default | Notes |
|----------|---------|-------|
| `id` | none | Entity ID or prefix: run/session/invocation/play |
| `--reason` | `""` | Recorded in `status_transitions` |
| `--recursive` | false | Also kill direct child entities |
| `--all-stale` | false | Sweep stale sessions/invocations (and their child-derived plays/shows) |
| `--threshold SECS` | 3600 | Only sweep entities started more than this long ago |
| `--dry-run` | false | Only valid with `--all-stale`; prints without cancelling |
| `--grace SECS` | 5.0 | Wait after SIGTERM before escalating to SIGKILL |

**`--recursive` scope boundary.** Recursion only reaches PID-bearing workers,
and an orchestrator row reaches them only through a link it recorded:

- Killing a **play** reaches its worker chain only if the play row records the
  session it started, in `plays.session_id`. One path binds that column: the
  Studio show importer, which resolves the session by name when it mirrors a
  show directory. A play created by a live run leaves it unset, and a worker
  session stores no play reference either, so there is no key to resolve those
  workers by. In that case the kill marks the play row `blocked`, prints an
  error saying no worker was stopped, and **exits 1** — a play kill never
  reports success it did not achieve. Kill the worker session ids directly
  (`li monitor` lists them). `--recursive` is not needed for either case: a
  play row carries no PID of its own, so resolving its workers is the kill.
- Killing a **session** with `--recursive` also cancels its linked invocation.
- A **show** id cannot be killed directly today: only `running` rows are
  killable, and show rows persist as `active` (never `running`), so
  `li kill <show-id>` is rejected as already-terminal, with or without
  `--recursive`.

To stop everything under a show, kill the play id or session id directly
(`li monitor <show-id>` lists its plays). `--all-stale` covers the abandoned
case only as far as the recorded links allow: a play older than `--threshold`
whose recorded worker session has gone terminal is marked `blocked`; a play
that records no worker session is left alone, because age by itself cannot
tell an abandoned play from one still doing hours of work. The sweep prints
one line naming how many rows it skipped for that reason, and reports them as
`skipped_unlinked_plays` in its closing counts. A show row is marked `aborted`
only once it is older than `--threshold` **and** all of its plays are terminal.

---

## `li invoke`

Group the sessions a skill spawns (e.g. `/show`, `/codex-pr-review`) into one parent
invocation record, so the runs list and Studio dashboard collapse "14 sessions" into a
single row. Opt-in — sessions spawned without `--invocation` behave exactly as before.
See the [CLI internals](internals/cli.md#invokepy-invocation-records). Source:
`cli/invoke.py`.

```bash
INV=$(li invoke start --skill show --prompt "resolve lionagi issues")
li play backend  ... --invocation "$INV"
li play frontend ... --invocation "$INV"
li invoke end "$INV" --status completed
```

| Subcommand | Flags | Notes |
|------------|-------|-------|
| `start` | `--skill` (required), `--plugin`, `--prompt`, `--metadata` | Opens an invocation; prints its id to stdout |
| `end ID` | `--status` (default `completed`), `--metadata` | Closes it with a canonical terminal status |
| `list` | `--skill`, `--status`, `--limit` (default 20) | Lists recent invocations |

---

## `li hooks`

Import an existing Claude Code or Codex hooks configuration into this project's
`.lionagi/settings.yaml` `hooks_external:` block, then record trust for the imported
commands so they are allowed to execute. Trust is hash-pinned: approval is recorded
against the content-hashed argv, so a command that changes after import must be
re-approved before it runs. Source: `cli/hooks.py`.

```bash
li hooks import claude                   # reads .claude/settings.json
li hooks import codex .codex/hooks.json  # explicit config path
li hooks trust                           # review and approve pending commands
li hooks trust --yes                     # record trust without the prompt
```

| Subcommand | Flags | Notes |
|------------|-------|-------|
| `import SOURCE [PATH]` | `--cwd` | `SOURCE` is `claude` or `codex`; `PATH` defaults to `.claude/settings.json` or `.codex/hooks.json` |
| `trust` | `--cwd`, `--yes` | Lists pending imported hook commands and records approval (content-hashed argv) |

---

## `li mcp`

Serve an [MCP](https://modelcontextprotocol.io) server over stdio. The server is
a control plane over this CLI: it submits `li` runs as **detached background
jobs** and answers questions about them, so a submit returns a `run_id`
immediately instead of blocking and the run keeps going in its own process
group, surviving an MCP-server restart. Requires the `mcp` extra
(`pip install 'lionagi[mcp]'`). Source: `lionagi/mcp/`.

```bash
li mcp            # serve over stdio (same as: python -m lionagi.mcp)
```

The server advertises a **single** tool, `request`, and every operation is a
namespaced verb passed to it. Seeing one entry in a client's `tools/list` is
correct. Call `request` with `help=true` for the catalog of verbs.

Register it with any MCP client (e.g. an `.mcp.json`). The key here is the local
name your client uses to launch the server; the name the server reports over the
protocol is `lion`:

```json
{
  "mcpServers": {
    "lion": { "command": "li", "args": ["mcp"] }
  }
}
```

Job records live under `~/.lionagi/mcp/jobs/<run_id>/`; the authoritative run
state is the CLI's own `~/.lionagi/runs/<run_id>/`. In `job.status`, the
top-level `status` is authoritative: the embedded `run` manifest is advisory and
its own `status` may lag.

The verb catalog, the `request` result contract, and a worked submit-and-poll
example are in the [MCP server reference](reference/mcp-server.md).

### When a run's process is gone and nothing recorded how it came out

A background run normally records its own end: the CLI's terminal hook writes it,
and a run stopped by `li kill` leaves it in the lifecycle store, which the server
caches onto the job record. A run whose process dies before either of those
happens leaves nothing behind at all — no surviving producer can ever write its
end.

Where an observation *positively establishes* that the run's process is gone —
the recorded pid holds no process, it disappears between two probes, or a live
process holds the number and started at a different time, so it is a different
process — `job.status`, `job.list` and `job.wait` record that end themselves and
then report it:

| Field | Value |
|-------|-------|
| `terminal` | `true` |
| `outcome` | `indeterminate` |
| `reason_code` | `process_gone_without_outcome` |
| `terminal_source` | `mcp_orphan_reaper` |

`outcome: "indeterminate"` means the process is conclusively gone and **no authoritative
outcome was reported**. It does not mean the work failed: the run may well have
finished what it was doing before it died, and nothing survived to say either
way. `failed` stays reserved for a reported terminal status classified as a
failure, and a caller may retry a `failed` run under its own policy. **Do not
automatically retry such a run** — an external side effect it never got to
report may already have committed.

`terminal_source` says what wrote the end: `cli_terminal_hook` (the run's own
terminal hook), `lifecycle_cache` (an end read back from the lifecycle store),
`spawn_failure` (the spawn was caught failing), `mcp_kill` (the run was killed
through this server), or `mcp_orphan_reaper` (this server, from the conclusive
observation above). It is null on records written before the field existed.

For a run ended this way, **`finished_at` is when the loss was established and recorded,
not when the process exited** — nothing surviving can report that instant. Any
duration derived from it is therefore an **upper bound** on how long the run
actually ran.

`liveness_conclusion` on `job.status` says what the observation established:
`process_gone`, `alive`, or `unknown`. Only `process_gone` can end a run.
`unknown` — a pid the OS cannot be asked about, a denied or unreadable identity
probe — never does, and such a run stays non-terminal and advisory
(`possibly_orphaned`), reported by `job.wait` under `stopped_without_end`.

`job.wait`'s **`all_terminal` means every valid requested run has a recorded
end**, including runs whose outcome is `indeterminate`. It does not mean every run
succeeded or reported an outcome; read each entry's `outcome` for that.

### Terminal notices

When a background run finishes, the server records its terminal status and, if a
delivery command is configured, runs it — on **every** terminal state, including
failure and kill. Nothing is configured out of the box, so the default is no
notice. The command is an argv list run directly (never through a shell); the
placeholders `{run_id}`, `{status}`, `{label}`, and `{target}` are substituted
into its arguments, and the same fields are offered as a JSON object on stdin.

Configure the command once via lionagi's own `notify.on_terminal` setting
(`~/.lionagi/settings.yaml` or a project `.lionagi/settings.yaml`):

```yaml
notify:
  on_terminal:
    adapter:
      kind: exec
      argv: ["/usr/local/bin/notify-run", "{run_id}", "{status}", "{target}"]
```

Or per submit: `notify` overrides the delivery command for one run (a JSON argv
list), and `notify_seat` fills the `{target}` placeholder. The environment
variables `LIONAGI_MCP_NOTIFY_COMMAND` and `LIONAGI_MCP_NOTIFY_TARGET` set a
process-wide default. Delivery outcome is recorded on the job and surfaced in
`job.status` (`notify_delivery`), so a notice that failed to send is visible
rather than silently lost. `job.list` carries the same outcome collapsed to one
word in `notify_delivery_state` — `delivered`, `delivered_unverified` when it
exited zero but that command shape's zero exit doesn't prove a send, `failed`
when the notifier reported its own failure, `unknown` when an attempt's final
result was interrupted or it was stopped for running past its deadline, or
`none` when no notifier was configured — so a run whose notice never went out
is spotted while scanning runs, not only when one is looked up. The full `job.status.notify_delivery`
object is null only for a non-terminal run.

---

## `li handshake`

Report what this installation is, for a program deciding whether it can talk to
it. Pair it with `--machine`, since a program is the only caller this is for.

```bash
li handshake --machine
```

```json
{"ok": true, "contract_version": 1,
 "data": {"contract_version": 1, "min_supported_version": 1,
          "implementation": "lionagi", "implementation_version": "0.30.2",
          "module": "/path/to/lionagi/cli"},
 "error": null}
```

`contract_version` is the machine-result contract this build speaks and
`min_supported_version` is the oldest it still accepts. A caller checks both
once at startup, then validates `contract_version` on every envelope afterwards
— the binary at a pinned path is replaced during normal operation, so a
handshake governs registration and never stands in for per-response checking.

`module` is where the code being served actually lives, which answers "is this
the checkout I think it is" without guessing from a version number.

---

## `li runs`

List the runs recorded on disk and what each one wrote.

```bash
li runs --machine [--limit N]
```

Each entry carries the run id, its state root, its artifact root, and the
artifacts found there. It reports which runs EXIST and what they left behind,
not whether any of them finished or succeeded — for that, ask `li job status` or
the MCP `request` operation `job.status`, which carry the terminal and outcome
derivations.

The artifact list is wrapped in the availability shape, so a directory that could
not be read is reported as unavailable with a reason rather than as a run that
produced nothing.

---

## `li lifecycle`

Report what the lifecycle store records about one CLI run.

```bash
li lifecycle <run-id> --machine
```

This is the one path from a run id to the rows the lifecycle writers actually
write. A normal teardown records an end; so does `li kill`, which writes the
row and signals the process without touching the MCP job record or the run
manifest. A caller holding only a run id and reading only those two would see a
dead process with no recorded end, which is what an orphaned run looks like.

The answer is read-only and carries its own availability. An established answer
with `found: false` means no session was ever recorded under this id. An
unavailable one means the store could not be read at all, which is not a
statement about the run. A caller that collapsed the two would report a run as
finished, or as never started, on the strength of a database it never opened.

The store consulted is the one `LIONAGI_STATE_DB_URL` names when it is set, and
the default otherwise — including for the question of whether a store exists at
all, so a configured store is never reported missing because the default path
is absent.

---

## Machine mode: `--machine`

Any command that reaches the dispatcher accepts `--machine`, which turns its
output into exactly one JSON object on stdout:

```json
{"ok": true, "contract_version": 1, "data": {...}, "error": null}
```

Exactly one of `data` and `error` is present, and `error.kind` is a closed set a
caller may branch on. Diagnostics, progress and warnings go to stderr, so stdout
carries the object and nothing else — a caller can parse it without scanning for
where the JSON starts.

Anything derived from a read that can fail is wrapped rather than flattened:

```json
{"available": false, "value": null, "reason_code": "unreadable",
 "detail": "permission denied"}
```

This keeps "there are no artifacts" and "the artifacts directory could not be
read" from sharing an encoding, which is the difference between a caller
reporting an empty result and reporting a broken one.

Check the exit status before parsing. **78 means nothing executed** — the
environment could not run the work at all — and stdout must not be parsed on it,
because attributing an environment fault to the submitted work is the
misattribution the code exists to prevent.

The full contract, including how `status`, `terminal` and `outcome` divide the
question of whether a run is over, is in
[ADR-0106](adr/ADR-0106-lion-machine-result-contract-v1.md).

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

See the [agent examples](https://github.com/ohdearquant/lionagi/tree/main/examples/agents)
for `minimal/` and `with-refs/`
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
| `timeout` | Default hard timeout in seconds |
| `resume_on_timeout` | Set to `once` for one bounded automatic continuation |
| `lion_system` | Prepend `LION_SYSTEM_MESSAGE` to the body (default: `true`) |
| `artifact_defaults` | Expected-artifact defaults; see [ADR-0064](adr/ADR-0064-cli-execution-outcome-and-completion-record.md) |

When `lion_system: true`, the global Lion system preamble is prepended to the body
to form the system prompt. Set it to `false` for a verbatim body (e.g. when the
profile already carries its own complete system prompt).

---

## Run-ID and persistence

Task-producing `agent`, fan-out, flow, and playbook invocations allocate a run
directory. Administrative commands such as `doctor` and `monitor` do not. Run
IDs use the format `YYYYMMDDTHHMMSS-{6hex}`. Source: `cli/_runs.py`.

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
| `LIONAGI_RUN_ID` | When explicitly set for a task-producing child process, reuse the supplied run ID | `cli/_runs.py` |
| `LIONAGI_HOME` | Override `~/.lionagi/` base dir | `lionagi/utils.py` |
| `LIONAGI_WORKER_LIVENESS_TIMEOUT` | Seconds `run()` waits for a CLI worker's first stream chunk before retrying once, then raising `WorkerLivenessError`; default `120`, `0` disables. Applied by default only to endpoints that stream output early (`claude_code`, `codex`) — buffered endpoints (`gemini-cli`, `pi`) are unaffected unless `liveness_timeout` is passed explicitly to `run()` | `lionagi/operations/run/run.py` |
| `OPENAI_API_KEY` | OpenAI REST API key (for `iModel`, not for `codex` CLI alias) | `lionagi/config.py` |
| `ANTHROPIC_API_KEY` | Anthropic REST API key (for `iModel`; `claude` alias uses `claude login` instead) | `lionagi/config.py` |
| `GEMINI_API_KEY` | Gemini API key (`gemini` provider, not `gemini-code` CLI auth) | `lionagi/config.py` |
| `GROQ_API_KEY` | Groq key | `lionagi/config.py` |

---

*Sources: `cli/agent.py` · `cli/team.py` · `cli/orchestrate/__init__.py` · `cli/orchestrate/fanout.py` · `cli/orchestrate/flow.py` · `cli/_providers.py` · `cli/_runs.py`*

Next: [Python API reference](api/index.md)
