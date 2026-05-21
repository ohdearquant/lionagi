---
name: summarize
description: >
  Mid-session context capture and proactive decision/pattern capture. Use when: significant
  progress made but session continues, approaching context limits, switching topics, checkpoint
  learnings, significant decisions made, patterns emerge, or session is winding down.
  Lighter than /session-summarize — stores to memory and continues.
allowed-tools: [Bash, Read, Write, Edit, Glob, Grep, mcp__khive__memory]
---

# Summarize (Mid-Session)

Capture context, learnings, and progress without ending the session. Store to memory, then continue.

## When to Use

- Significant milestone reached but more work ahead
- Switching to a different topic within same session
- Context getting long (>100k tokens) — checkpoint before compaction
- Ocean says "summarize", "capture this", "checkpoint"
- After completing a multi-step task, before starting the next

**Not** for session-ending summaries — use `/session-summarize` for that.

## The Workflow

### 1. Gather Context

Scan recent work to identify:
- What was accomplished
- Key decisions made (with rationale)
- Ocean's guidance (verbatim quotes)
- Patterns discovered
- Files modified
- Open threads / next steps

### 2. Store to Memory

Store the summary as episodic memory:

```python
memory.remember(
    content="""CHECKPOINT: {topic}

## Accomplished
- {achievement 1}
- {achievement 2}

## Decisions
- {decision}: {rationale}

## Ocean's Guidance
- "{quote}" — context: {why it matters}

## Key Learnings
- {insight 1}
- {insight 2}

## Files Modified
- {absolute/path/to/file} — {what changed}

## Next Steps
- {what to do next}
""",
    memory_type="episodic",
    importance=0.85,
)
```

For particularly important insights, store separately as semantic memory:

```python
memory.remember(
    content="PATTERN: {pattern_name} — {description}. Use when: {conditions}. Example: {brief example}.",
    memory_type="semantic",
    importance=0.9,
)
```

### 3. Optionally Write Checkpoint File

For substantial milestones, write `.khive/notes/checkpoints/checkpoint_YYYYMMDD_HHMMSS_{topic}.md`
with frontmatter (timestamp, lambda_id, topic, status: continuing) and sections: Progress,
Decisions (table with rationale + alternatives), Learnings, Next Steps.

### 4. Continue Working

After storing, resume work. Reference the checkpoint if needed:

```python
memory.recall(query="CHECKPOINT {topic}", limit=3)
```

## Proactive Capture Triggers

Fire a `memory.remember` call immediately when any of these occur — don't wait for Ocean to ask:

| Trigger | Action |
|---|---|
| **Decision made** | Store decision + rationale + alternatives considered |
| **Pattern discovered** | Store semantic memory with confidence score |
| **Significant work completed** | Episodic capture of what was done + outcome |
| **Problem solved** | Store approach + what worked/didn't |
| **Session winding down** | Offer to run `/session-summarize` or auto-capture key points |
| **Ocean expresses intent** | Note goals for future reference |

**Session wind-down signals**: Ocean says "thanks", "that's it", "done for now"; long pause after significant work; context switches to unrelated topic; time indicators ("gotta go", "wrapping up").

When wind-down detected, offer:
```
Before you go — quick capture of this session:
- [Key thing 1]
- [Key thing 2]
- [Decision made about X]
Want me to store this? (or run full /session-summarize)
```

## Decision & Pattern Capture

### Memory templates for inline capture

**Decision** (architecture choice, approach selection, trade-off):
```python
memory.remember(
    content="Decision: {what}. Chose {choice} over {alternatives}. Rationale: {why}.",
    memory_type="episodic", importance=0.85,
)
```

**Lesson learned** (unexpected failure or success):
```python
memory.remember(
    content="Lesson: {what_learned}. Context: {situation}. Applies when: {conditions}.",
    memory_type="semantic", importance=0.9,
)
```

### What's worth capturing

**Always capture (importance ≥ 0.8)**: architectural decisions, technology choices with rationale,
bug root causes + fixes, performance optimizations, security considerations, integration patterns,
Ocean's explicit preferences.

**Capture when significant (importance 0.6–0.8)**: refactoring approaches, test strategies,
debugging techniques, file organization decisions, naming conventions.

**Skip**: routine edits, typo fixes, standard boilerplate, obvious patterns already well-known.

## Quality Guide

### Include
- Concrete achievements with impact
- Decisions with alternatives considered
- Ocean's exact words with context
- Reusable patterns with "when to use"
- File paths (always absolute)
- What's next

### Skip
- Routine operations
- Verbose tool output
- Things that don't help future recall

## Key Principles

- **Fast > thorough**: This is a checkpoint, not a dissertation. 2-5 minutes max.
- **Memory-first**: Always store to memory. File is optional.
- **Continue after**: This skill does NOT end the session.
- **Compound**: Multiple checkpoints per session is fine — they build a trail.
- **Searchable**: Use clear prefixes (CHECKPOINT, PATTERN, DECISION, LESSON) for future recall.
- **Silent capture**: Don't interrupt Ocean's flow. Capture at natural breaks, not mid-thought.
- **Don't duplicate**: Check if pattern already stored before adding.

## Anti-Patterns

- Writing a full session summary (use `/session-summarize` for that)
- Spending >5 minutes on the checkpoint
- Skipping memory storage and only writing a file
- Not capturing Ocean's guidance when given
- Generic summaries without specifics ("worked on stuff")
- Over-capturing: not every line of code matters
