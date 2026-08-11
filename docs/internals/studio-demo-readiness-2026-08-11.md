# Lion Studio demo-readiness audit — 2026-08-11

This ledger records the 50 independently testable regressions resolved for the
Studio demo-readiness pass. Each row states the behavior a user or operator
should be able to rely on, the behavior observed before the fix, and the
automated evidence added or strengthened by the change.

The work is split into four reviewable branches so visual polish, frontend
scale, backend scale, and safety can be reviewed or reverted independently:

- `codex/studio-demo-readiness`
- `codex/studio-frontend-scale`
- `codex/studio-backend-scale`
- `codex/studio-safety`

## 1. Visual polish and interaction safety

| # | Expected behavior | Before this pass | Resolution and evidence |
|---:|---|---|---|
| 1 | Progress labels use normal UI capitalization. | The graph displayed `escalated` while peer states were title-cased. | Added the localized `progressEscalated` label for all 16 locales and pinned it in the execution-graph translation test. |
| 2 | Every shared modal has an accessible name tied to its visible title. | `role="dialog"` had no `aria-labelledby` relationship. | `Modal` now generates and binds a stable title id; `Modal.test.tsx` asserts the name. |
| 3 | Opening a shared modal moves focus inside it. | Keyboard focus remained behind the overlay. | The first usable control is focused on mount and covered by the modal interaction test. |
| 4 | Tab and Shift+Tab remain inside a shared modal. | Keyboard users could tab into the obscured application. | Added forward and reverse focus wrapping with a focused regression test. |
| 5 | Closing a shared modal returns focus to its launcher. | Focus was lost into the document body. | The previously focused element is restored on unmount and asserted in the modal test. |
| 6 | Parent re-renders do not steal focus inside an open modal. | A new `onClose` callback identity retriggered focus initialization. | The callback now flows through a ref; a rerender regression proves focus remains put. |
| 7 | Enter on the command-palette Close button only closes the palette. | The global Enter handler also executed the highlighted command. | Key handling is scoped to the command surface; `CommandPalette.test.tsx` clicks/focuses Close and presses Enter. |
| 8 | Keyboard focus stays within the open command palette. | Tab could escape to controls behind the palette. | Added a palette focus loop and a keyboard regression. |
| 9 | Closing the command palette restores the invoking control. | Focus was not returned after the overlay disappeared. | The palette records and restores the prior element; the interaction test verifies it. |
| 10 | Escape cannot silently discard edited schedule fields. | Escape immediately closed a dirty schedule dialog. | All Escape closes go through the dirty guard; the test edits a field, presses Escape, and expects the warning. |
| 11 | Backdrop clicks cannot silently discard edited schedule fields. | Clicking outside the dialog bypassed the existing `dirty` state. | Pointer closes now share the dirty guard and preserve the editor until discard is confirmed. |
| 12 | Cancel and header Close cannot silently discard edited schedule fields. | Both controls closed directly despite unsaved edits. | Every close path now offers Keep editing or Discard changes; the Cancel interaction is covered directly. |
| 13 | The custom schedule dialog has dialog semantics and contained focus. | It lacked an accessible name and complete focus management. | Added `aria-labelledby`, initial focus, focus wrapping, and restoration to the schedule dialog. |
| 14 | Canvas deletion is explicit and cannot submit an ancestor form. | A generic icon button had an implicit submit type and ambiguous label. | Delete controls use contextual labels and `type="button"`; `SidePanel.test.tsx` verifies both. |
| 15 | Canvas link-mode buttons expose the selected mode and never submit. | Mode state was visual-only and the buttons inherited submit behavior. | Added `aria-pressed`, explicit button types, and a form-submission regression. |

## 2. Frontend performance and state correctness

