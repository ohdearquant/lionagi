# Lion Studio follow-up issue drafts — verification copy

Date: 2026-08-11

These are candidate issue bodies for review after the 50-fix Studio pass in
draft PRs #3036–#3039. They are not proof that an issue should be opened. The
reviewer must follow
[`claude-studio-followup-handoff-2026-08-11.md`](./claude-studio-followup-handoff-2026-08-11.md),
reproduce against the four-PR integration, repeat duplicate search, and return
a verdict before any GitHub write.

Line anchors below refer to disposable integration commit
`9b0cc4b2afece744c94fc0886e2aaaf76f0e172c`. Every draft also names stable
symbols because lines will move. Local run ids under the MCP cluster are
private verification evidence and must be replaced by a minimal, scrubbed
reproduction in a public issue.

## Verdict summary

| ID | Candidate | Initial disposition | Priority | Architecture relation |
|---|---|---|---|---|
| F1 | Operator tail-first history | `OPEN_NEW` after measurement | P1 | ADR-0079 delta 5; ADR-0081 D7 gap |
| F2 | Bounded incremental RunDetail signals | `OPEN_NEW` after measurement | P1 | ADR-0079 delta 5; ADR-0081 D7 gap |
| F3 | Suppress resume polling while SSE is healthy | `OPEN_NEW` | P1 | ADR-0079 delta 6 |
| F4 | Stable bounded message-SSE cursor | `OPEN_NEW` | P1 correctness | ADR-0076 delta 2 |
| F5 | Bound per-viewer SSE database churn | `OPEN_NEW` after benchmark | P1 scale | ADR-0076 delta 5 |
| F6 | Complete server-derived live overview | `COMMENT_EXISTING` or `SPLIT` from #2979 | P1 | ADR-0079 delta 6; ADR-0080 delta 5 |
| F7 | Lazy, paged, virtualized Library | `OPEN_NEW` after measurement | P1 | ADR-0079 delta 5; ADR-0081 D7 gap |
| F8 | Batch schedule run summaries | `OPEN_NEW` | P1 | ADR-0079 delta 5; ADR-0078 D4 gap |
| F9 | Mobile schedule controls | `OPEN_NEW` after browser repro | P1 interaction | ADR-0080 D3 implementation defect |
| F10 | Expanded graph focus boundary | `OPEN_NEW` after browser repro | P1 a11y | ADR-0079 delta 1 |
| F11 | Only one run graph canvas while expanded | `OPEN_NEW` after profiler repro | P2 performance | ADR-0081 D7 gap |
| F12 | Team inbox tail pagination | `HOLD_FOR_REPRO` | P2 | ADR-0079 delta 5; ADR-0081 D7 gap |
| B1 | Ad-hoc worker uses configured concurrency | `OPEN_NEW` | P1 | ADR-0071 delta 7 |
| B2 | Scheduler action deadline and cleanup | `OPEN_NEW` after residual repro | P1 reliability | ADR-0070 delta 6 |
| B3 | Bound session-detail full-history aggregates | `HOLD_FOR_REPRO` | P1 candidate | ADR-0078 D3; ADR-0081 D7 |
| B4 | Measured startup reconciliation budget | `HOLD_FOR_REPRO` | P1 candidate | ADR-0076 delta 6 |
| S1 | Desktop WebView trust boundary | `ADR_FIRST` | P1 safety | ADR-0079 delta 7 |
| M1 | Codex effort override precedence | `OPEN_NEW` | P0 | ADR-0043 D2/D6 implementation gap |
| M2 | Fanout failure publishes authoritative terminal fact | `HOLD_FOR_REPRO` | P0 candidate | ADR-0106 D6; ADR-0107 |
| M3 | Fanout planner receives valid Mode vocabulary | `OPEN_NEW` | P1 | ADR-0043 D5 implementation gap |
| M4 | Remove inert resume-on-timeout from flow/fanout | `OPEN_NEW` | P1 contract | ADR-0062 delta 4; ADR-0095 D5 |
| M5 | Orchestration persistence under SQLite contention | `COMMENT_EXISTING` on #2275 | P0 regression | ADR-0064 delta 5; ADR-0056 D3 |
| M6 | Implement deterministic manifest fanout | `ADR_FIRST` | P1 roadmap | Proposed ADR-0110 |

## Cluster A — Studio live data and large histories

### F1 — Load Operator history newest-first with an explicit older cursor

**Proposed title:** `Operator hydrates the complete conversation before rendering a bounded tail`

**Impact.** Opening an old Operator conversation performs network transfer,
validation, allocation, and merge work proportional to its complete frame
history before showing the recent conversation. A long-lived daemon therefore
gets slower exactly when history becomes valuable.

**Evidence.** In `getOperatorConversation()`,
`apps/studio/frontend/src/lib/api.ts:450-488` requests 1,000 frames at a time in
a loop until `hasMore` is false and accumulates every page. The UI then derives
all display items and shows only `items.slice(-visibleCount)` in
`OperatorPanel.tsx:904-957`. `mergeOperatorFrames()` rebuilds a `Map`, sorts,
and only then evicts to the retained window in `operatorReducer.ts:55-80`.

