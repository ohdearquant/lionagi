# lionagi Marketplace

Claude Code marketplace plugins for the lionagi agent runtime. Install only the capabilities you need.

## What is this?

The lionagi marketplace bundles curated skills, agents, and configuration into installable Claude Code plugins. Each plugin targets a specific capability slice — show direction, research patterns, memory hygiene, the Lion Studio dashboard, and more. The manifest at `../.claude-plugin/marketplace.json` declares all available plugins.

## Install

```bash
# Add the lionagi marketplace to Claude Code
claude /plugin marketplace add khive-ai/lionagi

# Install a specific plugin
claude /plugin install show@lionagi
claude /plugin install research@lionagi
claude /plugin install devx@lionagi
```

## Plugins

| Name | Description |
|------|-------------|
| `show` | Direct multi-play DAGs with critic gating and worktree isolation |
| `play` | Author lionagi playbooks for li play / li o flow |
| `orchestrate` | Multi-agent orchestration via li o flow and li o fanout |
| `research` | Multi-perspective research with web search, codebase analysis, and synthesis |
| `memory` | Memory recall, MEMORY.md hygiene, auto-memory bootstrap |
| `kg-bridge` | Bridge lionagi runs/agents to khive knowledge graph |
| `devx` | Conventional commit, formatting, CI, PR, summarize, session-start/-summarize |

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
- **marketplace-plugins-knowledge** — fills `research`, `memory`, `kg-bridge`
- **marketplace-plugins-app** — fills `studio` (MCP server config) and `mcp-bundle`, `devx`
