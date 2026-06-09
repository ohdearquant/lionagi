# ADR-0078: The Casts Conceptual Model and Module Coherence

**Status**: Accepted — implemented
**Date**: 2026-06-09

## Context

Nine modules were added to lionagi over a short period — `casts`, `agent`,
`engines`, `orchestration`, `outcomes`, `hooks`, `state`, `studio`, `cli` —
without a written statement of how they relate to the established core
(`protocols`, `ln`, `service`, `session`, `operations`, `libs`) or to each
other. A five-module survey plus a verified debt inventory (35 items) found
the recurring failure mode was not bad code but **missing normative
boundaries**: duplicated construction stacks, two config surfaces for one
concept, name collisions across planes, and upward imports from lower layers.

Two clarifications resolve most of it.

## Decision 1: the casts conceptual model is normative

The vocabulary, as designed:

- **Pattern** — the composable atom of agent configuration. A frozen value
  object (`casts.pattern.Pattern`).
- **Role** and **Mode** — *special patterns* that carry specific material:
  a Role carries behavioral body and an emission contract (`emits`); a Mode
  carries cognitive behaviors and conflict declarations. `PatternKind` is the
  taxonomy discriminator for exactly this specialness — it is declarative
  vocabulary, not dead code.
- **Profile** — a named composition of patterns *plus persistence*: one Role,
  ordered Modes, conflict-validated, YAML round-trip (`from_yaml`/`to_yaml`).
  Identity lives here and only here.
- **Agent** — a **runtime concept**, never persisted. `AgentSpec` = Profile +
  runtime concerns (model, effort, tools, permissions, capability grants,
  hooks, cwd, MCP). `create_agent(spec)` is the **only** construction site
  that turns a spec into a live `Branch`, and the only site that grants
  emission capabilities.

Consequences applied:

- `AgentSpec.emits: tuple | None` — `None` grants the Role's declared
  contract; a tuple overrides it (engines grant stage-specific events); `()`
  grants nothing. The engine layer's post-construction re-grant is deleted;
  one grant source.
- `AgentConfig` (a persisted "agent" predating the model) is deprecated by
  delegation: its system-message composition delegates to `Profile`, its
  secure-guard wiring shares one helper with `AgentSpec`, its YAML round-trip
  warns `DeprecationWarning`.
- `casts/__init__.py` exports a curated surface; the previously-leaking
  private symbols are public (`field_name_for`, `SPAWN_ALLOWED_OPERATIONS`).

## Decision 2: two planes, meeting only at persisted state

```text
agent-model plane (reactive)            ops plane (persisted)
ln / protocols / service / session      cli  →  state (StateDB)  ←  studio
        ↓                                            ↑ hooks
casts (patterns) → agent (runtime)      SSE, artifacts, run rows
        ↓
engines / orchestration / operations
```

- The planes communicate **only** through `StateDB` and exported artifacts.
  `studio` imports nothing from `casts`/`agent`/`engines`; `casts`/`agent`
  import nothing from `state`/`studio`/`hooks`. This separation is deliberate
  and load-bearing — do not "unify" it.
- Shared foundations live **below both planes**: provider/model-spec tables
  moved from `cli/_providers.py` to `service/providers.py`; filesystem layout
  constants moved from `cli/_runs.py`/`cli/_agents.py` to `lionagi/_paths.py`.
  `cli` keeps re-export shims. Rule: **lower layers never import from `cli`.**
- Vocabulary is plane-scoped. `outcomes` (the ops-plane artifact contract,
  ADR-0021) renamed its classes that collided with the reactive plane's
  emission vocabulary: `Finding` → `ReviewFinding`, `ReviewVerdict` →
  `ReviewOutcome`. The casts emission types and their engine subclasses keep
  the short names — the bus owns them.
- One construction stack: the CLI orchestrator and casts-role workers compose
  `AgentSpec` and call `create_agent` rather than building `Branch` directly,
  so factory wiring (system-message assembly, MCP config, future guards)
  applies uniformly. `create_agent` gained additive `chat_model=` and
  `log_config=` passthroughs so the CLI's richer iModel construction injects
  at the factory rather than around it. Workers keep their exact capability
  surface via `grant_spawn` (`grant_emissions=False`); CLI runs use
  `load_settings=False`. Verbatim-prompt workers (explicit `system_prompt`,
  `--bare`) have no Role to compose and retain direct `Branch` construction
  by design.

## Decision 3: engines complete the ADR-0077 retrofit

- `operate_with_repair` now covers `research` exploration and `review`
  dimension/verify stages, not just `hypothesis`. Terminal text-consumed
  stages (synthesis, verdict) deliberately stay unrepaired.
- `EngineEvent` is `extra="forbid"`, matching its casts twins — one
  strictness rule on the whole bus.

## Correction to ADR-0075

ADR-0075 anticipated `pile[filter]` sugar replacing observer-level queries.
`pile[Type]` landed (`protocols/generic/pile.py`), but engines intentionally
continue to use `EngineRun.by_type` — agent emissions arrive as `Signal`
envelopes carrying capability bundles, and unwrapping them is observer-layer
knowledge `Pile` deliberately does not have (ADR-0077 §1). The "by_type
collapses to `flow.items[T]`" expectation is withdrawn.

## What was deliberately NOT unified

| Candidate | Verdict |
|-----------|---------|
| `orchestration/` into `engines/` | Deferred — thin shared glue with one-directional edges; folding is a rename, not a need. |
| `cli/orchestrate/flow.py` onto `PlanningEngine` | Deferred — flow re-drives plan/DAG/synthesis itself and only delegates `run_dag`; consolidation is high-blast-radius (`li play` is the daily driver) and needs its own pass with a live smoke. Budget/judge/repair do not cover the CLI path until then. |
| `hooks/` (1 of 11 points wired) | Forward deployment per ADR-0023b/c — wiring is roadmap, not debt. |
| `outcomes/` producers | Forward deployment per ADR-0021 — the CLI skill runner lands separately. |
| `state/` vs `protocols.generic` | Parallel by design: ops-plane SQLite rows vs in-process substrate. Documented, not merged. |

## Consequences

**Positive**: one construction stack, one grant source, one config surface
per concept, no upward imports into `cli`, plane-distinct vocabulary, repair
coverage everywhere weak workers run, and a studio run-detail view that
actually returns data (it read a manifest file nothing ever wrote).

**Negative**: `cli/_providers.py` and `cli/_runs.py` carry re-export shims
until external callers migrate; `AgentConfig` lingers deprecated; the
flow-onto-PlanningEngine consolidation remains open debt with a written
deferral above.

## References

- ADR-0072 (reactive capability bus), ADR-0074 (role composition and packs),
  ADR-0075 (engines), ADR-0077 (autonomy protections), ADR-0021 (artifact
  outcomes).
- Survey + debt inventory + design doc:
  `khive .khive/workspaces/20260909/lionagi-coherence/` (`survey/A–E`,
  `DEBT_INVENTORY.md`, `DESIGN.md`).
