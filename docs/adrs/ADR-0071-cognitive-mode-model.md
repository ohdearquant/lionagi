# ADR-0071: Cognitive Mode Model

**Status**: accepted
**Date**: 2026-05-29

## Context

lionagi is growing a "casts" layer for composable agent identity:
`Pattern → Profile → Actor → Branch`. A `Pattern` is the frozen, composable atom
of agent configuration; a `Profile` composes patterns into a configuration; an
`Actor` binds a profile to a persistent, governable identity; a `Branch` is the
live runtime. Two specializations of `Pattern` carry behavior: a **Role**
(intent-driven — *what* an agent does, its authority, boundaries, and artifacts)
and a **Mode** (cognitive — *how* an agent reasons).

Modes were initially a flat folder of twelve markdown files copied in from prior
prototyping, with no shared model. Three structural problems made them unusable
as a composable primitive:

1. **No contract.** Several "modes" produced artifacts or asserted authority —
   e.g. a `procedural` mode whose stated output was "a faithful execution log."
   That is role behavior, not a reasoning policy. Without an enforced boundary,
   modes and roles would collide and the two-axis design would collapse.

2. **No composition semantics.** The premise of a mode is that it stacks onto a
   role and onto other modes. But nothing said which combinations are coherent.
   `fast` (heuristic, skip enumeration) and `systematic` (exhaustive enumeration)
   cannot both govern the same reasoning step, yet nothing rejected the pair.

3. **No structure or discrimination.** The twelve modes overlapped (`slow` vs
   `systematic` vs `procedural`), left real gaps (no uncertainty-reasoning mode,
   no steelman-then-attack mode), and were never organized, so an orchestrator
   had no principled basis for selecting one.

The triggering event was the decision to build `Actor` ahead of the governance
layer (ADR-0070 et al.): governance binds to actors, an actor's behavior is
configured by its profile, and a profile composes modes. The mode model had to
be settled before actors could carry a stable, governable configuration.

## Decision

Model a **Mode** as a pure cognitive overlay: a *marked deviation* from a
default reasoning policy, organized on a small set of cognitive axes, with hard
conflict rules for composition. Author the modes as markdown with structured
YAML frontmatter, load them into a typed `Mode` (a `Pattern` subclass), and
enforce the cognitive-only contract at construction.

The model has five load-bearing choices:

**1. Modes are marked deviations from a default reasoning policy.** The unmarked
baseline is explicit: balanced tempo, focused single-frame search, ordinary
epistemic hygiene, pragmatic option generation, a cooperative stance, and
role-governed output. A mode is invoked only to deviate from that baseline. This
is why the roster carries the *marked* pole of each axis, not both poles — the
default already occupies the unmarked pole, so a "focused" or "intuitive" mode
would be redundant with no mode at all.

**2. The cognitive-only contract is enforced.** A `Mode` contributes a
behavioral instruction to the system prompt and selection metadata, and nothing
else. It never grants capabilities or resources, carries authority, or produces
artifacts — those belong to roles and other patterns. A `model_validator`
rejects any `Mode` constructed with non-empty `capabilities`, `resources`,
`authority`, or `boundaries`. The contract is a runtime invariant, not a
convention.

**3. Axes are organizational; `conflicts_with` is the mechanism.** Each mode
declares an `axis` (its cognitive dimension) and an explicit `conflicts_with`
set. Crucially, the axis does **not** determine conflict. Two modes on the same
axis routinely compose (`evidential` + `probabilistic`), while two modes on
different axes can hard-conflict (`fast` on the tempo axis vs `systematic` on the
search-topology axis). Conflict is therefore declared per mode, not inferred
from the axis. The axis exists for human/orchestrator organization and backs one
soft heuristic: "do not stack several modes from the same axis." The only hard
conflicts in the current roster are `fast ⊥ slow` and `fast ⊥ systematic`.

**4. Failure dynamics are one parameter-free mode, not a parameter system.**
A single `premortem` mode covers "assume the target failed, trace causes and
cascades, pair each failure with its repair." The target — a planned action, a
dependency, or a standing assumption — is supplied by the role and the task
context, not by a formal parameter. This merges the prototypes' `anticipatory`
(pre-action premortem) and `chaos` (assumption-removal) without introducing a
`parameters` mechanism that only one mode would have used.

