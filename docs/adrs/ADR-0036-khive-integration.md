# ADR-0036: Khive Integration — Toolkit and Memory Store

**Status**: Proposed
**Date**: 2026-05-25
**Targets**: lionagi 0.27.0
**Relates to**: ADR-0033 (capabilities — gates the memory verbs)

## Context

Khive is the Lion ecosystem's persistent memory + knowledge graph + GTD
substrate. Today, agents that want khive access must:

- Import the khive SDK directly in tool definitions, write their own wrappers
- Repeat the same wrapper code across projects (lionag2, atlas, waves, …)
- Manage the client lifecycle by hand

The lionag2 package at `/Users/lion/projects/lionag2/src/lionag2/tools/khive_/`
proves the integration pattern: a `KhiveToolkit` that registers khive verbs
as agent tools, plus a `KhiveKnowledgeStore` that wires khive memory to the
AG2 KnowledgeStore protocol. Six months of usage validates the shape.

**Khive SDK reality check (2026-05-25 audit)**:

- Package: `khive 1.0.0rc1`, installable from `/Users/lion/projects/heng/khive/production/khive-python/`
- Import surface: `from khive import AsyncKhive, VERB_CATALOG, to_openai_tools`
- API style: **flat verbs** — `client.recall()`, `client.remember()`,
  `client.assign()`. **No** `client.memory.recall()` sub-resources (lionag2's
  pattern is out of date for the current SDK).
- Transport: HTTP only in the SDK. The MCP/CLI servers are separate processes
  the SDK talks to over HTTP.
- Tool schemas: `VERB_CATALOG` already ships OpenAI-compatible specs.
  `to_openai_tools([...])` returns ready-to-use tool dicts.

**Triggering observation**: 0.27.0's governance work (ADR-0033 / 0034) needs
an audit destination ("memory.write" denials → khive). Shipping the khive
integration alongside the governance ADRs gives the audit trail a home.

## Decision

Ship `lionagi.integrations.khive` as a new module with two surfaces:

### 1. `KhiveToolkit` — register verbs as agent tools

```python
from lionagi.integrations.khive import KhiveToolkit

toolkit = KhiveToolkit.attach(
    branch,
    namespace="lambda:lionagi",
    services=("memory", "kg", "communication"),  # subset filter
)
# branch now has: recall, remember, search, list, create, link, send, inbox tools
```

Each service registers a documented subset of verbs (closed mapping below).
Tools use the pre-built OpenAI schemas from `VERB_CATALOG` — no
`function_to_schema` round-trip.

#### Service → verb mapping (v0)

| Service | Verbs |
|---|---|
| `memory` | `recall`, `remember`, `update` |
| `kg` | `create`, `link`, `search` (graph-scoped), `neighbors` (via `list`+filter) |
| `gtd` | `assign`, `next`, `complete` |
| `communication` | `send`, `inbox`, `mark_read` |

Default `services=("memory",)`. Power users opt into more.

### 2. `KhiveMemoryStore` — branch-level memory binding

`Branch` does not currently have a `.memory` attribute. This ADR adds an
optional `memory_store=` kwarg to `Branch.__init__`:

```python
from lionagi.integrations.khive import KhiveMemoryStore
from lionagi import Branch

store = KhiveMemoryStore(namespace="lambda:lionagi")
branch = Branch(name="researcher", memory_store=store)

await branch.memory.recall("prior findings on X")
await branch.memory.remember("conclusion: ...", importance=0.8)
```

`Branch.memory` returns the configured store; if none provided, a
`NullMemoryStore` (no-op) is used so calling code doesn't need null-checks.

### 3. Client lifecycle

A toolkit instance owns one `AsyncKhive` client, kept open for the toolkit's
lifetime (typically the lifetime of the branch). The client is closed on
`branch.close()` or explicit `toolkit.close()`. This avoids per-call HTTP
connection overhead (lionag2's per-call `async with` is correct but slow at
scale).

### 4. Capability gating

Khive verbs ARE the implementation of capabilities from ADR-0033:

| ADR-0033 capability | Gates which khive verbs |
|---|---|
| `memory.recall` | `recall` |
| `memory.write` | `remember`, `update` |
| `memory.delete` | `delete` (raw verb, not in any service preset) |
| `kg.search` | KG verbs that read |
| `kg.create` | `create`, `link` |
| `communication.*` | `send`, `inbox`, `mark_read` |

