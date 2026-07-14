# Research Synthesis

Research a topic with `li agent`, then resume the same branch to synthesize a report.
Two commands. One branch. Full context carried across both turns.

## Setup

```bash
pip install lionagi          # or: uv add lionagi
# claude — npm install -g @anthropic-ai/claude-code && claude login
```

## Command

```bash
li agent claude/sonnet \
  "Research the tradeoffs of event-sourcing vs CQRS: consistency, operational complexity, and ecosystem maturity"
```

```text
# output:
Event sourcing records every state change as an immutable log entry, giving you a
complete audit trail and point-in-time replay. CQRS separates read and write models,
allowing each side to scale independently...
[...continued response...]

[to resume] li agent -r 01965a3b-c4d2-7abc-def0-123456789abc "..."
```

Copy the branch ID from the `[to resume]` hint. Pass it to `-r`:

```bash
# -r restores the branch snapshot; find_branch() scans ~/.lionagi/runs/ automatically
li agent -r 01965a3b-c4d2-7abc-def0-123456789abc \
  "Synthesize your findings into a markdown report with a tradeoff table and a recommendation"
```

```text
# output:
## Event Sourcing vs CQRS: Synthesis

| Dimension            | Event Sourcing          | CQRS                  |
|----------------------|-------------------------|-----------------------|
| Consistency          | Strong — full event log | Eventual on read side |
| Operational overhead | High — replay infra     | Medium — two models   |
| Ecosystem maturity   | Moderate                | Mature in DDD circles |

**Recommendation**: prefer CQRS when read/write load is asymmetric...

[to resume] li agent -r 01965a3b-c4d2-7abc-def0-123456789abc "..."
```

To continue without copying the branch ID:

```bash
# -c reads ~/.lionagi/last_branch.json — no ID lookup needed
li agent -c "Add concrete implementation examples for the CQRS write side"
```

## Next

- [Multi-model pipeline](multi-model-pipeline.md) — add dependency edges between agents in a DAG
- [CLI reference: `li agent`](../cli-reference.md#li-agent) — all flags
