# Governance ADR Style Standard

**Purpose**: File naming, required sections, status lifecycle, cross-reference format, and type
ownership rules for governance ADRs (ADR-0041 and later).

Cross-references: [dsl-style.md](dsl-style.md), [trace-naming.md](trace-naming.md),
[commit-and-pr-style.md](commit-and-pr-style.md)

---

## 1. File And Title

- File: `docs/_archive/v0/ADR-NNNN-kebab-case-title.md`.
- First line: `# ADR-NNNN: Human Title`.
- Numbers are never reused.
- Keep the title stable after acceptance unless a replacement ADR supersedes it.

**Note**: ADR-0043 currently exists as `ADR-0043-governed-tool-declaration.md`. Use that file
name in references unless the file is deliberately renamed in a revision PR.

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
Depends on: ADR-0041, ADR-0050
Related: ADR-0044, ADR-0052
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

- First mention in prose: `ADR-0047 (Agent Charter)`.
- Dependency list: `Depends on: ADR-0044, ADR-0051`.
- Inline references: `See ADR-0050 OperationContext propagation`.
- Avoid bare phrases like "the charter ADR" — the governance set has several charter-adjacent ADRs.
- File paths: use repo-relative paths: `docs/_archive/v0/ADR-0047-agent-charter.md`.
- When overlaps exist, name the type owner explicitly. Example:
  `GateResult is owned by ADR-0044; ADR-0050 embeds references to it.`

**Good**:

```markdown
This module emits `GateResult` (ADR-0044) and stores a reference in `OperationContext`
(ADR-0050). See docs/_archive/v0/ADR-0044-tool-gates.md for the canonical type.
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

| Type | Owner ADR |
|------|-----------|
| `ImmutableEvidenceNode`, `EvidenceRef`, `EvidenceChain` | ADR-0041 |
| `TaskCertificate`, `CertificateState`, `Defensibility` | ADR-0042 |
| `GovernedToolMeta`, governed tool declaration fields | ADR-0043 |
| `GateResult`, `GateEnforcement`, `ToolGate` | ADR-0044 |
| `BreakGlassWindow`, `BreakGlassEvent` | ADR-0045 |
| `PermitToken`, `JITGrant` | ADR-0046 |
| `AgentCharter`, `CharterConstraint` | ADR-0047 |
| `SoDPolicy`, `RoleAssignment` | ADR-0048 |
| `LogTier` | ADR-0049 |
| `OperationContext`, `ServiceContext` | ADR-0050 |
| `ToolRegistryPolicy`, `RegistryEntry` | ADR-0051 |
| `PolicyBundle`, `PolicyRelease`, `PolicyResolver` | ADR-0052 |

**Critical**: ADR-0044 and ADR-0050 both currently sketch `GateResult`. P12 must consolidate
ownership to ADR-0044 before any implementation begins.

---

## 9. Example A: Correct Header And Dependency Block

```markdown
# ADR-9001: Example — Compiler Targets For A New DSL Feature

Status: proposed
Date: 2026-05-27
Decision owners: @governance-maintainers
Supersedes: none
Superseded by: none
Depends on: ADR-0041, ADR-0044, ADR-0047, ADR-0051, ADR-0052
Related: ADR-0048, ADR-0049, ADR-0050
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
Depends on: ADR-0044, ADR-0050
Related: ADR-0043

## Decision

`GateResult` is owned by ADR-0044. ADR-0050 may store `gate_result_ids` and gate summary
projections in `OperationContext`, but must not define a second `GateResult` type.
```

Why correct: resolves overlap by choosing a type owner instead of letting two ADRs drift.
(ADR-9002 is a fictional example number; use the next available real number when writing an
actual ADR.)
