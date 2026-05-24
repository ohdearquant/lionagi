# ADR-0031: Entity Header Pattern

**Status**: Proposed
**Date**: 2026-05-23
**Related**: ADR-0028 (status reasons surfaced in header), ADR-0030 (reuses EntityAction shape)

## Context

Studio's entity detail pages (`/shows/<topic>`, `/runs/<id>`,
`/projects/<name>`, `/sessions/<id>`, etc.) each tell a different
story in a different shape. The Run page leads with metric cards. The
Show page leads with the plan text. The Session page leads with a
branches table. The Project page is sparse — mostly counts.

This inconsistency is operational debt:

### 1. Operators relearn each page

Every page has its own visual hierarchy. The user has to figure out
where "current status" is, where "what to do next" is, and where the
raw evidence sits — separately for each entity type. The cost is not
the time per page; it's the *cognitive switching cost* between pages
during triage.

### 2. The "answer in 5 seconds" target is unmet

A well-designed entity page should answer in five seconds:

- What is this thing? (goal / purpose)
- What state is it in? (status)
- Why is it in that state? (reason — from ADR-0028)
- What's the last thing that happened? (last event)
- What action is available? (primary verb)

Today most pages bury at least three of these. The reviewer crash from
last week (`session.phantom.process_dead`) required clicking through
to the branches tab, scrolling logs, and inferring "this needs
pruning". That inference should be a button.

### 3. Action buttons drift across pages

The Run page has "Open", "Inspect". The Session page has different
verbs. The Show page has yet others. There is no shared catalogue of
actions and no shared decision about which actions are enabled in
which state. A consistent set of action descriptors — owned by the
backend, rendered by the frontend — is the simplest way to keep
action vocabulary aligned with entity state semantics.

### 4. The header pattern is the *primitive* the redesign needs

ChatGPT's frontend critique proposes redesigning every page. That is
many separate decisions. The header is a single primitive that
provides 60% of the consistency improvement and lets each page-level
redesign happen incrementally without re-litigating the structure
each time.

## Decision

Introduce a reusable `EntityHeader` frontend component, backed by a
backend-computed `header` field included in every entity *detail*
endpoint response. The backend owns the data shape and action
eligibility; the frontend owns presentation, confirmation UX, and
route transitions.

Initial rollout targets four pages: **Show, Run, Project, Session**.
Other entity pages (Play, Team, Invocation, Schedule, Agent, Playbook,
Plugin) migrate incrementally.

When ADR-0028 (status reasons) has not yet landed for a particular
entity type, the header degrades gracefully — it renders the status
pill alone without a reason tooltip. No blocker dependency.

### 1. Backend `header` field shape

Pydantic models in `apps/studio/server/schemas/entity_header.py`:

```python
from typing import Literal, Optional
from pydantic import BaseModel

# Mirrors ADR-0028's VALID_ENTITY_TYPES plus the library/config kinds
# the frontend renders headers for. Storage-backed entity kinds
# (left column) match ADR-0028 exactly; library/config kinds (right
# column) live on disk and don't participate in status_transitions.
#
# `run` is NOT in this list — /runs/<id> is a frontend route over the
# `session` entity, per ENTITY_ROUTE_ALIASES in ADR-0028.
EntityKind = Literal[
    # Storage entities (have rows, status, status_reason; see ADR-0028)
    "session", "show", "play", "invocation", "team", "schedule_run",
    # Library / config (filesystem-backed; no status_transitions)
    "project", "agent", "playbook", "plugin", "schedule",
]

# Deferred until ADR-0021 (chain_runs) lands: "chain", "chain_run".

ActionKind = Literal["primary", "secondary", "danger"]

ActionId = Literal[
    "open", "inspect", "retry", "prune", "edit", "abort",
    "open_artifacts", "open_logs", "open_workspace",
    "reassign", "snooze", "dismiss",
    "manual_trigger",              # for schedules
]

class EvidenceRef(BaseModel):
    kind: str
    id: Optional[str] = None
    path: Optional[str] = None
    ref: Optional[str] = None
    url: Optional[str] = None
    label: Optional[str] = None

class StatusReasonView(BaseModel):
    code: str
    summary: str
    evidence_refs: list[EvidenceRef] = []

class EntityAction(BaseModel):
    id: ActionId
    label: str
    kind: ActionKind = "secondary"
    method: Optional[Literal["GET", "POST", "DELETE", "PATCH"]] = None
    href: Optional[str] = None       # for navigation actions
    endpoint: Optional[str] = None   # for API actions
    requires_confirm: bool = False
    confirm_label: Optional[str] = None   # custom confirm prompt
    disabled: bool = False
    disabled_reason: Optional[str] = None

class EntityHeader(BaseModel):
    kind: EntityKind
    id: str
    title: str
    subtitle: Optional[str] = None
    goal: Optional[str] = None
    status: str
    status_taxonomy: str           # 'session', 'show', 'play', etc. — picks the StatusPill scheme from ADR-0025
    status_reason: Optional[StatusReasonView] = None
    status_source: Optional[str] = None   # e.g. shows.status_source from ADR-0011
    last_event: Optional["LastEvent"] = None
    next_action: Optional[EntityAction] = None
    owner: Optional["EntityOwner"] = None
    related: list["EntityLink"] = []
    actions: list[EntityAction] = []
    updated_at: Optional[float] = None
    created_at: Optional[float] = None

class LastEvent(BaseModel):
    summary: str
    at: float
    actor: Optional[str] = None        # session_id, user, doctor_auto, etc.
    href: Optional[str] = None

class EntityOwner(BaseModel):
    kind: Literal["agent", "user", "system"]
    id: Optional[str] = None
    label: str

class EntityLink(BaseModel):
    kind: EntityKind
    id: str
    label: str
    href: str
```

