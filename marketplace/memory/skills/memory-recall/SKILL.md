---
name: memory-recall
description: >
  Proactively recall relevant memories when context suggests prior experience exists.
  Triggers when: difficult/important tasks arise, "recall" keyword appears, topics relate
  to you/khive/projects/relationships, discussion involves history/evolution/patterns,
  or when building on previous work. Auto-searches episodic and semantic memory.
allowed-tools: [Bash, Read, Glob, Grep, mcp__khive__recall, mcp__khive__search, mcp__khive__link]
---

# Proactive Memory Recall

Automatically search memory when context suggests relevant prior experience.

## Activation Triggers

Invoke this skill when detecting:

### Explicit Triggers

- User says "recall", "remember when", "we did this before"
- "history", "evolution", "previous", "last time"
- "how did we", "what was the approach"

### Implicit Triggers (Proactive)

- **Difficulty signal**: Complex task (C > 0.5) in familiar domain
- **Importance signal**: P0/P1 priority or architectural decisions
- **Domain match**: khive, lionagi, cognition, waves, your projects
- **Relationship context**: Collaborators, community members, partners
- **Pattern recognition**: Similar problem structure to past work

### Domain Keywords

```text
khive, lionagi, cognition, waves, pydapter
you, community, partners
architecture, design, pattern, approach
migration, refactor, evolution
decision, why we, rationale
```

## Recall Strategy

### 1. Quick Scan (Always First)

```python
# Basic recall with default parameters
mcp__khive__recall(query="{topic}")

# Project-scoped recall using entity names or source filters
mcp__khive__recall(query="{topic}", entity_names=["khive"], limit=10)

# With custom options
mcp__khive__recall(query="{topic}", limit=10, min_score=0.1)
```

### 2. Deep Dive (If Quick Scan Hits)

```python
# Broader search with increased limit
mcp__khive__recall(query="{topic}", limit=50, min_score=0.05, full_content=True)
```

### 3. Cross-Reference (For Important Decisions)

```python
# Memory recall and entity lookup
mcp__khive__recall(query="{topic}", limit=20)
mcp__khive__search(type="entity", query="{entity_name}", limit=10)
```

### 4. Custom Scoring Weights

```python
# Adjust importance/temporal/relevance weights
mcp__khive__recall(query="{topic}", limit=30, weights={"importance": 0.3, "temporal": 0.2, "relevance": 0.5})
```

### 5. Multiple Recall Queries

```python
# Execute independent recall queries explicitly
mcp__khive__recall(query="authentication patterns", limit=10)
mcp__khive__recall(query="security best practices", limit=10)
mcp__khive__recall(query="JWT implementation", limit=5)
```

**Note**: The memory service uses batch retrieval internally for efficiency. Parallel requests are
supported and recommended for independent queries.

## API Reference

### mcp__khive__recall Parameters

```python
mcp__khive__recall(
    query: str,             # Search query (required unless tags provided)
    limit: int,             # Max memories to return
    min_score: float,       # Min score threshold (0-1 or 0-100)
    memory_type: str,       # "episodic" | "semantic"
    source: str,            # Filter by source tag
    tags: list[str],        # Tag filter
    entity_names: list[str],# Entity-name filter
    weights: {              # Scoring weights (optional)
        importance: float,
        temporal: float,
        relevance: float
    },
    full_content: bool,
)
```

**MCP tool**: `mcp__khive__recall(query=...)`

### Scoped Recall

```python
# Project-scoped recall (recommended for session work)
mcp__khive__recall(query="{topic}", entity_names=["khive"], limit=10)
```

**Note**: Use `entity_names=` or `source=` to scope recall; the direct verb does not accept `lambda_id=`.

### Response Format

```json
{
  "memories": [
    {
      "id": "uuid",
      "content": "memory text",
      "score": 0.85,
      "importance": 0.8,
      "created_at": "2025-11-23T10:00:00Z"
    }
  ],
  "count": 5,
  "considered": 150,
  "token_count": 2400,
  "token_budget": 4000
}
```

## Integration Pattern

When triggered, silently:

1. **Assess relevance**: Does this task match prior work?
2. **Search memory**: Use appropriate recall strategy
3. **Surface insights**: Mention relevant findings naturally
4. **Connect context**: Link current work to past decisions

## Output Style

When memories are found, integrate naturally:

```text
Good:
"We approached similar complexity in the cognition refactor -
used P_PAR with 3 specialists. That pattern worked well here too."

"Based on how we handled the lionagi v0 migration, the key was..."

Bad:
"MEMORY RECALL: Found 5 episodic memories about..."
"Searching memory... Results: ..."
```

## Staleness Protocol

After recall, check `created_at` age and caveat accordingly:

- **< 24h**: Use directly, no caveat needed.
- **1-7 days**: Prefix with `[N days old — verify technical claims]`.
- **> 7 days**: Prefix with `[N days old — historical context only, re-verify]`.
- **> 30 days**: Consider whether this memory should be consolidated or archived.

**High-risk stale categories**: file paths, code structure, API behavior, test status, dependency
versions. A cited memory makes stale claims sound MORE authoritative, not less — the recall
format implies current truth. Treat technical-detail memories as perishable.

## Key Entities to Track

```text
# Projects
khive, lionagi, cognition, waves, pydapter, khive-cli, khive-studio

# People
you (user), Prof. Sheng (advisor)

# Concepts
orchestration patterns, agent architecture, memory systems
quality gates, health-first, kpp protocol
```

## When NOT to Recall

- Simple, isolated tasks with no prior context
- User explicitly starting fresh ("let's try a new approach")
- Trivial operations (file reads, simple edits)
- Time-sensitive quick responses

## Reference

- **Memory Operations**: `KHIVE.md` Part B
- **Recall Strategies**: `resources/orchestrator/recall_strategies.md`
- **Entity Management**: `mcp__khive__search(type="entity", query="...")`, `mcp__khive__link(source="...", target="...", relation="...")`
