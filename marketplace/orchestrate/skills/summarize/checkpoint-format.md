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

For quick captures that don't warrant a file, delegate the capture to a sub-agent through
the plugin's MCP server rather than writing it out inline. `agent.submit` is a spawn verb —
ask for its current `schema_fingerprint` before the first call:

```json
{"help": "agent.submit"}
```

Then submit:

```json
{
  "ops": [
    {
      "op": "agent.submit",
      "args": {
        "query": ["claude"],
        "cwd": "/absolute/path/to/repository",
        "prompt": "Summarize this progress on [topic] in 5 bullet points: <decisions, changes, results, and next steps>"
      },
      "schema_fingerprint": "<from the help call above>"
    }
  ]
}
```

The reply carries a `run_id`, not the summary. Check the submit op's `ok` field, then wait
for the run in a separate call:

```json
{"ops": [{"op": "job.wait", "args": {"run_ids": ["<run_id>"]}}]}
```

`job.wait` is bounded. Check its op's `ok` field and the result's `all_terminal` field,
repeating while the run is still pending. Then read the console and artifact list:

```json
{"ops": [{"op": "job.output", "args": {"run_id": "<run_id>"}}]}
```

Run state is persisted under `~/.lionagi/runs/{run_id}/`; `job.output` reports the console,
artifacts, and run directory separately.

**Checkout-local alternative.** Inside a lionagi checkout,
`li agent claude --cwd "$(pwd)" --prompt "Summarize progress on [topic] in 5 bullet points"`
does the same capture as a foreground call. It needs no flag to persist: the CLI writes every
run under `~/.lionagi/runs/{run_id}/` on its own, so the difference from the MCP form is only
that you read the result directly instead of doing a `run_id` round-trip. (`--save DIR`
exists on `li o fanout` and `li o flow`, which write one artifact per worker, but not on
`li agent`.)

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
Want me to write a checkpoint or prepare a full session summary?
```

## Quality Guide

**Include**:
- Concrete achievements with impact
- Decisions with alternatives considered
- Reusable patterns with "when to use"
- Precise file paths; use absolute paths when passing them to tools
- What's next

**Skip**:
- Routine operations
- Verbose tool output
- Things that don't help future recall
