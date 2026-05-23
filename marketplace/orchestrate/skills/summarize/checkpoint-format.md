# Summarize: Checkpoint Format & Memory Patterns

Detailed reference for checkpoint structure, memory types, and capture templates.

## Checkpoint File Location and Structure

For substantial milestones, write a checkpoint to the project notes directory:

```
./notes/checkpoints/checkpoint_YYYYMMDD_HHMMSS_{topic}.md
```

Use this structure:

```markdown
---
timestamp: YYYY-MM-DDTHH:MM:SS
agent_id: [agent or session identifier, if known]
topic: [short topic slug]
status: continuing
---

## Progress

- [achievement 1]
- [achievement 2]

## Decisions

| Decision | Choice | Alternatives | Rationale |
|---|---|---|---|
| [what] | [chosen] | [others] | [why] |

## Learnings

- [insight 1]
- [insight 2]

## Next Steps

- [what to do next]
```

## Episodic Capture (inline, no file needed)

For quick captures that don't warrant a file, write a brief structured note to the run log:

```bash
# Session artifacts land in ~/.lionagi/runs/{run_id}/artifacts/
# For a running li agent session, --save persists the transcript automatically
li agent --save --prompt "Summarize progress on [topic] in 5 bullet points"
```

## Continue Working

After checkpointing, resume work. Reference the checkpoint if context is lost:

```bash
# Find prior checkpoints for a topic
ls ./notes/checkpoints/ | grep "topic_slug"
# Or grep recent runs
grep -r "CHECKPOINT" ~/.lionagi/runs/ --include="*.json" -l | sort -r | head -5
```

## Memory Type Distinction

This skill respects the episodic / semantic distinction:

- **Episodic** (what happened): accomplished work, decisions made, files changed, problems solved.
  These are time-bound and tied to a specific session or milestone.
- **Semantic** (how things work): patterns, principles, architectural insights, reusable techniques.
  These transcend individual sessions and should be written to the project notes for long-term reference.

Write episodic captures to checkpoint files (timestamped). Write semantic captures to a persistent
notes file (e.g., `./notes/patterns.md` or `./notes/architecture.md`).

## Decision & Pattern Capture Templates

**Decision** (architecture choice, approach selection, trade-off):
```markdown
## Decision: [what]
- **Chose**: [choice]
- **Over**: [alternatives]
- **Rationale**: [why]
- **Date**: YYYY-MM-DD
```

**Lesson learned** (unexpected failure or success):
```markdown
## Lesson: [what was learned]
- **Context**: [situation in which it arose]
- **Applies when**: [conditions]
- **Source**: [file or run that surfaced it]
```

**Pattern** (reusable technique or structure):
```markdown
## Pattern: [name]
- **Description**: [what it is]
- **Use when**: [conditions]
- **Example**: [brief reference]
```

## Session Wind-Down Response Template

When wind-down detected ("thanks", "that's it", "done for now", long pause, topic switch,
"gotta go", "wrapping up"), offer:

```
Before you go — quick capture of this session:
- [Key thing 1]
- [Key thing 2]
- [Decision made about X]
Want me to write a checkpoint? (or run full /session-summarize)
```

## Quality Guide

**Include**:
- Concrete achievements with impact
- Decisions with alternatives considered
- Reusable patterns with "when to use"
- File paths (always absolute)
- What's next

**Skip**:
- Routine operations
- Verbose tool output
- Things that don't help future recall
