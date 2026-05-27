# ADR-0039: Knowledge Substrate Minimal Interface

**Status**: Proposed
**Date**: 2026-05-26
**Depends on**: [ADR-0033](ADR-0033-unified-entity-state-model.md) (shared EvidenceRef definition)
**Related**: [ADR-0028](ADR-0028-status-reason-model.md), [ADR-0029](ADR-0029-artifact-contract.md), [ADR-0030](ADR-0030-attention-queue.md), [ADR-0034](ADR-0034-frontend-data-and-state-architecture.md), [ADR-0035](ADR-0035-design-system-and-component-library.md)

## Context

lionagi sessions produce rich activity traces (messages, tool calls, model
responses) but no structured *knowledge*. The gap:

| What exists | What's missing |
|------------|----------------|
| Messages (ephemeral conversation) | Claims (durable learned facts) |
| DataLogger (activity audit trail) | Knowledge lifecycle (observed → verified → disputed → superseded) |
| Tool results (raw output) | Evidence-backed assertions about what results *mean* |
| Branch state (in-memory) | Cross-session retrieval ("what do we know about X?") |

An agent that reviews 50 PRs learns nothing persistent. The next
session starts cold. The operator can't ask "what has the agent learned
about our codebase?" because there's no substrate to hold the answer.

### Design constraints

1. **Storage-agnostic.** The data layer will change (SQLite → Postgres →
   distributed). The protocol MUST NOT leak storage assumptions.
2. **Evidence-first.** No claim without at least one structured evidence
   ref. This is the core opinion — it's what makes knowledge auditable.
3. **Zero-config in library mode.** A developer using `Branch` in a
   script should not need to configure anything. Knowledge methods exist
   but are no-ops until a store is provided.
4. **Branch-native.** Follows the existing 4-manager pattern. Becomes
   the 5th manager, not an external add-on.
5. **Tastefully opinionated.** The API surface guides users toward
   correct usage patterns. Hard to misuse, easy to use well.

## Decision

### The Knowledge Protocol

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class KnowledgeStore(Protocol):
    """Minimal protocol for durable knowledge persistence.

    Implementations:
      - NullKnowledgeStore: no-op (library default, zero overhead)
      - MemoryKnowledgeStore: in-process dict (testing, short-lived scripts)
      - SQLiteKnowledgeStore: local file (Studio mode)
      - RemoteKnowledgeStore: governed remote store
    """

    async def write_claim(
        self,
        claim: "Claim",
    ) -> str:
        """Persist a claim. Returns claim_id."""
        ...

    async def query(
        self,
        question: str,
        *,
        scope: "Scope | None" = None,
        status: "list[str] | None" = None,
        limit: int = 10,
    ) -> "list[Claim]":
        """Semantic retrieval of relevant claims."""
        ...

    async def transition(
        self,
        claim_id: str,
        new_status: str,
        *,
        evidence: "list[EvidenceRef] | None" = None,
        reason: str | None = None,
    ) -> None:
        """Move a claim through its lifecycle."""
        ...

    async def supersede(
        self,
        old_id: str,
        new_id: str,
    ) -> None:
        """Mark old claim superseded by new one."""
        ...

    async def history(
        self,
        claim_id: str,
    ) -> "list[ClaimEvent]":
        """Return lifecycle events for a claim."""
        ...
```

Five methods. That's the entire contract. Implementations can range from
a dict to a distributed graph store.

### The Claim dataclass

```python
from dataclasses import dataclass, field
from uuid import uuid4

@dataclass
class Claim:
    """A unit of learned knowledge with evidence and lifecycle."""

    text: str
    evidence: list[EvidenceRef]  # MUST be non-empty (enforced at construction)
    claim_status: str = "observed"
    confidence: float = 1.0
    # Confidence in the CLAIM being true (the fact itself).
    # Distinct from StateReason.confidence in ADR-0033, which measures
    # confidence in a reason explaining an operational state.

    # Auto-populated by Branch.learn()
    id: str = field(default_factory=lambda: str(uuid4()))
    project: str | None = None
    scope_type: str | None = None  # project | repo | agent | run
    scope_id: str | None = None
    session_id: str | None = None
    branch_id: str | None = None
    actor_type: str = "agent"     # agent | user | system | tool
    actor_id: str | None = None
    created_at: float = 0.0

    # Optional structured metadata
    domain: str | None = None     # e.g., "codebase", "api", "policy"
    tags: list[str] = field(default_factory=list)
    supersedes: str | None = None

    def __post_init__(self):
        if not self.evidence:
            raise ValueError(
                "A Claim requires at least one EvidenceRef. "
                "If evidence is truly absent, this is a hypothesis — "
                "use kind='model_inference' with low confidence."
            )
