---
name: summarize
description: >
  Mid-session context capture and proactive decision/pattern capture. Use when: significant
  progress made but session continues, approaching context limits, switching topics, checkpoint
  learnings, significant decisions made, patterns emerge, or session is winding down.
  Lighter than /session-summarize — stores to memory and continues.
allowed-tools: [Bash, Read, Write, Edit, Glob, Grep]
---

# Summarize (Mid-Session)

Capture context, learnings, and progress without ending the session. Store to memory, then continue.

## When to Use

- Significant milestone reached but more work ahead
- Switching to a different topic within same session
- Context getting long (>100k tokens) — checkpoint before compaction
- User says "summarize", "capture this", "checkpoint"
- After completing a multi-step task, before starting the next

**Not** for session-ending summaries — use `/session-summarize` for that.

## The Workflow

### 1. Gather Context

Scan recent work to identify:
- What was accomplished
- Key decisions made (with rationale)
- User's guidance (verbatim quotes)
- Patterns discovered
- Files modified
- Open threads / next steps

### 2. Write Checkpoint File

Write `./notes/checkpoints/checkpoint_YYYYMMDD_HHMMSS_{topic}.md` (or `$LIONAGI_NOTES_DIR` if set)
with frontmatter (timestamp, topic, status: continuing) and sections: Progress, Decisions (table
with rationale + alternatives), Learnings, Next Steps.

Checkpoint template:

```markdown
---
timestamp: 2026-05-21T22:30:00Z
topic: {topic}
status: continuing
---

# CHECKPOINT: {topic}

## Accomplished
- {achievement 1}
- {achievement 2}

## Decisions
| Decision | Chose | Over | Rationale |
|---|---|---|---|
| {decision} | {choice} | {alternatives} | {why} |

## User's Guidance
- "{quote}" — context: {why it matters}

## Key Learnings
- {insight 1}
- {insight 2}

## Files Modified
- {absolute/path/to/file} — {what changed}

## Next Steps
- {what to do next}
```

### 3. Continue Working

After writing the checkpoint, resume work. Future sessions can grep the checkpoints dir for
context: `grep -r "CHECKPOINT: {topic}" ./notes/checkpoints/`.

## Proactive Capture Triggers

Write a brief checkpoint note or append to the checkpoint file immediately when any of these
occur — don't wait for the user to ask:

| Trigger | Action |
|---|---|
| **Decision made** | Store decision + rationale + alternatives considered |
| **Pattern discovered** | Store semantic memory with confidence score |
| **Significant work completed** | Episodic capture of what was done + outcome |
| **Problem solved** | Store approach + what worked/didn't |
| **Session winding down** | Offer to run `/session-summarize` or auto-capture key points |
| **User expresses intent** | Note goals for future reference |

**Session wind-down signals**: User says "thanks", "that's it", "done for now"; long pause after significant work; context switches to unrelated topic; time indicators ("gotta go", "wrapping up").

When wind-down detected, offer:
```
Before you go — quick capture of this session:
- [Key thing 1]
- [Key thing 2]
- [Decision made about X]
Want me to store this? (or run full /session-summarize)
```

## Decision & Pattern Capture

### Inline capture templates

Write a one-liner to the checkpoint file:

**Decision** (architecture choice, approach selection, trade-off):
```
Decision: {what}. Chose {choice} over {alternatives}. Rationale: {why}.
```

**Lesson learned** (unexpected failure or success):
```
Lesson: {what_learned}. Context: {situation}. Applies when: {conditions}.
```

### What's worth capturing

**Always capture (importance ≥ 0.8)**: architectural decisions, technology choices with rationale,
bug root causes + fixes, performance optimizations, security considerations, integration patterns,
User's explicit preferences.

**Capture when significant (importance 0.6–0.8)**: refactoring approaches, test strategies,
debugging techniques, file organization decisions, naming conventions.

**Skip**: routine edits, typo fixes, standard boilerplate, obvious patterns already well-known.

## Quality Guide

### Include
- Concrete achievements with impact
- Decisions with alternatives considered
- User's exact words with context
- Reusable patterns with "when to use"
- File paths (always absolute)
- What's next

### Skip
- Routine operations
- Verbose tool output
- Things that don't help future recall

## Key Principles

- **Fast > thorough**: This is a checkpoint, not a dissertation. 2-5 minutes max.
- **File-first**: Always write a checkpoint file at `./notes/checkpoints/`. That's the storage.
- **Continue after**: This skill does NOT end the session.
- **Compound**: Multiple checkpoints per session is fine — they build a trail.
- **Searchable**: Use clear prefixes (CHECKPOINT, PATTERN, DECISION, LESSON) for future grep.
- **Silent capture**: Don't interrupt the user's flow. Capture at natural breaks, not mid-thought.
- **Don't duplicate**: Check if pattern already captured before appending.

## Anti-Patterns

- Writing a full session summary (use `/session-summarize` for that)
- Spending >5 minutes on the checkpoint
- Skipping the checkpoint file
- Not capturing the user's guidance when given
- Generic summaries without specifics ("worked on stuff")
- Over-capturing: not every line of code matters
