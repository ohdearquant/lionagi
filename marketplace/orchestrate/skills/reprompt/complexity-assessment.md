# Complexity Assessment C(τ)

## Signals

- `F`: Files touched
- `I`: Integration points (crate boundaries, proofs, schemas)
- `D`: Dependency depth
- `N`: Novelty (0-10)
- `R`: Risk/criticality (0-10)

## Formula

```
C(τ) := clamp01(0.22*log1p(F) + 0.22*log1p(I) + 0.18*D + 0.18*(N/10) + 0.20*(R/10))
```

**Bias rule**: If you think "simple", add +0.1 unless you can name exact invariants + gates.

## Quick Assessment

| C(τ)    | Class    | Time Est  | What It Feels Like                        |
| ------- | -------- | --------- | ----------------------------------------- |
| < 0.3   | Trivial  | 2-5m      | "I know exactly what to do"               |
| 0.3-0.6 | Standard | 10-20m    | "Clear path, needs testing"               |
| 0.6-0.8 | High     | 30-60m    | "Multiple moving parts, need coordination"|
| ≥ 0.8   | Systemic | 1-2h      | "Cross-cutting, phased, needs validation" |

## Requirements Expansion

Write `requirements.md` with stable IDs:

| ID Type  | Meaning                | Example                            |
| -------- | ---------------------- | ---------------------------------- |
| `REQ-E*` | Explicit (from prompt) | "Check ADR against proof"          |
| `REQ-I*` | Implicit (inferred)    | "Locate ADR files per crate"       |
| `CON-*`  | Constraint             | "Max 4 foreground agents"          |
| `EXIT-*` | Exit gate              | "All tests pass"                   |
| `XCON-*` | Cross-cutting          | "Cross-crate contract consistency" |