| # | Expected behavior | Before this pass | Resolution and evidence |
|---:|---|---|---|
| 16 | A closed Operator dock performs no loading, catalog, history, or SSE work. | `OperatorPanel` stayed mounted and ran its effects while rendering `null`. | `AppShell` conditionally mounts the dock; its lifecycle test proves close unmounts and reopen initializes it. |
| 17 | A fresh narrow window shows the primary Studio surface first. | Operator opened by default and obscured the application on small widths. | Fresh sessions below the desktop threshold start closed while an explicit saved choice still wins. |
| 18 | Rapid Operator conversation changes are last-selection-wins. | A slow response for conversation A could overwrite a later selection B. | A generation guard invalidates stale loads; the delayed-A/fast-B regression remains on B. |
| 19 | Opening a completed run does not replay its entire message history over SSE. | Run detail unconditionally opened the message stream at cursor zero. | Stream eligibility waits for the matching snapshot and excludes terminal sessions; the completed fixture opens zero message streams. |
| 20 | Local Vite development honors the configured API proxy target. | Dev ports bypassed Vite and called hostname port 8765 directly, so `STUDIO_API_URL` had no effect and required backend CORS. | Dev ports now use same-origin `/api`; API-base tests cover 3000/5173 and remote-host access through the proxy. |
| 21 | Rapid run-detail navigation is last-selection-wins. | A slow detail response for run A could replace run B after navigation. | Initial loads now cancel on id change/unmount; the delayed-A/fast-B test remains on B. |
| 22 | Calendar rendering cost scales with visible cells, not interval occurrences. | A one-second interval iterated roughly 2.6 million firings per month per schedule. | Projection jumps between visible day/hour boundaries; the one-second month fixture produces 31 projections. |
| 23 | Large ASAP graphs retain dependency order without an explosive Dagre dummy-node burden. | Exact large `minlen` values expanded a 111-node fixture to about 1,910 dummy nodes, and an adversarial 99-node graph still predicted 1,522. | Burden-based gap scaling applies at every node count; the fixtures stay within 222 and 198 predicted dummies respectively, while deterministic tests preserve finite positions and dependency order. |
| 24 | `/playbooks` lands on the visible Playbooks library tab. | It redirected to the hidden unfinished `workflow` tab. | The retired-route mapping now targets `tab=playbook` and is regression-tested. |
| 25 | Schedule polling never starts a second refresh while one is active. | Timer, manual, and mutation refreshes could overlap the entire request fan-out. | `useSchedulesData` coalesces active work and queues exactly one trailing refresh when changes arrive mid-flight; deferred-fan-out tests assert that behavior. |
| 26 | Schedule summary requests use bounded concurrency. | The UI launched one run-history request per schedule simultaneously. | A stable-order worker pool caps concurrent summaries at six; the concurrency test records the maximum. |
| 27 | Engine-run filter drafts do not query until Apply is submitted. | Every keystroke changed hook dependencies and hit the database. | Draft and applied filters are separate; the test types without a request and observes one request on submit. |
| 28 | Engine runs after row 100 remain reachable. | The route had a hard `limit=100` and no pagination control. | Added offset paging and Load more; the regression loads and displays row 101. |
| 29 | Library detail always belongs to a row visible under the current search. | Filtering out the selected item left its unrelated stale detail open. | Selection reconciliation now depends on the actual items, query, and selection; the search regression moves to a visible result. |
| 30 | The footer's heavyweight database statistics are not polled on the health cadence. | `/api/stats` ran every 30 seconds alongside the cheap health probe. | Health remains 30 seconds while stats refresh every five minutes after a delayed first read; fake timers pin the request counts. |
| 31 | A stats failure cannot erase a valid health result. | Health and stats shared one `Promise.all`, so either failure discarded both readings. | The probes now update independently with separate in-flight guards and hidden-tab suppression. |

## 3. Backend scale, contention, and data lifecycle

| # | Expected behavior | Before this pass | Resolution and evidence |
|---:|---|---|---|
| 32 | Every enabled schedule is evaluated, even when more than 100 exist. | The scheduler inherited `StateDB.list_schedules()`'s public 100-row default. | Its internal service requests an unbounded list; a 101-schedule regression sees every id. |
| 33 | The daemon scheduler reuses its `StateDB` engine across a tick/fire lifecycle. | Every small scheduler state operation opened `StateDB` and reapplied schema setup. | The production scheduler owns one persistent service and closes it on stop; `StateDB.open()` count tests stay at one. |
| 34 | Invocation list GETs never run schema application or take a writer lock on SQLite. | The read route used default writable `StateDB`. | SQLite list reads use the supported read-only opener; a test replaces schema application with a failure sentinel. |
| 35 | Invocation detail GETs never run schema application or take a writer lock on SQLite. | Detail also defaulted to writable opens unless a caller opted in. | Detail now selects read-only mode automatically when supported; the same failure-sentinel test covers it. |
| 36 | One invocation list request uses one database context for rows and totals. | Rows, total, and completed total opened three separate contexts. | Route-private helpers share one read snapshot; an open-count regression asserts exactly one. |
| 37 | Invocation child health uses a constant number of child-session queries per page. | A 200-row page performed one child-session query per invocation. | Child sessions are fetched in one bounded `IN` query and grouped in memory; the test proves the per-row method is never called. |
| 38 | Invocation process snapshots cannot block the async server loop. | Synchronous `ps` work ran directly inside the polled async route. | Snapshot capture runs in `asyncio.to_thread`; the regression records a non-event-loop thread id. |
| 39 | Run-list process snapshots cannot block the async server loop. | The run list had the same synchronous `ps` path. | It now offloads the snapshot; a separate thread-identity regression covers this route. |
| 40 | Full-history role aggregation uses one set query, not one query per 500 ids. | A large progression issued repeated role-count statements. | SQLite `json_each` expands the id set once; a 1,201-id regression records one statement. |
| 41 | Full-history action-message aggregation uses one message query. | Action hydration also issued one statement per 500 ids. | One ordered `json_each` join replaces the chunk loop; the 1,201-id regression records one type lookup plus one message query. |
| 42 | The stale-invocation reaper eventually examines every running row. | It loaded only the newest 1,000, so an older crashed row could remain forever. | Oldest-first `(started_at,id)` keyset pages cover the full set; a stale row behind 1,000 recent rows is reaped. |
| 43 | Explicit admin pruning refuses running/non-terminal sessions. | Naming a live session id deleted it immediately. | Explicit prune reuses retention's terminal-status recheck; the regression gets `pruned=0` and reads the row back. |
| 44 | Explicit pruning removes only the selected session's lineage. | It followed deletion with a database-wide orphan-message sweep. | Lineage-scoped progression/message cleanup replaces the global sweep; an unrelated orphan is proven to survive. |

