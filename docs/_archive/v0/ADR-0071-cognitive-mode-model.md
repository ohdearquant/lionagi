# ADR-0071: Cognitive Mode Model

**Status**: accepted
**Date**: 2026-05-29

## Context

lionagi is growing a "casts" layer for composable agent identity:
`Pattern → Profile → Actor → Branch`. A `Pattern` is the frozen, composable atom
of agent configuration; a `Profile` composes patterns into a configuration; an
`Actor` binds a profile to a persistent, governable identity; a `Branch` is the
live runtime. Two specializations of `Pattern` carry behavior: a **Role**
(intent-driven — *what* an agent does) and a **Mode** (cognitive — *how* an
agent reasons).

Modes were initially a flat folder of twelve markdown files copied in from prior
prototyping, with no shared model. Three structural problems made them unusable
as a composable primitive:

1. **No contract.** Several "modes" produced artifacts or asserted authority —
   e.g. a `procedural` mode whose stated output was "a faithful execution log."
   That is role behavior, not a reasoning policy.

2. **No composition semantics.** Nothing said which combinations are coherent.
   `fast` (heuristic, skip enumeration) and `systematic` (exhaustive enumeration)
   cannot both govern the same reasoning step, yet nothing rejected the pair.

3. **No structure.** The twelve modes overlapped, left real gaps, and were never
   organized, so an orchestrator had no principled basis for selecting one.

## Decision

### Pattern is a thin abstract frozen dataclass

`Pattern` inherits `Params` (from `lionagi.ln.types.base`) and `Composable`
(from `lionagi.protocols._concepts`). It carries only `name` and `description`.
Concrete patterns subclass it and add their own fields. A `PatternKind` enum
(`OTHER`, `ROLE`, `MODE`) is exposed via a `kind` property that each subclass
overrides.

Previous fields — `capabilities`, `resources`, `authority`, `boundaries`,
`effort`, `prompt`, `extra` — are all removed from Pattern. lion's governance
model is allowlist-only: things not allowed simply don't exist in the agent's
scope. Deny-rules (`boundaries`) are a contradiction. `authority` collapsed into
`capabilities` (what the actor can DO). `resources` (what the actor can ACCESS)
may belong on Role or Profile, not on the abstract base. `effort` and `extra`
were speculative.

### Mode purity is structural, not enforced

A Mode has two fields beyond Pattern: `behaviors` (the cognitive overlay text
composed into system prompts) and `conflicts_with` (frozenset of mode names).
It has no `capabilities`, `resources`, `authority`, `boundaries`, or `extra`
fields — so there is nothing to police at runtime. The entire validator /
`_ReadOnlyDict` / `model_copy` override / fail-closed loader machinery from the
previous iteration is deleted. Frozen dataclass + no mutable fields = total
purity by construction.

### conflicts_with is mode-mode only

`conflicts_with` declares hard conflicts between modes. If a role restricts
which modes it accepts, the role should declare an allowlist of permitted modes
(not yet implemented). Mode-role conflicts are the role's concern, not the
mode's.

Only three conflict declarations exist in the current roster:

- `fast` conflicts with `[slow, systematic]`
- `slow` conflicts with `[fast]`
- `systematic` conflicts with `[fast]`

### Modes are authored as inline Python modules

Each mode is one `roles/modes/<stem>.py` file exposing a single `MODE = Mode(...)`.
The module stem uses underscores; the canonical `name` may contain dashes
(`constraint_solving.py` → `name="constraint-solving"`). `description` is dense —
phase, overhead, and pairing info compressed into one line — and `behaviors` is
the cognitive-overlay text composed into system prompts; `conflicts_with` is
optional. Roles/modes are a closed built-in set (not user-definable — users
extend via packs); `list_modes()` is the roster source and `Mode.load(name)`
resolves a canonical name. There is no markdown or YAML frontmatter.

`description` is separate from `behaviors` because it is meant for orchestrators
or agents to pick the mode they need; it is not part of the behavioral content.

## The Mode Schema

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | Mode identifier. |
| `description` | `str` | Dense summary for orchestrator selection. |
| `behaviors` | `str` | Cognitive overlay text, composed into system prompts. |
| `conflicts_with` | `frozenset[str]` | Mode names that cannot share a stack. |

## The Mode Roster

Fourteen modes organized across cognitive dimensions (compressed into each
mode's `description`, not a separate field or enum).

| Mode | Summary |
|------|---------|
| `fast` | Heuristic pattern-match for recognized, low-novelty problems. |
| `slow` | Deliberate step-by-step reasoning — depth on one chain. |
| `systematic` | Exhaustive branch/case coverage — breadth across branches. |
| `framing` | Generate multiple problem representations before solving. |
| `evidential` | Gate assertions by source support and inference traceability. |
| `probabilistic` | Reason under uncertainty — priors, calibration, expected value. |
| `constraint-solving` | Filter by hard constraints before optimizing feasible options. |
| `adversarial` | Steelman a claim, then attack its strongest version. |
| `premortem` | Assume failure, trace causes and cascades, pair each with a repair. |
| `empathetic` | Model stakeholder constraints and incentives — loop stability. |
| `metacognitive` | Watch reasoning for drift from the assigned objective. |
| `associative` | Broad cross-domain scanning; divergent tangents as signal. |
| `socratic` | Question-led elicitation rather than supplying the answer. |
| `visual-spatial` | Reason over topology and flow before sequential detail. |

## Consequences

**Positive**

- Pattern is simple enough that subclasses can add exactly the fields they need
  without inheriting speculative ones they don't.
- Mode purity requires zero runtime enforcement — no validators, no special
  dict subclass, no model_copy override. Less code, fewer failure modes.
- The inline-Python module format makes editing a mode trivial — one file, one
  `MODE` object, no boilerplate fields and no frontmatter parsing.
- `conflicts_with` is the single mechanism for composition rules, mode-mode
  only. Simple to reason about, simple to extend.

**Negative**

- No `validate_mode_stack` function exists yet — conflict checking must be done
  by consumers. This is intentional; the function belongs where stacks are
  assembled (Profile or orchestrator), not in the mode model itself.
- Role-mode compatibility is not yet implemented. When roles declare which modes
  they permit, the allowlist will live on Role.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Keep Pattern as a fat BaseModel with capabilities/resources/authority/boundaries | Speculative fields that no consumer used. Contradicts allowlist-only governance (boundaries = denylist). Over-engineered for a frozen value object. |
| Enforce mode purity with validators and _ReadOnlyDict | Unnecessary complexity when the type simply doesn't have the fields. Structural absence is cheaper and more reliable than runtime policing. |
| Keep `ModeAxis` as a field and enum | Axes have no runtime behavior — they don't determine conflict, don't help with team composition, don't gate anything. Compressed into the description string where orchestrators can read it. |
| Keep `tier`, `phase_scope`, `overhead`, `composes_well_with`, `when_to_use`, `when_not_to_use` as fields | No consumer reads them programmatically. Dense prose in `description` serves the same purpose with less schema surface. |
| Keep a separate `mode.py` with registry, cached loader, ModeConflictError | Mode is 20 lines of dataclass. A separate module with registry, deep-copy isolation, and custom exception is disproportionate. |

## References

- `lionagi/casts/pattern.py` — `Pattern`, `PatternKind`, `Mode`, `Mode.load`,
  `list_modes`.
- `lionagi/protocols/_concepts.py` — `Composable`, `Composed` ABCs.
- `lionagi/casts/roles/modes/*.py` — the fourteen built-in mode definitions
  (one `MODE` per module).