`next_action` is a duplicate pointer into `actions[]` for emphasis —
the header renders it prominently. If `next_action` is set, its `id`
must appear in `actions[]`. The duplication is deliberate: the
backend declares its judgment about the primary verb, the frontend
honors it.

### 2. Endpoint contract

Every detail endpoint includes `header` in its response:

```json
GET /api/shows/sweep

{
  "header": {
    "kind": "show",
    "id": "sweep",
    "title": "sweep",
    "subtitle": "marketplace OSS discovery",
    "goal": "Resolve 81 OSS discovery issues by merging implementations and bug fixes.",
    "status": "active",
    "status_taxonomy": "show",
    "status_reason": {
      "code": "show.blocked.no_ready_plays",
      "summary": "All 12 plays are pending; no play has its dependencies resolved.",
      "evidence_refs": [
        {"kind": "play", "id": "...", "label": "rust-cleanup"},
        {"kind": "play", "id": "...", "label": "ci-fixes"}
      ]
    },
    "last_event": {
      "summary": "Plan committed at sweep/_show.md",
      "at": 1716517000.0,
      "actor": "operator"
    },
    "next_action": {
      "id": "edit", "label": "Edit plan", "kind": "primary",
      "href": "/shows/sweep/plan/edit"
    },
    "owner": {"kind": "user", "label": "operator"},
    "related": [
      {"kind": "project", "id": "lionagi", "label": "lionagi", "href": "/projects/lionagi"}
    ],
    "actions": [
      {"id": "edit", "label": "Edit plan", "kind": "primary", "href": "..."},
      {"id": "open_workspace", "label": "Open workspace", "kind": "secondary", "href": "..."},
      {"id": "abort", "label": "Abort show", "kind": "danger", "endpoint": "/api/shows/sweep/abort", "method": "POST", "requires_confirm": true, "confirm_label": "Abort the sweep show? Pending plays will be cancelled."}
    ],
    "updated_at": 1716517300.0,
    "created_at": 1716000000.0
  },
  "plays": [...],
  ...rest of the existing detail payload
}
```

The detail payload itself is unchanged — `header` is *added* alongside
existing fields. Pages that don't yet render `EntityHeader` ignore it
at no cost.

### 3. Frontend component

```tsx
// apps/studio/frontend/components/entity/EntityHeader.tsx
// StatusPill is the existing component at apps/studio/frontend/components/StatusPill.tsx
// (default export). ADR-0028 adds a `reason` prop to it; this header
// passes the prop, NOT a tooltip child, so the API stays consistent
// with what ADR-0028 specifies and with the current component's
// prop-based API surface (see apps/studio/frontend/components/StatusPill.tsx:17).
import StatusPill from "@/components/StatusPill";
import { ActionButton } from "@/components/entity/ActionButton";
import type { EntityHeader as TEntityHeader } from "@/lib/types";

export function EntityHeader({ header }: { header: TEntityHeader }) {
  return (
    <header className="entity-header">
      <div className="entity-header__title-row">
        <h1>{header.title}</h1>
        <StatusPill
          taxonomy={header.status_taxonomy}
          value={header.status}
          reason={header.status_reason ?? undefined}
        />
        {header.next_action && (
          <ActionButton action={header.next_action} prominent />
        )}
      </div>

      {header.goal && <p className="entity-header__goal">{header.goal}</p>}

      <div className="entity-header__meta">
        {header.last_event && <LastEventLine event={header.last_event} />}
        {header.owner && <OwnerChip owner={header.owner} />}
        {header.related.map((link) => (
          <RelatedChip key={link.id} link={link} />
        ))}
      </div>

      <div className="entity-header__actions">
        {header.actions
          .filter((a) => a.id !== header.next_action?.id)
          .map((action) => (
            <ActionButton key={action.id} action={action} />
          ))}
      </div>
    </header>
  );
}
```

