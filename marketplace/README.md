# lionagi Marketplace

Claude Code marketplace plugin for the lionagi agent runtime.

## What is this?

The lionagi marketplace provides a Claude Code plugin that teaches agents how to use lionagi's multi-agent orchestration capabilities: DAG workflow planning, playbook authoring, multi-play show execution, and Lion Studio integration.

## Prerequisites

- Claude Code CLI (v1.x+)
- lionagi >= 0.26.0: `pip install lionagi`
- Optional: Lion Studio for monitoring: `li studio`

## Install

```bash
claude /plugin marketplace add ohdearquant/lionagi
claude /plugin install orchestrate@lionagi
```

## Plugin

| Name | Skills | Agents | Description |
|------|--------|--------|-------------|
| `orchestrate` | orchestrate, show, playbook | orchestrator, critic | Multi-agent orchestration: DAG workflows, playbooks, shows with quality gates |

## Sample Playbooks

Ready-to-use playbooks in `examples/playbooks/`:

```bash
cp examples/playbooks/feature.playbook.yaml ~/.lionagi/playbooks/
li play feature "add OAuth login"
```

## Getting Help

- File issues: https://github.com/ohdearquant/lionagi/issues
- Discussions: https://github.com/ohdearquant/lionagi/discussions
- Documentation: https://lionagi.ai

## Decision Record

See ADR-0003 (`docs/_archive/v0/ADR-0003-claude-code-marketplace.md`).
