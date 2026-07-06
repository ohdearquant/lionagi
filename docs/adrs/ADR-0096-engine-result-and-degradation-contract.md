# ADR-0096: Engine Result & Degradation Contract

**Status**: Proposed (advisor-hardened) — pending λ:leo spec gate + Fable sign-off before dependent impl
**Date**: 2026-07-06
**Scope**: `lionagi/engines/` — the return value and failure semantics of `Engine.run()`. Extends ADR-0075 (engine design) and ADR-0077 (autonomy protections); does not relitigate them.
**Authored by**: `advisor` subagent (engine: Opus 4.8 xhigh) from packet `.khive/workspaces/20260706/engine-adr/PACKET.md`. All code cites re-verified at source at main `a38add627`.

---

## Context

`lionagi/engines/` is the "safe front door" over the raw reactive `session.flow` substrate: five reusable orchestration shapes (`PlanningEngine`, `ResearchEngine`, `ReviewEngine`, `HypothesisEngine`, `CodingEngine`) over a stateless `Engine` base + per-run `EngineRun`. Its advertised value is four protections that raw reactive flow lacks: a judge quality-gate, a hard `EngineBudgetError` agent cap, a deadline watchdog, and shielded partial-export on cancel. Under the #122 / #67 adoption mandate ("we can ban subagent and still be fine"), this construct is the intended front door.

A live dogfood on 2026-07-06 (λ:lionagi + analyst, real `codex` model; `tests/engines/` = 190 passing) found the mechanics work in isolation but the construct is **not adoption-ready as a general front door**, for three evidenced reasons: protections are inconsistent across the five engines, a real crash exists under reactive expansion (**gap-1**, reproduced live — FRICTION_LOG run 5a), and there is no structured-result path for programmatic callers (**gap-3/gap-4**). Evidence: `.khive/workspaces/20260706/engine-dogfood/REPORT.md` and `FRICTION_LOG.md`.

This ADR resolves **one contract seen from two faces**: what `Engine.run()` **RETURNS** (structured result, skipped dimensions surfaced) and how it **DEGRADES** (budget/deadline behavior, real partial-export coverage). The two are mechanically coupled — the degrade path's return value *is* the result contract — so they are decided together.

---

## Teardown (refute first)

Per the packet's §5 refute mandate. I attacked the framing before authoring; the honest verdicts follow, some of which sharpen the recommendations and one of which changes a fork's shape.

### T1 — "One ADR, one contract" — does gap-3 (result shape) genuinely separate from gap-1 (crash)?

**Strongest case for splitting:** the gap-1 crash-*stop* is a 1-site control-flow fix that needs no new result type. `EngineRun.wait_quiescence()` (`lionagi/engines/engine.py:402`) already filters `asyncio.CancelledError` out of its collected `task_errors` (`engine.py:410`) before raising an `ExceptionGroup` (`engine.py:417-419`). Adding `EngineBudgetError` to that same filter stops the research/verifier crash *today*, as an isolated bug PR, with zero result-shape work. gap-3 (return the events) is pure additive API surface with no failure semantics. On that reading they are separable and should ship on different clocks.

**Why it loses (framing holds):** stopping the crash without a result contract silently converts a *loud crash* into a *silent truncated success*. After the filter, a budget-truncated research run returns its normal synthesized `str` with **no signal that a whole subtree was dropped** — which is exactly gap-4's already-demonstrated failure (a confident `ReviewVerdict` that never had `correctness` data). The dogfood is direct evidence that silent-partial is the *more* dangerous mode, not the safer one. The crash fix's *output* ("what does a truncated run return, and how does the caller know it was truncated?") is undefined without the result object. So the design is one contract: Fork A's degrade path returns Fork C's shape; Fork B guarantees that shape is non-empty. **Verdict: the "one contract, four sides" framing survives on the merits — the coupling is mechanical, not rhetorical.**

**The honest nuance Leo should have:** *sequencing* can front-load the crash-stop. The `wait_quiescence` `EngineBudgetError` filter (Fork A, part 1) is strictly safe to land as an immediate hotfix ahead of the full contract, because crash→silent-empty is strictly better than crash→total-data-loss for a run that already did real work — **provided** the degraded-flag follow-up (Fork C) lands to close the gap-4 silence it opens. Design is one ADR; delivery can be two PRs with a named ordering.