```

The `ValueError` on empty evidence is the core opinion. You physically
cannot create a claim without evidence. Weak evidence is allowed (typed
honestly), but empty evidence is not.

### Claim lifecycle

```text
observed → verified       (human/trusted process confirms)
observed → disputed       (conflicting evidence arrives)
observed → superseded     (newer claim replaces)
inferred → verified       (upgraded by external confirmation)
inferred → disputed       (disproven)
hypothesis → observed     (evidence found)
hypothesis → disputed     (falsified)
verified → disputed       (new contradicting evidence)
disputed → verified       (resolution in favor)
disputed → superseded     (both replaced)
any → superseded          (factual replacement)
```

Transition validation is in the store implementation, not the protocol.
The protocol just takes `new_status: str`. This keeps the protocol
stable while implementations can enforce stricter rules.

### Branch integration: the 5th manager

```python
class Branch:
    def __init__(
        self,
        ...,
        knowledge_store: KnowledgeStore | None = None,
    ):
        ...
        self._knowledge_store = knowledge_store or NullKnowledgeStore()

    async def learn(
        self,
        claim: str,
        evidence: list[EvidenceRef],
        *,
        status: str = "observed",
        confidence: float = 1.0,
        domain: str | None = None,
        tags: list[str] | None = None,
    ) -> str | None:
        """Record a learned fact with evidence. Returns claim_id.

        This is the primary knowledge-write API. It enforces:
        - Non-empty evidence (raises ValueError)
        - Auto-populates session/branch/actor context
        - Delegates to the configured KnowledgeStore

        Returns None if store is NullKnowledgeStore (library mode).
        """
        c = Claim(
            text=claim,
            evidence=evidence,
            claim_status=status,
            confidence=confidence,
            session_id=str(self._session_id) if self._session_id else None,
            branch_id=str(self.id),
            domain=domain,
            tags=tags or [],
            created_at=now_utc().timestamp(),
        )
        return await self._knowledge_store.write_claim(c)

    async def recall(
        self,
        question: str,
        *,
        limit: int = 5,
        status: list[str] | None = None,
    ) -> list[Claim]:
        """Retrieve relevant knowledge claims.

        Default: returns only active claims (observed, inferred, verified).
        Pass status=["disputed"] to include contested knowledge.
        """
        if status is None:
            status = ["observed", "inferred", "verified"]
        return await self._knowledge_store.query(
            question, status=status, limit=limit
        )

    async def verify(
        self, claim_id: str, evidence: list[EvidenceRef] | None = None
    ) -> None:
        """Upgrade a claim to verified status."""
        await self._knowledge_store.transition(
            claim_id, "verified", evidence=evidence
        )

    async def dispute(
        self, claim_id: str, reason: str, evidence: list[EvidenceRef] | None = None
    ) -> None:
        """Mark a claim as disputed with reason."""
        await self._knowledge_store.transition(
            claim_id, "disputed", evidence=evidence, reason=reason
        )
```

Four user-facing methods on Branch: `learn`, `recall`, `verify`, `dispute`.
That's the tasteful opinion — small surface, hard to misuse.

**Branch method → Store method mapping**:

| Branch (user-facing) | Store (protocol) | Notes |
|----------------------|------------------|-------|
| `learn(claim, evidence, ...)` | `write_claim(Claim)` | Auto-populates session/branch context |
| `recall(question, ...)` | `query(question, ...)` | Default filters out disputed/superseded |
| `verify(claim_id, evidence)` | `transition(claim_id, "verified", evidence)` | Convenience wrapper |
| `dispute(claim_id, reason, evidence)` | `transition(claim_id, "disputed", evidence, reason)` | Convenience wrapper |
| — | `supersede(old_id, new_id)` | Store-only; agents propose new claims, admin/human executes supersession |
| — | `history(claim_id)` | Store-only; lifecycle events surfaced via dedicated UI, not in-line agent flow |

`supersede` and `history` are intentionally store-only. Agents shouldn't supersede their own claims; the next observation creates a new claim, and an explicit human/admin decision links them via supersession.

**Session-level configuration is the primary entry point**:

```python
session = Session(
    knowledge_store=SQLiteKnowledgeStore("~/.lionagi/knowledge.db"),
)
branch = session.new_branch(system="You are a researcher")
# branch._knowledge_store == session._knowledge_store
```

When `Session.new_branch()` creates a Branch, it passes the session's `knowledge_store` to the new Branch. Per-Branch override is allowed for isolation testing (e.g., one Branch uses `NullKnowledgeStore` to opt out without affecting siblings):

```python
isolated_branch = session.new_branch(
    system="Sandbox agent",
    knowledge_store=NullKnowledgeStore(),  # override
)
```

The default flow: configure the store ONCE at the Session level. The override is a power-user seam, not the common path.

### The NullKnowledgeStore (zero-config default)

```python
class NullKnowledgeStore:
    """No-op store. Library mode. Zero overhead."""

    async def write_claim(self, claim: Claim) -> str | None:
        return None

    async def query(self, question, **kwargs) -> list[Claim]:
        return []

    async def transition(self, claim_id, new_status, **kwargs) -> None:
        pass

    async def supersede(self, old_id, new_id) -> None:
        pass

    async def history(self, claim_id) -> list:
        return []
