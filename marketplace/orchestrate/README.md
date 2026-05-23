# orchestrate

Claude Code plugin for lionagi's multi-agent orchestration. Nine skills and two agent profiles covering workflow planning, execution, quality gating, code review, debugging, and development methodology.

## Prerequisites

- Claude Code CLI
- lionagi >= 0.26.0: `pip install lionagi`
- Optional: Lion Studio for monitoring: `li studio`

## Install

```bash
claude /plugin marketplace add ohdearquant/lionagi
claude /plugin install orchestrate@lionagi
```

## Skills

| Skill | Description |
|-------|-------------|
| `orchestrate` | Plan and execute multi-agent workflows via `li o flow`, `li o fanout`, `li play` |
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

```bash
# Run a playbook
li play feature "add user authentication"

# Fan out parallel workers
li o fanout claude "audit this module for dead code" -n 4

# Plan a DAG flow
li o flow claude "refactor the auth module" --dry-run

# Start Studio for monitoring
li studio
```

## lionagi folder setup

The `~/.lionagi/` directory is created on first use:

```
~/.lionagi/
├── playbooks/          # .playbook.yaml files (li play reads from here)
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

Copy any of them to get started:

```bash
cp examples/playbooks/feature.playbook.yaml ~/.lionagi/playbooks/
li play feature "add OAuth login"
```

## Getting help

- Issues: https://github.com/ohdearquant/lionagi/issues
- Discussions: https://github.com/ohdearquant/lionagi/discussions
- Source: `lionagi/cli/` for CLI, `apps/studio/` for Studio, `lionagi/state/` for data model

## Source code reference

- CLI orchestration: `lionagi/cli/orchestrate/flow.py`, `lionagi/cli/orchestrate/fanout.py`
- Agent system: `lionagi/cli/agent.py`, `lionagi/agent/`
- Playbooks: `~/.lionagi/playbooks/*.playbook.yaml`
- State DB schema: `lionagi/state/schema.sql`
- Studio: `apps/studio/server/`, `apps/studio/frontend/`
- Scheduler: `apps/studio/server/scheduler/`
