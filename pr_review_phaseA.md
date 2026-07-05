# Review: PR #1718 ADR-0093 Phase A

Verdict: REQUEST_CHANGES
Findings: 4 Blocker, 0 High, 0 Medium, 0 Low

## Blockers

### 1. UI-visible "invocation" copy still exists on reachable surfaces

Evidence: `apps/studio/frontend/src/routes/invocations/$id.tsx:12` keeps `/invocations/$id`
reachable, while `apps/studio/frontend/src/routes/invocations/$id.tsx:53` renders
`PageHeader title="Invocation"`, `apps/studio/frontend/src/routes/invocations/$id.tsx:103`
renders "Sessions in this invocation", and
`apps/studio/frontend/src/routes/invocations/$id.tsx:121` renders "No sessions spawned under this
invocation yet." `apps/studio/frontend/src/routes/engines/index.tsx:354` also renders
"Launched - invocation" and `apps/studio/frontend/src/routes/engines/index.tsx:356` says
"Track progress in Invocations."

Why this matters: The phase-A gate says the invocation noun must not surface in any UI-visible
string/label/filter/column, with `refs.invocation_id` allowed only as internal join plumbing.
Keeping the detail route working is compatible with preserving the URL and data behavior, but not
with continuing to render the retired product noun.

Suggested fix: Rename visible invocation copy to Run/schedule-run language on the still-reachable
detail and engine launch surfaces while preserving the underlying `/invocations/$id` route and API
join plumbing.

### 2. Project lens does not fully scope Operations

Evidence: `apps/studio/frontend/src/routes/index.tsx:122` passes the selected project into
`aggregateRuns`, but `apps/studio/frontend/src/lib/run-model.ts:233-239` only applies that project
to `listRuns` and `listSchedules`; it still calls `listShows()` and `listEngineRuns({ limit })`
globally. The resulting script and flow runs are forced to `project: null` at
`apps/studio/frontend/src/lib/run-model.ts:176-179` and
`apps/studio/frontend/src/lib/run-model.ts:208-209`, and the canvas filter at
`apps/studio/frontend/src/routes/index.tsx:169-177` has no project check that removes them.

Why this matters: ADR-0093 says the project lens scopes Operations fully. With a project selected,
the canvas can still show global script and flow runs from other contexts, while Library is the
surface where global items are supposed to remain visible and labeled.

Suggested fix: For phase A, either exclude projectless/global sources whenever `opts.project` is
set, or add/project through backend filters or session joins for every source before including it
in the Operations query.

### 3. Aggregation fan-out is unbounded by the advertised limit

Evidence: `apps/studio/frontend/src/lib/run-model.ts:231` defaults `limit` to 300, but
`apps/studio/frontend/src/lib/run-model.ts:236-238` calls `listSchedules()` and `listShows()`
without a limit. It then calls `listScheduleRuns` once per returned schedule at
`apps/studio/frontend/src/lib/run-model.ts:250-255` and `getShow` once per returned show at
`apps/studio/frontend/src/lib/run-model.ts:258-260`. The schedules API itself has no list limit
at `lionagi/studio/services/schedules.py:600-607`.

Why this matters: The Operations canvas is supposed to retire the "render/fetch all history" defect
class. This implementation virtualizes rendering, but the data loader can still issue O(all
schedules + all shows) requests and collect up to 100 runs per schedule plus every play from every
show before the UI applies its 24h window.

Suggested fix: Bound the source fan-out structurally: add a capped unified endpoint, add server-side
window/source limits, or cap/concurrency-limit the client fan-out with an explicit "load more"
path. The default canvas query should not require one request per schedule/show.

### 4. Partial source failures are silently rendered as empty or incomplete data

Evidence: `apps/studio/frontend/src/lib/run-model.ts:235-238` converts admin health, schedules,
shows, and engine-run failures into `null` or empty arrays, `apps/studio/frontend/src/lib/run-model.ts:253-254`
drops failed schedule-run fetches, and `apps/studio/frontend/src/lib/run-model.ts:258-260` drops
failed show details. The Operations page only shows an error when `aggregateRuns` rejects at
`apps/studio/frontend/src/routes/index.tsx:305-309`; otherwise an incomplete result can fall
through to the empty state at `apps/studio/frontend/src/routes/index.tsx:314-317`.

Why this matters: Per-source degradation is acceptable, but the canvas must not imply "No runs
match" when a source failed. Operators need to know whether the view is complete, especially when
schedule/script/flow sources disappear independently.

Suggested fix: Return source-level warnings/errors from `aggregateRuns` and render a visible
partial-data banner. Keep stale prior data during live refresh failures or mark it as stale instead
of swallowing polling errors silently.

## Looks Right

- `/runs`, `/invocations`, `/kanban`, `/playfield`, and `/shows` list routes have `beforeLoad`
  redirects, while `/runs/$id`, `/invocations/$id`, and `/shows/$topic` still have detail routes.
- The new Operations chips, stream, board, table, and slide-over all consume `Run.status` from the
  unified run model; the only new `deriveRunStatus` calls are inside `run-model.ts`.
- Stream and board projections use `@tanstack/react-virtual`; table and stream render through the
  bounded `visible` slice with load-more.
- Light and dark token changes define matching status-neutral/data/canvas variables; I did not see
  an obvious contrast regression in the touched tokens. The only `legacy` hits are dependency/tooling
  docs or package names, not Studio UI copy.

## Commands Run

- `git fetch origin main`: updated `origin/main` from `11f7a6275` to `94f99998a`.
- `npm run typecheck && npm run lint && npm run test && npm run build`: typecheck passed; lint
  exited 0 with 6 warnings; Vitest passed 86/86; production build passed with existing bundle-size
  and dynamic-import warnings.
- Targeted sweeps: `rg -n -i "invocation|legacy" apps/studio/frontend`, `rg -n "deriveRunStatus|status"`
  across the Operations modules, route redirect grep, and token/theme diff review.

## What I Did Not Check

- I did not run dev servers, e2e loops, or screenshot/visual theme checks, per instruction.
- I did not post this review to GitHub.

Domain utility: LOW - shared recall found one prior status-projection note, but knowledge.suggest
returned no domains, so the ADR and local code drove the review.

VERDICT: REQUEST_CHANGES - blockers 1, 2, 3, 4
