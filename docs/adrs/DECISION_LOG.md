# Lion Studio Decision Log

Lightweight decisions that don't warrant a full ADR. Full ADRs reserved for
changes to: data authority, execution identity, persistence/schema, CLI-vs-Studio
boundary, plugin/editability semantics, transport/runtime, major UX architecture.

---

## 2026-05-20 — Runs page pagination default is 100

Decision: Use 100/page for runs list.
Why: 376 sessions currently; 100 keeps render cost low without excessive paging.
Revisit when: sessions exceed 2,000 or list render exceeds noticeable latency.

## 2026-05-20 — Plugin source badges show marketplace name, not "third-party"

Decision: Display actual marketplace directory name (e.g., `Anthropic Official`,
`khive`) instead of generic "third-party" label. Raw slug in tooltip.
Why: Multiple third-party sources exist; generic label hides provenance.
Display mapping: `marketplace` → `Lion Marketplace`, `claude-plugins-official` →
`Anthropic Official`, others → title-cased directory name.

## 2026-05-20 — _show.md always collapsed on shows detail

Decision: `_show.md` content collapsed by default on all shows (active, completed,
imported). No auto-open.
Why: Plays table + DAG + accordion are the primary surfaces. Two-column layout
with open _show.md cramps the play accordion.
Previous: auto-opened for active/running shows. Reverted after design review.

## 2026-05-20 — Toast system is custom, not library

Decision: Build custom Toast component (~155 LOC) instead of adopting
react-hot-toast, sonner, or similar.
Why: Zero component-library pattern (ADR-0013). Toast is simple enough that
a custom implementation is cleaner than adding a dependency.
Revisit when: ADR-0013's threshold (3+ complex primitives needed) is met.

## 2026-05-20 — Errors section renamed to "Tool errors"

Decision: Run detail section labeled "Tool errors", not "Errors".
Why: "Errors" implies session failure. "Tool errors" communicates that these are
intermediate tool-level failures, not session-level outcomes. Paired with
"intermediate tool errors" labeling on the Overview.
