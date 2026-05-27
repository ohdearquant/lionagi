# ADR-0035: Design System and Component Library

**Status**: Proposed
**Date**: 2026-05-26
**Supersedes**: [ADR-0013](ADR-0013-zero-dependency-ui.md) (zero-component-library UI) — for product surfaces
**Depends on**: [ADR-0033](ADR-0033-unified-entity-state-model.md) (renders NormalizedState)
**Related**: [ADR-0031](ADR-0031-entity-header-pattern.md), [ADR-0034](ADR-0034-frontend-data-and-state-architecture.md), [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md)

## Context

ADR-0013 chose zero external component dependencies: no Radix, no
shadcn, no headless UI. The rationale was that Studio is a power-user
tool with one primary user, and custom components keep bundle size
minimal and control total.

That assumption no longer holds. The frontend now requires:

- **Dialogs** for confirmation actions (ADR-0031 entity actions)
- **Dropdown menus** for row actions in tables
- **Popovers** for filter chips and column pickers
- **Comboboxes** for project scope selection and search
- **Tooltips** for status badges and graph nodes
- **Command palette** for keyboard-first navigation
- **Focus traps** for modals and drawers
- **Roving tabindex** for table row navigation

Each of these requires correct ARIA roles, keyboard handling, focus
management, and screen reader announcements. Building them from scratch
is a multi-week effort that produces worse accessibility than existing
headless libraries designed specifically for this purpose.

ADR-0013 itself noted: "This trade-off would not be acceptable for a
public-facing product." The product direction has now changed — Studio
is becoming the operational interface, not just Ocean's local tool.

## Decision

### Component architecture

```text
components/ui/      — generic primitives (shadcn/ui source-owned)
components/lion/    — domain-aware design system
features/*/         — product surfaces (import from lion/, never from Radix directly)
```

**This ADR is the canonical owner of `components/lion/`**. Future feature ADRs that introduce new domain components MUST amend this ADR's component list (PR against §Domain components below). The catalog must remain discoverable in one place; scattering definitions across feature ADRs is the failure mode this rule prevents.

### Primitive layer: shadcn/ui

Use shadcn/ui as source-owned primitives. shadcn is not a dependency —
it generates component source files that we own and modify. The
underlying accessibility primitives come from Radix.

Adopted primitives:

| Primitive | Source | Use case |
|-----------|--------|----------|
| Button | shadcn | All buttons |
| Badge | shadcn | Metadata labels (model, source, version) |
| Dialog | shadcn/Radix | Confirmation actions, entity details |
| DropdownMenu | shadcn/Radix | Row actions, context menus |
| Popover | shadcn/Radix | Filter chips, column picker |
| Tooltip | shadcn/Radix | Status explanations, graph node details |
| Command | shadcn/Radix | Cmd/Ctrl-K command palette |
| Tabs | shadcn/Radix | Inspector panels, detail page sections |
| Sheet | shadcn/Radix | Mobile sidebar, inspector drawer |

NOT adopted (use existing custom):

| Component | Reason |
|-----------|--------|
| Table | TanStack Table + custom markup for `LionDataTable` |
| Form inputs | Existing custom inputs are sufficient for current scope |
| Toast | Existing Toast provider works; migrate later if needed |

### Domain components: `components/lion/`

These encode Lion Studio's operational semantics:

```text
lion/
  status-badge.tsx        — single status with tone + icon
  state-stack.tsx          — compound state: "Failed · Infra OK · Trace present"
  severity-indicator.tsx   — left-border severity accent
  data-table/
    lion-data-table.tsx    — TanStack Table wrapper with toolbar, filters, URL sync
    table-toolbar.tsx      — search, filter chips, column picker, density toggle
    table-pagination.tsx   — cursor pagination
  object-header.tsx        — universal detail page header (ADR-0031)
  object-lineage.tsx       — clickable breadcrumb chips
  attention-item.tsx       — attention queue row
  data-freshness-badge.tsx — "Live · verified 8s ago"
  section-boundary.tsx     — per-section error boundary
  connection-banner.tsx    — SSE disconnect indicator
  knowledge/
    knowledge-lens.tsx          — contextual claim listing for an entity scope
    claim-card.tsx              — single claim with status, confidence, evidence count
    claim-status-badge.tsx      — observed | inferred | hypothesis | verified | disputed | superseded
    evidence-trail.tsx          — expandable list of EvidenceRefs with source links
    confidence-meter.tsx        — visual confidence indicator (0.0–1.0)
```

The `knowledge/*` components implement the behavioral spec from [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md) §"Product Implications". This ADR owns the catalog and rendering rules; ADR-0039 owns the data model and behavioral semantics.

