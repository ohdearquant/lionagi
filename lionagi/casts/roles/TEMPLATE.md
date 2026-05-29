---
# REQUIRED. Short lowercase identifier. Used in compose() and CLI flags.
name: role-name

# REQUIRED. Dense, orchestrator-facing selection signal — what the role does,
# when to pick it, and (folded into the prose) its rough effort level. This is
# NOT part of the prompt body; it is what an orchestrator reads to choose a role.
description: One to three sentences. What problem this role solves, when to select it over adjacent roles, and a rough effort signal (low/medium/high) woven into the sentence.
---

# Role Name

<!-- One paragraph mission line — the behavioral essence, written to be composed
     directly into a system prompt. Behavioral, not domain-specific; domain
     vocabulary belongs in packs. -->

## Principles

<!-- 4–6 load-bearing BEHAVIORAL rules — how the role reasons and acts. Each
     actionable, not aspirational. Bad: "Be thorough." Good: "Read the full spec
     before writing any output." Decision-rights and enforcement do NOT go here;
     they live in the pack (see below). -->

- [Principle 1.]
- [Principle 2.]
- [Principle 3.]
- [Principle 4.]

## Anti-Patterns

<!-- 4–5 failure modes specific to this role. Prefer "Doing X" — concrete acts,
     not vague tendencies. The mistakes this role is most likely to make. -->

- [Anti-pattern 1.]
- [Anti-pattern 2.]
- [Anti-pattern 3.]
- [Anti-pattern 4.]

## Artifacts

<!-- 1–3 concrete outputs. Name the artifact, not the process.
     "Source-annotated findings document" not "thorough research." -->

- [Artifact 1.]
- [Artifact 2.]

<!--
=== WHAT IS NOT IN THIS FILE ===

The role body is the prompt-facing behavioral contract: Mission + Principles +
Anti-Patterns + Artifacts. Keep it dense — the reader is an agent, respect the
token window, express the intent near-losslessly.

Operational policy — authority (decision-rights), boundaries (what requires
hand-off), and escalations (routed by capability_needed, never by named role) —
does NOT live here. It is runtime-facing and lives in a pack
(lionagi/casts/packs/default.yaml), keyed by this role's `name`. The orchestrator
/ operation node layers the policy onto the behavioral role at runtime. Users
plug in their own packs to override or extend the default.

=== LENGTH TARGET ===
~25–30 lines of body content. Shorter is better than padded.
-->
