# ADR-0003: Claude Code Marketplace

**Status**: Amended (v2 catalog — see below)
**Date**: 2026-05-19
**Amended**: 2026-05-21

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

## v2 Catalog (Current — Phase 0, 2026-05-21)

Four plugins are in the active catalog. No external dependencies beyond the lionagi
package are required for any of the four.

| Plugin | Scope |
|--------|-------|
| `show` | Direct multi-play DAGs with critic gating and worktree isolation |
| `play` | Author lionagi playbooks for li play / li o flow |
| `orchestrate` | Multi-agent orchestration via li o flow and li o fanout |
| `devx` | Conventional commit, formatting, CI, PR, summarize, session-start/-summarize |

### Deleted plugins (v1 → v2)

| Plugin | Reason |
|--------|--------|
| `research` | Contains private trading IP that cannot be shipped in a public package. Removed entirely; no replacement planned in the public catalog. |
| `kg-bridge` | Tight coupling to khive MCP server. lionagi's public marketplace must not depend on khive internals. Removed; khive users can configure the bridge manually. |

### Deferred plugins (v2.1+)

| Plugin | Reason | Target |
|--------|--------|--------|
| `studio` | Depends on FastAPI backend route contracts (ADR-0004) not yet implemented. No standalone value without the backend. | v2.1 after ADR-0004 stabilises |
| `mcp-bundle` | Depends on the lionagi canonical MCP server, which is not yet in a shippable state. | v2.1 alongside `studio` |
| `memory` | Both shipped skills had hard khive-MCP dependencies that violated the no-external-deps goal: `memory-recall` called `mcp__khive__recall` / `mcp__khive__search`, and `migrate-memory` described optional khive memory sync that was awkward without it. The whole plugin will be rewritten against `~/.lionagi/runs/` and Studio APIs. | v2.1 |

The `_deferred_plugins` block in `.claude-plugin/marketplace.json` records `studio`,
`mcp-bundle`, and `memory` so they are not silently forgotten.

This play establishes the skeleton (manifests, directory structure, README, this ADR). Plugin content (skills, agents, MCP server configuration) is added in three subsequent plays: `marketplace-plugins-core`, `marketplace-plugins-knowledge`, and `marketplace-plugins-app`.

## v1 Historical Catalog (2026-05-19, superseded)

The original decision described a nine-plugin catalog:

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

This nine-plugin catalog was the initial intent. The v2 amendment above records what
shipped in Phase 0 and why the remaining four were removed or deferred.

## Consequences

**Positive**
- Users install only the capability slices they need, keeping Claude Code context lean.
- Each plugin can version independently; `studio` can ship a breaking MCP config change without bumping `devx`.
- MCP server configuration can be bundled per plugin (`studio`, `mcp-bundle`) once the FastAPI backend route set is stable.
- Clear ownership boundary: each plugin directory is a self-contained unit that external contributors or downstream forks can understand and extend.

**Negative**
- More manifests to maintain: root `marketplace.json` plus four `plugin.json` files (v2) must stay in sync as plugin names or descriptions change.
- Skills authored in `firm/resources/skills/` (canonical) must be copied or symlinked into `marketplace/<plugin>/skills/` for external installs — two places to update per skill change.
- The `plugin.json` schema is not yet finalized by Anthropic; field names or required keys may shift before GA, requiring a sweep across all four manifests.
- `studio` and `mcp-bundle` are deferred to v2.1 and no longer ship standalone manifests in Phase 0; they are recorded only in the `_deferred_plugins` block of the root manifest.

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
