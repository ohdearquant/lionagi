# ADR-0022: Run Step Provenance — Model, Agent, and Provider Disclosure

**Status**: Proposed
**Date**: 2026-05-21
**Extends**: ADR-0009 (SQLite state layer), ADR-0012 (execution lineage), ADR-0017 (session lifecycle)

## Context

Individual run steps don't disclose what model, provider, or agent
definition they used. The data is partially available but not persisted
or surfaced:

### What's missing

**Sessions** have `agent_name` (TEXT) — the agent profile name from
`-a reviewer`. But:

- No `model` column. The resolved model spec (`claude/claude-sonnet-4-6`,
  `openai/gpt-4.1`) is not stored.
- No `provider` column. Whether the run used Claude Code, Codex, OpenAI
  API, or Anthropic API is not recorded.
- No `agent_definition_hash`. The agent profile (`~/.lionagi/agents/reviewer.md`)
  can change between runs — there's no snapshot of which version was used.
- `agent_name` is only set for `li agent -a` runs. Flow ops and play
  agents get `NULL`.

**Branches** have `node_metadata` JSON which *can* hold `chat_model`, but:

- The write path in `agent.py` only copies `chat_model` if it's in
  `branch_dict` — and it's frequently missing (empty in 5/5 sampled rows).
- Flow ops create multiple branches with different models — the per-branch
  model is the right place, but it's not reliably written.
- No `provider` or `effort` in `node_metadata` either.

**The runs list** shows agent name and status but not model. The run
detail page shows messages but not which model produced them. For runs
via pre-configured agents (`-a reviewer`, `-a architect`), the agent
profile specifies the model — but the actual resolved model (after
defaults, overrides, and fallbacks) is not captured.

### Why this matters

1. **Debugging**: "Why did this run produce bad output?" — was it using
   sonnet when it should have been opus? Can't tell from the DB.
2. **Cost tracking**: Model choice drives cost. Without model info per
   session, cost attribution is impossible.
3. **Agent evolution**: Agent profiles change. If `reviewer.md` switches
   from sonnet to opus, historical runs should show what they actually
   used, not what the profile currently says.
4. **Compliance**: For future audit trails, knowing which model processed
   which data is a hard requirement.

## Decision

### Add first-class provenance columns to sessions

```sql
ALTER TABLE sessions ADD COLUMN model       TEXT;  -- resolved model spec: "claude/claude-sonnet-4-6"
ALTER TABLE sessions ADD COLUMN provider    TEXT;  -- provider name: "claude_code", "codex", "openai", "anthropic"
ALTER TABLE sessions ADD COLUMN effort      TEXT;  -- effort level: "low", "medium", "high", "xhigh"
ALTER TABLE sessions ADD COLUMN agent_hash  TEXT;  -- SHA-256 of agent definition file content at invocation time
```

These are **resolved values** — not what the config says, but what the
runtime actually used after all defaults, overrides, and fallbacks.

### Add first-class provenance to branches

```sql
ALTER TABLE branches ADD COLUMN model       TEXT;  -- branch-level model (may differ from session for multi-agent flows)
ALTER TABLE branches ADD COLUMN provider    TEXT;
ALTER TABLE branches ADD COLUMN agent_name  TEXT;  -- the agent role within a flow (e.g., "explorer", "analyst")
```

Branch-level provenance matters for flows where different agents use
different models. A flow session might use sonnet for the explorer and
opus for the critic — the session-level model is the "default" or
"primary" model; branch-level is the actual.

### Write points

| Event | Who writes | What |
|-------|-----------|------|
| `li agent` start | `agent.py` | Session: `model`, `provider`, `effort`, `agent_hash`, `agent_name` |
| `li play` start | `flow.py` | Session: `model` (default), `provider`, `effort`. Per-op branches: `model`, `provider`, `agent_name` |
| `li o flow` start | `flow.py` | Same as play |
| `li o fanout` start | `fanout.py` | Session: `model`, `provider`. Worker branches: `model`, `agent_name` |
| Branch creation (any) | `_orchestration.py` | Branch: `model`, `provider`, `agent_name` from the agent config |

### Agent hash computation

```python
import hashlib
from pathlib import Path

def agent_definition_hash(agent_name: str) -> str | None:
    """SHA-256 of the agent profile file content at invocation time."""
    from lionagi.utils import LIONAGI_HOME
    path = LIONAGI_HOME / "agents" / f"{agent_name}.md"
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
```

