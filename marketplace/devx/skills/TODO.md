# Absent Skills — Deliberate Omission

The `session-start` and `session-summarize` skills are intentionally not shipped
in this bundle. They depend on project-specific memory context and identity files
that differ per installation and cannot be made portable without a copy step.

When a future PR adds these, the source files should be copied from:
- `resources/orchestrator/identity_continuity.md` (session-start)
- The canonical `/session-summarize` skill in the operator's CC installation

The `/summarize` skill in `marketplace/devx/skills/summarize/` IS shipped and
covers mid-session context capture. It references `/session-summarize` as an
optional escalation path — that escalation works if the operator has the skill
installed locally, and is a no-op otherwise.

See ADR-0003 §Devx for the full rationale.
