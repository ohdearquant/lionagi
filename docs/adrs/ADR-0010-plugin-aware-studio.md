# ADR-0010: Plugin-Aware Studio UI

**Status**: Accepted
**Date**: 2026-05-20
**Extends**: ADR-0003 (marketplace), ADR-0007 (auto-discovery), ADR-0009 (SQLite state layer)

## Context

Lion Studio currently presents skills, agents, and playbooks as flat lists scanned
from `~/.lionagi/{skills,agents,playbooks}/`. This ignores the plugin structure
established in ADR-0003: nine marketplace plugins at `marketplace/` already group
skills and agents by capability domain (devx, show, orchestrate, etc.).

The Skills page (62 skills) has no grouping — users see `commit`, `fmt`, `ci` next
to `flow-it`, `reprompt`, `write-playbook` with no indication they belong to
different plugins. Agent and playbook definitions are versioned through the definitions
API (ADR-0016) but not associated with their parent plugin.

Claude Code's plugin format provides a standard packaging unit: `plugin.json`
manifest + `skills/`, `agents/`, `hooks/`, `.mcp.json`, `output-styles/`,
`monitors/`, `themes/`, `bin/`, `settings.json`. Studio should present this
structure faithfully — it becomes the visual management layer for what CC loads.

## Decision

### 1. Plugin discovery backend (`services/plugins.py`)

A new service scans two locations for plugins:

| Source | Path | Writeable |
|--------|------|-----------|
| Marketplace | `{LIONAGI_HOME}/../marketplace/` (repo-relative) | Yes |
| Third-party | `~/.claude/plugins/cache/*/*/` | No (read-only) |

For each plugin directory, the service:
- Reads `.claude-plugin/plugin.json` for metadata (name, version, description)
- Falls back to directory name if no manifest exists
- Enumerates `skills/*/SKILL.md`, `agents/*.md`, `hooks/hooks.json`, `.mcp.json`
- Returns component counts and lists per plugin

The marketplace manifest (`.claude-plugin/marketplace.json`) provides the canonical
list for repo plugins. Third-party plugins are discovered by directory scan.

### 2. API surface

```
GET  /api/plugins/              → list all plugins with component counts
GET  /api/plugins/{name}        → plugin detail: metadata + component lists
GET  /api/plugins/{name}/skills/{skill}  → skill content (read-only)
GET  /api/plugins/{name}/agents/{agent}  → agent content (read-only view; editing via Agents page)
```

The existing `/api/skills/`, `/api/agents/`, and `/api/definitions/` endpoints
remain — the plugins API is the grouping/browsing layer, not an editing surface.

### 3. Frontend: Plugins replaces Skills in nav

Navigation changes from `Playbooks | Agents | Skills | Runs | Shows` to
`Playbooks | Agents | Plugins | Shows | Runs`.

**Plugin list page**: cards showing name, description, source badge (marketplace
name or `Lion Marketplace`), component counts (N skills, M agents).

**Plugin detail page**: tabbed layout:
- **Skills** tab: two-pane list+detail, read-only SKILL.md viewer
- **Agents** tab: agent names/descriptions with `Open in Agents →` cross-links
- **Hooks** tab: `hooks.json` viewer (read-only)
- **MCP** tab: `.mcp.json` viewer (read-only)
- **README** tab: rendered markdown (proportional font for prose)

### Editability matrix

| Component | Source | Editable in Studio? | Where? |
|-----------|--------|-------------------|--------|
| Marketplace agent definition | `marketplace/*/agents/*.md` | Yes | Agents page (definitions API) |
| Marketplace playbook | `marketplace/*/playbooks/*.yaml` | Yes | Playbooks page (definitions API) |
| Marketplace skill | `marketplace/*/skills/*/SKILL.md` | No | Read-only in Plugins; edit in text editor |
| Third-party plugin (any component) | `~/.claude/plugins/cache/` | No | Read-only everywhere |
| Hooks, MCP config | Plugin dirs | No | Read-only; managed by Claude Code |

Skills are **not** editable through the definitions API. They are Claude Code
skill instructions, not lionagi definitions. The Plugins page is a read-only
browser with cross-links to editable surfaces (Agents, Playbooks).

### 4. Third-party plugins are read-only

Plugins from `~/.claude/plugins/cache/` are displayed but not editable. The cache
is managed by Claude Code's `plugin add/remove` commands. Studio surfaces them for
visibility, not management.

### 5. Cross-links and source badges (added post-review)

Plugin components need explicit navigation to their editable counterparts and
clear source/editability indicators:

**Cross-links from plugin detail tabs**:
- Agent tab: `Open in Agents →` link per agent (routes to `/agents/{name}`)
- Skill tab: `View source` link, `Copy path` action
- All tabs: `Open raw` for filesystem path

**Source badges on plugin list and detail**:
| Badge | Meaning |
|-------|---------|
| `Lion Marketplace` | From `marketplace/`, writable |
| `{marketplace name}` | From third-party cache, title-cased directory name (e.g., `Anthropic Official`, `khive`). Read-only |
| `Versioned` | Definition tracked in definitions API (agents, playbooks) |
| `Read-only` | Filesystem artifact, not version-tracked |

**Empty plugin stubs** (0 skills, 0 agents) are de-emphasized — muted row
treatment, grouped under "Scaffolded" section — but not hidden, since hiding
them complicates plugin discovery debugging.

**Skill search**: Add a filter input inside the selected plugin's Skills tab,
not just the plugin-level filter.

**Markdown rendering**: README and skill docs render prose in proportional
font with code fences in monospace, not the current all-monospace treatment.

### 6. Deferred: CLI sync, monitors, themes, LSP

These are not part of this ADR:
- `li plugin sync` CLI (marketplace → CC plugin install) — separate feature
- Monitors, themes, LSP components — surface in Studio when usage warrants
- Model-agnostic abstraction — current format is CC-native; abstract later

## Consequences

**Positive**
- Skills and agents gain context: users see which plugin provides each capability.
- Editing flows through existing definitions versioning — no new persistence layer.
- Third-party plugin visibility without management complexity.
- Direct alignment with CC's plugin model — what you see in Studio is what CC loads.

**Negative**
- Plugin discovery adds startup scan cost (~50ms for 9 plugins, negligible).
- Two navigation paths to the same skill (Plugins → devx → commit vs. direct URL).
- `plugin.json` may not exist for all marketplace plugins yet — fallback to dir name.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|-------------|
| Keep Skills as top-level, add plugin grouping filter | Doesn't surface the plugin concept; users still think in flat lists |
| Replace both Skills and Agents nav with Plugins | Agents page has profile editing that doesn't map cleanly to plugin context; keep separate |
| Add plugin management (install/remove/update) | Scope creep — CC handles lifecycle; Studio is the viewer/editor |

## Implementation

Two parallel agents:

1. **Backend**: `services/plugins.py` + `routers/plugins.py` — plugin scanner, API endpoints
2. **Frontend**: `/plugins` page (list + detail with tabs), Shell nav update, API client types

Both build on existing patterns: `services/skills.py` for scanning, `app/agents/page.tsx`
for two-pane layout, `lib/api.ts` for type-safe fetch wrappers.