### T2 — the typed-result-object fork: does lionagi already have a result idiom to match?

I grepped `operations/`, `Branch.operate`, and the run family. **lionagi has no generic result-envelope idiom.** `branch.operate` (`lionagi/session/branch.py:695`), `communicate`, and `run_and_collect` (`lionagi/operations/run/run.py:402`, `-> Any`) all return bare `text | dict | BaseModel`. The only structured returns are *domain* Pydantic models (`FlowControlVerdict`, `FlowCertificate`, `FlowPlan` — surfaced in the corpus check) and dict returns (`session.flow` → `{"operation_results": ...}`, `HypothesisRun.export()` → `dict[str,str]` at `hypothesis.py:415`). There is nothing to match; whatever this ADR picks *sets* the engine-layer convention. That removes the "bespoke object ignores an existing convention" smell — there is no convention. It also raises the bar: the choice should be one lionagi would want to generalize later, i.e. typed, not stringly.

### T3 — base-class default partial-export: honest, or manufactured false confidence?

This is the attack that **changed a fork's shape.** A base default that *synthesizes prose from whatever events exist* is dishonest for exactly the engines that lack an override: a "partial review" LLM-synthesized from 0 completed dimensions reads as a confident verdict (gap-4, live-demonstrated). An explicit empty would be more truthful than that. **But** the dichotomy "synthesize vs empty" is false. A base default that returns the **structured partial** — the collected events plus a `degraded=True` flag and reason, and *no fabricated synthesis text* — is both universal and honest: it says "here is what I collected; I did not finish," and it manufactures nothing. This reframes Fork B: the base default should surface *structure + the degraded flag*, never a synthesized verdict. Engines that *can* honestly synthesize a partial (research/hypothesis/coding already do) override to *add* `.text`; engines that cannot (review with 0 dims, planning with no plan) inherit the honest structured-empty-but-flagged default. That fills the Review/Planning hole for free without the false-confidence failure. Fork B and Fork C merge here.

### T4 — gap-1 fix location: where is the line between grace and masking?

