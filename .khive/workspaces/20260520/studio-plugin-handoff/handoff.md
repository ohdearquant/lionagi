# Studio Plugin Organization — Handoff

**Date**: 2026-05-20 | **Branch**: `feat/studio-monitoring-polish` | **Last commit**: `de46533de`

## What's Done

### SQLite State Layer (fully working)
- Schema: messages, progressions, sessions, branches, definitions tables
- Live message streaming via `on_message_added` hook in MessageManager
- `li agent` + `li play` (flow/fanout) stream messages into SQLite in real-time
- `li state import` migrated 549 runs (376 sessions, 2035 branches, 146K messages, 821MB DB)
- `li state ls` lists sessions

### Backend APIs
- `GET/POST /api/sessions/` — list + SSE stream (optimized: 28ms for 376 sessions)
- `GET/PUT/POST /api/definitions/` — versioned agent/playbook/skill files (disk = source of truth, SQLite = edit history)
- `GET /api/skills/` — scans `~/.lionagi/skills/`, parses frontmatter, returns 62 skills
- All existing playbooks/agents/runs/shows APIs still work

### Frontend (all verified in browser)
- **Runs page**: reads from SQLite, shows 376 sessions, live SSE updates, RunStepCard detail view
- **Playbooks page**: two-pane layout — list left, detail right with inline edit, save (versioned), rollback, Run button (redirects to /runs/{id})
- **Agents page**: two-pane layout — list left, detail right with inline edit, versioning, rollback, version history sidebar
- **Skills page**: two-pane layout — 62 skills, filter, detail with allowed-tools badges and full content
- **Nav**: Playbooks | Agents | Skills | Runs | Shows

## What's Next: Plugin-Aware Organization

### The Problem
Skills/agents currently live in two places with different structures:
1. `~/.lionagi/skills/` — flat dirs, legacy format (62 skills, many are symlinks to `firm/`)
2. `marketplace/` — proper Claude Code plugin format, organized by capability plugin

Studio should present a unified, plugin-grouped view and serve as the management UI.

### Claude Code Plugin Taxonomy (from source code analysis)

```
plugin-name/
├── plugin.json          # Manifest: name, version, description, author, deps
├── skills/              # Directory-based: skill-name/SKILL.md
│   └── skill-name/
│       └── SKILL.md     # YAML frontmatter + markdown body
├── agents/              # Flat .md files with YAML frontmatter
│   └── agent-name.md
├── hooks/
│   └── hooks.json       # Lifecycle hooks (PreToolUse, PostToolUse, etc.)
├── .mcp.json            # MCP server configs
├── output-styles/       # Custom output formatting
└── README.md
```

**Marketplace manifest** (`.claude-plugin/marketplace.json`):
```json
{
  "name": "lionagi",
  "version": "0.1.0",
  "plugins": [
    { "name": "show", "source": "./marketplace/show", "description": "..." },
    { "name": "devx", "source": "./marketplace/devx", "description": "..." }
  ]
}
```

**Install locations** Claude Code uses:
- `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/` — marketplace installs
- `~/.claude/skills/` — user-level skills
- `.claude/skills/` — project-level skills

### Current Marketplace Layout (already exists at `marketplace/`)

```
marketplace/
├── README.md
├── devx/           → ci, commit, fmt, pr, status, summarize, wake-up + reviewer agent
├── show/           → show skill + critic, play-gate, show-final-gate agents
├── play/           → write-playbook skill
├── orchestrate/    → flow-it, reprompt skills + orchestrator agent
├── research/       → progress-research skill + researcher agent
├── memory/         → memory skills
├── kg-bridge/      → bridge-design skill
├── studio/         → stub (needs MCP server config)
└── mcp-bundle/     → stub
```

### Design Direction

**Studio becomes the plugin management UI:**

1. **Plugin browser page** — list all plugins (from marketplace/ + any installed third-party). Each plugin card shows: name, description, skill count, agent count, enabled/disabled toggle.

2. **Plugin detail view** — click a plugin to see its contents organized by component type:
   - Skills tab: list of skills with SKILL.md content, edit + version
   - Agents tab: list of agents with .md content, edit + version  
   - Hooks tab: hooks.json viewer/editor
   - MCP tab: .mcp.json viewer
   - README tab

3. **Definitions versioning** — already working. Every edit to a skill or agent `.md` file writes to disk + records a version in SQLite. The definitions API already supports `kind` = agent/playbook/skill.

4. **Sync with Claude Code** — the marketplace/ dir IS the Claude Code plugin source (installed via `claude /plugin marketplace add khive-ai/lionagi`). Edits in Studio propagate to Claude Code automatically because they write to the same files.

5. **Third-party plugins** — scan `~/.claude/plugins/cache/` for installed plugins and present them read-only in Studio (or with local overrides that get versioned).

### Implementation Plan (parallel agents)

