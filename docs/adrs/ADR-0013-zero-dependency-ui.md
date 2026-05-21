# ADR-0013: Zero Component-Library UI

**Status**: Accepted
**Date**: 2026-05-20

## Context

Lion Studio's frontend uses Next.js 16 + React 19 + TypeScript + Tailwind CSS
with no UI component library (no Radix, no shadcn, no Headless UI). All interactive
components — Badge, StatusPill, Button, Toast, tabs, accordions, two-pane
layouts, definition editors — are custom-built with Tailwind.

Note: the app does use **visualization libraries** (ReactFlow for PlayDag and
WorkerCanvas, dagre for layout). "Zero component-library" means no general-purpose
UI primitive library, not zero frontend dependencies.

This pattern emerged organically: the first components were simple enough that
importing a library was unnecessary, and subsequent components followed the
established style. The question arises with each new UI primitive: should we
adopt a headless component library (Radix, Headless UI, Ark UI) or continue
building custom?

The decision was explicitly tested during the ADR-0012 design review. The toast
system (~100 LOC custom) was the first component where a library (react-hot-toast,
sonner) would have been materially simpler. We chose custom.

## Decision

**Continue building custom components. No UI component library.**

The threshold for reconsidering: when the app needs **3+ of the following
simultaneously**, adopting a headless primitive library becomes justified:

1. Modal dialogs (not just toasts — actual blocking dialogs with focus trap)
2. Popovers with positioning logic (dropdown menus, tooltips with arrows)
3. Command palette / combobox with keyboard navigation and fuzzy search
4. Multi-select / autocomplete inputs
5. Accessible disclosure (tabs, accordions) with ARIA state management

Currently the app uses custom tabs (plugin detail) and collapsible accordions (play details, branch sections) that work without library-grade ARIA state management. These existing components do not count toward the threshold because they are simple show/hide toggles without focus-trap, arrow-key navigation, or roving tabindex requirements. The threshold counts complex primitives that genuinely need a headless library's accessibility infrastructure. The command palette (deferred to a later phase in ADR-0012) would be the trigger — when it ships, evaluate whether adopting Radix or similar is warranted.

### What "custom" means in practice

- Layout primitives: flexbox/grid via Tailwind utility classes.
- Interactive components: React state + event handlers + Tailwind transitions.
- Theming: CSS custom properties in `globals.css`, class-based dark/light toggle.
- Accessibility: manual ARIA attributes where needed (not systematically audited).
- Animation: CSS transitions and Tailwind `animate-*` utilities. No Framer Motion.

### Approved exceptions — content-rendering primitives

Certain rendering tasks involve enough complexity that a hand-rolled implementation would
duplicate significant library work without adding value. The threshold for approving a
dependency as an exception to the zero-library rule is: the primitive simultaneously
requires async parsing, a well-specified extension grammar, and tight React reconciliation
integration — characteristics that individually justify custom code but together define a
rendering pipeline.

**`react-markdown` + `remark-gfm`** are approved as the markdown rendering stack.
Markdown rendering clears the exception threshold for the following reasons:

1. **GFM extension grammar** — tables, task lists, autolinks, and strikethrough each have
   their own tokenizer rules. Implementing even a subset of GFM correctly is a non-trivial
   parser project.
2. **React reconciliation** — naive innerHTML injection bypasses React's tree; a React-aware
   renderer is required for safe, diffable markdown output inside component trees.
3. **Async parsing pipeline** — the remark/rehype AST pipeline enables safe HTML sanitization,
   lazy plugin loading, and future extension (e.g., syntax highlighting) without rewriting the
   renderer.

`react-markdown` + `remark-gfm` may be used wherever markdown rendering is genuinely needed:
plan documents, agent/playbook descriptions, plugin manifests, show `_show.md` content, and
session summaries. They are not a general escape hatch — UI layout, interactive components, and
data display still follow the zero-library rule.

### What we accept as trade-offs

- Accessibility coverage is best-effort, not systematic. This is acceptable for
  a power-user tool with one primary user. It would not be acceptable for a
  public-facing product.
- Each new primitive costs ~50-150 LOC of implementation. At the current pace
  (~1 new component per design phase), this is sustainable.
- Focus management, portal rendering, and scroll locking are done ad-hoc per
  component. If multiple components need these, a shared utility layer is
  preferable to a full library adoption.

## Consequences

**Positive**
- Zero dependency means zero upgrade churn, zero breaking changes from upstream,
  zero style conflicts or specificity wars.
- Every component matches the design system exactly — no overriding library
  defaults or fighting opinionated styling.
- Bundle size stays minimal. No tree-shaking concerns.
- Full control over behavior: the toast system dismisses exactly when and how
  we want, the status pills use exactly the tokens we define.

**Negative**
- Accessibility is incomplete. A screen reader user would have a degraded
  experience on some interactive elements.
- Some patterns (focus trap, click-outside-to-close, scroll lock on overlay)
  get reimplemented per component instead of shared.
- New contributors must learn the custom component patterns rather than
  referring to a library's documentation.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|-------------|
| Radix UI | Best headless option, but adds 10+ packages for primitives we don't need yet. Evaluate at command palette time. |
| shadcn/ui | Copy-paste model is appealing but brings Radix as a dependency and imposes its own abstraction layer on top of Tailwind. |
| Headless UI (Tailwind Labs) | Smaller surface area than Radix but less complete. Same "not needed yet" argument applies. |
| React Aria (Adobe) | Excellent accessibility but heavyweight and opinionated about state management. Overkill for this app. |