The `ActionButton` component (reused from ADR-0030's Attention Queue
items) handles:

- `href`-only actions → `<a>` / Next.js `Link`
- `endpoint` + `method` actions → fetch + toast + refresh
- `requires_confirm` → modal with `confirm_label`
- `disabled` → grayed out with tooltip showing `disabled_reason`

### 4. Action eligibility — backend decides

Backend computes `actions[]` from entity state. Examples:

```python
def compute_show_actions(show: ShowRow) -> list[EntityAction]:
    actions = [
        EntityAction(id="edit", label="Edit plan", kind="primary",
                     href=f"/shows/{show.topic}/plan/edit"),
        EntityAction(id="open_workspace", label="Open workspace",
                     href=f"/shows/{show.topic}/workspace"),
    ]
    if show.status == "active":
        actions.append(EntityAction(
            id="abort", label="Abort show", kind="danger",
            endpoint=f"/api/shows/{show.topic}/abort", method="POST",
            requires_confirm=True,
            confirm_label="Abort the show? Pending plays will be cancelled.",
        ))
    return actions

def compute_session_actions(s: SessionRow) -> list[EntityAction]:
    actions = [EntityAction(id="open", label="Open run",
                            kind="primary", href=f"/runs/{s.id}")]
    if s.status == "failed" and s.invocation_kind in ("play", "agent"):
        actions.append(EntityAction(
            id="retry", label="Retry", kind="secondary",
            endpoint=f"/api/runs/{s.id}/retry", method="POST",
        ))
    if s.status_reason_code and s.status_reason_code.startswith("session.phantom"):
        actions.append(EntityAction(
            id="prune", label="Prune", kind="danger",
            endpoint=f"/api/admin/sessions/{s.id}", method="DELETE",
            requires_confirm=True,
            confirm_label="Prune this phantom session? This deletes the session row and any associated artifacts directory.",
        ))
    return actions
```

The frontend never decides "should this entity show a Retry button" —
it just renders what the backend gave it. This keeps the
state-to-action mapping in one place.

### 5. Graceful degradation when ADR-0028 not landed

If a backend service has not yet been updated to populate
`status_reason`, the header passes `reason={undefined}` to
`<StatusPill>`. ADR-0028 specifies that the pill renders unchanged
when `reason` is undefined (no tooltip affordance, no popover). No
errors, no broken UI.

Per-entity rollout order:

1. ADR-0028 columns added (schema migration)
2. CLI / executor writes reasons for one entity type (say, sessions)
3. Session detail endpoint includes `status_reason` in its header
4. Repeat for next entity type

Pages migrate to `EntityHeader` independently of reason rollout. The
header is useful even with `status_reason = None`.

### 6. Initial rollout: 4 pages

| Page | Existing struct | Why first |
|---|---|---|
| **Run detail** (`/runs/<id>`) | Metric cards + tabs | Most-visited; current page hides "why this failed" |
| **Show detail** (`/shows/<topic>`) | Plan text + plays table | Needs the goal + next-action clarity most |
| **Session detail** (`/sessions/<id>`) | Branches table | Phantom diagnostics need reason + prune action in the header |
| **Project detail** (`/projects/<name>`) | Sparse counts | Currently weakest page; header alone gives it shape |

Second iteration: Play, Team, Invocation. Third iteration: Agent,
Playbook, Plugin, Schedule (library + admin items).

### 7. File map

New files:

```text
apps/studio/server/schemas/entity_header.py     # Pydantic models
apps/studio/server/services/entity_header.py    # compute_*_header() per entity
apps/studio/frontend/components/entity/EntityHeader.tsx
apps/studio/frontend/components/entity/ActionButton.tsx
apps/studio/frontend/components/entity/EntityOwnerChip.tsx
apps/studio/frontend/components/entity/EntityRelatedChip.tsx
apps/studio/frontend/lib/types/entity_header.ts   # mirror Pydantic via TS interfaces
```

Modified files:

```text
apps/studio/server/routers/runs.py              # include header on detail
apps/studio/server/routers/shows.py             # include header on detail
apps/studio/server/routers/sessions.py          # include header on detail
apps/studio/server/routers/projects.py          # include header on detail
apps/studio/frontend/components/StatusPill.tsx  # add `reason` prop (per ADR-0028)
apps/studio/frontend/app/runs/[id]/page.tsx     # mount EntityHeader
apps/studio/frontend/app/shows/[topic]/page.tsx # mount EntityHeader
apps/studio/frontend/app/sessions/[id]/page.tsx # mount EntityHeader
apps/studio/frontend/app/projects/[name]/page.tsx # mount EntityHeader
```

## Consequences

**Positive**

- Every adopted page answers "what / status / why / last / next /
  evidence" in the same place, with the same component, styled the
  same way.

