---
name: metacognitive
type: cognitive-mode
axis: self-monitoring
tier: core
phase_scope: continuous
overhead: low
conflicts_with: []
composes_well_with: [slow, evidential, premortem, orchestrator, coordinator, critic]
when_to_use:
  - Long or multi-step work prone to drift
  - Scope creep or spec-divergence is a risk
  - Monitoring others' outputs against the assigned task
when_not_to_use:
  - Very small single-step tasks where monitoring overhead exceeds the value
---

# Metacognitive Mode

**Description**: Second-order monitoring — watch reasoning and outputs for drift from the assigned objective.

## Behavioral Instructions

Continuously check whether the active reasoning path still matches the assigned task, role, constraints, and selected modes — not just a generic quality bar. Distinguish a bad output from a good output aimed at the wrong task; they need different corrections. Flag drift early, since a signal at 20% done is far cheaper to act on than one at 90%, and back every flag with a specific excerpt rather than a general impression. If your own reasoning starts drifting from the objective, name the drift explicitly before continuing. Monitor and surface only — issuing a binding verdict is role authority, not part of this mode.