Rule: No feature component imports Radix directly. If a shared
primitive doesn't exist yet, add it to `components/ui/` first.

### Table engine: TanStack Table

TanStack Table provides headless table logic (sorting, filtering,
pagination, column visibility, row selection). Lion Studio provides
the markup and Tailwind styling via `LionDataTable`.

Relationship to shadcn's DataTable:

- shadcn's `Table` component provides markup primitives (`<Table>`,
  `<TableHeader>`, `<TableRow>`, `<TableCell>`)
- `LionDataTable` wraps TanStack Table logic + shadcn Table markup +
  Lion-specific toolbar, URL sync, density modes, and row actions

### Status badge contract

Generic `Badge` (shadcn) is for metadata labels: `codex/gpt-5.5`,
`local`, `v1.0.0`. Never for operational status.

Operational status uses domain components:

```tsx
<StatusBadge tone="danger" icon={XCircle}>Failed</StatusBadge>

<StateStack
  outcome="failed"
  health="ok"
  delivery="present"
/>
// Renders: "Failed · Infra OK · Trace present"
```

Status rendering rules:

- Never use color alone — always icon + text + color
- Dark mode does not change status semantics
- Critical/warning states always have icons
- Reduced motion disables pulse/spin animations

### Semantic color tokens

Extend existing CSS custom properties (which already exist in
`globals.css`) to use the `--ls-` prefix for the severity system:

```css
:root {
  --ls-critical-bg: ...;
  --ls-critical-border: ...;
  --ls-critical-text: ...;
  --ls-critical-accent: ...;
  /* warning, info, success, neutral variants */
  --ls-focus-ring: ...;
}
.dark {
  /* dark variants of all above */
}
```

Components reference semantic tokens, not raw Tailwind colors:

```text
Good: bg-[var(--ls-critical-bg)]
Bad:  bg-red-50
```

**Prefix decision**: `--ls-` is the canonical design-token namespace. Lion Studio uses these directly. Custom themes may override values in theme-specific CSS files. Token NAMES stay stable across themes; only their VALUES vary.

The existing design token system in `globals.css` already uses CSS
custom properties with dark mode via `.dark` class. This ADR extends
that system with the severity-specific tokens, not replaces it.

### Semantic palette: status colors

The severity → tone mapping in [ADR-0033](ADR-0033-unified-entity-state-model.md) determines color category. The concrete color values are defined here as the canonical palette:

| Tone | Light mode | Dark mode | Usage |
|------|-----------|-----------|-------|
| `danger` | red-700 / red-100 bg | red-300 / red-950 bg | failed, aborted, dead, missing |
| `warning` | amber-700 / amber-100 bg | amber-300 / amber-950 bg | timed_out, partial, stalled, disputed |
| `info` | blue-700 / blue-100 bg | blue-300 / blue-950 bg | running, due, observed |
| `success` | green-700 / green-100 bg | green-300 / green-950 bg | succeeded, completed, merged, verified |
| `neutral` | gray-700 / gray-100 bg | gray-300 / gray-900 bg | cancelled, skipped, unknown, hypothesis |

These map directly to CSS custom properties: `--ls-danger-text`, `--ls-danger-bg`, `--ls-warning-text`, etc. Components NEVER reference raw Tailwind color classes for status; they reference these semantic tokens.

**Knowledge-specific tones** follow the same palette:

- `observed` → info (assumes clean evidence)
- `inferred` → info (with reduced opacity to signal lower certainty)
- `hypothesis` → neutral (typed honestly, low confidence)
- `verified` → success
- `disputed` → warning
- `superseded` → neutral (archived, not failed)

This palette is the source of truth. Issues #1178, #1179 (filter chip colors, graph node colors) MUST consume from this palette, not invent local colors.

### Dark mode

Already supported via `darkMode: "class"` in `tailwind.config.ts`.
Extend with:

- Pre-hydration inline script to apply `.dark` before React renders
  (prevents flash of wrong theme)
- Theme preference: `light | dark | system`, stored in localStorage
- System preference via `prefers-color-scheme` media query
- Theme toggle in nav showing resolved state: "System · Dark"

### Accessibility target: WCAG 2.2 AA

Non-negotiable rules:

- Never use color alone to communicate state (every status has icon + text + color)
- Focus state visible on every interactive element
- Keyboard can reach every action
- Tables use semantic `<table>` markup with `aria-sort`
- Live updates use `aria-live` (assertive for critical, polite for info)
- Graph has keyboard navigation AND table fallback
- Reduced motion: `prefers-reduced-motion` disables all animations

