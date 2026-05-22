# lionagi Marketplace

Claude Code marketplace plugins for the lionagi agent runtime. Install only the capabilities you need.

## What is this?

The lionagi marketplace bundles curated skills, agents, and configuration into installable Claude Code plugins. Each plugin targets a specific capability — structured workflow runs, memory management, playbook authoring, and multi-agent orchestration. The manifest at `../.claude-plugin/marketplace.json` declares all available plugins.

## Prerequisites

- Claude Code CLI (v1.x+)
- lionagi installed: `pip install lionagi` (>= 0.26.0)
- No other dependencies required for the 5 catalog plugins.

## Install

```bash
# Add the lionagi marketplace to Claude Code
claude /plugin marketplace add ohdearquant/lionagi

# Install a specific plugin
claude /plugin install show@lionagi
claude /plugin install devx@lionagi
claude /plugin install orchestrate@lionagi
claude /plugin install play@lionagi
claude /plugin install memory@lionagi
```

## Where to start

- **New to lionagi?** Install `devx` first — it covers commit, fmt, ci, pr, and init.
- **Orchestrating multi-step workflows?** Install `show` + `orchestrate`.
- **Authoring playbooks?** Install `play`.
- **Maintaining MEMORY.md?** Install `memory` (note: memory-recall is deferred to v2.1; migrate-memory is available now).

## Plugins

| Name | Description |
|------|-------------|
| `show` | Orchestrate multi-play workflows: decompose a goal into li-play invocations, gate each output, and adapt the plan based on results. |
| `play` | Author lionagi playbooks — the YAML files that li play and li o flow use to define and invoke reusable agent workflows. |
| `orchestrate` | Multi-agent DAG orchestration via li o flow and li o fanout: write flow specs, validate them, fire parallel agents, and monitor execution. |
| `devx` | Development workflow skills: conventional commits, formatting, CI checks, PR creation, and session summaries. |
| `memory` | Maintain the auto-memory space: prune stale files, condense MEMORY.md, and keep project context navigable across sessions. |

## Decision record

See ADR-0003 (docs/adrs/ADR-0003-claude-code-marketplace.md) for the architectural rationale behind this structure.
