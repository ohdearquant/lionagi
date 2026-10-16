# Lion Studio frontend

Vite 8 + React 19 single-page client for Lion Studio. It monitors active
orchestrations, explores run history, manages schedules and definitions, and
provides the Operator dock. The API is the FastAPI service in
`lionagi/studio/`; the browser does not persist LionAGI state itself.

## Develop locally

From the repository root, start the API in one terminal:

```bash
uv run uvicorn lionagi.studio.app:app --reload --host 127.0.0.1 --port 8765
```

Then start the client in another:

```bash
cd apps/studio/frontend
npm ci --legacy-peer-deps
npm run dev
```

Open <http://localhost:5173>. Vite proxies `/api`, `/health`, and
`/openapi.json` to `127.0.0.1:8765`. Override that target for an isolated
daemon with `STUDIO_API_URL=http://127.0.0.1:<port>`.

For the normal product launch, use `li studio`; for the full source hot-reload
experience, use `li studio --dev`.

## Main spaces

| Route        | Purpose                                                    |
| ------------ | ---------------------------------------------------------- |
| `/fleet`     | Live orchestration and agent monitor, with run detail      |
| `/attention` | Work needing operator attention                            |
| `/mission`   | Attention, active work, recent history, pulse, and spend   |
| `/library`   | Agents, playbooks, skills, plugins, teams, and MCP servers |
| `/schedules` | Calendar, schedule editing, and execution history          |
| `/designer`  | Visual orchestration designer                              |
| `/system`    | Health, maintenance, definitions, engines, and engine runs |

Legacy deep links such as `/runs/:id`, `/playbooks/:name`, `/teams`, and
`/admin/maintenance` remain routed to their current space.

## Recorded usage

Skill and plugin details show recorded invocations only. Per-skill and per-plugin
tracking coverage is unknown; zero records display Not recorded for both count
and last use, without claiming that the skill or plugin has never been used.
Counts cover all matching records. Recorded success rate divides `completed`
records by all finished records (`completed`, `completed_empty`, `failed`,
`timed_out`, `aborted`, and `cancelled`); running records are excluded, and empty
completions and cancellations are not successes. No finished records, or an older
server missing their aggregate, display an unavailable rate.

The recent list contains five records ordered by update time, with ages measured
from those updates. Failed usage requests show an unavailable state with a retry
action, without replacing failure with zero or an empty-history claim. Last
tracked use is the latest recorded start across all matching records, independent
of the recent list and later completion or cleanup. Older servers without this
aggregate show Unknown for nonempty results.

## API resolution

The runtime chooses an API base in this order:

1. `window.__STUDIO_API_BASE__` (desktop and hosted runtime injection)
2. `VITE_STUDIO_API_BASE` (build-time override)
3. the local Vite development proxy
4. same-origin API for Docker and reverse-proxy deployments

If the desktop shell injects `window.__STUDIO_AUTH_TOKEN__`, the API client
adds it as a bearer token. Do not expose an unauthenticated daemon on a
non-loopback interface. See [HOSTING.md](./HOSTING.md) for the deployment and
authentication model.

## Checks

```bash
npm run typecheck
npm run lint
npm test
npm run build
npm run e2e
```

The Playwright suite builds the SPA and launches an isolated seeded daemon; it
does not use your normal `~/.lionagi/state.db`.

For the backend, desktop shell, and product-level instructions, see the
[Studio README](../README.md) and [Studio guide](../../../docs/guides/studio.md).