**5. Modes are authored as markdown, loaded on demand.** Each mode is a
`roles/modes/*.md` file: YAML frontmatter for structured selection/composition
metadata, and a body with a one-line `Description` and a `Behavioral
Instructions` paragraph. `builtin_modes()` parses them lazily (cached); the
module import stays O(1) and does no file I/O. Markdown keeps the prompt text
reviewable as prose while the frontmatter stays machine-parseable.

## The Mode Schema

A `Mode` is a `Pattern` subclass (`kind="mode"`). The behavioral instruction is
stored in the inherited `prompt` field so it composes into the system prompt
exactly like any other pattern.

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | Mode identifier (from Pattern). |
| `prompt` | `str` | The behavioral instruction, composed into the system prompt. |
| `description` | `str` | One-line cognitive-style summary, for selection. |
| `axis` | `ModeAxis` | Cognitive dimension. Organizational, not a conflict rule. |
| `tier` | `core \| extended` | `core`: general-purpose roster. `extended`: niche. |
| `phase_scope` | `pre \| during \| post \| continuous \| all` | When in a reasoning/DAG step the overlay applies. |
| `overhead` | `low \| medium \| high` | Relative cognitive cost. Scheduling hint only. |
| `conflicts_with` | `frozenset[str]` | Mode names that cannot share a stack (hard rule). |
| `composes_well_with` | `frozenset[str]` | Mode/role names that pair well (soft hint). |
| `when_to_use` | `tuple[str, ...]` | Selection triggers. |
| `when_not_to_use` | `tuple[str, ...]` | Over-use / failure conditions. |

A `Mode` must **not** carry `capabilities`, `resources`, `authority`, or
`boundaries`. These belong to roles and patterns; a mode that needs one is
misfiled. The `model_validator` enforces this at construction.

## The Axis Model

Seven axes organize the fourteen modes. Each axis has a default (unmarked) pole
that the baseline reasoning policy already occupies.

| Axis | Default pole | Marked modes |
|------|-------------|--------------|
| Tempo | Balanced deliberation | `fast`, `slow` |
| Search topology | Focused, single-frame | `systematic`, `framing`, `associative` |
| Epistemic accounting | Informal support tracking | `evidential`, `probabilistic` |
| Feasibility | Pragmatic option generation | `constraint-solving` |
| Skeptical stress | Cooperative sanity check | `adversarial`, `premortem` |
| Perspective | Task-native verbal view | `empathetic`, `socratic`, `visual-spatial` |
| Self-monitoring | Local role-level checks | `metacognitive` |

Same-axis membership implies neither conflict nor compatibility — it is a
grouping for selection. Tempo is the only axis whose members are mutually
exclusive, and that exclusivity is encoded explicitly in `conflicts_with`, not
derived from the shared axis.

## The Mode Roster

Fourteen modes: eleven core, three extended.

### Core

| Mode | Axis | One-line role |
|------|------|---------------|
| `fast` | tempo | Heuristic pattern-match for recognized, low-novelty problems. |
| `slow` | tempo | Deliberate step-by-step reasoning — depth on one chain. |
| `systematic` | search-topology | Exhaustive branch/case coverage — breadth across branches. |
| `framing` | search-topology | Generate multiple problem representations before solving. |
| `evidential` | epistemic-accounting | Gate assertions by source support and inference traceability. |
| `probabilistic` | epistemic-accounting | Reason under uncertainty — priors, calibration, expected value. |
| `constraint-solving` | feasibility | Filter by hard constraints before optimizing feasible options. |
| `adversarial` | skeptical-stress | Steelman a claim, then attack its strongest version. |
| `premortem` | skeptical-stress | Assume failure, trace causes and cascades, pair each with a repair. |
| `empathetic` | perspective | Model stakeholder constraints and incentives — loop stability. |
| `metacognitive` | self-monitoring | Watch reasoning for drift from the assigned objective. |

### Extended

| Mode | Axis | One-line role |
|------|------|---------------|
| `associative` | search-topology | Broad cross-domain scanning; divergent tangents as signal. |
| `socratic` | perspective | Question-led elicitation rather than supplying the answer. |
| `visual-spatial` | perspective | Reason over topology and flow before sequential detail. |

## Composition