```

A developer using lionagi as a library pays zero cost. No config, no
database, no setup. `branch.learn()` silently returns None. `branch.recall()`
returns an empty list. The API exists but does nothing until you opt in.

### Audit trail integration

Every knowledge action (`learn`, `verify`, `dispute`, `supersede`) MUST emit a `Log` entry to the Branch's `DataLogger`. The KnowledgeStore handles claim persistence; the DataLogger captures the *action* of writing knowledge. This is what makes the evidence chain auditable end-to-end.

Log entry shape:

```python
{
  "event": "knowledge.learn" | "knowledge.verify" | "knowledge.dispute" | "knowledge.supersede",
  "claim_id": str,
  "claim_status": str,
  "evidence_count": int,
  "evidence_kinds": list[str],
  "scope_type": str | None,
  "scope_id": str | None,
  "actor_type": str,
  "actor_id": str | None,
  "branch_id": str,
  "session_id": str | None,
  "timestamp": float,
}
```

The Branch.learn() implementation emits this log entry before delegating to the store. Failures to write the log do NOT block the knowledge write; they emit a warning to the DataLogger's auxiliary error channel.

Combined with [ADR-0033](ADR-0033-unified-entity-state-model.md)'s reason-code namespace (specifically `knowledge.*` codes), this gives operators a complete activity trail: who created what claim with what evidence, when it was verified or disputed, by whom.

### The MemoryKnowledgeStore (testing/scripts)

```python
class MemoryKnowledgeStore:
    """In-process dict store. For tests and short-lived scripts."""

    def __init__(self):
        self._claims: dict[str, Claim] = {}

    async def write_claim(self, claim: Claim) -> str:
        self._claims[claim.id] = claim
        return claim.id

    async def query(self, question, *, scope=None, status=None, limit=10):
        results = list(self._claims.values())
        if status:
            results = [c for c in results if c.claim_status in status]
        # Simple substring match for in-memory mode
        if question:
            results = [c for c in results if question.lower() in c.text.lower()]
        return results[:limit]

    async def transition(self, claim_id, new_status, *, evidence=None, reason=None):
        if claim_id in self._claims:
            self._claims[claim_id].claim_status = new_status

    async def supersede(self, old_id, new_id):
        if old_id in self._claims:
            self._claims[old_id].claim_status = "superseded"
            self._claims[old_id].supersedes = new_id

    async def history(self, claim_id):
        return []  # In-memory mode doesn't track history
```

### Evidence construction helpers (the opinionated part)

```python
def from_message(session_id: str, message_id: str, quote: str | None = None) -> EvidenceRef:
    """Evidence from a chat message. The `quote` becomes EvidenceRef.detail."""
    return EvidenceRef(kind="message", session_id=session_id, message_id=message_id, detail=quote)

def from_tool_result(tool_call_id: str, summary: str | None = None) -> EvidenceRef:
    """Evidence from a tool execution result. The `summary` becomes EvidenceRef.detail."""
    return EvidenceRef(kind="tool_result", tool_call_id=tool_call_id, detail=summary)

def from_user_statement(session_id: str, message_id: str) -> EvidenceRef:
    """Evidence from an explicit user statement."""
    return EvidenceRef(kind="user_statement", session_id=session_id, message_id=message_id)

def from_artifact(artifact_id: str, path: str | None = None) -> EvidenceRef:
    """Evidence from a produced artifact (file/report/output)."""
    return EvidenceRef(kind="artifact", artifact_id=artifact_id, path=path)