## 4. Safety, privacy, and transport contracts

| # | Expected behavior | Before this pass | Resolution and evidence |
|---:|---|---|---|
| 45 | Every unsafe `/api` request carries non-simple JSON proof, including bodyless actions. | Content-Type was enforced only when a request body was detected, leaving empty form POSTs cross-origin sendable. | Middleware now requires `application/json` for all unsafe API methods; hardening tests prove a form-style request is rejected before mutation. |
| 46 | Every frontend mutation automatically satisfies that JSON contract. | Bodyless POST/DELETE callers did not consistently set Content-Type. | `fetchJson` centrally supplies JSON Content-Type for POST/PUT/PATCH/DELETE across object, tuple, and `Headers` inputs. |
| 47 | Permanent SSE client errors stop reconnecting. | Generic SSE retried 400/401/403/404/422 every two seconds forever. | Permanent 4xx responses now terminate while 408/425/429, 5xx, and network failures remain retryable; API tests cover both classes. |
| 48 | Signal SSE resumes after the last delivered sequence. | Reconnects restarted at zero and replayed the complete signal history. | The client advances `after_seq` and the backend honors it; disconnect/reconnect tests resume at N+1. |
| 49 | Secret material cannot escape by being used as a JSON mapping key. | Operator argument redaction scrubbed values but preserved observable mapping keys. | Mapping keys now pass through the same text scrubber before recursive projection; direct and read-path regressions agree. |
| 50 | URL query credentials are redacted across common field spellings. | Access/private key variants in camel, snake, kebab, and dot notation survived adapter URL redaction. | A shared credential-field predicate covers all projections; the spelling matrix masks every tested variant. |

## Additional hardening delivered with the 50 fixes

- Remote images are blocked by default on every untrusted Markdown surface, so
  agent/tool content cannot create an external tracking request.
- The authenticated application document no longer executes the remote
  `analytics.khive.ai` script.
- Desktop startup verifies a cheap authenticated `/api/identity` response
  rather than waiting for the heavyweight database/filesystem statistics route.
- Desktop bearer-token generation now fails closed if the OS entropy source
  cannot provide the complete random token, instead of falling back to zeros.
- Invalid ICU-like `~/.lionagi/skills/<name>/` copy is corrected in all locales,
  and locale tests now fail on formatter errors instead of logging through them.
- Every production TSX `<button>` has an explicit type; additional library and
  graph-editor fields have accessible labels.
- React's act environment is configured centrally, removing the test suite's
  false warning flood so real interaction warnings remain visible.
- The frontend dependency lock overrides the vulnerable transitive `nanoid`
  release; `npm audit` reports zero vulnerabilities.
- The Studio frontend README now describes the actual Vite commands, ports,
  environment variables, and current route map.

## Literal visual walkthrough gate

The seeded daemon and Vite application were prepared for an interactive pass,
but the in-app browser permission was denied before navigation. Per the browser
control safety contract, no alternate browser automation or hidden Playwright
run was used to work around that decision. The rows below are therefore an
explicit pending manual gate, not a claimed walkthrough.

| Interaction | Expected after the fixes | Observed in a real browser |
|---|---|---|
| Fresh 390 px session; open and close Operator | Primary app is initially visible; opening works; closing unmounts the dock and stops its requests/stream. | **Pending browser permission** |
| Open command palette; Tab to Close; press Enter | Focus remains in the palette and Enter closes without executing a command; focus returns to the launcher. | **Pending browser permission** |
| Edit a schedule; try Escape, backdrop, Cancel, then Discard | Every dirty close attempt warns; Keep editing preserves values; Discard closes. | **Pending browser permission** |
| Open a completed run and inspect Network | The message snapshot renders without full message SSE replay; persisted signals replay once for graph history and self-close. A running run continues streaming and retains signals after completion. | **Pending browser permission** |
| Render a month containing one-second schedules | Calendar remains responsive and shows at most the first firing per visible cell. | **Pending browser permission** |
| Type Engine filters, then press Apply and Load more | Typing sends no list requests; Apply sends one; row 101 is reachable. | **Pending browser permission** |
| Select Library item A, then search for B | Stale A detail disappears and selection follows a visible result. | **Pending browser permission** |
| Visit `/playbooks` | URL resolves to `/library?tab=playbook`, with the visible Playbooks tab selected. | **Pending browser permission** |
| Leave Studio open across health/stat intervals | Health updates independently; heavyweight stats do not fire every 30 seconds. | **Pending browser permission** |
