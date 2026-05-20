# ADR-0003: Claude Code Marketplace

**Status**: Accepted
**Date**: 2026-05-19

## Context

lionagi is evolving from a Python SDK into a daily-driver agent runtime (ADR-0001). Users now reach for it for show direction, research workflows, memory hygiene, and the Lion Studio dashboard — capability sets that differ significantly in what they require. Installing all of these as a single undifferentiated package forces users to accept the full dependency surface when they may only want one slice.

Claude Code's plugin marketplace provides a tested distribution primitive: a root `marketplace.json` manifest lists available plugins; each plugin lives at `marketplace/<name>/` with its own `plugin.json`, skills, agent profiles, and optional MCP server configuration. The khive repository at `/Users/lion/projects/khive/khive/marketplace/` has validated this pattern in production.

## Decision

Adopt the Claude Code marketplace pattern inside the lionagi repository. The structure is:

```
.claude-plugin/marketplace.json          # root manifest
marketplace/<plugin>/.claude-plugin/plugin.json  # per-plugin manifest
marketplace/<plugin>/skills/             # bundled skills (added in later plays)
marketplace/<plugin>/agents/             # bundled agent profiles (added in later plays)
```

Nine plugins cover the full capability surface by scope:

| Plugin | Scope |
|--------|-------|
| `show` | Direct multi-play DAGs with critic gating and worktree isolation |
| `play` | Author lionagi playbooks for li play / li o flow |
| `orchestrate` | Multi-agent orchestration via li o flow and li o fanout |
| `research` | Multi-perspective research with web search, codebase analysis, and synthesis |
| `memory` | Memory recall, MEMORY.md hygiene, auto-memory bootstrap |
| `kg-bridge` | Bridge lionagi runs/agents to khive knowledge graph |
| `devx` | Conventional commit, formatting, CI, PR, summarize, session-start/-summarize |
| `studio` | Lion Studio dashboard — runs/agents/playbooks/shows monitoring UI with FastAPI backend MCP |
| `mcp-bundle` | Lionagi canonical MCP server access for agents |

This play establishes the skeleton (manifests, directory structure, README, this ADR). Plugin content (skills, agents, MCP server configuration) is added in three subsequent plays: `marketplace-plugins-core`, `marketplace-plugins-knowledge`, and `marketplace-plugins-app`.

## Consequences

**Positive**
- Users install only the capability slices they need, keeping Claude Code context lean.
- Each plugin can version independently; `studio` can ship a breaking MCP config change without bumping `devx`.
- MCP server configuration can be bundled per plugin (`studio`, `mcp-bundle`) once the FastAPI backend route set is stable.
- Clear ownership boundary: each plugin directory is a self-contained unit that external contributors or downstream forks can understand and extend.

**Negative**
- More manifests to maintain: root `marketplace.json` plus nine `plugin.json` files must stay in sync as plugin names or descriptions change.
- Skills authored in `firm/resources/skills/` (canonical) must be copied or symlinked into `marketplace/<plugin>/skills/` for external installs — two places to update per skill change.
- The `plugin.json` schema is not yet finalized by Anthropic; field names or required keys may shift before GA, requiring a sweep across all nine manifests.
- `studio` and `mcp-bundle` manifests are intentionally incomplete stubs until the FastAPI route contracts from ADR-0004 are implemented; downstream consumers of those plugins will see empty capability until `marketplace-plugins-app` lands.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| One mega-plugin — single `plugin.json` at `.claude-plugin/` with all skills and agents bundled | No capability slicing; every user gets the full surface even if they only want `devx`; context window cost is proportional to plugin size |
| Separate npm-style packages — one npm/PyPI package per capability slice | ADR-0001 established that lionagi stays as a single monorepo; splitting into per-plugin packages creates the release-drift and version-pin-alignment problems that motivated the monorepo decision |
| Symlink `firm/resources/skills/` directly into `marketplace/<plugin>/skills/` | `firm` is a private repository; marketplace plugins must be self-contained for external installs by users who do not have access to `firm` |

## References

- [ADR-0001: Lion Studio as Internal App](ADR-0001-lion-studio-internal-app.md) — establishes monorepo boundary and daily-driver app direction
- [ADR-0002: Lion Studio Tech Stack](ADR-0002-studio-tech-stack.md) — establishes FastAPI backend stack that the `studio` and `mcp-bundle` plugins depend on
- [ADR-0004: Filesystem Data Layer](ADR-0004-filesystem-data-layer.md) — establishes the FastAPI backend route contracts that `studio` and `mcp-bundle` plugins will eventually configure
- khive marketplace reference implementation: `/Users/lion/projects/khive/khive/marketplace/`

---

## Appendix — Skills Absent from `devx` Bundle at Time of Authoring

`session-start.md` and `session-summarize.md` skills were not present in `firm/` at the time the
`marketplace-plugins-app` play ran. They are therefore omitted from the `devx` plugin bundle.
A TODO comment is left in `marketplace/devx/skills/` for a future play to copy them in once they
land in `firm/resources/skills/`.

Source: `marketplace-plugins-app/_intent.md:102`