**Verification strategy** (addresses issue #1020 — 47 a11y findings):

| Layer | Tool | Gate |
|-------|------|------|
| Static (CI) | `eslint-plugin-jsx-a11y` | Block merge on `error` level |
| Component (CI) | `@testing-library/jest-dom` + `jest-axe` | Each `components/lion/*` has an a11y test |
| Page (manual + CI) | Lighthouse / Axe DevTools | Score ≥95 on Dashboard, Runs, Show detail |
| Live | Manual screen reader sweep (VoiceOver, NVDA) | Quarterly, owned by frontend lead |

Component-level a11y test pattern:

```typescript
import { axe } from "jest-axe";

it("StatusBadge has no axe violations", async () => {
  const { container } = render(<StatusBadge tone="danger" icon={XCircle}>Failed</StatusBadge>);
  expect(await axe(container)).toHaveNoViolations();
});
```

Every component in `components/lion/` MUST ship with an a11y test. Components in `components/ui/` (shadcn primitives) inherit Radix's verified a11y but get smoke tests for our customizations.

### Keyboard-first interaction

Scoped shortcut system with priority ordering:

```text
Dialog/modal > Editor/input > Graph > Table > Page > Global
```

Core shortcuts (ship in v1):

| Shortcut | Action |
|----------|--------|
| `Cmd/Ctrl-K` | Command palette |
| `?` | Keyboard help |
| `/` | Focus search |
| `g d` | Go to Dashboard |
| `g r` | Go to Runs |
| `g s` | Go to Shows |
| `j/k` | Table row navigation |
| `Enter` | Open selected |
| `Esc` | Clear selection / close overlay |

Shortcut rules:

- No printable shortcuts fire inside input/textarea/contenteditable
- `Cmd/Ctrl-K` always opens command palette unless modal captures it
- `Escape` closes innermost overlay

### Error boundaries

Per-section on dashboard and detail pages. Page-level only when
primary entity cannot load.

```text
System health fails but runs load:
  → SystemHealthPanel shows inline error
  → AttentionQueue adds "System health unavailable" item
  → Rest of dashboard renders normally

All API calls fail:
  → Full-page "Backend unreachable" with last cached timestamp
```

### Loading states

Section-level skeletons. Never blank an entire page during background
refresh. Keep last known data with "verifying..." freshness indicator
during revalidation.

## Consequences

**Positive**

- Correct accessibility from day one via Radix primitives
- Keyboard-first interaction for operator efficiency
- Consistent status rendering across all surfaces via domain components
- Source-owned components (shadcn) — no locked dependencies
- Dark mode with no flash

**Negative**

- Radix primitives add ~15-25KB (gzipped). Combined with [ADR-0034](ADR-0034-frontend-data-and-state-architecture.md)'s TanStack Query (~40KB) + Zustand (~2KB), total new bundle weight is ~60KB gzipped.
- Migration effort: existing custom components need gradual replacement
- [ADR-0013](ADR-0013-zero-dependency-ui.md)'s "zero dependency" principle is explicitly abandoned for product surfaces. Preserved as a constraint for any future ultra-light embed mode.
- Catalog ownership concentrated in this ADR — feature ADRs MUST amend rather than fork. This creates merge contention in component-heavy phases; mitigated by clear catalog entries and small per-component PRs.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Continue zero-dependency (ADR-0013) | Accessibility debt grows with every new interactive component; ADR-0013 itself flagged this |
| MUI / Ant Design / Chakra | Opinionated styling conflicts with existing Tailwind design system; large bundle |
| Headless UI (Tailwind Labs) | Fewer primitives than Radix; no command palette; less active maintenance |
| Build everything custom with correct ARIA | Multi-week effort per component; will produce worse a11y than battle-tested Radix |

## References

- [ADR-0013](ADR-0013-zero-dependency-ui.md) — Zero Component-Library UI (superseded by this ADR for product surfaces)
- [ADR-0031](ADR-0031-entity-header-pattern.md) — Entity Header Pattern (consumes design system)
- [ADR-0033](ADR-0033-unified-entity-state-model.md) — Unified Entity State Model (status/severity/tone semantics)
- [ADR-0034](ADR-0034-frontend-data-and-state-architecture.md) — Frontend Data & State Architecture
- [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md) — Knowledge Substrate (claim/evidence components)
- shadcn/ui documentation
- Radix Primitives documentation
- WCAG 2.2 — Contrast (Minimum), Focus Visible, Target Size
- TanStack Table documentation
- `eslint-plugin-jsx-a11y`, `jest-axe` — accessibility CI tooling
- Issue #1020 — a11y baseline (driven by verification strategy above)
- Issue #1168, #1178, #1179 — design system polish addressed via semantic palette
