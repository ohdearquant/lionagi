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

## Canvas layout

Small acyclic execution graphs use Dagre with exact ASAP ranks: a node's column
is its longest dependency path from a source. Before layout, the client counts
real nodes plus twice the sum of pinned rank spans over **distinct directed
endpoint pairs**, matching graphlib's non-multigraph edges. Unknown endpoints
do not contribute. Self-loops are charged conservatively. Graphs above 192 work
units or 128 nodes use placement within the same ranks, with siblings ordered
by node ID, instead of Dagre's crossing optimization. All original node and edge
records remain available, including parallel edges.

A known non-self edge that does not advance its computed rank also bypasses
Dagre: its cycle-breaking pass can reverse edges and invalidate the original
span estimate. Depth traversal uses sorted node and predecessor IDs so fallback
positions are stable by ID under input permutations. Cyclic ranks are a
defensive placement convention; they cannot describe a valid dependency order.

The limits are an empirical reference-host choice, not a wall-clock deadline.
The committed benchmark runs the full layout pipeline with one warm-up and
seven uninstrumented timing samples per fixed shape. A separate untimed pass
counts Dagre calls and normalization dummies by wrapping both in process; no
dependency or project file is written. Run it from this directory with Node 24
or newer:

```sh
node benchmarks/layout-budget.mjs
```

`--fixtures` times the committed module. `--sweep` runs all candidate boundary
families against an in-memory copy of it with only the two limits substituted;
`--chord-sweep` runs only the long-chord family that way.

The measured sweep paired work/node caps at 96/64, 144/96, 192/128 and 240/160.
The declared selection criterion was the largest pair whose sampled boundary
families all had a median at or below 20 ms on the reference host. Families
include chains, a long chord, isolated-node caps, node caps with chain edges,
fan-out, fan-in with many dummies, and complete two- and three-layer components.
Padding isolated nodes makes the work-bound fixtures reach each exact limit.

Node 26.7.0, Dagre 0.8.5, Darwin arm64, Apple M2 Max; memory pressure was 2
before each phase. Other machine load was not controlled. These are sampled
observations, not a universal worst-case proof or a browser-frame measurement:

Each row is one pass except the last, which is the worst observation across the
two passes that measured it: the sweep alone gave it a largest median of
18.811 ms, repeating the same families in the fixture pass gave the 21.888 ms
three-layer median and 28.375 ms fan-out maximum.

| Work / node caps       | Largest family median | Largest sample | Decision        |
| ---------------------- | --------------------: | -------------: | --------------- |
| 96 / 64                |              9.168 ms |      14.074 ms | Qualified       |
| 144 / 96               |             14.550 ms |      19.487 ms | Qualified       |
| 192 / 128              |             16.158 ms |      17.156 ms | Selected        |
| 240 / 160 (two passes) |             21.888 ms |      28.375 ms | Did not qualify |

The spread between those two passes is 3.1 ms, and the margin from the selected
16.158 ms median to the 20 ms criterion is 3.8 ms, so the selected pair is not
separated from the rejected one by a wide gap. The 192/128 pair includes a
measured 128-isolated-node cap and exact 192-work cases. This does not imply
that node 129 is inherently slow, or independently optimize the two caps. An
additional 39-node long-chord control at work 191 had an 18.671 ms maximum,
further illustrating that the caps do not promise a 16 ms frame. The raw
per-sample timings are not committed; each row above is the summary the
benchmark prints.

Wrapping and folding were re-checked alongside admission and remain downstream
geometry choices. Seven fan-out siblings occupy one column (566 × 747 including
margins); eight wrap into three columns (1042 × 327). Their work is only 22 and
25 respectively, so both still use Dagre. Lowering the wrap threshold would
not avoid normalization work already paid before wrapping. The 111-node fan-in
instead bypasses Dagre before wrapping its wide rank.

The 1500 px fold row admits six 210 px cards at the deep-rank minimum 48 px gap:
`6 × 210 + 5 × 48 = 1500`. With 56 px horizontal margins its width is 1556 px;
on a 1280 px panel the existing 15% fit padding gives zoom 0.715, above the
0.65 readability floor. Seven such cards need 1758 px before margins and would
fall below that floor. With the shallow 90 px gap, five cards occupy 1410 px;
the sixth crosses 1500 and starts another safe chain row. Final correctness
tests retain the seven/eight sibling and five/six chain boundaries, along with
folding on the fallback path. Branch/join boundaries and tall graphs still
prevent folding. The two visual thresholds do not decide Dagre admission, and
this work leaves them as they were: they were re-checked for geometry, not
re-derived from measured layout time.

There is no hysteresis or position cache across graph changes. Crossing an
admission boundary can move many existing nodes, and Dagre can also reposition
nodes when a graph grows within the admitted range. Resize reuses stored bounds;
expanding a graph applies the same layout policy. Browser painting, input
latency, panning, and viewport culling are outside these measurements.

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