**Reproduction to retain.** Seed one conversation with 100,000 protocol frames,
close/reopen Operator, and capture request count, transferred bytes,
time-to-first-conversation, and peak browser heap. Confirm that unresolved old
proposals remain discoverable before changing the API.

**Expected.** Initial selection fetches a bounded newest page. Older history is
loaded explicitly through an opaque backward cursor; state and DOM remain
bounded; unresolved proposals remain reachable.

**Actual.** Initial selection walks the forward cursor to EOF and temporarily
holds all frames even though the reducer and view retain a tail.

**Smallest coherent scope.** Add a daemon tail/backward-page contract, expose
`has_older` and an opaque cursor, initialize the reducer from one page, and add
Load older or upward pagination. Keep the current protocol frame validation.

**Non-goals.** Do not introduce a global frontend cache or redesign Operator's
proposal protocol.

**Acceptance.** A 100,000-frame fixture reaches first render with one bounded
history response; retained raw frames and rendered rows remain under a fixed
tested cap; loading older preserves stable order and deduplication; an
unresolved proposal outside the newest page is still reachable.

**Duplicate/ADR note.** No matching issue was found by the initial Operator
history/pagination searches. This is an implementation slice of ADR-0079 delta
5 and Proposed ADR-0081 D7.

### F2 — Project RunDetail signals incrementally and retain a bounded raw window

**Proposed title:** `RunDetail retains and rescans every signal for the lifetime of a run`

**Impact.** Long executions consume increasing heap and do repeated whole-array
work on every new signal. Graph and activity views can become progressively
slower even though only a bounded recent activity window is useful to the UI.

**Evidence.** `RunDetail.tsx:1471-1475` deliberately replays all persisted
signals for terminal sessions. The stream append at `:1611-1619` performs
linear `.some()` deduplication and copies the whole array. Gate, operation
graph, node status, and node activity projections rescan `signalEvents` at
`:1877-1948`.

**Reproduction to retain.** Replay 100,000 realistic signals with repeated
node updates. Capture append duration by percentile, committed React render
time, retained heap, and final graph/status/activity projections.

**Expected.** A reducer/index updates durable projections incrementally. Raw
events are retained in a fixed recent ring and older evidence is paged on
demand.

**Actual.** Raw event state is unbounded and append/derivation work grows with
complete history.

**Smallest coherent scope.** Introduce id/sequence indexing, incremental gate
and per-node projections, a fixed raw-event ring, and a paged evidence view.
Persisted terminal replay may still rebuild projections, but must do so in
bounded batches without placing all rows in React state.

**Acceptance.** After 100,000 signals, retained raw events stay below a fixed
tested cap (recommended initial cap: 2,000); duplicate ids do not change state;
append latency does not trend upward with total historical count; graph, gate,
node status, and node activity equal the reference full-history projection;
older events remain pageable.

**Duplicate/ADR note.** This is not #3013: #3037 wires node activity into both
canvases. It implements ADR-0079 delta 5 and Proposed ADR-0081 D7.

### F3 — Stop the 750 ms detail poll while resumed-run SSE is healthy

**Proposed title:** `Resumed RunDetail polls full invocation and session detail every 750 ms beside SSE`

**Impact.** Every open resumed run continuously executes two comparatively
heavy detail reads even while message and signal streams already carry live
state. Multiple viewers multiply database work.

**Evidence.** `RunDetail.tsx:1531-1570` calls `getInvocation()` and
`getSession()` in a 750 ms loop whenever `resumeWatch` is set. Message and
signal streams are active independently at `:1572-1624`.

**Reproduction.** Resume a running session, leave its detail open for 30
seconds, and count detail GETs while both streams remain connected.

**Expected.** Healthy streams drive live state. A lifecycle/disconnect fallback
polls a lightweight status with bounded backoff, and terminal convergence
performs one final detail refresh.

**Actual.** The client performs about 40 invocation reads and 40 session-detail
reads per viewer per 30 seconds until the resumed invocation ends.

**Scope.** Track stream health and terminal frames; suppress detail polling
while healthy; use a lightweight lifecycle query after transport loss; retain
one guarded terminal refresh.

**Acceptance.** Fake timers show zero repeated detail GETs during 30 seconds of
healthy SSE, exactly one terminal detail refresh, and bounded exponential
fallback after a forced disconnect. Reconnect and terminal races must remain
last-selection-wins.

**Duplicate/ADR note.** No initial duplicate was found. ADR-0079 delta 6 owns
the freshness contract.

### F4 — Give message SSE a collision-safe cursor and bounded batches

**Proposed title:** `Session message SSE is unbounded and can skip equal-timestamp inserts`

**Impact.** A large replay can become one unbounded query/response burst, and a
late message sharing the maximum delivered timestamp is skipped forever.

**Evidence.** `get_session_messages_after()` in
`lionagi/studio/services/sessions.py:886-915` filters only
`m.created_at > after_ts`, orders only by timestamp, and has no `LIMIT`.
`stream_session_route()` starts `after_ts` at zero and advances only the
timestamp at `:1030-1053`; the client stream URL carries no resume cursor in
`apps/studio/frontend/src/lib/api.ts:1320-1337`. Thus an active RunDetail that
already loaded a 200-message tail can replay the complete message history again
when SSE connects.