**Agent 1: Plugin backend service**
- New `services/plugins.py` — scan `marketplace/` for plugins, parse `plugin.json` manifests, enumerate skills/agents/hooks per plugin
- Also scan `~/.claude/plugins/cache/` for third-party installed plugins
- `GET /api/plugins/` — list all plugins with component counts
- `GET /api/plugins/{name}` — plugin detail (skills, agents, hooks, mcp)
- The existing definitions API handles versioning; this is just the discovery/grouping layer

**Agent 2: Plugin browser frontend**
- Replace current Skills nav item with "Plugins" (or keep Skills as sub-view)
- Plugin list page: cards showing name, description, components
- Plugin detail page: tabbed view (Skills | Agents | Hooks | MCP | README)
- Each tab reuses existing two-pane patterns for editing
- Wire save/rollback to existing definitions API

**Agent 3: Marketplace sync**
- `li plugin sync` CLI command — ensures `~/.lionagi/skills/` symlinks point to `marketplace/` skills
- `li plugin ls` — list installed plugins and their components
- Read `.claude-plugin/marketplace.json` to understand the canonical plugin list

### Files to Reference

| File | What it contains |
|------|-----------------|
| `apps/studio/server/services/definitions.py` | Versioned file CRUD (disk + SQLite) |
| `apps/studio/server/services/skills.py` | Current skill scanner (flat dir) |
| `apps/studio/server/services/agents.py` | Current agent scanner (flat dir) |
| `apps/studio/frontend/app/agents/page.tsx` | Two-pane + versioning pattern to reuse |
| `apps/studio/frontend/lib/api.ts` | All API client types/functions |
| `marketplace/` | Plugin dirs with skills/ + agents/ |
| `.claude-plugin/marketplace.json` | Marketplace manifest |
| `lionagi/state/schema.sql` | SQLite schema (definitions table) |
| `_references/claude-code/src/utils/plugins/schemas.ts` | Plugin manifest Zod schema |
| `_references/claude-code/src/skills/loadSkillsDir.ts` | How CC loads skills from dirs |
| `_references/claude-code/src/utils/plugins/pluginDirectories.ts` | Where CC stores plugins |

### Official Plugin Docs (from code.claude.com, May 2026)

**Full component list** (a plugin can contain any/all of these):

| Component | Location | Notes |
|---|---|---|
| Skills | `skills/<name>/SKILL.md` | Directory-based, preferred over commands |
| Commands | `commands/<name>.md` | Legacy flat files, still works |
| Agents | `agents/<name>.md` | Subagent definitions with frontmatter |
| Hooks | `hooks/hooks.json` | Event handlers (PreToolUse, PostToolUse, SessionStart, Stop) |
| MCP Servers | `.mcp.json` | Auto-started when plugin is active |
| LSP Servers | `.lsp.json` | Code intelligence (go-to-def, diagnostics) |
| Output Styles | `output-styles/<name>.md` | Custom response formatting |
| Monitors | `monitors/monitors.json` | Background watchers |
| Themes | `themes/<name>.json` | Color themes (experimental) |
| Bin | `bin/` | Executables added to Bash PATH |
| Settings | `settings.json` | Default agent, status line config |

**Critical**: components go at plugin root, NOT inside `.claude-plugin/`. Only `plugin.json` goes in `.claude-plugin/`.

**SKILL.md frontmatter** (complete field reference):
```yaml
name, description, when_to_use, argument-hint, arguments,
disable-model-invocation, user-invocable, allowed-tools,
model, effort, context (fork), agent, hooks, paths, shell
```

**Plugin source types** in marketplace.json:
- Relative path: `"./plugins/my-plugin"`
- GitHub: `{"source":"github","repo":"owner/repo","ref":"main","sha":"abc"}`
- Git URL: `{"source":"url","url":"https://...git"}`
- npm: `{"source":"npm","package":"@org/plugin","version":"^2.0"}`

**Skill discovery priority** (higher wins on name conflict):
1. Enterprise (managed settings)
2. Personal (`~/.claude/skills/`)
3. Project (`.claude/skills/`)
4. Plugin (namespaced: `plugin:skill-name`)

**Plugin install scopes**:
- `user` → `~/.claude/settings.json`
- `project` → `.claude/settings.json` (committed)
- `local` → `.claude/settings.local.json` (gitignored)

**For dev testing**: `claude --plugin-dir ./my-plugin` (no marketplace needed)

### Key Decisions Needed

1. **Nav structure**: Replace Skills with Plugins? Or keep both (Plugins groups, Skills flat view)?
2. **Third-party plugin editing**: Read-only or allow local overrides?
3. **plugin.json generation**: Should Studio auto-generate `plugin.json` for marketplace plugins that don't have one yet?
4. **Model-agnostic later**: Current format is Claude Code specific. Plan for Codex/other agent CLIs by abstracting the manifest layer.
5. **Monitors + Themes + LSP**: Surface these in Studio or defer?
6. **Plugin install scope UI**: Show/manage which scope each plugin is installed at?
