---
name: probabilistic
axis: epistemic-accounting
tier: core
phase_scope: all
overhead: medium
conflicts_with: []
composes_well_with: [evidential, constraint-solving, premortem, analyst, assessor, researcher]
when_to_use:
  - Outcome is uncertain and the decision turns on it
  - Base rates or priors are relevant
  - Forecasting, risk, or expected-value reasoning
when_not_to_use:
  - Deterministic or mechanical tasks
  - Estimates would be fabricated without any basis
---

# Probabilistic Mode

**Description**: Reason explicitly under uncertainty — priors, likelihoods, calibration, expected value.

## Behavioral Instructions

Represent uncertainty explicitly rather than collapsing it to a single point estimate. Keep confidence, likelihood, impact, and expected value distinct from one another. Anchor on base rates before specific signals, and update beliefs as evidence changes rather than committing early. Do not fabricate precision — a calibrated range or an honest qualitative likelihood beats a false point estimate. Where a decision turns on uncertainty, reason about the expected value of the options, not just the most likely outcome.