**Reproduction.** Seed more than one batch of messages, including two with the
same timestamp; insert the second equal-timestamp row after the first is
delivered. Record delivered ids and maximum rows returned by one query.

**Expected.** A stable opaque `(created_at,id)` cursor drains capped pages and
resumes after the last delivered row without duplication or loss.

**Actual.** Every connection starts at zero; the query is timestamp-only and
unbounded, so it defeats the bounded initial detail window as well as exposing
the timestamp collision.

**Scope.** Add a server cursor and fixed batch cap, immediately drain nonempty
pages, teach the fetch-SSE client to resume, and preserve explicit done and
heartbeat frames.

**Acceptance.** A 100,000-message replay is read in batches of at most 500;
both equal-timestamp messages are delivered once; reconnect at row N resumes at
N+1; terminal sessions send all remaining pages before `done`.

**Duplicate/ADR note.** No initial SSE cursor issue was found. This is the
correctness half of ADR-0076 delta 2; do not combine it with connection pooling
in F5.

### F5 — Separate SSE subscription lifetime from per-poll database setup

**Proposed title:** `Each idle Studio SSE viewer repeatedly opens SQLite connections and probes schema`

**Impact.** Every session viewer polls at 500 ms and multiplies connection,
PRAGMA, schema-probe, and terminal-state query work even when no event exists.

**Evidence.** Message and signal generators poll and separately read terminal
state every 500 ms in `sessions.py:1030-1053` and `:1079-1112`.
`get_signals_after()` opens a database and probes `sqlite_master` on every call
in `lionagi/studio/services/signals.py:14-39`.

**Measurement gate.** Instrument `_open_db`, SQL execution, and disconnect
cleanup with 1, 10, and 50 idle viewers for 30 seconds. This issue should open
only if the integrated measurement confirms the predicted growth.

**Expected.** A stream reuses a read resource or subscribes to a shared
broadcaster; schema capability is cached; disconnect closes promptly; idle
cost has a documented bound.

**Actual.** Connection and setup work repeats per viewer per tick.

**Scope.** Cache immutable schema discovery, combine or reuse stream reads, and
choose either a long-lived read connection or a bounded broadcaster. Do not
change payload semantics or switch to native `EventSource`, which cannot attach
the bearer token.

**Acceptance.** With 50 idle streams, connection creation no longer grows at
roughly four opens per viewer per second; query/connection counts and CPU are
captured in a repeatable benchmark; cancelling all clients releases all stream
resources; one slow client cannot stall another.

**Duplicate/ADR note.** Separate from F4. ADR-0076 delta 5 and ADR-0077's typed
repository delta cover the target.

### F6 — Serve a complete, coherent live overview instead of capped browser joins

**Proposed title:** `Fleet and Mission silently omit active work beyond fixed first-page caps`

**Initial verdict:** `COMMENT_EXISTING` on #2979 if a server-derived attention
and live-work read model fits that issue; otherwise split a broader read-model
tracker and cross-link #2979.

**Impact.** Active work beyond the first 200 runs/200 invocations in Fleet or
200 runs/100 invocations in Mission can disappear. Each browser independently
reconciles multiple endpoints every three seconds.

**Evidence.** `useFleet()` calls `listInvocations({limit: 200})` and
`listRuns({per_page: 200})` at `useFleet.ts:65-97`. `useLiveBoard()` joins five
endpoints with 200/100 caps at `useLiveBoard.ts:68-108` and dispatches a
whole-board clock tick every second at `:62-66`.

**Reproduction.** Seed 250 active runs and 250 active invocations with parents
distributed across page boundaries. Compare database truth, Fleet, Mission,
and attention counts. Capture request count and payload over one minute.

**Expected.** A server-owned active snapshot/delta contract returns coherent
counts and explicit pagination/truncation; every active item is reachable.

**Actual.** Clients join fixed first pages and do not expose the missing count
as truncation.

**Scope.** Define the server read model first, then migrate Fleet, Mission, and
attention. Keep History pagination separate.

**Acceptance.** With 250 active entities, every entity is reachable or the UI
shows an exact truncation/remaining count; one snapshot version supplies
coherent counts and attention reasons; the browser no longer performs five
endpoint reconciliation on each poll; hidden tabs back off.

**Duplicate/ADR note.** #2979 already owns server-derived attention. ADR-0079
delta 6 and ADR-0080 delta 5 define the larger consistency target.

### F7 — Load, search, and render Library by active tab

**Proposed title:** `Library eagerly fetches every catalog and renders every matching row`

**Impact.** Library startup, search, and DOM cost grow with all installed
resources, including kinds the UI intentionally hides.

**Evidence.** `useLibraryData()` fetches eight catalogs together in
`apps/studio/frontend/src/routes/library.tsx:84-128`. Workflow and engine are
hidden as unfinished at `:51-55` and `:359-374` but are still fetched. Search
filters the aggregate array client-side at `:371-388`, and `filtered.map()`
mounts every result at `:573-637`.

**Measurement gate.** Seed 10,000 items per kind. Record initial requests,
transferred rows, DOM node count, input latency, and deep-link selection time.

**Expected.** Initial load requests only the selected tab; server-side cursor
search and a windowed/virtual list bound payload and DOM; deep-linked selection
resolves deterministically.