`validate_mode_stack(modes)` is the composition gate. It raises
`ModeConflictError` on the first hard conflict (the check is symmetric — a
conflict declared on either mode counts) and returns advisory warnings for soft
issues such as several modes drawn from one axis.

Legal stacks express layered reasoning discipline:

- `evidential + probabilistic + slow` — source-classify each claim, quantify the
  residual uncertainty, and reason it through step by step.
- `adversarial + evidential` — attack the strongest version of a claim, but
  ground every objection in a source.
- `visual-spatial + systematic + constraint-solving` — reason topologically,
  cover all interfaces, prune designs that violate hard constraints.

Illegal stacks are rejected by `conflicts_with`:

- `fast + slow` — opposite poles of the tempo axis.
- `fast + systematic` — `fast` skips enumeration; `systematic` requires complete
  branch coverage. They cannot both govern one reasoning step.

## Consequences

**Positive**

- The cognitive-only contract is a runtime invariant. A mode cannot silently
  acquire authority or grant tool access; the validator rejects it at
  construction, so the role/mode boundary cannot erode over time.
- Composition is machine-checkable. An orchestrator (or a future DAG control
  node) can reject an incoherent mode stack before an actor runs, and can select
  modes by axis, trigger, and phase.
- The markdown-plus-frontmatter format keeps prompt text reviewable as prose
  while exposing structured metadata for selection. Editing a mode is editing one
  small file.
- The roster is small and discriminated. Fourteen modes on seven axes is a set an
  orchestrator can reason about, versus an unbounded flat list.

**Negative**

- The modes are runtime-loaded package data — the first non-Python data files
  shipped inside the `lionagi` package. They must be explicitly included in the
  wheel (see References), and a missing file surfaces only at
  `builtin_modes()` call time, not import time.
- `axis` not determining conflict is a subtlety. A reader who assumes same-axis
  means mutually exclusive will be wrong; the model leans entirely on
  `conflicts_with`. This is documented but is a learning cost.
- `Profile` currently holds a single `mode`. Using more than one mode at once
  requires extending `Profile` to a `tuple[Mode, ...]`; `validate_mode_stack` is
  built for that but the wiring is deferred to a later change.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Keep modes as a flat list with no axes or conflict rules | Provides no basis for selection and no way to reject incoherent stacks (`fast + systematic`). The composability premise of modes is unusable without conflict semantics. |
| Use `axis` as the conflict/exclusivity group (one mode per axis) | Empirically false: `evidential` + `probabilistic` share an axis and compose; `fast` + `systematic` are cross-axis and conflict. Axis-as-conflict would both forbid good stacks and permit bad ones. |
| Keep `procedural` as a mode | It produces an artifact ("execution log") and only composes meaningfully with the operator/deployer/migrator cluster, where its discipline is already the role's core. A pattern that fits one role-cluster and produces an artifact is role behavior, not a cognitive overlay. Relocated to the operator role. |
| Parameterize a `counterfactual-stress` mode with `target` and `depth` | Introduces a `parameters` mechanism that only one mode would use. `target` is contextual (the role supplies it) and `depth` is just composition with `slow`/`systematic`. Collapsed to a parameter-free `premortem`. |
| Add a `minimalist` mode for scope control | "Produce the smallest adequate output" is an output constraint or task policy, not a reasoning strategy. It belongs in a task verbosity setting, a role boundary, or an artifact schema. |
| Store selection metadata (`when_to_use`, etc.) in the markdown body | Prose is not reliably machine-parseable. Moving triggers and conflict/compose sets into YAML frontmatter makes selection a structured lookup, not a regex over prose. |

## References

- ADR-0070 (Governance Tracing) — governance binds to actors; the actor's
  profile composes modes. The mode model is a prerequisite for governable actor
  configuration.
- `lionagi/casts/mode.py` — `Mode`, `ModeAxis`, `ModeConflictError`,
  `load_mode_file`, `builtin_modes`, `get_mode`, `validate_mode_stack`.
- `lionagi/casts/pattern.py` — `Pattern` base class and the `Role` sibling
  specialization.
- `lionagi/casts/roles/modes/*.md` — the fourteen built-in mode definitions.
- `pyproject.toml` (`[tool.hatch.build.targets.wheel] artifacts`) — packages the
  mode markdown into the wheel so `builtin_modes()` resolves them at runtime.
