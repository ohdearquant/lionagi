# Lion Studio Frontend

Next.js 14 frontend for [Lion Studio](https://github.com/khive-ai/lionagi),
the internal observability dashboard for lionagi runs, agents, and playbooks.
Talks to the Lion Studio backend at `process.env.NEXT_PUBLIC_STUDIO_API_BASE`
(default `http://localhost:8765`).

## Routes

| Route                     | View                                                                                                           |
| ------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `/`                       | Redirects to `/playbooks`.                                                                                     |
| `/playbooks`              | Playbook table sourced from `~/.lionagi/playbooks/*.playbook.yaml`, with status and metadata.                  |
| `/playbooks/[name]`       | Playbook detail with YAML view and step DAG visualization.                                                     |
| `/playbooks/[name]/edit`  | Edit an existing playbook YAML.                                                                                |
| `/playbooks/new`          | Author a new playbook YAML.                                                                                    |
| `/agents`                 | Agent profile table sourced from `~/.lionagi/agents/*.md`.                                                     |
| `/agents/[name]`          | Agent profile detail with frontmatter, body, and edit link.                                                    |
| `/agents/[name]/edit`     | Edit an existing agent profile.                                                                                |
| `/agents/new`             | Author a new agent profile.                                                                                    |
| `/runs`                   | Runs table from `~/.lionagi/runs/{id}/run.json`, with cost/duration/status filters.                            |
| `/runs/[id]`              | Run detail with branch timelines, messages, API calls, DAG visualization, and live SSE stream for active runs. |

## Foundation

- `lib/types.ts` — data shapes returned by the Lion Studio backend (FastAPI on
  port 8765 by default). Run state types match `~/.lionagi/runs/` manifest
  fields; agent profile types match the YAML frontmatter schema.
- `lib/api.ts` — typed fetch wrappers per backend endpoint using
  `API_BASE = process.env.NEXT_PUBLIC_STUDIO_API_BASE || 'http://localhost:8765'`.
- `lib/ws.ts` (if present) — reconnecting SSE/WebSocket hook for live run streams.
- `components/` — shared UI on the dark neutral theme: `neutral-950` backgrounds,
  `neutral-800` borders, `neutral-500` muted text, `neutral-200` primary text.
- `components/canvas/` — ReactFlow + dagre DAG visualization reused for playbook
  step DAGs and run branch DAGs.

## Local development

```bash
# From repo root, install backend + studio deps
uv pip install -e '.[studio]'

# Start backend
li studio start --frontend-mode none --port 8765

# In another shell: start frontend (dev mode)
cd apps/studio/frontend
npm install --legacy-peer-deps   # pre-existing ESLint peer conflict, will resolve in a future cleanup
npm run dev
```

Visit http://localhost:3000.

## See also

- [ADR-0001](../../../docs/adrs/ADR-0001-lion-studio-internal-app.md) — Lion Studio is an internal monorepo app
- [ADR-0002](../../../docs/adrs/ADR-0002-studio-tech-stack.md) — Lion Studio tech stack selection
- [ADR-0003](../../../docs/adrs/ADR-0003-claude-code-marketplace.md) — Claude Code marketplace pattern