- Backend-computed `actions[]` keeps state-to-action eligibility in
  one place per entity, not duplicated across pages.

- `ActionButton` and `EntityAction` are reused by ADR-0030's Attention
  Queue — one descriptor shape, two consumers.

- Migration is per-page, not big-bang. Pages without the header keep
  working; pages with the header look consistent.

- Graceful degradation when ADR-0028 hasn't reached a given entity
  type — the header renders the status pill alone. No blocker
  dependency.

- Frontend stays a renderer. Adding a new entity-type page in the
  future means writing one `compute_<entity>_header()` function and
  mounting `<EntityHeader />`.

**Negative**

- Adds a backend-computed payload to every detail response. For the
  initial 4 endpoints this is a few hundred bytes; negligible.

- Pages in transition will look inconsistent — half have the header,
  half don't. Acceptable for a few weeks of rollout.

- The `EntityAction` enum (`ActionId`) is a closed set in v1.
  Adding a new action verb is a backend + frontend change. Trade-off:
  open string would mean drift. Closed set keeps the action vocabulary
  curated.

- Backend has to know about routes (`href: "/runs/<id>"`). This
  couples server logic to frontend URL structure. The mitigation: a
  single `apps/studio/server/services/routes.py` module that owns the
  route mapping, used by every `compute_*_header()`.

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| Compute the header entirely client-side from existing fields | Duplicates business logic. The frontend would have to know what `status='gate_failed' AND attempt=2` means and which actions to offer — that's domain semantics, not presentation. |
| New `GET /api/entities/<kind>/<id>/header` endpoint | Forces two round trips per page (header + detail). Embedding `header` in the existing detail endpoint costs nothing extra and removes the loading-state coordination problem. |
| Hardcode action buttons per page component | What we have today. Drift is inevitable. The whole reason for this ADR. |
| Redesign every entity page in one iteration | Too much surface in one PR; couples the primitive's design to every page's design simultaneously. Per-page rollout is safer. |
| Use a generic CRUD framework (e.g., react-admin) | Wrong shape — Studio's entities are not CRUD records, they are operational state. The header is curated, not generated from a schema. |
| Server-rendered HTML for the header (skip the API field) | Mixes server-rendered HTML into a Next.js client-rendered app. Would need a separate render path and break SSR consistency. |
| Skip the `next_action` duplication, derive in frontend ("first primary action wins") | Loses backend's ability to pick a *contextual* primary action that isn't always position-0. Cheap to keep both. |

## Non-Goals

- **No role-specific layouts.** Studio is single-user (ADR-0008). When
  multi-user becomes a goal, role-specific defaults can plug into the
  same `EntityHeader` via props.

- **No general redesign of entity pages.** This ADR adds a header, not
  a page redesign. Tabs, tables, log panels, raw markdown viewers all
  stay where they are.

- **No hiding of raw internals.** The header sits on top; existing
  internals stay accessible below it. Studio's users are technical;
  hiding state is the wrong move.

- **No header on list pages.** `EntityHeader` is for detail pages. List
  rows have their own (much smaller) summary chip rendered by a
  separate component.

- **No theming / customization of action verbs by users.** Action
  vocabulary is fixed; localization is out of scope for v1.

- **No undo for executed actions.** Confirmations are the only safety
  net. Undo is a separate, larger design.

- **No keyboard shortcuts on headers in v1.** Command palette /
  shortcuts are deferred per ADR-0032's non-goal list.

## References

- [ADR-0025](ADR-0025-session-status-vocabulary.md) — `StatusPill` component (reused via `status_taxonomy`).
- [ADR-0028](ADR-0028-status-reason-model.md) — `status_reason` field surfaced in tooltip / popover.
- [ADR-0030](ADR-0030-attention-queue.md) — Reuses `EntityAction` shape and `ActionButton` component.
- [ADR-0024](ADR-0024-session-health-and-admin-surface.md) — Phantom session reasons feed Session header actions (Prune).
- `apps/studio/frontend/components/StatusPill.tsx` — Existing pill component (default export; this ADR + ADR-0028 add a `reason` prop).
- ChatGPT frontend design review (external) — proposed an entity header on every page; this ADR scopes the rollout to four pages first, defers the rest, and pins the `EntityAction` shape so the Attention Queue and entity headers share one descriptor.

### Prior art

- **Stripe Dashboard entity headers** — every object (Customer,
  Payment, Subscription) has a top-of-page header with object id,
  status pill, primary action button, last event line, related links.
  The pattern is so consistent that operators can scan an unfamiliar
  object type and orient in seconds. Direct visual influence.

- **GitHub Issue header** — title + status pill (open/closed/merged)
  - primary action (close/reopen) + assignees + labels — same shape,
  same intent. The header is the page; everything below is evidence.

- **Linear ticket detail** — header with title, status, owner,
  related cycle/project links, then the body below. Same model.
