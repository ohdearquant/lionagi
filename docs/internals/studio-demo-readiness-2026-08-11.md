# Lion Studio demo-readiness audit — 2026-08-11

The demo-readiness pass is split into four branches so visual polish, frontend
scale, backend scale, and safety can be reviewed or reverted independently:

- `codex/studio-demo-readiness` (this branch)
- `codex/studio-frontend-scale`
- `codex/studio-backend-scale`
- `codex/studio-safety`

This ledger records only the regressions **this branch** resolves. Row numbers
are shared across the whole pass, so rows absent here are not missing — they
land with the branch that carries them, and each row is stated as resolved only
where its code actually merges. Each row states the behavior a user or operator
should be able to rely on, the behavior observed before the fix, and the
automated evidence added or strengthened by the change.

## 1. Visual polish and interaction safety

| # | Expected behavior | Before this pass | Resolution and evidence |
|---:|---|---|---|
| 1 | Progress labels use normal UI capitalization. | The graph displayed `escalated` while peer states were title-cased. | Added the localized `progressEscalated` label for all 16 locales and pinned it in the execution-graph translation test. |
| 2 | Every shared modal has an accessible name tied to its visible title. | `role="dialog"` had no `aria-labelledby` relationship. | `Modal` now generates and binds a stable title id; `Modal.test.tsx` asserts the name. |
| 3 | Opening a shared modal moves focus inside it. | Keyboard focus remained behind the overlay. | The first usable control is focused on mount unless a child already claimed focus in its own mount effect; both paths are covered by the modal interaction tests. |
| 4 | Tab and Shift+Tab remain inside a shared modal. | Keyboard users could tab into the obscured application. | Added forward and reverse focus wrapping with a focused regression test. |
| 5 | Closing a shared modal returns focus to its launcher. | Focus was lost into the document body. | The launcher is captured during the first render (before child effects can move focus) and restored on unmount; asserted in the modal tests, including the self-focusing-child case. |
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

## 2. Frontend performance and state correctness (rows carried by this branch)

Rows 16–19 and 21–29 of this section belong to `codex/studio-frontend-scale`
and are documented there; this branch carries three rows.

| # | Expected behavior | Before this pass | Resolution and evidence |
|---:|---|---|---|
| 20 | Local Vite development honors the configured API proxy target. | Dev ports bypassed Vite and called hostname port 8765 directly, so `STUDIO_API_URL` had no effect and required backend CORS. | Dev ports now use same-origin `/api`; API-base tests cover 3000/5173 and remote-host access through the proxy. |
| 30 | The footer's heavyweight database statistics are not polled on the health cadence. | `/api/stats` ran every 30 seconds alongside the cheap health probe. | Health remains 30 seconds while stats refresh every five minutes after a delayed first read; fake timers pin the request counts. |
| 31 | A stats failure cannot erase a valid health result. | Health and stats shared one `Promise.all`, so either failure discarded both readings. | The probes now update independently with separate in-flight guards and hidden-tab suppression. |

Rows 32–44 (backend scale, contention, and data lifecycle) ride
`codex/studio-backend-scale`, and rows 45–50 (safety, privacy, and transport
contracts) ride `codex/studio-safety`; neither set is asserted here.

## Additional hardening delivered with this branch

- Remote images are blocked by default on every untrusted Markdown surface, so
  agent/tool content cannot create an external tracking request.
- The authenticated application document no longer executes the remote
  `analytics.khive.ai` script.
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
explicit pending manual gate, not a claimed walkthrough. Walkthrough rows for
behavior carried by the sibling branches gate those branches, not this one.

| Interaction | Expected after the fixes | Observed in a real browser |
|---|---|---|
| Open command palette; Tab to Close; press Enter | Focus remains in the palette and Enter closes without executing a command; focus returns to the launcher. | **Pending browser permission** |
| Edit a schedule; try Escape, backdrop, Cancel, then Discard | Every dirty close attempt warns; Keep editing preserves values; Discard closes. | **Pending browser permission** |
| Leave Studio open across health/stat intervals | Health updates independently; heavyweight stats do not fire every 30 seconds. | **Pending browser permission** |
