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

## Library catalogs

Each Library tab loads only its own catalog; Playbook loads both bundled templates
and custom playbooks. All loads the visible catalogs. Hidden workflow and engine
catalogs are loaded only for an explicit detail deep link on any tab except Hooks;
withheld tab names alone do not load them. A workflow detail opens the shared
workflow editor, whose engine picker needs the engine catalog, so that pane
fetches the engine catalog itself; a failure there leaves the editor usable and
reports the failure inline with its own retry. Hooks is a full-page configuration surface:
it owns its configuration requests and does not load a hidden Library detail.
Selecting or leaving a hidden detail does not reload the visible catalog.
Switching tabs loads the sources that tab has not fetched yet and renders a tab
already visited from what it fetched; a late response from the previous tab
cannot replace the active list or its errors. The create form belongs to the tab
that opened it and closes when the tab changes, and a tab change returns the
collapsed layout to its list. Create, delete, and clone completions refresh the
current tab even if navigation occurred while the change was pending, and drop
the completed kind's catalog so a tab that lists it fetches it again on its next
visit. They update selection only if the tab still matches the tab where the
action began and the selection has not moved, and do not navigate after the
Library unmounts. Visible catalog failures are named
in the master banner; hidden failures and loading appear in their detail pane.
Retrying one pane does not reload the other pane's catalogs. Failed reloads clear
previously loaded agent and engine detail data. A missing engine is reported as
not found only after a successful catalog response. The
`tab` and `sel` query parameters remain the links to a tab and selected item.

Catalog-list request counts are one for Agent, Skill, Plugin or MCP; two for Playbook;
six for All; and zero for Hooks or a withheld tab name alone. An explicit hidden detail
adds one request outside Hooks; a workflow detail adds a second for the engine catalog
its editor picks from, and retries only that catalog when it fails. Row selection adds
none; a tab already visited renders what it fetched and requests nothing again, except
a source that failed, which is attempted again on the next visit. Retry requests only
the sources that failed. A completion refreshes the current tab's sources and any
current hidden detail.
A workflow created on All loads six visible sources plus its workflow catalog, along
with the engine catalog its editor picks from.

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