def from_file(path: str, repo: str | None = None, commit: str | None = None) -> EvidenceRef:
    """Evidence from a file on disk or in a repo."""
    return EvidenceRef(kind="file", path=path, repo=repo, commit_sha=commit)

def from_url(url: str, fetched_at: float | None = None, content_hash: str | None = None) -> EvidenceRef:
    """Evidence from a web resource."""
    return EvidenceRef(kind="url", url=url, fetched_at=fetched_at, content_hash=content_hash)

def from_model_inference(session_id: str, message_id: str, rationale: str) -> EvidenceRef:
    """Evidence from model reasoning. The weakest kind — be honest about confidence."""
    return EvidenceRef(kind="model_inference", session_id=session_id, message_id=message_id, detail=rationale)

def from_human_assertion(user_id: str, asserted_at: float, detail: str | None = None) -> EvidenceRef:
    """Evidence from a human's external assertion (e.g., 'I verified this manually')."""
    return EvidenceRef(kind="human_assertion", id=user_id, fetched_at=asserted_at, detail=detail)
```

The helper name matches the `kind` value 1:1 — `from_X()` produces `EvidenceRef(kind="X")`. The naming makes the strength hierarchy visible: `from_file` and `from_artifact` (definitive) > `from_tool_result` (observed) > `from_url` (external, may rot without content_hash) > `from_message` and `from_user_statement` (situated in conversation) > `from_model_inference` (weakest — agent reasoning only).

`from_human_assertion` is special: it's strong evidence (a human said so) but is only as good as the human's verification process. Use it sparingly and document the verification source.

Eight helpers, eight kinds — see [ADR-0033](ADR-0033-unified-entity-state-model.md) §"EvidenceRef" for the canonical kind enumeration.

### Reactive knowledge extraction (hook pattern)

```python
# Example: auto-extract knowledge from tool results
async def extract_knowledge_hook(branch, message):
    """Hook that fires on every ActionResponse to detect learnable facts."""
    if not isinstance(message, ActionResponse):
        return
    # Custom extraction logic per tool
    for result in message.content.results:
        if result.tool_name == "read_file" and result.output:
            # Application-specific: extract facts from file reads
            pass

# Registration
branch.msgs._on_message_added.append(extract_knowledge_hook)
```

This is optional and application-specific. The substrate provides the
storage; extraction logic lives in the application layer.

### Configuration tiers

```python
# Tier 1: Library mode (default)
branch = Branch(system="You are a researcher")
# knowledge_store = NullKnowledgeStore() implicitly

# Tier 2: Local persistence (Studio mode)
from lionagi.knowledge import SQLiteKnowledgeStore
branch = Branch(
    system="...",
    knowledge_store=SQLiteKnowledgeStore("~/.lionagi/knowledge.db"),
)
```

Each tier adds capability without changing the Branch API. The developer's
code (`branch.learn(...)`, `branch.recall(...)`) is identical across tiers.

### Worked example: knowledge accumulation across sessions

A PR review agent accumulates codebase patterns over many runs:

```python
from lionagi import Branch, Session
from lionagi.knowledge import SQLiteKnowledgeStore, from_file, from_tool_result, from_user_statement

# Session 1, Monday: agent reviews PR #1842
session = Session(
    knowledge_store=SQLiteKnowledgeStore("~/.lionagi/knowledge/payments.db"),
)
branch = session.new_branch(system="You review pull requests for the payments service.")

result = await branch.operate(
    instruction="Review PR #1842, focus on transaction handling",
    tools=[read_file_tool, github_pr_tool],
)

# Agent observes a pattern, records it
claim_id = await branch.learn(
    "payments-service uses the saga pattern for multi-step transactions",
    evidence=[
        from_file("src/sagas/payment_saga.py", repo="acme/payments", commit="abc123"),
        from_tool_result("tc_pr1842_001", "GitHub PR shows saga.compensate() in error path"),
    ],
    domain="architecture",
    tags=["saga", "transactions"],
)
# Persists to SQLite. claim_id is returned.

# --- Session 2, Wednesday: different agent reviews PR #1851 ---
session2 = Session(knowledge_store=SQLiteKnowledgeStore("~/.lionagi/knowledge/payments.db"))
branch2 = session2.new_branch(system="You review pull requests for the payments service.")

# Before reviewing, agent recalls prior knowledge
context = await branch2.recall("What patterns does this codebase use for transactions?")
# Returns: [Claim(text="payments-service uses the saga pattern...", confidence=1.0, status="observed", ...)]