**Actual.** All catalogs gate readiness and every match mounts.

**Scope.** Add per-kind paged search, lazy tab loading, and list windowing.
Preserve partial-error presentation and URL `tab`/`sel` behavior.

**Acceptance.** A 10,000-item fixture initially requests only one catalog,
keeps rendered result rows under a fixed cap, searches without downloading the
complete catalog, and resolves a deep-linked selection not present in the
first page.

**Duplicate/ADR note.** #2732 is skill-stat accuracy, not Library paging. This
is ADR-0079 delta 5 and Proposed ADR-0081 D7.

### F8 — Batch schedule definitions with their recent-run summaries

**Proposed title:** `Schedules polling still performs one recent-runs request per schedule`

**Impact.** The frontend fix in #3037 caps concurrency but request and database
work remain linear in schedule count every 30 seconds.

**Evidence.** `useSchedulesData()` lists all schedules and then calls
`listScheduleRuns(schedule.id, {limit: 25})` for each row at
`components/schedules/data.ts:77-79` and `:109-147`. The six-worker pool bounds
pressure, not total work.

**Reproduction.** Seed 1,000 schedules with recent runs. Capture browser
request count, daemon SQL count, total payload, and refresh latency.

**Expected.** One paged or batched application query returns schedule summaries
with the required recent-run slice or latest-run aggregate.

**Actual.** One refresh performs 1 + N HTTP calls.

**Scope.** Add a batched summary/query contract and migrate the hook. Keep
full per-schedule run history as an explicit detail query.

**Acceptance.** Rendering 1,000 schedules uses O(1) or a fixed number of paged
requests rather than 1,001; recent-25 semantics are explicitly defined;
partial failures remain attributable; overlapping refresh coalescing from
draft PR #3037 remains green.

**Duplicate/ADR note.** No initial duplicate was found. This implements
ADR-0079 delta 5 and the query boundary in Proposed ADR-0078 D4.

## Cluster B — Responsive and accessible Studio surfaces

### F9 — Keep schedule actions and policies reachable on mobile

**Proposed title:** `Schedule detail hides Run now, Delete, and policy controls below md width`

**Impact.** At a 390 px viewport, operators cannot trigger or delete a schedule
or edit missed-fire and overlap policies.

**Evidence.** The only action/policy rail is `hidden ... md:flex` in
`ScheduleDetailModal.tsx:771-822`. Run now, Delete, missed-fire policy, and
overlap policy occur inside that hidden rail.

**Required browser reproduction.** Open a schedule at 390 px and attempt all
four operations using touch and keyboard. Retain a screenshot of the full
modal; source inspection alone is insufficient.

**Expected.** Every schedule capability available on desktop is reachable on a
narrow viewport with clear destructive confirmation.

**Actual.** The complete rail is removed from layout below `md`.

**Scope.** Add a mobile action/policy section, drawer, or stacked layout using
the same controls and dirty-state owner. Do not duplicate form state.

**Acceptance.** At 390 px, all four controls are visible/reachable, focus order
is logical, touch targets meet the component-system minimum, dirty close guards
still work, and desktop layout remains unchanged.

**Duplicate/ADR note.** This is an implementation defect against ADR-0080's
Schedules-space responsibility, not a new architecture decision.

### F10 — Make expanded run graph a complete modal focus boundary

**Proposed title:** `Expanded run graph declares a dialog but does not manage or contain focus`

**Impact.** Keyboard and assistive-technology users can remain behind or tab
out of the expanded graph, and closing it does not reliably restore the
launcher.

**Evidence.** `RunDetail.tsx:2159-2205` renders a custom `role="dialog"` with a
close button but no initial-focus, focus-trap, backdrop, inert-background,
scroll-lock, or restoration logic. A window-level Escape handler exists at
`:1690-1697`; it closes the overlay without restoring focus. The overlay does
not use the now-hardened shared Modal primitive.

**Required browser reproduction.** Expand a graph, record `document.activeElement`,
Tab/Shift+Tab through the boundary, press Escape, close explicitly, and inspect
the accessibility tree.

**Expected.** Expanded graph has an accessible name, focus enters and stays
inside, Escape/backdrop policy is explicit, background is inert, and focus
returns to Expand.

**Actual.** Only ARIA role/modal/name are present.

**Scope.** Reuse or extend a shared dialog shell while preserving the graph
toolbar and canvas keyboard interactions.

**Acceptance.** Automated focus-loop/restoration tests and a real keyboard
walkthrough pass in both directions; axe reports no dialog-name/focus-boundary
violation; closing does not reset graph selection.

**Duplicate/ADR note.** ADR-0079 delta 1 owns shared interactive primitives.
Keep this separate from F11 unless one implementation genuinely removes the
second canvas and fixes the modal boundary together.

### F11 — Mount one heavy run graph canvas across inline and expanded modes

**Proposed title:** `Expanding RunDetail mounts a second React Flow canvas instead of moving one`

**Impact.** Large graph layout, subscriptions, DOM, and rendering work are
duplicated while the expanded overlay is open.

**Evidence.** The inline `WorkerCanvas` remains mounted at
`RunDetail.tsx:2140-2157`; `graphExpanded` mounts a second `WorkerCanvas` at
`:2159-2205`.