The crash has **three distinct shapes** (verified at source, beyond the packet's enumeration):

- **Spawned discretionary coro** (research `_explore` at `research.py:203`, spawned at `:187`/`:191`; review `_verify` spawned at `review.py:163`): `make_agent` raises `EngineBudgetError` (`engine.py:264`) *inside* an already-scheduled task; `wait_quiescence` collects it into an `ExceptionGroup` and re-raises (`engine.py:417-419`). This is FRICTION_LOG run 5a.
- **Root-level structured gather** (review dimension fan-out via `ln_gather` at `review.py:149`, guarded by `except BaseException:` → `cancel_active()` → re-raise at `review.py:150-154`): the raise propagates out of `_run`; `run()`'s handler catches **only** `asyncio.CancelledError` (`engine.py:683`), so a synchronous `EngineBudgetError` bypasses partial-export and propagates raw. This is FRICTION_LOG run 3 (0.3s raw raise).
- **Root-level sequential** (coding `_plan`/`_implement` at `coding.py:623`/`:649`; planning `_plan` at `planning.py:95`): same escape as above, raw raise, but rarer because sequential + few agents.

And a **fourth engine already degrades gracefully**: `HypothesisEngine` wraps every stage `make_agent` inside `_guard` (`hypothesis.py:593-599`, `except Exception:` → `notify("stage_error")`), so a budget raise inside a spawned stage becomes a benign `stage_error` and the run reaches synthesis. **Hypothesis is the existing proof that the guard-wrapper mitigation works** — Fork A is partly "make the other engines behave like hypothesis already does."

The grace/masking line: `EngineBudgetError` is a **control signal** ("stop making agents"), not a **failure**. It must never escape `run()` as an exception. Where it is raised determines the honest handling:

- **discretionary/expansion** work (spawned `_explore`, spawned `_verify`, depth teams) → that branch stops; the run continues. This is grace, and it is already what `spawn()` does at the schedule gate (`engine.py:333-338`, degrade-to-`None`).
- **mandatory root pipeline** work (the first reviewer, the plan, the implementer) not completing because `max_agents` is set below the pipeline's floor → that is a **misconfiguration the operator must see**. It should route to `_partial_export` and return a result flagged `degraded` with reason `"budget"` — never a silent clean-looking success, and never a raw crash.

So "graceful degrade" that *swallows* a too-tight `max_agents` silently would be masking. The mitigation surfaces it as a first-class `degraded=True, degrade_reason="budget"` on the result plus the existing `budget_exhausted` `on_event` (fired once via `_notify_budget_once`, `engine.py:215-223`). Loud enough to catch a misconfig, structured enough not to crash.

### T5 — steelman "do nothing / document the sharp edges."

The cheapest path: document that ReviewEngine/PlanningEngine don't degrade and that structured access needs the `engine._run(engine.new_run(), ...)` bypass. **Why it is insufficient for #122 specifically:** the adoption thesis is "ban subagents, use engines, and be fine" — its entire value is the *safety guarantee*. (a) A documented crash is not a safe front door. The gap-1 race destroys a run that already did real, paid work; no doc makes `max_agents` a *safe ceiling* rather than "usually fine, occasionally detonates your whole run." (b) Documenting "structured access needs the bypass" tells every CI-gate / dashboard adopter to *forfeit all four protections* to read their own results — i.e. the front door is usable only for prose, which is precisely the non-goal. Documentation converts a product claim ("safe front door") into a caveat sheet. For #122, that fails the goal by definition, not by degree.

### Corpus check (khive recall + compose + graph)

- Prior semantic memory `31475c41` (λ:lionagi, 50m before this authoring) independently records the exact gap-1 mechanism and cites — corroborates the packet, no contradiction.
- Memory `db5cbbd3` (the reactive-engine adoption thesis) confirms the framing: "how to use reactive well" = "use it through an engine"; the protections *are* the product. This raises the stakes on gap-1 (the protection that most defines the pitch is the one that crashes).
- Compose surfaced generic distributed-systems corroboration, not lionagi-specific prior art: **"a timeout is a budget contract for a whole unit of work; deadline exhaustion is operationally different from a generic failure and must drive different handling"** (atom `a3-e-0201`). That is precisely the T4 argument — budget/deadline is a control signal, not an error — arrived at independently. **"Background import with partial failure report"** (atom `a3-h-0104`): the honest pattern is "finished with N rejected rows" linking to a structured result, *not* a generic "complete" toast — direct support for Fork C's `degraded` + `skipped` over a bare string. No corpus hit contradicts the design.

---

## The model — one contract, two faces

```text
                Engine.run(prompt) ─────────────► EngineResult   (the RETURNS face, Fork C)
                     │                                 ▲   ▲
   success path ─────┤                                 │   │
                     │                                 │   └── .degraded / .skipped  (gap-4, Fork C)
   budget/deadline ──┤──► _partial_export(run) ────────┘
        degrade      │        (base default = honest structured partial, Fork B)
        (Fork A)     │
                     └──► EngineBudgetError is a CONTROL signal, never an escape:
                          • spawned/gathered discretionary  → stop that branch, continue
                          • root mandatory pipeline          → route to _partial_export, flag degraded
```

Every arrow terminates in the same `EngineResult`. That is why it is one ADR.

---

## Fork A — degrade behavior for gap-1 (root-level `make_agent` on budget exhaustion)

| Option | Pros | Cons | Cost divergence over time |
|---|---|---|---|
| **A1. `try/except EngineBudgetError` at every callsite** (mirror `judge`'s self-defense at `engine.py:646-647`) | Local, explicit, per-site semantics | Repeated across `research.py:197`, `review.py:171`/`:192`, `coding.py`, and every *future* engine's spawn shape; one missed callsite = the crash returns | Grows linearly with engine count; a latent trap for every new engine author — the exact bug we are fixing, re-openable forever |
| **A2. Defend inside `make_agent`** (return a sentinel instead of raising) | One site | Changes `make_agent`'s contract for *all* callers, including terminal `exempt=True` stages and the root mandatory calls that *should* surface a misconfig; erases the ability to distinguish "expansion declined" from "pipeline can't run" | Cheap now, expensive later — collapses two semantics into one, then needs re-splitting when misconfig-visibility is demanded |
| **A3 (RECOMMENDED). Two-point central fix: (i) `wait_quiescence` treats `EngineBudgetError` as benign (mirror the existing `CancelledError` filter, `engine.py:410`); (ii) `run()`'s handler also catches `EngineBudgetError` → route to `_partial_export`, flag `degraded`** | Fixes all three crash shapes at two central sites; no per-callsite churn; spawned discretionary work degrades exactly like `spawn()` and like hypothesis's `_guard` already do; root mandatory shortfall becomes a flagged degraded result, not a crash and not a silent success | `run()` catching `EngineBudgetError` must not swallow a *mixed* `ExceptionGroup` (budget + a real error) — must re-raise if any non-budget error is present; needs care | Flat: new engines inherit correct behavior for free; the semantics live in the base, not in each author's memory |

**Recommendation: A3.** `EngineBudgetError` is a control signal; it belongs to the base runtime, not to each engine author. Two edits:

1. **`wait_quiescence` (`engine.py:402-419`)** — extend the filter at `engine.py:410` so `EngineBudgetError` is excluded from `task_errors` alongside `asyncio.CancelledError`. This converts every *spawned/gathered discretionary* budget raise (research `_explore`, review `_verify`) into a benign "expansion stopped," matching what `spawn()` and hypothesis `_guard` already do. This is the safe immediate hotfix (T1).
2. **`run()` (`engine.py:660-778`)** — widen the `except asyncio.CancelledError` arm (`engine.py:683`) to a sibling `except EngineBudgetError` arm that routes to `_partial_export` (the same `_partial_export` + shield + `_PARTIAL_EXPORT_TIMEOUT_S` machinery, `engine.py:710-733`) and builds an `EngineResult` with `degraded=True, degrade_reason="budget"`. This catches the *root mandatory* raw-raise (review gather, coding/planning sequential). **Guard:** if the caught object is an `ExceptionGroup` containing any non-`EngineBudgetError`, re-raise it — a real crash must not be laundered into a partial (T4 masking guard).

The existing `exempt=True` terminal stages (research synth `research.py:274`, review verdict `review.py:219`, planning synth `planning.py:130`, coding verify `coding.py:857`) are untouched — they already bypass the budget gate and must, so partial-export can synthesize.

---

## Fork B — per-engine vs base-class partial-export (gap-2)

| Option | Pros | Cons | Cost divergence over time |
|---|---|---|---|
| **B1. Per-engine, base stays `None`** (`engine.py:780-782`) | "Author opted out" is explicit | Two shipped engines (`ReviewEngine`, `PlanningEngine`) *already* have the hole; every new engine can re-open it; the cheapest, most-approachable engine (Review) has the least safety — the exact adoption trap | Every engine forever owes a hand-written override or silently discards in-flight work |
| **B2. Base default synthesizes prose from events** | Every engine "degrades to something" | Dishonest for the engines that lack a real override: an LLM-synthesized "partial review" from 0 completed dimensions reads as a confident verdict — manufactures the gap-4 false confidence | Cheap now; erodes trust in every partial result later; "worse than empty" |
| **B3 (RECOMMENDED). Base default returns the *honest structured partial*: collected `events_by_type` + `degraded=True` + reason + `skipped`, and NO fabricated synthesis text. Engines override to *enrich* with `.text` synthesis** | Universal (fills Review/Planning for free); manufactures nothing; the base default is the same `EngineResult` shape Fork C defines; research/hypothesis/coding keep their real synthesized partials by overriding to add `.text` | Base default's `.text` is empty/marker-only for non-overriding engines — a caller wanting prose from a truncated Review still gets structure, not prose (acceptable: there was no complete work to narrate) | Flat and honest: the floor is "structured truth"; prose is an opt-in improvement, never a fabrication |

**Recommendation: B3** (the T3 reframing). The base `_partial_export` (`engine.py:780`) stops returning bare `None` and instead returns the structured `EngineResult(text="", events_by_type=<from run.by_type>, skipped=<from run._emission_failures>, degraded=True, degrade_reason=..., run=run)`. `ReviewEngine` and `PlanningEngine` need **no override** to become honest — they inherit the structured partial. `ResearchEngine`/`HypothesisEngine`/`CodingEngine` keep their overrides (`research.py:145`, `hypothesis.py:491`, `coding.py:1009`) but now those overrides *set `.text`* on the base structured result rather than returning a bare `str`. A "partial review verdict with 0 dimensions done" means exactly `EngineResult(text="", degraded=True, skipped=[all dims], events_by_type={})` — truthful, not a confident empty verdict.

---

## Fork C — results-dict vs typed result object (gap-3 + gap-4)

| Option | Pros | Cons | Cost divergence over time |
|---|---|---|---|
| **C1. Return a mapping** `{"text":..., "events_by_type":..., "skipped":..., "degraded":...}` | Cheap; additive | Breaks every existing `str` caller (they now get a `dict`); stringly-typed keys; no IDE/type help; fails the world-class-OSS typed-surface bar (HC-7) | Stringly-typed dicts metastasize; each consumer re-implements key access + defaults |
| **C2. Plain typed object** `EngineResult` (not a `str`) with `.text`, `.events_by_type()`, `.skipped`, `.degraded` | Typed, self-documenting | Breaks back-compat at the ONE place it is owed: `isinstance(result, str)`, `json.loads(result)`, `result + "\n"`, `print(result)`-of-prose all change; needs a loud migration | Clean type, but a real break at the exact contract boundary the constraints protect |
| **C3 (RECOMMENDED). `EngineResult(str)` — subclasses `str`, carrying `.text` (alias), `.events_by_type()`, `.skipped`, `.degraded: bool`, `.degrade_reason`, `.run`** | Typed AND byte-for-byte back-compatible: `str(result)`, `isinstance(result, str)`, concatenation, `json.loads`, file writes all keep working because it **is** a `str` whose value is the synthesized text; adds the structured surface as attributes | `str` subclass is slightly unusual (attributes set in `__new__`, immutable text); a caller doing `type(result) is str` (exact-type check, rare) sees the subclass; `.run` holds a live `EngineRun` (retention footgun) | Flat: the one public contract (`await engine.run(prompt)`) is preserved exactly; the richer surface is purely additive and discoverable |

**Recommendation: C3.** Back-compat is owed only at `await engine.run(prompt) -> result`, and today `result` is a `str` that callers print, concatenate, and parse. A `str` subclass preserves that contract *exactly* while adding the typed structured surface. This is the sweet spot the constraints point at: internal shapes free to change, the one public return type preserved.

Illustrative signature (design only — no implementation):

```python
class EngineResult(str):
    """Return type of Engine.run(): a str (the synthesized text, back-compat)
    that also carries the run's structured outcome. `str(result)` == the text."""

    # constructed in __new__(cls, text, *, events_by_type, skipped, degraded,
    # degrade_reason, run); attributes below are set there.
    degraded: bool            # True if budget/deadline truncated the run
    degrade_reason: str       # "" | "budget" | "deadline"
    skipped: list[str]        # dimensions/stages that never reported (gap-4)
    def events_by_type(self, t: type) -> list: ...   # snapshot of run.by_type(t)
    run: EngineRun            # live handle for power users (see retention note)
```

- **gap-3** is answered by `.events_by_type()` + `.run` — structured domain events (`IssueFound`, `FindingEmitted`, `JudgeVerdict`) are now reachable *with the protections intact*, no `_run`-bypass. `run()` already holds the `run` handle in both the success (`engine.py:682`) and degrade (`engine.py:710`) paths; it wraps the `_run`/`_partial_export` text into `EngineResult` at the return boundary (`engine.py:777-778`), so engine authors keep returning `str` from `_run` and the wrapping is central.
- **gap-4** is answered by `.skipped` + `.degraded` — sourced from `run._emission_failures` (already populated at `engine.py:328`, copied to the engine at `engine.py:742`). For `ReviewEngine`, `.skipped` maps the failed reviewer agents to dimension names, so "the `correctness` dimension never reported" is a first-class field, not a silence.

---

## Negative consequences (honest cost accounting)

- **`str`-subclass surprise (C3).** `EngineResult` is not a plain `str`. Code doing `type(result) is str` (exact-type identity, not `isinstance`) sees the subclass and may branch wrong. Rare, but real. Documented, not eliminable without giving up back-compat.
- **`.run` retention footgun (C3).** `.run` holds a live `EngineRun`, which holds the `Session` and its branches. A caller who stashes `EngineResult` objects in a list retains whole sessions. Mitigation: `.events_by_type()` returns a *snapshot* so power users can read structure and drop the result; the `.run` handle is documented as "live; do not retain." We are trading a memory footgun for zero-copy power-user access.
- **Silent-drop moves, it does not vanish (A3).** Filtering `EngineBudgetError` at `wait_quiescence` means a budget-truncated subtree of discretionary work is *dropped* rather than *crashing*. That is the correct trade (it was discretionary), but it is still lost work; the honesty rests entirely on `degraded=True` + the `budget_exhausted` event being surfaced. If a downstream consumer ignores `.degraded`, the drop is invisible to them — we cannot force them to check it.
- **`run()` handler complexity (A3).** Widening `run()` to catch `EngineBudgetError` adds a mixed-`ExceptionGroup` discrimination requirement (re-raise if any non-budget error is present). This is subtle async code in the most safety-critical method; it needs its own tests (a budget error and a real error raised from sibling spawned tasks in the same run must still surface the real error).
- **Burden on engine authors (all forks).** New engines must: return `str` from `_run` (unchanged), and if they introduce a *new* spawn shape, route agent creation through `run.spawn`/`make_agent` so the central `EngineBudgetError` handling applies — an engine that hand-rolls its own `asyncio.ensure_future` around `make_agent` outside the run's tracking can still crash. This is a documented convention, not a compiler-enforced one. Fork B raises a subtler burden: authors must now *decide* what a meaningful partial is for their shape (or accept the honest structured-empty default) — that is inherent complexity this ADR surfaces rather than adds.
- **What C3 forecloses.** Committing `Engine.run()` to `EngineResult(str)` makes it awkward to later return a *non-text-primary* result (e.g. an engine whose natural output is a dict, not prose). If such an engine arrives, it either serializes to text for `str(result)` or this contract is revisited. We are betting engines are prose-terminal (all five are today).

---

## Constraints check

- **Back-compat (owed only at `engine.run()`):** C3 preserves it exactly — `EngineResult` *is* a `str`. ✔
- **No stubs / design only:** signatures are illustrative; no implementation here. ✔
- **ADR-0075 / ADR-0077 lineage:** this extends the run contract; the reactive substrate (`operations/flow.py`, `spawn`/`wait_quiescence` quiescence model) is untouched. ✔
- **Published read surface (HC-5):** `EngineResult` is an **in-process return value**, not persisted. The CLI already reads `engine._emission_failures` (`engine.py:742`) and writes `engine_runs.error`; `.skipped` surfaces that same data, adding no new `state.db` / `session_signals` payload. **This ADR does NOT touch the DB contract** — no versioning/announce needed. ✔

---

## Risks & open questions for the spec gate

- **R1 (for the gate to rule):** should the `wait_quiescence` `EngineBudgetError` filter (Fork A part 1) ship as an **immediate hotfix PR ahead** of the full contract (T1 sequencing), accepting the interim window where truncation is crash-free but not yet flagged `degraded`? Advisor recommendation: **yes**, because crash→silent-empty strictly beats crash→data-loss, *conditional on* the Fork C follow-up being committed in the same milestone to close the gap-4 silence.
- **R2:** `degrade_reason` vocabulary — is `{"", "budget", "deadline"}` sufficient, or does the gate want a richer taxonomy (e.g. `"budget:root"` vs `"budget:expansion"`) so a misconfigured `max_agents` is distinguishable from expected expansion-capping? Advisor lean: two-value is enough for v1; `.skipped` already distinguishes "mandatory stage missing" from "discretionary branch dropped."
- **R3:** `.run` exposure — expose the live handle at all, or snapshot-only (`.events_by_type()`)? Exposing it is the power-user affordance for #122 CI-gate callers; hiding it is safer. Advisor lean: expose but document as live/non-retainable.
- **R4 (Ocean-level, flagged not decided):** none. This is an engine-layer API contract, reversible, non-strategic — squarely a spec-gate/Fable call, not an Ocean fork.

---

## Implementation fences (for the Sonnet build after sign-off)

- **MAY** add `EngineResult(str)` as the return type of `Engine.run()`, with `.text`, `.events_by_type()`, `.skipped`, `.degraded`, `.degrade_reason`, `.run`.
- **MAY** extend `wait_quiescence`'s error filter (`engine.py:410`) to exclude `EngineBudgetError`, and add an `except EngineBudgetError` arm to `run()` (`engine.py:683`) that routes to `_partial_export`.
- **MAY** replace base `_partial_export`'s `return None` (`engine.py:782`) with the honest structured `EngineResult` default; **MAY** update the three existing overrides to set `.text` on it.
- **MAY NOT** change the reactive substrate (`operations/flow.py`, `session.flow`, `spawn`/`wait_quiescence` *quiescence model*) beyond the one error-filter line — ADR-0075/0077 ground is fixed.
- **MAY NOT** make `EngineResult` anything other than a `str` subclass — the moment it stops being a `str`, back-compat at the one owed boundary breaks.
- **MAY NOT** have `run()` swallow a mixed `ExceptionGroup` — if any non-`EngineBudgetError` is present, it MUST re-raise (no laundering a real crash into a partial).
- **MAY NOT** fabricate synthesis text in the base `_partial_export` default (Fork B / T3) — the default carries structure + `degraded`, never a manufactured prose verdict.
- **MAY NOT** fold in Engine PR-C (judge-notify at `engine.py:641`, doc qualifier at `docs/reference/engines.md:21`) — out of scope, separate PR (below).
- **Verify by:**
  1. A test that reproduces FRICTION_LOG run 5a (`ResearchEngine`, `max_agents=10`, verbose root spawning ~9 `_explore`) and asserts `run()` returns an `EngineResult` with `.degraded is True` and non-empty `.text`/`.events_by_type(FindingEmitted)` — **no `ExceptionGroup` escapes**.
  2. A test that reproduces FRICTION_LOG run 3 (`ReviewEngine`, `max_agents=1`) and asserts a `degraded` `EngineResult`, **not** a raised `EngineBudgetError`.
  3. A test that a *mixed* failure (one spawned task raises `EngineBudgetError`, a sibling raises `ValueError`) still surfaces the `ValueError` — masking guard.
  4. A test that `ReviewEngine` with a deadline hit and `correctness` emission-failed returns `.skipped == ["correctness"]` and `.degraded is True` (gap-4 closed).
  5. A back-compat test: `isinstance(await engine.run(x), str)` and `str(result) == result.text` for all five engines.
  6. Full `tests/engines/` (190 baseline) stays green.

---

## Out of scope but related (do not fold in)

- **Engine PR-C** (separate trivial non-spec PR, sequenced after the merged reactive PRs): judge-notify on the allow branch (`engine.py:641`) so a passing gate is observable; and qualifying the `docs/reference/engines.md:21` claim ("On budget exhaustion `Engine.run()` calls `_partial_export()`… instead of raising" — currently true only for the watchdog `CancelledError` path). Note: once Fork A lands, that doc line becomes broadly true (budget raises also route to partial-export), so PR-C's doc edit should be reconciled with this ADR's delivery, not written blind.
- **gap-5 reliability (emission-repair rates on CLI workers):** the `operate_with_repair` retry tuning for CLI providers (`engine.py:292`) is a reliability concern, not a contract concern; `.skipped` *surfaces* it but does not *fix* it. Separate track.
- **`on_event` coverage parity across engines** (review missing `agent_start`/`agent_done` vs research `research.py:229-248`): DX papercut, trivial, not this ADR.

---

## Evidence artifacts

- Dogfood report: `.khive/workspaces/20260706/engine-dogfood/REPORT.md`
- FRICTION_LOG (gap-1 repro = run 5a; budget raw-raise = run 3): `.khive/workspaces/20260706/engine-dogfood/FRICTION_LOG.md`
- Source (all at main `a38add627`): `lionagi/engines/engine.py`, `research.py`, `review.py`, `planning.py`, `hypothesis.py`, `coding.py`; `docs/reference/engines.md`
- Baseline: `tests/engines/` = 190 passing at dogfood time.
