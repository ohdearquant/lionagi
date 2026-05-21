# lionagi Marketplace

Claude Code marketplace plugins for the lionagi agent runtime. Install only the capabilities you need.

## What is this?

The lionagi marketplace bundles curated skills, agents, and configuration into installable Claude Code plugins. Each plugin targets a specific capability — structured workflow runs, multi-agent research, memory management, playbook authoring, and multi-agent orchestration. The manifest at `../.claude-plugin/marketplace.json` declares all available plugins.

## Install

```bash
# Add the lionagi marketplace to Claude Code
claude /plugin marketplace add ohdearquant/lionagi

# Install a specific plugin
claude /plugin install show@lionagi
claude /plugin install research@lionagi
claude /plugin install devx@lionagi
```

## Plugins

| Name | Description |
|------|-------------|
| `show` | Orchestrate multi-step agent workflows with quality gates and isolated workspaces |
| `play` | Define and run reusable workflow templates (playbooks) that parameterize agent tasks |
| `orchestrate` | Plan and run multi-agent pipelines: fan out to parallel workers or chain agents in dependency order |
| `research` | Run structured research across multiple viewpoints using web search, codebase analysis, and synthesis |
| `memory` | Persist and recall project context — decisions, patterns, and facts — across sessions |
| `devx` | Development workflow skills: conventional commits, formatting, CI checks, PR creation, and session summaries (session-start/session-summarize planned) |

## Coming soon

| Name | Status |
|------|--------|
| `studio` | Deferred: `li studio mcp` is not implemented yet; re-list when the MCP server ships. |
| `mcp-bundle` | Deferred: skeleton only; re-list when agent skills and real MCP server config land. |

## Decision record

See ADR-0003 (docs/adrs/ADR-0003-claude-code-marketplace.md) for the architectural rationale behind this structure.

## Plugin content

Plugin skills, agents, and MCP server configuration are populated in subsequent plays:

- **marketplace-plugins-core** — fills `show`, `play`, `orchestrate` with skills and agent profiles
- **marketplace-plugins-knowledge** — fills `research`, `memory`
- **marketplace-plugins-app** — fills `studio` (MCP server config) and `mcp-bundle`, `devx`
