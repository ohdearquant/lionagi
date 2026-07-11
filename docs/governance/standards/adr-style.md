# Governance ADR Style Standard

**Purpose**: File naming, required sections, status lifecycle, cross-reference format, and type
ownership rules for governance ADRs (v0-0041 and later, in the numbering current at authoring
time).

Cross-references: [dsl-style.md](dsl-style.md), [trace-naming.md](trace-naming.md),
[commit-and-pr-style.md](commit-and-pr-style.md)

> **Historical note**: this standard predates the ADR corpus restructure. Every bare `ADR-00NN`
> example below in the 0041-0052 range is a reference into the archived v0 corpus
> (`docs/_archive/v0/ADR-00NN-*.md`), re-anchored here as `v0-00NN` because those numbers now
> collide with unrelated ADRs in the current `docs/adr/` corpus. Per `docs/adr/dispositions.yaml`,
> the entire v0-0041-v0-0052 governance type set was merged into
> [ADR-0087 — Evidence-backed governed execution](../../adr/ADR-0087-evidence-backed-governed-execution.md).
> The file-naming and archive-path rules in §1 and §5 already anticipated this split and remain
> current guidance; the §8 type-ownership table is the pre-consolidation (P12-era) design intent,
> not a live ownership index.

---

## 1. File And Title

- File: `docs/adr/ADR-NNNN-kebab-case-title.md` (the canonical corpus; see `docs/adr/TEMPLATE.md`).
- Archived v0 records live at `docs/_archive/v0/` and keep their original numbering; new ADRs never land there.
- First line: `# ADR-NNNN: Human Title`.
- Numbers are never reused.
- Keep the title stable after acceptance unless a replacement ADR supersedes it.

**Note**: v0-0043 is archived as `docs/_archive/v0/ADR-0043-governed-tool-declaration.md`. Use
that file name in references unless the file is deliberately renamed in a revision PR.

---

## 2. Required Header

Every governance ADR begins with:

```markdown
# ADR-00NN: Short Title

Status: proposed
Date: YYYY-MM-DD
Decision owners: @owner-a, @owner-b
Supersedes: none
Superseded by: none
Depends on: v0-0041, v0-0050
Related: v0-0044, v0-0052
```

`Status` values: `proposed`, `accepted`, `revised`, `superseded`, `rejected`.

---

## 3. Required Sections (In Order)

1. Context
2. Decision
3. Scope
4. Non-Goals
5. Interfaces And Types
6. Runtime Semantics
7. Evidence And Trace Requirements
8. Test Requirements
9. Consequences
10. Migration
11. Cross-References

---

## 4. Status Lifecycle

```text
proposed → accepted → revised → superseded
proposed → rejected
```

- `proposed`: design under review; implementation may prototype behind flags only.
- `accepted`: implementation must follow it.
- `revised`: minor semantic change merged without replacing the decision identity.
- `superseded`: a later ADR replaces this one for future implementation.
- `rejected`: explicitly not adopted.

Do not move `superseded` back to `accepted`. Write a new ADR if a decision returns in a
materially different form.

---

## 5. Cross-Reference Format

- First mention in prose: `v0-0047 (Agent Charter)`.
- Dependency list: `Depends on: v0-0044, v0-0051`.
- Inline references: `See v0-0050 OperationContext propagation`.
- Avoid bare phrases like "the charter ADR" — the governance set has several charter-adjacent ADRs.
- File paths: use repo-relative paths: `docs/adr/ADR-0001-element-identity-and-polymorphic-serialization-envelope.md` (archived v0 records: `docs/_archive/v0/ADR-0047-agent-charter.md`).
- When overlaps exist, name the type owner explicitly. Example:
  `GateResult is owned by v0-0044; v0-0050 embeds references to it.`

**Good**:

```markdown
This module emits `GateResult` (v0-0044) and stores a reference in `OperationContext`
(v0-0050). See docs/_archive/v0/ADR-0044-tool-gates.md for the v0 record of the type.
```

**Bad** — ambiguous reference:

```markdown
This follows the gate ADR for result types.
```

---

## 6. When To Write A New ADR

Write a new ADR when:

- The public API shape changes.
- A runtime invariant changes.
- A previously accepted ADR becomes false.
- A new persistence or retention guarantee is introduced.
- A security boundary changes.
- A governance primitive gains a new owner.

---

## 7. When To Revise An Existing ADR

Revise (using `Status: revised`) when:

- Clarifying terms without changing behavior.
- Importing a canonical type owner instead of duplicating it.
- Correcting a filename or section reference.
- Adding test or trace requirements to match an already accepted decision.
- Adding non-contradictory examples from DSL or P9 tracing standards.

---

## 8. Type Ownership Table

Each governance type has exactly one ADR owner. Other ADRs may reference or import the type
but must not redefine it.

| Type | Owner (v0 ADR) |
|------|-----------|
| `ImmutableEvidenceNode`, `EvidenceRef`, `EvidenceChain` | v0-0041 |
| `TaskCertificate`, `CertificateState`, `Defensibility` | v0-0042 |
| `GovernedToolMeta`, governed tool declaration fields | v0-0043 |
| `GateResult`, `GateEnforcement`, `ToolGate` | v0-0044 |
| `BreakGlassWindow`, `BreakGlassEvent` | v0-0045 |
| `PermitToken`, `JITGrant` | v0-0046 |
| `AgentCharter`, `CharterConstraint` | v0-0047 |
| `SoDPolicy`, `RoleAssignment` | v0-0048 |
| `LogTier` | v0-0049 |
| `OperationContext`, `ServiceContext` | v0-0050 |
| `ToolRegistryPolicy`, `RegistryEntry` | v0-0051 |
| `PolicyBundle`, `PolicyRelease`, `PolicyResolver` | v0-0052 |

**Critical**: v0-0044 and v0-0050 both currently sketch `GateResult`. P12 must consolidate
ownership to v0-0044 before any implementation begins.

---

## 9. Example A: Correct Header And Dependency Block

```markdown
# ADR-9001: Example — Compiler Targets For A New DSL Feature

Status: proposed
Date: 2026-05-27
Decision owners: @governance-maintainers
Supersedes: none
Superseded by: none
Depends on: v0-0041, v0-0044, v0-0047, v0-0051, v0-0052
Related: v0-0048, v0-0049, v0-0050
```

Why correct: dependencies name the owning ADRs for evidence, gates, charters, registry, and
policy resolution — not generic prose. (ADR-9001 is a fictional example number; use the next
available real number when writing an actual ADR.)

---

## 10. Example B: Resolving A Type Overlap

```markdown
# ADR-9002: Example — Type Ownership Consolidation

Status: accepted
Date: 2026-06-03
Decision owners: @governance-maintainers
Supersedes: none
Superseded by: none
Depends on: v0-0044, v0-0050
Related: v0-0043

## Decision

`GateResult` is owned by v0-0044. v0-0050 may store `gate_result_ids` and gate summary
projections in `OperationContext`, but must not define a second `GateResult` type.
```

Why correct: resolves overlap by choosing a type owner instead of letting two ADRs drift.
(ADR-9002 is a fictional example number; use the next available real number when writing an
actual ADR.)
