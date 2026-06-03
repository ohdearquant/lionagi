# Follow-up issue drafts — 2026-06 (from #1132)

> **DRAFT ONLY.** None of these have been filed via the GitHub API. The issue
> numbers below (`#1259`–`#1263`) are **placeholders** chosen to follow the
> current max issue/PR number (#1258 as of 2026-06-03; issues and PRs share one
> sequence on GitHub, so `#1255`–`#1258` are already taken by merged PRs). When
> these are actually minted, reconcile the real numbers with the in-code
> `TODO(#NNNN)` breadcrumbs added in the same PR.
>
> Parent tracking issue: **#1132** — *chore(tracking): file follow-up issues for
> asyncio Phase 2/3, a11y deferrals, and rotting TODOs*.

---

## #1259 — asyncio Phase 2: migrate `Event.completion_event` off `asyncio.Event`

**Labels:** `tech-debt`, `p3`, `concurrency`
**Title:** `chore(concurrency): asyncio Phase 2 — migrate Event.completion_event to anyio (needs .clear() audit)`

### Body

Phase-2 follow-up to the asyncio→anyio sweep (PR #1116, tracking #1043).

**Site:** `lionagi/protocols/generic/event.py:309` — `_completion_event: asyncio.Event`,
created lazily in `completion_event` (line ~331) and `.set()` on terminal status
transitions (lines ~335, ~375).

**Why deferred:** `anyio.Event` has **no `.clear()`** — it is single-shot by
design. The current code never calls `.clear()` (the event fires once on the
first terminal transition and is never reset), so a migration is *likely* safe,
but this must be confirmed by an audit before swapping the type. Classification:
**inference** (based on reading the three `.set()` call sites and the absence of
any `.clear()`), not a verified guarantee.

**Acceptance criteria:**
- [ ] Audit all readers/writers of `completion_event` for reset semantics.
- [ ] If no reset is needed, replace `asyncio.Event` with `anyio.Event`
      (or `lionagi.ln.concurrency.Event`).
- [ ] If a reset *is* needed somewhere, document why and keep `asyncio.Event`
      with a justifying comment.
- [ ] Update the in-code breadcrumb to point at the resolution.

---

## #1260 — asyncio Phase 3: migrate `Processor.queue` off `asyncio.Queue`

**Labels:** `tech-debt`, `p3`, `concurrency`
**Title:** `chore(concurrency): asyncio Phase 3 — migrate Processor.queue to lionagi.ln.concurrency.Queue`

### Body

Phase-3 follow-up to the asyncio→anyio sweep (PR #1116, tracking #1043).

**Site:** `lionagi/protocols/generic/processor.py:68` — `self.queue = asyncio.Queue(...)`,
with `put_nowait` + `asyncio.QueueFull` handling at line ~117 and `qsize()` /
backpressure checks in `queue_full`.

**Why deferred:** the anyio / `lionagi.ln.concurrency` queue API shape differs
from `asyncio.Queue` — there is no drop-in `QueueFull` exception or `qsize()`
in the same form, so the `enqueue`/`queue_full` paths need rework rather than a
type swap.

**Acceptance criteria:**
- [ ] Map `asyncio.Queue` usage (`put_nowait`, `QueueFull`, `qsize`, `maxsize`)
      to the target queue API.
- [ ] Preserve current backpressure semantics (return `False` when full).
- [ ] Add/extend tests covering full-queue and capacity-refresh behavior.
- [ ] Update the in-code breadcrumb to point at the resolution.

---

## #1261 — a11y: wire `SidePanel.tsx` form labels (`label-has-associated-control`)

**Labels:** `tech-debt`, `p3`, `a11y`, `frontend`
**Title:** `fix(a11y): wire SidePanel form labels — resolve label-has-associated-control (#1020 follow-up)`

### Body

Follow-up to the a11y polish pass (PR #1114). The biggest deferred block is in
`apps/studio/frontend/components/canvas/SidePanel.tsx`, which currently carries
a file-level `eslint-disable jsx-a11y/label-has-associated-control` (lines 1–4)
and a `TODO(#1020 follow-up)` breadcrumb but **no concrete tracking issue** —
this issue is that home.

**Why deferred:** ~10 `label-has-associated-control` violations; each `<label>`
needs `htmlFor`/`id` wiring (or nesting the control), which is mechanical but
touches every form field in the panel.

**Acceptance criteria:**
- [ ] Add `htmlFor`/`id` pairing (or control nesting) for each labeled field.
- [ ] Remove the file-level `eslint-disable jsx-a11y/label-has-associated-control`.
- [ ] `eslint` passes on `SidePanel.tsx` with no per-line disables.
- [ ] Update/remove the `TODO(#1020 follow-up)` breadcrumb.

---

## #1262 — Studio: server-side invocation pagination on the runs page

**Labels:** `tech-debt`, `p3`, `frontend`
**Title:** `feat(studio): server-side invocation pagination on runs page (ADR-0020)`

### Body

`apps/studio/frontend/app/runs/page.tsx:253` carries `TODO(ADR-0020): server-side
invocation pagination — paginate by invocation` with no tracking issue.

**Why deferred:** invocations currently load unpaginated; large runs render the
full list client-side. ADR-0020 specifies paginating by invocation.

**Acceptance criteria:**
- [ ] Add server-side pagination (cursor or offset) for invocations per ADR-0020.
- [ ] Wire the runs page to request pages and render incrementally.
- [ ] Update the in-code TODO to reference this issue.

---

## #1263 — clarify / resolve rotting TODO in `select/utils.py`

**Labels:** `tech-debt`, `p3`
**Title:** `chore: resolve rotting TODO in select/utils.py (make selection a field model)`

### Body

`lionagi/operations/select/utils.py:14` carries an undated, unowned TODO:
`# TODO: Make select a field to be added into a model, much like reason and action`.

**Why deferred:** design decision, not a quick fix — making `select` a field
model parallels how `reason`/`action` are modeled. Named in #1132 as a "rotting
TODO with no issue/owner/date"; filing this gives it a home (no in-code
breadcrumb change required beyond optionally appending `(#1263)`).

**Acceptance criteria:**
- [ ] Decide whether selection should become a reusable field model.
- [ ] Either implement it or close with rationale and remove the TODO.

---

## Director / human actions still required

- These five issues must be **filed via GitHub** (not done here — drafting only).
- After minting, replace the placeholder `#1259`–`#1263` refs in the in-code
  breadcrumbs (`event.py`, `processor.py`) with the real numbers.
- Per #1132 fix item 3: update the PR body template to require resolving or
  breadcrumbing deferred items.