**Measurement gate.** Expand a 500-node graph and record React commits, layout
calls, React Flow roots, DOM nodes, heap, and input latency before and after
expansion.

**Expected.** Exactly one canvas instance owns layout and live graph state; the
surface changes container/portal presentation without duplicating work.

**Actual.** Both canvases coexist.

**Scope.** Portal or conditionally move the single canvas while retaining
dimensions, selection, status/activity projection, and fit behavior.

**Acceptance.** Exactly one React Flow root and one `WorkerCanvas` are mounted
in both modes; expanding a 500-node fixture does not run a second layout;
selection and viewport survive expand/collapse.

**Duplicate/ADR note.** Proposed ADR-0081 D7 requires incremental large graph
rendering. This is distinct from F10's focus contract.

### F12 — Page the Team inbox from the newest messages

**Proposed title:** `Team detail materializes and renders the complete inbox`

**Initial verdict:** `HOLD_FOR_REPRO` until a realistic large team inbox is
measured.

**Evidence.** `TeamDetailModal` fetches one full `TeamDetail` at
`routes/teams/index.tsx:186-211`, reverses a copy of `team.messages`, and mounts
every message plus a `Timestamp` at `:255-277`.

**Missing experiment.** Seed 100,000 inbox messages and capture endpoint
payload, modal time-to-content, DOM count, timer/render work, and heap.

**Target if confirmed.** Newest-page response with an opaque older cursor,
explicit total/window metadata, and virtualized rendering.

**Acceptance if opened.** First content arrives from one bounded page; DOM
remains bounded; Load older preserves chronological order and scroll anchor;
exact total remains visible.

**Duplicate/ADR note.** ADR-0079 delta 5 and Proposed ADR-0081 D7 already define
the architectural target. Do not open from static inspection alone.

## Cluster C — Scheduler, backend, and desktop reliability

### B1 — Execute independent ad-hoc claims up to the configured capacity

**Proposed title:** `MAX_ADHOC_CONCURRENT is configured above one but the worker executes rows serially`

**Impact.** One slow ad-hoc task holds the only active worker pass; configured
capacity four does not increase throughput for independent work.

**Evidence.** `MAX_ADHOC_CONCURRENT` defaults to four in
`lionagi/studio/config.py:287`. `worker.claim_and_execute()` loops candidates
and awaits `_execute_claimed()` before the next row at
`scheduler/worker.py:426-485`. `SchedulerEngine._maybe_start_worker_pass()`
allows only one pass and explicitly notes it does not increase row-claiming
throughput at `scheduler/engine.py:1283-1297`; `:1305-1336` reserves and releases
one configured slot around each serial execution.

**Reproduction.** Queue two eligible rows with different concurrency keys and
an executor that waits on an event. At cap two, assert both enter execution
before either event is released.

**Expected.** Independent rows overlap up to the configured ad-hoc cap; equal
non-null concurrency keys remain serialized.

**Actual.** The second row is not considered until the first child exits.

**Scope.** Atomically claim a bounded batch, execute tracked tasks under the
existing slot pool, retain per-key exclusion and lease-guarded terminal writes,
and cleanly join/cancel tasks on shutdown.

**Acceptance.** Event-based tests prove two independent tasks overlap at cap
two, active count never exceeds the cap, equal keys never overlap, and
exception/cancellation/lease loss releases capacity without allowing stale
terminal writes.

**Duplicate/ADR note.** Closed #2750 moved the worker pass off the scheduler
tick; closed #2751 introduced the independent slot pool. Neither made rows
inside one pass concurrent. ADR-0071 delta 7 records the remaining target.

### B2 — Enforce a positive deadline around scheduler subprocess actions

**Proposed title:** `Hung scheduler actions have no outer deadline or timed-out cleanup contract`

**Impact.** A child that never exits can retain a scheduled global slot
indefinitely. On the ad-hoc lane, the serial worker means one hung child blocks
all later claims despite the nominal cap.

**Evidence.** `spawn_and_wait()` accepts no deadline at
`lionagi/studio/scheduler/subprocess.py:577-648`; it drains and waits until exit.
Cancellation cleans the process group at `:649-659`, but no timeout initiates
that path. The worker-pass comment calls this out at
`scheduler/engine.py:1283-1293`.

**Residual reproduction gate.** After confirming the #2750 fix, run one
never-exiting ad-hoc child and enough never-exiting scheduled children to fill
their respective lanes. Show that capacity does not recover without external
cancellation.

**Expected.** Every launched action has a positive execution deadline; expiry
terminates the whole owned process group, releases capacity, and writes one
guarded `timed_out` outcome.

**Actual.** The shared launcher has no outer deadline.

**Scope.** Publish deadline semantics in the action contract, enforce them in
the shared launcher, use TERM then KILL with a bounded grace, and guard terminal
writes against cancellation/normal-exit races.

**Acceptance.** A fake child that never exits is terminated at the deadline;
no process-group descendant survives the cleanup budget; the slot is released;
exactly one terminal `timed_out` transition wins against concurrent cancel and
normal exit.

**Duplicate/ADR note.** Cross-link open #2535 (completion evidence) and #2755
(confirmed-dispatch orphan). Do not solve either inside this deadline issue.
Closed #2750 covered tick latency, not the residual capacity leak. ADR-0070
delta 6 owns this target.

