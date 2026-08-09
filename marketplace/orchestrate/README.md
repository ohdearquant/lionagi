# orchestrate

Claude Code plugin for lionagi's multi-agent orchestration. Twelve skills and two agent profiles covering workflow planning, execution, quality gating, code review, debugging, and development methodology.

Orchestration is reached through the MCP server this plugin ships — enabling the plugin is
enough, there is nothing separate to install for it to work. A lionagi checkout with `li` on
PATH remains a secondary, local path for the few things it does better; every skill notes it
where it applies, but the calls you'll actually make go through the server.

## Prerequisites

- Claude Code CLI
- `uv` on PATH — the plugin's MCP server runs as `uvx --from lionagi[mcp] li mcp serve`, and
  `uv` is the only thing that needs to already be present; `uvx` resolves and caches lionagi
  itself on first use. Without it the plugin's tool fails visibly, naming the missing
  prerequisite, rather than silently doing nothing.
- Optional, only if you want local commands: run `pip install lionagi` for the CLI, or
  `pip install 'lionagi[studio]'` if you also want `li studio` for the monitoring UI.

## Install

```bash
claude /plugin marketplace add ohdearquant/lionagi
claude /plugin install orchestrate@lionagi
```

Enabling the plugin registers its MCP server automatically; no `.mcp.json` to write and
nothing further to configure.

## Skills

| Skill | Description |
|-------|-------------|
| `orchestrate` | Plan and execute multi-agent workflows via the plugin's MCP server (`flow.submit`, `fanout.submit`, `play.submit`) |
| `play` | Run an existing saved playbook and retrieve its background result |
| `fanout` | Run independent workers in parallel, optionally with synthesis |
| `flow` | Run a dependency DAG with dry-run planning and reactive capacity |
| `show` | Orchestrate multi-play shows with quality gates and adaptive replanning |
| `playbook` | Author `.playbook.yaml` files — reusable parametric workflow templates |
| `pr-review` | Multi-perspective PR review with parallel specialist reviewers and critic synthesis |
| `review` | General-purpose code review checklist (correctness, API, tests, readability, security) |
| `security-review` | Threat-model security review rubric with CWE mapping and severity calibration |
| `debug` | Systematic debugging workflow: research → orchestrate agents → escalate |
| `summarize` | Mid-session context capture: checkpoint decisions, patterns, and progress |
| `tdd` | Test-driven development discipline: Red-Green-Refactor with gate checks |

## Agent profiles

| Agent | Role |
|-------|------|
| `orchestrator` | DAG planner: decomposes tasks, assigns workers, manages artifacts, synthesizes results |
| `critic` | Quality gate: adversarial review, evidence-based verdicts (APPROVE/REJECT) |

## Quick start

Every call goes through one tool, `mcp__plugin_orchestrate_lion__request`, with two optional
inputs: `help` and `ops`. Ask for the catalog first — a catalog request and an operations
request return differently-shaped replies, and the server refuses a request that asks for
both at once:

```json
{"help": true}
```

That returns the full verb catalog, which is the authority — the server resolves whatever
lionagi release `uvx` has cached, so the catalog is the source of truth over anything written
here. It covers more than orchestration (schedules, run history, state and lifecycle reads,
server health), and it also names some verbs only to say the server will not run them, with
the reason. The orchestration verbs the skills in this bundle call are `agent.submit`,
`flow.submit`, `fanout.submit`, `play.submit`, `job.status`, `job.output`, `job.list`,
`job.wait`, `job.kill`, `profile.list` and `profile.show`. Ask `help='<verb>'` for one verb's
parameter schema before filling in `args`.

`ops` is always an array of `{"op": "<verb>", "args": {...}}` objects, even for one operation.
The four spawn verbs (`agent.submit`, `flow.submit`, `fanout.submit`, `play.submit`) each also
need a `schema_fingerprint` — a sibling of `args`, not a member of it — copied from that verb's
`help` reply; an op missing it or carrying one from a different schema is refused with the
current fingerprint.

One wrinkle, worth knowing before it costs you a round-trip: when a call names a `playbook`,
the schema includes that playbook's own declared arguments, so the fingerprint depends on it.
For those calls the fingerprint has to come from `help={"verb": "<verb>", "playbook": "<the
same name>"}`. Because `play.submit` requires a playbook, unqualified `help='play.submit'`
does not return a usable fingerprint. `play.submit` always needs the qualified form:

```text
// Run a playbook — fingerprint from help={"verb": "play.submit", "playbook": "feature"}
{"ops": [{"op": "play.submit", "args": {"playbook": "feature", "prompt": "add user authentication", "cwd": "/absolute/path/to/repository"}, "schema_fingerprint": "<from that playbook-qualified help call>"}]}

// Fan out parallel workers — no playbook, so help='fanout.submit' is the source
{"ops": [{"op": "fanout.submit", "args": {"query": ["claude"], "prompt": "audit this module for dead code", "num_workers": 4, "cwd": "/absolute/path/to/repository"}, "schema_fingerprint": "<from help='fanout.submit'>"}]}

// Plan and run a DAG flow — no playbook named, so help='flow.submit' is the source
{"ops": [{"op": "flow.submit", "args": {"query": ["claude"], "prompt": "refactor the auth module", "cwd": "/absolute/path/to/repository"}, "schema_fingerprint": "<from help='flow.submit'>"}]}
```

Each of these returns a `run_id`. `job.wait` observes it for a bounded window; check the op's
`ok` field and the result's `all_terminal` field, and repeat when the run is still pending.
Only then read what it wrote:

```json
{"ops": [{"op": "job.wait", "args": {"run_ids": ["<run_id>"]}}]}
{"ops": [{"op": "job.output", "args": {"run_id": "<run_id>"}}]}
```

See [`skills/orchestrate/reference.md`](skills/orchestrate/reference.md) for the full
verb reference, including `schema_fingerprint` details and the checkout-local `li` command
each verb corresponds to.

**Checkout-local alternative.** Inside a lionagi checkout with `li` on PATH, the same three
operations run in the foreground as `li play feature "add user authentication" --cwd "$(pwd)"`,
`li o fanout claude "audit this module for dead code" -n 4 --cwd "$(pwd)"`, and
`li o flow claude "refactor the auth module" --cwd "$(pwd)"`. `li studio` starts Lion Studio for
monitoring — there is no MCP verb for the Studio server itself, since it is a long-lived
process rather than a call that returns.

## lionagi folder setup

The `~/.lionagi/` directory is created on first use:

```
~/.lionagi/
├── playbooks/          # .playbook.yaml files (play.submit and li play both read from here)
├── agents/             # Agent profiles (<name>/<name>.md or <name>.md)
├── runs/               # Run persistence (auto-managed)
├── teams/              # Team inbox files (auto-managed)
├── skills/             # CC-compatible skill files
├── settings.yaml       # Global settings (model defaults, hooks)
└── state.db            # SQLite state database (sessions, shows, schedules)
```

Set `LIONAGI_SHOWS_ROOT` to a directory you control before using the `show` skill, and start
Studio with the same value. The skill uses `~/.lionagi/worktrees/` as a documented git
worktree convention; that directory is not auto-managed by lionagi.

## Sample playbooks

Ready-to-use playbooks in `examples/playbooks/` in the lionagi repo:

| Playbook | Purpose |
|----------|---------|
| `feature.playbook.yaml` | End-to-end feature implementation |
| `pr-review.playbook.yaml` | Multi-perspective PR review |
| `test-coverage.playbook.yaml` | Iterative test coverage |
| `research.playbook.yaml` | Technical research pipeline |
| `resolve-issues.playbook.yaml` | GitHub issue resolution |
| `doc-alignment.playbook.yaml` | Documentation generation/alignment |

Copy any of them into `~/.lionagi/playbooks/` to get started — this step is a plain file copy
regardless of which interface you run the playbook through next:

```bash
cp examples/playbooks/feature.playbook.yaml ~/.lionagi/playbooks/
```

Then run it through the plugin's MCP server. Ask for the fingerprint first, naming the
playbook — that is what resolves its declared arguments into the schema the call is judged
against:

```json
{"help": {"verb": "play.submit", "playbook": "feature"}}
```

```json
{"ops": [{"op": "play.submit", "args": {"playbook": "feature", "prompt": "add OAuth login"}, "schema_fingerprint": "<from the help call above>"}]}
```

**Checkout-local alternative.** Inside a lionagi checkout, `li play feature "add OAuth login"`
runs the same playbook in the foreground.

## Getting help

- Issues: https://github.com/ohdearquant/lionagi/issues
- Discussions: https://github.com/ohdearquant/lionagi/discussions
- Source: `lionagi/mcp/` for the MCP server, `lionagi/cli/` for the CLI it wraps,
  `lionagi/studio/` for the Studio backend and `apps/studio/frontend/` for its UI,
  `lionagi/state/` for the data model

## Source code reference

- MCP server: `lionagi/mcp/server.py` (the `request` tool), `lionagi/mcp/verbs.py` (verb registry)
- CLI orchestration: `lionagi/cli/orchestrate/flow.py`, `lionagi/cli/orchestrate/fanout.py`
- Agent system: `lionagi/cli/agent.py`, `lionagi/agent/`
- Playbooks: `~/.lionagi/playbooks/*.playbook.yaml`
- State DB schema: `lionagi/state/schema.sql`
- Studio: `lionagi/studio/` (backend), `apps/studio/frontend/` (UI)
- Scheduler: `lionagi/studio/scheduler/`
