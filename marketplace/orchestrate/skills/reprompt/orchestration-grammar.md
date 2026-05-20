# Operator Grammar & Strategic Intent

λ parses operators not just as syntax, but as **strategic opportunities**.

## Operators

| Symbol    | Meaning   | Strategic Opportunity                                                       |
| --------- | --------- | --------------------------------------------------------------------------- |
| `\|\|`    | Separator | **Default: delimiter**. Upgrade to P_PAR only after independence check      |
| `&&`      | Sequence  | Build context. **Twist**: Can I parallelize if tasks touch different files? |
| `->`      | Flow      | Data dependency. Output of A _must_ feed B. Use P_FLOW                      |
| `for x`   | Iteration | Batching. Unroll to P_PAR when items are independent                        |
| `if/else` | Branch    | Competition. High uncertainty? Use P_CHO (tournament)                       |
| `end:`    | Gate      | Definition of Done. Hard validation, no exit without evidence               |

## Independence Check (Critical for Safe Parallelism)

Before inferring `P_PAR`, verify:

```
Independent(τ₁, τ₂) iff:
  - No shared write targets (or writes are commutative)
  - No intermediate output dependencies
  - Neither is a correctness gate for the other
```

**If uncertain**: Default to `P_SEQ`. The Coward anti-pattern is preferable to The Zombie.

## Creative Heuristics (Override Defaults When Logical)

| Heuristic                | Trigger                                        | Action                                      |
| ------------------------ | ---------------------------------------------- | ------------------------------------------- |
| **The Hidden Parallel**  | User said `&&` but tasks touch different files | Upgrade to P_PAR                            |
| **The Fast Fail**        | High-risk step at end of sequence              | Move to Phase 0 (fail early)                |
| **The Speculative Race** | Ambiguous solution, high downstream cost       | Use P_CHO (tournament), let results decide  |
| **The Hybrid**           | Systemic task (C ≥ 0.8)                        | Nest P_PAR inside P_MULT phases             |
| **The Probe**            | Uncertainty Ξ(ψ) > 0.7                         | Run 5-min discovery before committing       |
| **The Checkpoint**       | C(τ) ≥ 0.8                                     | Insert validation gates between every phase |
| **The Mapper**           | Cross-crate/cross-module task                  | Build invariant→code mapping before edits   |

**Logging requirement**: When applying a heuristic, log rationale in `validation.md`.

## AST Format

Write to `ast.yaml`:

```yaml
raw: "<original prompt>"
nodes:
  - id: N1
    kind: loop|seq|par|choice|gate|step
    op: for|if|&&|->|PAR|SEP|end
    text: "<source fragment>"
    children: [N2, N3]
meta:
  iterator: { var: "c", values: ["khive-types", "khive-score"] }
  exit: "end: correct, aligned, committed"
  cross_cutting: ["cross-crate consistency"]
assumptions:
  - id: ASSUMPTION-1
    text: "|| treated as SEP (delimiter)"
    falsifiable: "if user says 'parallel', reparse"
```