# Agent uses this context, doesn't need to re-discover the pattern

# Later in the review, agent finds the new PR uses a different pattern
new_claim_id = await branch2.learn(
    "PR #1851 introduces 2PC alongside saga for cross-service transactions",
    evidence=[
        from_file("src/coordinators/two_phase.py", repo="acme/payments", commit="def456"),
        from_tool_result("tc_pr1851_003", "PR description: explicitly mentions 2PC for inventory+payment"),
    ],
    domain="architecture",
    tags=["2pc", "transactions"],
)

# --- Session 3, Friday: operator reviews knowledge ---
# Operator opens Run detail for PR #1851 review
# Knowledge lens shows: "PR #1851 introduces 2PC..."
# Operator clicks "Verify" after checking the PR themselves
await branch2.verify(new_claim_id, evidence=[
    from_user_statement(session_id=str(session2.id), message_id=operator_msg_id),
])
# Status now: verified
# Both claims persist; future agents querying "transactions" get both with appropriate status
```

This is the substrate in action: agents accumulate, agents recall, humans verify, the store persists across sessions. Every claim has evidence. Every transition is auditable.

### What the protocol does NOT specify

| Concern | Why excluded |
|---------|-------------|
| Storage schema | Data layer will change. Protocol is async interface only |
| Embedding/vector search impl | Store implementation detail. MemoryStore uses substring; SQLiteStore might use FTS5; governed stores use embeddings |
| Deduplication logic | Application-specific. Some stores merge duplicates, others keep all |
| Retention policy | Operational config, not protocol |
| Access control | Store implementation concern, not protocol concern |
| Export format (PROV, OTel) | Export adapter layer, not substrate interface |
| Graph relationships between claims | v2 concern. v1 claims are independent units |

### Claim severity derivation

Knowledge claims have their own severity, derived from `claim_status` and `confidence`. This is a parallel function to [ADR-0033](ADR-0033-unified-entity-state-model.md)'s `derive_severity()` for operational entities — they share the same (severity, tone) value space but compute from different inputs.

```python
def derive_claim_severity(claim: Claim) -> tuple[str, str]:
    """Returns (severity, tone) for a knowledge claim."""

    # Critical: nothing for claims in v1 (claims aren't operational failures)

    # Warning: actively contested or unconfirmed-too-long
    if claim.claim_status == "disputed":
        return ("warning", "warning")
    if claim.claim_status == "hypothesis" and claim.confidence < 0.5:
        return ("warning", "warning")

    # Info: pending validation, active observations
    if claim.claim_status in ("observed", "inferred"):
        return ("info", "info")
    if claim.claim_status == "hypothesis":
        return ("info", "neutral")

    # Success: confirmed
    if claim.claim_status == "verified":
        return ("neutral", "success")

    # Neutral: archived
    if claim.claim_status == "superseded":
        return ("neutral", "neutral")

    return ("neutral", "neutral")