The toolkit checks `branch.capabilities` before registering each verb. If
the capability is missing, the verb is skipped (not registered as a tool)
rather than registered-and-blocked-at-call — better DX: the agent never sees
a tool it can't use.

### 5. Optional extra

```toml
# pyproject.toml
khive = ["khive>=1.0.0rc1"]
```

The module is importable without the extra (lazy `import khive` inside
`AsyncKhive` usage), but a clear ImportError fires if a user tries to
construct `KhiveToolkit` without the extra installed.

### 6. Auto-discovery / detection

```python
from lionagi.integrations.khive import khive_available

if khive_available():
    KhiveToolkit.attach(branch, ...)
else:
    log.info("khive not installed; falling back to in-memory store")
```

Matches the lionag2 idiom and works with the [khive] extra.

## Consequences

**Positive**

- One library, one client, one auth path for all khive access from lionagi.
- KhiveToolkit + KhiveMemoryStore + capability gating give a complete
  "memory-aware agent" preset in 0.27.0 (`CapabilitySet.research_with_memory()`
  becomes trivial).
- Reuses khive's pre-built OpenAI schemas (`VERB_CATALOG`) — no maintenance of
  duplicate schemas.
- Branch.memory abstraction unblocks future memory backends (PostgreSQL,
  Redis, etc.) without changing the agent-facing API.

**Negative**

- Adds a memory layer Branch didn't have. One more thing to keep alive,
  serialize, audit.
- Khive SDK is still 1.0.0rc1 — pinning to `>=1.0.0rc1` may need bumping as
  the SDK stabilizes. Each minor bump is a potential break.
- The flat-verb API change from lionag2's sub-resource pattern means people
  who copy lionag2 code into lionagi will hit `AttributeError: 'AsyncKhive'
  object has no attribute 'memory'`. Documented but a footgun for the
  ecosystem.

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| Don't integrate; let agents wrap khive themselves | Defeats the point; every project re-implements the same wrapper |
| Ship only `KhiveToolkit`, no memory store binding | `branch.memory.recall(...)` is the ergonomic ask Ocean wants; toolkit alone forces tool-call indirection |
| Use `function_to_schema` to generate khive tool specs from SDK signatures | Reinvents what `VERB_CATALOG` already provides; loses curated descriptions |
| Per-call `async with AsyncKhive(...)` (lionag2 pattern) | HTTP overhead per tool call adds latency; persistent client is correct given async-first design |
| Match lionag2 service grouping (`memory`/`graph`/`communication`) | SDK uses `memory`/`kg`/`gtd`/`communication`; aligning with the SDK reduces translation mismatches |
| Build a transport-agnostic wrapper (MCP/CLI/HTTP) | Khive SDK already abstracts transport; lionagi shouldn't re-do it |

## Open Questions (resolve before implementation)

1. **`Branch.memory` API surface**: just `recall`/`remember`/`update` (minimal),
   or expose the full toolkit verb set (`branch.memory.assign`, etc.)?
   Recommendation: minimal, with `branch.toolkit.khive.X()` for the rest.
2. **NullMemoryStore behavior**: silent no-op (return `None` for recall,
   discard remember writes) vs raise `NotImplementedError`? Recommendation:
   silent no-op with `logger.debug` so test code doesn't crash.
3. **Default namespace**: `os.getenv("KHIVE_NAMESPACE")` fallback to
   `"lambda:lionagi"` vs no default (force explicit). Recommendation:
   env fallback, fail loud if neither set.

## References

- `/Users/lion/projects/heng/khive/production/khive-python/src/khive/_core/tools.py:14-286`:
  `VERB_CATALOG` — the canonical verb list
- `/Users/lion/projects/lionag2/src/lionag2/tools/khive_/khive_toolkit.py`:
  validated integration pattern (sub-resource API — needs updating)
- `/Users/lion/projects/lionag2/src/lionag2/tools/khive_/khive_store.py`:
  KhiveKnowledgeStore reference impl
- `lionagi/protocols/action/manager.py:65-100`: `register_tool` accepts plain
  callables and explicit tool_schema dicts — direct injection of
  VERB_CATALOG specs works
- `lionagi/session/branch.py:104-228`: where `memory_store=` kwarg is added
- ADR-0033: capability declarations that gate which khive verbs register
- ADR-0034: hook-based governance — uses khive memory as audit destination
