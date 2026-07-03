# ADR-0088 steer-adherence table — SMOKE (N far below pre-registered — NOT evidence)

**Pre-registered gate**: PASS if arm2-arm1 >= 0.4 absolute AND arm2 >= 0.8 on >= 2 of 4 providers AND arm0 <= 0.1

| provider | arm0_no_steer | arm1_steer_buried | arm2_steer_rendered |
|---|---|---|---|
| claude_code | 0.00 [0.00,0.66] | 1.00 [0.34,1.00] | 1.00 [0.34,1.00] |

Errored trials excluded from proportions: 0

**Gate result**: INCOMPLETE (0 of 0 complete providers clear; 0 of 1 providers have >= 20 valid trials on every arm)

This table is a SMOKE run (N=2/arm, claude_code only) proving the harness runs end-to-end and produces the table — it is NOT the pre-registered N>=20/cell evidence run and the gate result above is not a real verdict.
