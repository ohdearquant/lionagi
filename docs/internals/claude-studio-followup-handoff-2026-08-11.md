# Claude handoff: verify the post-demo Lion Studio issue backlog

Date: 2026-08-11

## Purpose

Review the four Studio demo-readiness pull requests as one integrated change,
finish the literal browser walkthrough that remains pending, and decide which
follow-up drafts in
[`studio-followup-issue-drafts-2026-08-11.md`](./studio-followup-issue-drafts-2026-08-11.md)
are worth opening. Do not open, edit, close, or comment on GitHub issues during
the verification pass. Return verdicts to the repository owner first.

This is a verification handoff, not an implementation assignment. The issue
drafts deliberately include candidates that should be rejected, merged into an
existing issue, or held for measurement if the evidence does not survive the
integrated baseline.

## Scope and baseline

The 50 completed fixes are split across four open draft PRs:

| PR | Branch | Head reviewed here | Scope |
|---|---|---|---|
| [#3036](https://github.com/ohdearquant/lionagi/pull/3036) | `codex/studio-demo-readiness` | `ac45ca1ca469a3dbe3da93f2f0ffd0156d5f9675` | visual and interaction polish |
| [#3037](https://github.com/ohdearquant/lionagi/pull/3037) | `codex/studio-frontend-scale` | `aefaf86d59d3217ad4d9a30b198dbee39463b9cd` | frontend scale and state correctness |
| [#3038](https://github.com/ohdearquant/lionagi/pull/3038) | `codex/studio-backend-scale` | `4c50409e2049ebef9ed4a137b651a5fc7c5458f9` | database, scheduler, and lifecycle scale |
| [#3039](https://github.com/ohdearquant/lionagi/pull/3039) | `codex/studio-safety` | `d169e5f14a2c98f539824409be081ac97409267c` | transport, desktop startup, and redaction |

A disposable local integration of those heads exists at
`/private/tmp/lionagi-studio-integration.iaOUH7`, commit
`9b0cc4b2afece744c94fc0886e2aaaf76f0e172c`. It is detached and is evidence,
not a branch to publish. Rebuild the merge if any PR head has moved, and record
the exact heads in the verdict.

The completed-fix ledger is
[`studio-demo-readiness-2026-08-11.md`](https://github.com/ohdearquant/lionagi/blob/ac45ca1ca469a3dbe3da93f2f0ffd0156d5f9675/docs/internals/studio-demo-readiness-2026-08-11.md)
in #3036.
Treat its first 50 rows as regression coverage, not as open-issue candidates.
A failure caused by one of those changes belongs back on the corresponding
draft PR; do not open a second issue for it.

## Non-negotiable evidence boundary

The automated integration was green, but the literal visual walkthrough was
not completed. Browser-control permission was denied before navigation, and
the ledger correctly marks every observed cell as **Pending browser
permission**. Do not rewrite that as "visually verified" based on Vitest,
Playwright source, a production build, or static inspection.

Before recommending any visual or interaction issue, run the integrated app in
a real browser and record:

- integration commit and browser version;
- viewport and seeded data size;
- exact click/keyboard steps;
- expected and observed behavior;
- pass/fail verdict;
- screenshot, Network evidence, console trace, or performance trace as
  appropriate.

Use browser-visible evidence for behavior and trace/counter evidence for
performance. Source code can explain a confirmed mechanism; it is not by
itself a visual walkthrough.

## Required walkthrough

Run at both a desktop viewport and 390 px unless a row says otherwise.

| Interaction | Expected product behavior under review | Evidence to retain |
|---|---|---|
| Fresh narrow session; open and close Operator | Primary app starts visible. Opening initializes Operator. Closing unmounts it and stops catalog/history/stream requests. | screenshot plus Network request timeline |
| Open command palette; Tab and Shift+Tab; focus Close; press Enter | Focus remains inside; Enter on Close does not execute a command; close restores focus. | focused element sequence and console |
| Edit a schedule; attempt Escape, backdrop, Cancel, browser Back; choose Keep editing and Discard | Every dirty close warns, edited values survive Keep editing, and Discard closes. | steps and focused element after each branch |
| At 390 px, open schedule detail | Run now, Delete, missed-fire policy, and overlap policy are all reachable without switching viewport. | full-page screenshot and touch/keyboard steps |
| Open a completed run, then a running/resumed run | Completed detail does not replay messages over SSE; persisted signals reconstruct the graph and close. Running detail streams and converges at terminal. | Network stream count, transferred rows, final status |
| Expand and collapse a run graph | Expanded view behaves as a modal, focus cannot escape, close restores focus, and only one heavy canvas is mounted. | accessibility tree, focused element, React/DOM count |
| Render a month with one-second schedules | Calendar remains responsive and emits at most the first firing in each visible cell. | performance trace and projected-row count |
| Type Engine filters, Apply, then Load more | Typing sends no list request; Apply sends one; row 101 is reachable and stale page responses cannot append. | Network log |
| Select Library A, then search for B | A disappears from detail and selection follows a visible row. | screenshot and URL search state |
| Visit `/playbooks` | It resolves to `/library?tab=playbook` with Playbooks visible. | final URL and selected tab |
| Leave Studio open across health/stat intervals | Health updates independently; heavyweight stats do not run every 30 seconds. | five-minute Network trace |

If browser access is still unavailable, return `BLOCKED_VISUAL_GATE` for these
rows. Do not substitute hidden automation to work around an explicit
permission denial.

## Issue-verification workflow

For each candidate draft:

1. Reproduce on the current four-PR integration, not on one isolated branch.
2. Record the stable symbol and current line range. Line numbers in the draft
   are anchors to the reviewed integration commit, not permanent identifiers.
3. Separate three claims:
   - **Observed:** runtime, browser, query-count, or manifest evidence.
   - **Confirmed mechanism:** the source path that accounts for the
     observation.
   - **Inference:** impact not yet measured. Keep it labelled as inference.
4. Search open and closed GitHub issues using the title, symptom, stable
   symbols, and referenced ADR. Read likely matches; title search alone is not
   a duplicate check.
5. Map the candidate to an architecture status:
   - `CONFORMS`: a defect against an accepted invariant;
   - `IMPLEMENTATION_GAP`: a proposed/aspirational ADR already defines the
     target;
   - `DECISION_REQUIRED`: expected behavior is not yet settled;
   - `NO_ADR_IMPACT`: bounded implementation issue.
6. Choose exactly one verdict:
   - `OPEN_NEW`
   - `COMMENT_EXISTING`
   - `CLOSE_AFTER_MERGE`
   - `SPLIT`
   - `HOLD_FOR_REPRO`
   - `ADR_FIRST`
   - `REJECT`

Only use `OPEN_NEW` when all of the following hold:

- the problem remains on the integrated heads;
- no open or closed issue already owns the same failure mechanism;
- a behavior issue has a real reproduction;
- a performance issue has a request, row, query, connection, DOM, heap, or
  timing measurement;
- expected behavior is backed by a product invariant or ADR;
- the issue owns one coherent failure mechanism;
- acceptance criteria are black-box testable and contain a measurable bound;
- public evidence is scrubbed of tokens, private prompts, and full local paths.

Use P0 only for data/safety failure, an automated false terminal conclusion,
capacity exhaustion, or a reproducible demo blocker. Static `O(n)` inspection
alone is not P0 evidence.

## MCP dogfood evidence

Two read-only fanout attempts produced useful product evidence even though
they did not produce the requested audit artifact. These paths are private
local inputs for verification and must not be pasted verbatim into a public
issue:

Both jobs launched an installed `li` from the uv cache. Their `--cwd` pointed
at the disposable integration tree, but cwd does not select the Python source
being executed. The implicated mechanisms are also present in integration
commit `9b0cc4b2a`, yet the runtime artifacts are only **installed-build
reproductions**, not executions of that commit. Re-run every MCP candidate
against an editable install or a built artifact whose commit identity is
recorded before recommending `OPEN_NEW`.

| Run | Relevant private evidence | Observed result |
|---|---|---|
| `20260811T140958-6700a3` | `~/.lionagi/mcp/jobs/<run>/job.json`, `console.log`, and `~/.lionagi/runs/<run>/run.json` | argv requested `--effort=high`; provider received `reasoning.effort=max` for Spark and failed. `run.json` says `failed`; MCP job status says `indeterminate`. Persistence setup also hit `database is locked`. |
| `20260811T141233-e39d3e` | same three files | five-worker plan took 74.1 s, seven invented modes were silently dropped, persistence writes repeatedly hit `database is locked`, the 600 s run timed out, `--resume-on-timeout` did not resume, and no artifact was harvested. |

For a public reproduction, construct the smallest scrubbed invocation that
proves one contract at a time. Do not use the audit prompt, home-directory
paths, bearer data, or unrelated warnings as issue content.

The drafts for these observations are intentionally separate. Effort
precedence, terminal outcome reconciliation, planner vocabulary,
resume-on-timeout truthfulness, and SQLite persistence are five different
failure mechanisms.

## Known issue relationships to verify before opening anything

| Existing item | Current action |
|---|---|
| [#2275](https://github.com/ohdearquant/lionagi/issues/2275) SQLite lock contention | Closed, but the same class recurred in a five-worker run. Prefer a regression comment/reopen recommendation over a duplicate unless the new failure is demonstrably a different layer. |
| [#2923](https://github.com/ohdearquant/lionagi/issues/2923) WAL safety policy | Adjacent storage-policy decision, not a substitute for bounded write admission/retry. Cross-link only. |
| [#2979](https://github.com/ohdearquant/lionagi/issues/2979) server-derived attention | The proposed live-overview read model partially contains this. Prefer expanding or cross-linking it after scope review. |
| [#2769](https://github.com/ohdearquant/lionagi/issues/2769) automatic retention | Exact owner of unbounded Studio retention. Do not duplicate. |
| [#3016](https://github.com/ohdearquant/lionagi/issues/3016) richer runtime graph | Exact owner of one-node authored graph precedence. Do not duplicate. |
| [#3013](https://github.com/ohdearquant/lionagi/issues/3013) node activity wiring | Appears resolved by #3037; recommend `CLOSE_AFTER_MERGE` only after browser verification. |
| [#3011](https://github.com/ohdearquant/lionagi/issues/3011) Fleet maximum update depth | Hold until reproduced on the integration; do not repeat the old hypothesized cause without a fresh trace. |
| [#2967](https://github.com/ohdearquant/lionagi/issues/2967) skipped/cancelled/aborted graph status | `NodeSkipped` is now mapped; update only the cancelled/aborted residue if it still reproduces. |
| [#3030](https://github.com/ohdearquant/lionagi/issues/3030) Operator pause/resume/steer | Existing product feature issue. Do not duplicate. |
| [#3033](https://github.com/ohdearquant/lionagi/issues/3033) proposal-wait behavior | Keep tied to its existing proposal-flow owner and branch context; do not recreate it from an integration symptom without a fresh reproduction. |
| [#2535](https://github.com/ohdearquant/lionagi/issues/2535) leader exit vs descendant completion | Cross-link from process deadline work; do not merge deadline and completion-proof semantics. |
| [#2750](https://github.com/ohdearquant/lionagi/issues/2750) worker pass blocked scheduler ticks | Closed after the pass moved off the tick critical path. New deadline work must prove the remaining capacity/liveness failure. |
| [#2751](https://github.com/ohdearquant/lionagi/issues/2751) ad-hoc slot policy | Closed after an independent ad-hoc pool was added. New throughput work must prove that the pool is configured above one but consumed serially. |
| [#2755](https://github.com/ohdearquant/lionagi/issues/2755) confirmed-dispatch orphan | Existing lifecycle issue. Cross-link only. |
| [#2732](https://github.com/ohdearquant/lionagi/issues/2732), [#2843](https://github.com/ohdearquant/lionagi/issues/2843), [#2847](https://github.com/ohdearquant/lionagi/issues/2847), [#2727](https://github.com/ohdearquant/lionagi/issues/2727) | Existing Library statistics, Operator image/snapshot, model picker, and release-gating work. Do not republish them under broader polish titles. |

## ADR interpretation

- ADR-0056 and ADR-0071 are retrospective. Their current-vs-ideal deltas may
  produce issues; do not claim that the ideal is already guaranteed.
- ADR-0076 and ADR-0079 are retrospective. The new bounded-data and stream
  deltas describe measured debt without selecting a library or transport
  prematurely.
- ADR-0106 and ADR-0107 are aspirational machine-result contracts. The MCP
  outcome issue is an implementation gap, not a change to their target.
- ADR-0110 is Proposed/Aspirational. A tracker may say "implement ADR-0110";
  it must not say deterministic manifest fanout is current or accepted behavior.

## Required output

Return one table, followed only by short notes for blockers:

| Candidate | Integrated baseline | Reproduced? | Measurement | Closest issue | ADR status | Verdict | Issue-ready title | Blocking evidence |
|---|---|---|---|---|---|---|---|---|

For every `OPEN_NEW`, include a final, copy-ready GitHub body using the draft's
sections. For every `COMMENT_EXISTING`, provide the issue number and a short
copy-ready comment. For `HOLD_FOR_REPRO`, name the missing experiment. Do not
perform the GitHub write; the repository owner will authorize that separately.