### B3 — Make session-detail cost proportional to the requested evidence window

**Proposed title:** `Windowed session detail still expands complete branch histories for aggregates`

**Initial verdict:** `HOLD_FOR_REPRO` until a post-#3038 benchmark proves the
remaining cost and identifies which projection dominates.

**Evidence.** `get_session()` windows display messages at
`lionagi/studio/services/sessions.py:647-751`, but parses each complete
progression into `full_msg_ids` and runs full-history role, bounds, and action
queries at `:717-755`. #3038 reduced chunked query count; it did not make those
aggregates proportional to the 200-message window. `_branch_message_stats()`
also materializes full action/error/file summaries at `:578-644`.

**Missing experiment.** Benchmark 200-message detail for sessions containing
1,000, 10,000, and 100,000 messages, with SQL timing and response bytes broken
down by projection. Confirm PostgreSQL behavior separately if available.

**Target if confirmed.** Denormalized or set-owned bounded aggregates, with
large error/evidence lists behind explicit cursors; default detail cost should
track the requested page, not complete history.

**Acceptance if opened.** The 200-message endpoint stays within an agreed
latency/query/response budget at 100,000 messages; full totals remain correct;
large error outputs are capped/redacted and pageable.

**Duplicate/ADR note.** Proposed ADR-0078 D3 and ADR-0081 D7 define the target.
Do not describe #3038's query-count fix as a complete resolution or regression.

### B4 — Give startup reconciliation a measured readiness budget and progress

**Proposed title:** `Studio readiness waits on serial reconciliation with no visible phase or budget`

**Initial verdict:** `HOLD_FOR_REPRO`; correctness requires reconciliation
before ready, so "defer it all" is not an acceptable fix.

**Evidence.** The ASGI lifespan starts the scheduler and awaits
`run_startup_reconciliation()` before yielding at
`lionagi/studio/app.py:172-208`. That function executes six reapers serially at
`lionagi/studio/services/lifecycle.py:811-850`.

**Missing experiment.** Seed a production-shaped large database with stale and
live rows. Measure process start, liveness availability, each reaper, and
readiness. Identify authoritative vs maintenance-only work.

**Target if confirmed.** Preserve the reconciliation correctness gate while
paging/optimizing it, expose startup phase/progress, and defer only work whose
result cannot make a stale execution look healthy.

**Acceptance if opened.** A fixed large fixture reaches ready within an agreed
budget; health distinguishes alive/reconciling/ready; no stale execution is
served as healthy before its authoritative reconciliation; phase timings are
observable.

**Duplicate/ADR note.** ADR-0076 D3 makes reconciliation a readiness gate;
delta 6 asks for a measured bound rather than bypassing it.

### S1 — Define and enforce the desktop WebView privilege boundary

**Proposed title:** `Desktop WebView exposes daemon bearer authority to page JavaScript with CSP disabled`

**Initial verdict:** `ADR_FIRST`. The security target needs an explicit bridge
and CSP decision before implementation is split into issues.

**Impact.** Any future script injection into the privileged desktop document
can read the bearer token and call the local daemon with the same authority as
the application.

**Evidence.** Tauri config sets `csp: null` and `withGlobalTauri: true` at
`apps/studio/desktop/src-tauri/tauri.conf.json:10-15`. `build_init_script()`
places the token on `window.__STUDIO_AUTH_TOKEN__` at
`desktop/src-tauri/src/lib.rs:56-69`; `resolveAuthToken()` reads it from normal
page JavaScript at `frontend/src/lib/api.ts:39-51`.

**Threat-model reproduction.** In a packaged test build, inject a benign probe
script through a controlled test seam and attempt to read the token and call an
authenticated identity endpoint. Record which Tauri globals are reachable.

**Expected.** Page content has the minimum capability needed for typed JSON/SSE
requests; arbitrary content cannot extract a reusable bearer or invoke
unrelated native APIs. The ADR must explicitly state that a fully compromised
renderer can still exercise whatever scoped API capability the legitimate SPA
owns; CSP and token hiding do not erase that boundary.

**Actual.** The reusable token and global Tauri surface are page-visible, with
no CSP.

**Decision scope.** Amend/accept the ADR-0079 trust-boundary delta: production
CSP, whether global Tauri is needed, a scoped request/stream bridge, navigation
allowlist, and migration of authenticated fetch/SSE. Then split implementation
only if the bridge and policy can be reviewed independently.

**Acceptance after decision.** Restrictive production CSP blocks unapproved
external and inline script probes; global Tauri is off unless justified; page
JavaScript cannot read or exfiltrate a reusable token; the bridge exposes only
the accepted command/request surface; ordinary JSON, reconnecting SSE, startup
identity, and packaged navigation remain green. The test and documentation do
not claim protection after full renderer compromise.

**Duplicate/ADR note.** No initial matching issue was found. ADR-0079 delta 7
records the required decision.

## Cluster D — MCP and fanout friction found while dogfooding LionAGI

The runtime records below came from the uv-cache installed `li`, not from an
editable execution of integration commit `9b0cc4b2a`; `--cwd` selected the
audited repository but not the imported package. Source inspection confirms
the same mechanisms in the integration tree, but every candidate still needs
a commit-identified editable/build reproduction before opening.