```

Both `derive_severity` (operational) and `derive_claim_severity` (knowledge) emit into the same Attention Queue ([ADR-0030](ADR-0030-attention-queue.md)) and use the same (severity, tone) values rendered by the same components ([ADR-0035](ADR-0035-design-system-and-component-library.md)).

The functions stay separate because claims are NOT operational entities — `disputed` is not the same kind of severity as `failed`. Keeping them separate prevents semantic muddling.

### Scope type semantics

The `scope_type` and `scope_id` fields on Claim enable multi-level
knowledge organization:

| scope_type | scope_id example | Meaning |
|-----------|------------------|---------|
| `project` | `"payments-platform"` | Fact applies to the whole project |
| `repo` | `"github.com/acme/payments"` | Fact about a specific repo |
| `agent` | `"pr-reviewer"` | Fact the agent learned about itself/its domain |
| `run` | `"run_abc123"` | Fact specific to one execution (ephemeral) |

Scoping enables `recall()` to filter by relevance. A PR reviewer agent
queries `scope_type=repo` knowledge, not all project-level facts.

## Product Implications (Frontend)

### Knowledge as lens (not nav section)

Knowledge surfaces contextually on entity pages:

| Page | Knowledge lens |
|------|---------------|
| Run detail | "What did this run learn?" — claims produced by this run |
| Show detail | "What changed?" — claims created/superseded during this show |
| Agent detail | "What does this agent know?" — claims scoped to this agent |
| Attention item | "Why?" — evidence trail explaining the attention-worthy state |
| Artifact detail | "What claims does this support?" — claims citing this artifact |

### Claim rendering in the UI

Claims render like status (same NormalizedState pattern from ADR-0033):

```text
observed · confidence 0.9 · from tool_result
inferred · confidence 0.6 · from model_inference
disputed · 2 conflicting evidence refs
verified · by user · 2026-05-20
```

### Attention Queue integration

Knowledge events that enter the Attention Queue:

| Event | Severity | Action |
|-------|----------|--------|
| Claim disputed (was verified) | warning | "Review conflicting evidence" |
| Low-confidence claim aged 7d+ without verification | info | "Verify or discard" |
| Claim superseded by lower-confidence replacement | warning | "Review supersession" |
| Knowledge write rejected (empty evidence) | info | "Agent attempted knowledge write without evidence" |

### Search integration

Claims are searchable via `GET /api/search?q=...&type=knowledge`.
Each result carries claim_status, confidence, evidence count, and scope.

## Consequences

**Positive**

- Zero-cost in library mode (NullStore is truly zero overhead)
- Evidence-first is enforced at the API level, not by convention
- Protocol is storage-agnostic — can migrate without changing user code
- Follows established Branch manager pattern (5th manager, not addon)
- Progressive disclosure: Null → Memory → SQLite → governed remote store
- Frontend lens pattern avoids "empty graph browser" UX trap

**Negative**

- Adds a 5th manager to Branch (minimal complexity — NullStore is 10 LOC)
- `recall()` quality depends entirely on store implementation (substring
  match in MemoryStore is crude — acceptable for testing only)
- No built-in deduplication in the protocol — stores must handle it
- Graph relationships between claims are deferred to v2

## Non-Goals

Explicitly out of scope for v1; some may move into v2:

- **Graph relationships between claims** (claim → entails → claim, claim → contradicts → claim). Claims are independent units in v1. Edges are v2.
- **Automated extraction from all messages** (e.g., LLM-driven claim mining from any tool result). Too noisy; v1 is opt-in via `branch.learn()` calls.
- **Cross-project knowledge sharing**. Scope isolation first. Sharing is a governed feature requiring access control; it is deferred to a future extension of the store protocol.
- **Inference / reasoning over claims**. The substrate stores knowledge; the agent reasons. We don't ship a rules engine over claims.
- **Structured claim schemas** (typed templates beyond free-text). Free-text first; structure emerges from observed usage patterns, then becomes a v2 capability.
- **Embedding model selection UI**. Implementation detail of the store. The user configures store, not internals.
- **Claim deletion**. Knowledge is append-only with lifecycle (dispute, supersede). No `branch.forget()` method.
- **Knowledge import/export across stores**. PROV/OTel export is v2. Import needs deduplication logic not yet designed.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Knowledge as separate service (not on Branch) | Breaks the manager pattern; requires separate lifecycle management; harder to auto-populate session context |
| Mandatory store configuration | Violates zero-config in library mode; adoption barrier |
| Untyped evidence (just a string) | Defeats the auditability thesis; can't render evidence trails in UI |
| Claims without lifecycle | No way to evolve knowledge; facts become stale without mechanism to dispute/supersede |
| Full graph DB protocol (nodes + edges + traversal) | Over-specified for v1; locks in storage assumptions; most use cases are claim + retrieval |
| Auto-extraction from all messages | Too magical; generates low-quality claims; better as opt-in hook pattern |

## References

- [ADR-0028](ADR-0028-status-reason-model.md) — Status Reason Model (parallel concept: structured reasons with evidence)
- [ADR-0029](ADR-0029-artifact-contract.md) — Artifact Contract (artifacts as evidence)
- [ADR-0030](ADR-0030-attention-queue.md) — Attention Queue (disputed/low-confidence claims surface here)
- [ADR-0033](ADR-0033-unified-entity-state-model.md) — Unified Entity State Model (canonical EvidenceRef definition)
- [ADR-0034](ADR-0034-frontend-data-and-state-architecture.md) — Frontend Data & State Architecture (knowledge.* SSE events, knowledge query keys)
- [ADR-0035](ADR-0035-design-system-and-component-library.md) — Design System & Component Library (KnowledgeLens, ClaimCard, ClaimStatusBadge components)
- Branch architecture: 4-manager facade → 5-manager facade with KnowledgeStore
- W3C PROV (future export target, not v1 schema)
- Issue #1175: Knowledge substrate first-class in lionagi
