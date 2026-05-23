---
name: summarize
description: >
  Mid-session context capture and proactive decision/pattern capture. Use when: significant
  progress made but session continues, approaching context limits, switching topics, checkpoint
  learnings, significant decisions made, patterns emerge, or session is winding down.
  Lighter than /session-summarize — captures progress and continues.
allowed-tools: [Bash, Read, Write, Edit, Glob, Grep]
---

# Summarize (Mid-Session)

Capture context, learnings, and progress without ending the session. Write a checkpoint, then continue.

## When to Use

- Significant milestone reached but more work ahead
- Switching to a different topic within the same session
- Context getting long (>100k tokens) — checkpoint before compaction
- After completing a multi-step task, before starting the next
- Patterns or decisions have emerged that should be recorded

**Not** for session-ending summaries — use `/session-summarize` for that.

## Proactive Capture Triggers

Fire a capture immediately when any of these occur:

| Trigger | Action |
|---|---|
| **Decision made** | Record decision + rationale + alternatives considered |
| **Pattern discovered** | Note the pattern with "when to use" conditions |
| **Significant work completed** | Capture what was done + outcome |
| **Problem solved** | Record approach + what worked / what didn't |
| **Session winding down** | Offer to run `/session-summarize` or auto-capture key points |
| **Topic switch** | Quick checkpoint before context shifts |

**Session wind-down signals**: "thanks", "that's it", "done for now", long pause after significant
work, context switch to unrelated topic, time indicators ("gotta go", "wrapping up").

## What's Worth Capturing

**Always**: architectural decisions, technology choices with rationale, bug root causes + fixes,
performance optimizations, security considerations, integration patterns.

**When significant**: refactoring approaches, test strategies, debugging techniques,
file organization decisions, naming conventions.

**Skip**: routine edits, typo fixes, standard boilerplate, obvious patterns already documented.

## Key Principles

- **Fast > thorough**: This is a checkpoint, not a dissertation. 2-5 minutes max.
- **File-first**: Write to `./notes/checkpoints/` so checkpoints survive context resets.
- **Continue after**: This skill does NOT end the session.
- **Compound**: Multiple checkpoints per session is fine — they build a trail.
- **Searchable**: Use clear prefixes (CHECKPOINT, PATTERN, DECISION, LESSON) for future grep.
- **Silent capture**: Don't interrupt flow. Capture at natural breaks, not mid-thought.

## Anti-Patterns

- Writing a full session summary mid-session (use `/session-summarize`)
- Spending >5 minutes on the checkpoint
- Only commenting in-line without writing a retrievable file
- Generic summaries without specifics ("worked on stuff")
- Over-capturing: not every line of code matters

See [checkpoint-format.md](checkpoint-format.md) for detailed checkpoint template,
memory type distinction, decision/pattern/lesson capture templates, and quality guide.

## Relevant Source Files

- `lionagi/cli/_runs.py` — run persistence at `~/.lionagi/runs/{run_id}/`
- `lionagi/cli/_logging.py` — structured logging conventions used in CLI sessions
- `lionagi/cli/agent.py` — `li agent --save` for artifact persistence