### M1 — Emit the current Codex effort override so explicit CLI effort wins

**Proposed title:** `Codex --effort can be ignored in favor of the user's model_reasoning_effort default`

**Impact.** A caller can explicitly request a supported effort and still send
the user's higher global default. For models with a lower ceiling this causes a
provider 400 before useful work starts.

**Observed private reproduction.** Fanout run `20260811T140958-6700a3` recorded
`--effort=high` in `job.json`, but the provider rejected
`reasoning.effort=max` for gpt-5.3 Codex Spark. The public reproduction must use
a minimal prompt and scrubbed config.

**Confirmed mechanism.** Spark is in the xhigh ceiling table at
`lionagi/service/providers.py:50-75`. `CodexProvider` emits
`-c reasoning_effort=...` at `lionagi/providers/openai/codex.py:584-590`, while
the effective current Codex configuration uses `model_reasoning_effort`; the
stale override does not replace the user default.

**Expected.** Explicit task/CLI effort has highest precedence and is clamped to
the selected model before process launch.

**Actual.** User configuration remains effective and can make the request
invalid.

**Scope.** Emit the current Codex configuration key through the existing TOML
encoder, keep per-model clamp logic, and test direct-agent and orchestration
paths with a conflicting user default.

**Acceptance.** With user default `max` and explicit `high`, the spawned Codex
process receives effective `high`; Spark does not send max; an explicit
max/ultra request still clamps to xhigh; direct agent, fanout planner, worker,
and synthesis inherit the resolved value without re-overriding it.

**Duplicate/ADR note.** No initial matching issue was found. This is an
implementation gap against Proposed ADR-0043 D2/D6, not a new precedence rule.

### M2 — Publish an authoritative terminal fact on fanout failure before exit

**Proposed title:** `A failed fanout can exit without publishing its authoritative MCP lifecycle outcome`

**Initial verdict:** `HOLD_FOR_REPRO`. The observed run also suffered SQLite
persistence failure, so the terminal symptom may be part of M5 rather than an
independent defect.

**Observed private evidence.** For run `20260811T140958-6700a3`, orchestration
`run.json` says `failed`, while the MCP sidecar ended as
`exited / indeterminate / process_gone_without_outcome` via the orphan reaper.
The manifest is corroborating evidence only; it is not lifecycle authority.

**Architecture constraint.** ADR-0106 D6 says durable lifecycle state is
authoritative. ADR-0107 correctly assigns `indeterminate` when the process is
conclusively gone and no authoritative outcome was published. Do not "fix"
this by letting `job.status` override lifecycle from `run.json`.

**Isolation experiment.** With persistence healthy, inject a planner/provider
failure after spawn and before worker execution. Verify whether
`stop_live_persist()` and the registered terminal callback publish `failed` to
the MCP sidecar before process exit. Repeat with an injected SQLite lock to
separate producer correctness from persistence degradation.

**Expected.** A caught, classified fanout failure persists one authoritative
terminal fact before the process returns its failure exit code; the sidecar
reports `failed` from the terminal hook/lifecycle cache.

**Actual.** In the observed run, no authoritative fact reached the sidecar, so
the reaper honestly reported indeterminate.

**Scope if independent.** Repair the producer-to-terminal-callback path and
fence races. If the only failing case is persistence setup/write contention,
close this candidate into M5/#2275.

**Acceptance if opened.** Healthy persistence plus injected fanout failure
always yields durable `terminal=true,outcome=failed`; hook-vs-reaper races are
first-writer-wins; missing/unpublishable outcome remains indeterminate; manifest
content never overrides lifecycle.

### M3 — Give fanout planning the executable Mode vocabulary and fail invalid plans

**Proposed title:** `Fanout planners invent Mode names that execution silently drops`

**Impact.** The planner appears to assign reasoning overlays, but workers run
without them. The plan shown to the operator is not the configuration executed.

**Observed private reproduction.** Run `20260811T141233-e39d3e` generated five
assignments and logged seven unknown Mode warnings, including
`reproducibility-first`, `accessibility-focused`, and `threat-modeling`; all were
dropped.

**Confirmed mechanism.** Fanout planning supplies only `role_roster()` at
`lionagi/cli/orchestrate/fanout.py:254-264`. Flow planning supplies both
`role_roster()` and `mode_roster()` at `flow.py:2814-2829`. Execution's
`resolve_modes()` warns and drops unknown/disallowed values at
`_orchestration.py:258-285`.

**Expected.** A generated plan contains only loadable, role-permitted Modes, or
planning fails explicitly before workers start.

**Actual.** The planner lacks the vocabulary and invalid requests degrade
silently.

**Scope.** Add the catalog and role allowlists to fanout planning; validate the
plan before materialization; use either one bounded replan or an explicit
`FanoutPlanError`, not warning-and-drop.

**Acceptance.** Generated-plan fixtures produce zero dropped-mode warnings;
an unknown or disallowed Mode causes a typed plan failure (or exactly one
bounded replan); the final plan and effective worker Modes are identical.

**Duplicate/ADR note.** No initial matching issue was found. Proposed ADR-0043
D5 already says invalid explicit Modes fail before materialization.

### M4 — Stop advertising automatic timeout resume on flow and fanout

