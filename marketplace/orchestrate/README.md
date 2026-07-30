# orchestrate

Claude Code plugin for lionagi's multi-agent orchestration. Nine skills and two agent profiles covering workflow planning, execution, quality gating, code review, debugging, and development methodology.

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
- Optional, only if you want the CLI or Lion Studio locally inside a checkout:
  `pip install lionagi` (or `uv pip install lionagi`), then `li studio` for the monitoring UI.

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

That returns the full verb catalog. The eleven verbs published today: `agent.submit`,
`flow.submit`, `fanout.submit`, `play.submit`, `job.status`, `job.output`, `job.list`,
`job.wait`, `job.kill`, `profile.list`, `profile.show`. Ask `help='<verb>'` for one verb's
parameter schema before filling in `args` — the server resolves whatever lionagi release
`uvx` has cached, so this is the source of truth over anything written here.

`ops` is always an array of `{"op": "<verb>", "args": {...}}` objects, even for one operation.
The four spawn verbs (`agent.submit`, `flow.submit`, `fanout.submit`, `play.submit`) each also
need a `schema_fingerprint` — a sibling of `args`, not a member of it — copied from that verb's
`help='<verb>'` reply; an op missing it or carrying a stale one is refused with the current
fingerprint:

```json
// Run a playbook
{"ops": [{"op": "play.submit", "args": {"playbook": "feature", "prompt": "add user authentication"}, "schema_fingerprint": "<from help>"}]}

// Fan out parallel workers
{"ops": [{"op": "fanout.submit", "args": {"prompt": "audit this module for dead code", "num_workers": 4}, "schema_fingerprint": "<from help>"}]}

// Plan and run a DAG flow
{"ops": [{"op": "flow.submit", "args": {"prompt": "refactor the auth module", "agent": "orchestrator"}, "schema_fingerprint": "<from help>"}]}
```

Each of these returns a `run_id`. Poll or block on it, and read what it wrote:

```json
{"ops": [{"op": "job.wait", "args": {"run_ids": ["<run_id>"]}}]}
{"ops": [{"op": "job.output", "args": {"run_id": "<run_id>"}}]}
```

See [`skills/orchestrate/reference.md`](skills/orchestrate/reference.md) for the full
verb reference, including `schema_fingerprint` details and the checkout-local `li` command
each verb corresponds to.

**Checkout-local alternative.** Inside a lionagi checkout with `li` on PATH, the same three
operations run in the foreground as `li play feature "add user authentication"`,
`li o fanout claude "audit this module for dead code" -n 4`, and
`li o flow claude "refactor the auth module" --dry-run`. `li studio` starts Lion Studio for
monitoring — there is no MCP verb for the Studio server itself, since it is a long-lived
process rather than a call that returns.

## lionagi folder setup

The `~/.lionagi/` directory is created on first use:

```
~/.lionagi/
├── playbooks/          # .playbook.yaml files (play.submit and li play both read from here)
├── agents/             # Agent profiles (<name>/<name>.md or <name>.md)
├── runs/               # Run persistence (auto-managed)
├── shows/              # Show workspaces (auto-managed)
├── worktrees/          # Git worktrees for isolated play execution
├── teams/              # Team inbox files (auto-managed)
├── skills/             # CC-compatible skill files
├── settings.yaml       # Global settings (model defaults, hooks)
└── state.db            # SQLite state database (sessions, shows, schedules)
```

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

Then run it through the plugin's MCP server (ask `help='play.submit'` first for the current
`schema_fingerprint`):

```json
{"ops": [{"op": "play.submit", "args": {"playbook": "feature", "prompt": "add OAuth login"}, "schema_fingerprint": "<from help>"}]}
```

**Checkout-local alternative.** Inside a lionagi checkout, `li play feature "add OAuth login"`
runs the same playbook in the foreground.

## Getting help

- Issues: https://github.com/ohdearquant/lionagi/issues
- Discussions: https://github.com/ohdearquant/lionagi/discussions
- Source: `lionagi/mcp/` for the MCP server, `lionagi/cli/` for the CLI it wraps,
  `apps/studio/` for Studio, `lionagi/state/` for the data model

## Source code reference

- MCP server: `lionagi/mcp/server.py` (the `request` tool), `lionagi/mcp/verbs.py` (verb registry)
- CLI orchestration: `lionagi/cli/orchestrate/flow.py`, `lionagi/cli/orchestrate/fanout.py`
- Agent system: `lionagi/cli/agent.py`, `lionagi/agent/`
- Playbooks: `~/.lionagi/playbooks/*.playbook.yaml`
- State DB schema: `lionagi/state/schema.sql`
- Studio: `apps/studio/server/`, `apps/studio/frontend/`
- Scheduler: `apps/studio/server/scheduler/`