16-char truncated SHA-256 is sufficient for "same or different" checks.
Not a security hash — just a content fingerprint.

### Model resolution disclosure

The model string stored must be the **fully resolved** spec, not the
input. Resolution chain:

```text
User input: "sonnet"
  → parse_model_spec(): ModelSpec(model="claude/claude-sonnet-4-6")
  → agent profile override: (none, use parsed)
  → effort override: (none)
  → fast mode: (none)
  → RESOLVED: "claude/claude-sonnet-4-6"  ← this is what gets stored
```

If the user passes `--model sonnet`, the DB stores
`claude/claude-sonnet-4-6`, not `sonnet`. This ensures the column is
stable and comparable across runs.

### Studio display

#### Runs list

Add a **Model** column to the runs list:

| Name | Agent | Model | Status | Duration |
|------|-------|-------|--------|----------|
| play:backend | architect | claude/claude-sonnet-4-6 | completed | 87m |
| agent | reviewer | openai/codex-mini-latest | completed | 15m |
| flow | — | claude/claude-sonnet-4-6 | running | 3h |

The model column shows the session-level model. For multi-model flows,
a badge or tooltip shows "2 models" with the breakdown.

#### Run detail

Each step/branch shows its own model and agent:

```text
Step: explorer (claude/claude-sonnet-4-6, effort=high)
  [messages...]

Step: critic (claude/claude-opus-4-6, effort=high)
  [messages...]
```

#### Agent reference

When `agent_name` is set and the agent definition file exists, the run
detail page shows a link to the agent profile. The `agent_hash` enables
a "definition changed since this run" indicator:

```text
Agent: reviewer (definition changed since this run)
```

### Backfill for existing sessions

Existing sessions have `model = NULL`. No automated backfill — the
resolved model is lost for historical runs. The model column is nullable;
Studio renders NULL as "—" or "unknown."

For imported filesystem runs (`source_kind = 'imported_fs'`), the import
path can extract model from `run.json` → `manifest.model_spec` if present.

## Consequences

**Positive**

- Every run discloses its model, provider, and effort level — no guessing.
- Multi-model flows show per-branch model, not just the session default.
- Agent definition snapshots via hash enable "drift since this run" detection.
- Cost attribution becomes possible (model → pricing table → cost per run).
- Fully resolved model specs are stable and comparable.

**Negative**

- Four new columns on sessions, three on branches. Reconciled via
  `_reconcile_columns()` — no migration runner needed.
- Write path changes in `agent.py`, `flow.py`, `fanout.py`,
  `_orchestration.py`. Must ensure all paths write provenance.
- Historical runs have NULL model — incomplete data for old runs.
- `agent_hash` is a point-in-time snapshot; it doesn't store the actual
  content. If the agent file is deleted, the hash is unverifiable.
  Acceptable — the hash answers "same or different," not "what was it."

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Store model in `node_metadata` JSON only | Not queryable — "show all runs using opus" requires JSON parsing every row |
| Store full agent definition content | Too large for a column; agent files can be multi-KB. Hash is sufficient for drift detection |
| Derive model from branch `node_metadata` | Already attempted — write path is unreliable, data is missing in practice |
| Store user-input model string (e.g., "sonnet") | Not stable — alias resolution changes over time. Resolved spec is canonical |
| Store model on sessions only (not branches) | Loses per-agent model info for multi-model flows |
| Automated backfill from filesystem runs | Model resolution is not deterministic from historical data — the agent profile and defaults may have changed since the run |

## References

- [ADR-0009](ADR-0009-sqlite-state-layer.md) — SQLite state layer (sessions + branches schema)
- [ADR-0012](ADR-0012-studio-execution-lineage.md) — Execution lineage (provenance columns)
- [ADR-0017](ADR-0017-session-lifecycle-status.md) — Session lifecycle
- `lionagi/cli/agent.py` — Session + branch creation
- `lionagi/cli/orchestrate/flow.py` — FlowOp agent model resolution
- `lionagi/cli/_providers.py` — `parse_model_spec()` resolution chain

### Prior art

- **W3C PROV-DM** (2013) — The Entity-Activity-Agent triple maps to lionagi's
  Session-Branch-Message model. Our provenance columns capture a simplified
  form of the PROV derivation relation.
- **Buneman-Khanna-Tan 2001** ("Why and Where: A Characterization of Data
  Provenance", ICDT) — Distinguishes why-provenance (which inputs produced
  this output) from where-provenance (which source contributed). `agent_hash`
  captures where-provenance; the branch message chain captures why-provenance.