**Proposed title:** `Flow and fanout publicly advertise --resume-on-timeout but do not implement it`

**Impact.** MCP clients and CLI users believe a timed-out orchestration gets one
automatic continuation, but the process simply terminates. A client may wait
for work that will never start.

**Observed private reproduction.** Run `20260811T141233-e39d3e` contained
`--timeout=600 --resume-on-timeout`, timed out, produced no resumed attempt, and
ended `timed_out`.

**Confirmed mechanism.** `_run_fanout()` has no `resume_on_timeout` parameter at
`fanout.py:63-92`; `run_orchestrate()` does not forward
`args.resume_on_timeout` at `orchestrate/__init__.py:950-1001`. The option is
nevertheless in CLI help and MCP golden/schema projections. The same
parser/handler audit is required for flow.

**Architecture constraint.** ADR-0095 D5 classifies Fanout as `rerun_only` and
defers universal auto-resume because completed side effects may not be
idempotent. Do not implement an automatic rerun merely to satisfy the stale
flag.

**Expected.** Public schemas advertise only behavior the selected surface
implements under a documented recovery contract.

**Actual.** A common parser option leaks into flow/fanout schemas without a
consumer.

**Scope.** Remove the option from flow/fanout help, parser projections, MCP
schemas, and goldens while preserving direct agent behavior. If product intent
is to retain it, require a new per-surface ADR before implementation.

**Acceptance.** Parser-to-handler conformance proves every projected
non-presentation option is consumed; `resume_on_timeout` remains on agent and
is absent from flow/fanout CLI and MCP contracts; generated fingerprints and
goldens update together.

**Duplicate/ADR note.** No initial matching issue was found. ADR-0062 delta 4
records the contract gate; ADR-0066 is the aspirational generated MCP surface.

### M5 — Make orchestration persistence tolerate ordinary SQLite writer contention

**Proposed action:** comment on or recommend reopening closed
[#2275](https://github.com/ohdearquant/lionagi/issues/2275), not a new issue.

**Regression evidence.** Both private dogfood runs hit `database is locked`.
Run `20260811T140958-6700a3` failed during live-persistence setup; run
`20260811T141233-e39d3e` queued repeated ordered-retry writes during a five-worker
fanout.

**Confirmed mechanism.** SQLite write transactions use `BEGIN IMMEDIATE` after
a bounded busy timeout at `lionagi/state/db.py:564-578` and `:831-839`.
`start_live_persist()` catches any setup exception and disables persistence for
the run at `lionagi/cli/orchestrate/_orchestration.py:1513-1532`.

**Expected.** Ordinary transient contention does not silently remove live and
terminal observability for the complete run.

**Actual.** Setup can permanently disable persistence, while later events can
accumulate noisy failed retries.

**Minimal public reproduction.** Hold a deterministic short writer
transaction from a daemon connection while starting five small fanout workers.
Record sanitized transaction timing, retry decisions, and durable session/
terminal counts. Do not paste private audit prompts or paths.

**Recommended scope.** Add idempotent bounded retry or durable spooling at the
orchestration-persistence boundary. Do not add global automatic retries to
`StateDB._tx()`; ADR-0056 D3 deliberately surfaces contention after the busy
window because arbitrary transactions may not be replay-safe.

**Acceptance for reopened #2275.** Under deterministic transient contention,
setup does not permanently disable persistence; ordered retries drain;
duplicate events are not written; every started session reaches a durable
terminal state or reports one explicit persistence failure; PostgreSQL behavior
is unchanged.

**Relationship.** #2923 is the adjacent WAL-version policy, not this
idempotent write-admission problem. ADR-0064 delta 5 records the execution-level
target.

### M6 — Implement the deterministic manifest-fanout contract

**Proposed title:** `Implement ADR-0110 deterministic manifest fanout without an LLM planning phase`

**Initial verdict:** `ADR_FIRST`: confirm/accept Proposed ADR-0110 before
opening the implementation tracker.

**Impact.** Explicitly decomposed work still pays a planner round, can be
rewritten, and can acquire invalid configuration. In the dogfood run, planning
five already-specified lenses took 74.1 seconds and invented seven invalid
Modes.

**Evidence.** Current `_run_fanout_inner()` always calls `plan()` before worker
materialization at `lionagi/cli/orchestrate/fanout.py:235-276`. ADR-0110 already
specifies manifest validation, per-leg briefs/configuration, parallel execution,
durable round state, artifacts, and machine-result integration.

**Expected.** A caller with explicit briefs can submit them unchanged; no
planner model is invoked; validation is deterministic; per-leg and round
outcomes are durable and queryable.

**Actual.** Existing fanout always performs generative decomposition.

**Scope.** Implement ADR-0110 as written. Do not redesign it in the GitHub
issue and do not replace the existing planner fanout until compatibility and
naming decisions in the ADR are accepted.

**Acceptance.** Use ADR-0110's verify-by list as the tracker checklist,
including zero planner calls, byte-stable validated briefs, concurrency caps,
partial artifact availability, and `job.output` round results.

**Duplicate/ADR note.** No initial matching implementation issue was found.
ADR-0110 remains Proposed/Aspirational, so this is roadmap work rather than a
defect against current accepted behavior.
