# ADR-0088 steer-adherence table — EVIDENCE

**Pre-registered gate**: PASS if arm2-arm1 >= 0.4 absolute AND arm2 >= 0.8 on >= 2 of 4 providers AND arm0 <= 0.1

| provider | arm0_no_steer | arm1_steer_buried | arm2_steer_rendered |
|---|---|---|---|
| claude_code | 0.00 [0.00,0.16] | 0.55 [0.34,0.74] | 1.00 [0.84,1.00] |
| codex | 0.00 [0.00,0.16] | 0.00 [0.00,0.16] | 0.00 [0.00,0.16] |

Errored trials excluded from proportions: 0

**Gate result**: FAIL (1 of 2 complete providers clear; 2 of 2 providers have >= 20 valid trials on every arm)
