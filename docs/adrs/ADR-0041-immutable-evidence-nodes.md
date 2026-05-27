# ADR-0041: Immutable Evidence Nodes

**Status**: proposed
**Date**: 2026-05-26
**Depends on**: [ADR-0033](ADR-0033-unified-entity-state-model.md) (EvidenceRef definition), [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md) (Claim, KnowledgeStore protocol)
**Related**: [ADR-0042](ADR-0042-task-certificate.md), [ADR-0049](ADR-0049-log-tier-governance.md), [ADR-0028](ADR-0028-status-reason-model.md), [ADR-0029](ADR-0029-artifact-contract.md)

## Context

lionagi's `DataLogger` writes `Log` entries on each tool call and session event. Those entries are
plain append-only dicts: they record what happened, but nothing prevents the session process from
mutating or overwriting them after the fact. An agent running with write access to its own log
directory can rewrite history. More subtly, the `EvidenceRef` type defined in
[ADR-0033](ADR-0033-unified-entity-state-model.md) is a Pydantic model with no hashing. A
`Claim` from [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md) is append-only in its
lifecycle state machine (observed → verified → disputed → superseded) but that ordering is
enforced only by application logic — there is no cryptographic link between one claim and the next
that would detect out-of-order insertion or silent replacement.

The concrete failure scenario: a governed agent is authorized to write files under a restricted
directory. It invokes a file-write tool, produces an `ActionResponse`, and that response enters
the DataLogger. If the agent can later modify that log entry — or if a future store implementation
silently overwrites on a key collision — the audit record no longer reflects what actually executed.
This matters the moment anyone asks "what did the agent do and how do we know?"

The cross-cutting principle at stake here is **"Evidence is first-class, not logs"** (principle #2
of this ADR set). Logs are operational: they tell you what happened at runtime. Evidence carries
the policy version, gate results, and authorization context active *at execution time*. A log entry
becomes evidence only when it is structurally impossible to alter it after the fact.

### The applicable design pattern

prior research addresses this with a hash-chain pattern: every evidence node includes a
`chain_hash = SHA256(payload_hash || prev_chain_hash)`. Appending a new node produces a new
`chain_hash` that depends on every node before it; tampering with any earlier node invalidates all
subsequent `chain_hash` values. prior research adds the supersession pattern: errors are
corrected by inserting a new node with `supersedes_id` pointing backward, leaving the original
untouched. prior research introduces `_sensitive_fields` as a `ClassVar[set[str]]`: fields
excluded from the hash and from serialized output (raw tool inputs may contain secrets that should
not appear in audit exports). These three patterns compose cleanly, and the translation to lionagi
requires no new infrastructure.

### Why lionagi needs this

[ADR-0042](ADR-0042-task-certificate.md) (Task Certificate) will sign a proof that a task
completed with specific evidence. That signature is worthless if the evidence it covers can be
modified after signing. [ADR-0049](ADR-0049-log-tier-governance.md) (Log Tier Governance)
classifies records as MUTABLE, PROTECTED, or IMMUTABLE — but tier classification is a policy;
`ImmutableEvidenceNode` is the mechanism that makes the IMMUTABLE tier structurally enforceable at
the Python level, before any storage layer is involved.

## Decision

Introduce `ImmutableEvidenceNode` as the base class for all evidence-grade records in lionagi.
`EvidenceRef` (ADR-0033) and `Claim` (ADR-0039) acquire these properties via mixin or subclass.
DB-level enforcement is explicitly deferred to KHive v1 (see Non-Goals).

### 1. Core data model

```python
from __future__ import annotations

import hashlib
import json
from typing import Any, ClassVar
from uuid import UUID

from pydantic import ConfigDict, Field

from lionagi.protocols.generic.element import Element


def _sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _canonical_json(obj: dict) -> str:
    """Deterministic JSON serialization for hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


#: Sentinel for the first node in a chain (no predecessor).
GENESIS_HASH: str = "0" * 64


class ImmutableEvidenceNode(Element):
    """Base for evidence-grade records that must be tamper-evident.

    Hash semantics
    --------------
    content_hash : SHA256 over domain fields only (excludes _sensitive_fields,
        supersedes_id, chain hashes).  Used for deduplication — two nodes with
        identical domain content produce the same content_hash.

    chain_hash : SHA256(payload_hash || prev_chain_hash).  The payload_hash is
        SHA256 over domain fields PLUS supersedes_id (so the supersession link
        is covered), still excluding _sensitive_fields.  chain_hash binds each
        node to its predecessor; any mutation of a node or its position in the
        chain invalidates all chain_hash values that follow.

    Immutability contract
    ---------------------
    Nodes are sealed during Pydantic model initialization.  The fields node_id, created_at,
    content_hash, chain_hash, prev_chain_hash, and supersedes_id MUST NOT be
    mutated after model_post_init.  Pydantic's frozen model configuration
    enforces this contract at the Element layer.

    Sensitive fields
    ----------------
    Subclasses declare _sensitive_fields: ClassVar[set[str]] to name fields
    that must be excluded from content_hash and from serialization output.
    This prevents raw tool inputs containing secrets from appearing in
    audit exports or hash computations.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )

    # --- Chain bookkeeping (set by seal()) ---
    content_hash: str = Field(default="", frozen=True)
    chain_hash: str = Field(default="", frozen=True)
    prev_chain_hash: str = Field(default=GENESIS_HASH, frozen=True)

    # --- Supersession (backward pointer only) ---
    supersedes_id: UUID | None = Field(default=None, frozen=True)

    # --- Subclass contract ---
    _sensitive_fields: ClassVar[set[str]] = set()

    @property
    def node_id(self) -> UUID:
        """Compatibility alias for Element.id in evidence-oriented APIs."""
        return self.id

    def model_post_init(self, __context: Any) -> None:
        """Seal new nodes while preserving stored hashes on loaded nodes."""
        if not self.content_hash or not self.chain_hash:
            content_hash, chain_hash = self._compute_hashes()
            object.__setattr__(self, "content_hash", self.content_hash or content_hash)
            object.__setattr__(self, "chain_hash", self.chain_hash or chain_hash)

    # ------------------------------------------------------------------
    # Internal hashing
    # ------------------------------------------------------------------

    def _domain_dict(self, *, include_supersedes: bool) -> dict:
        """Return serialisable dict of domain fields, excluding sensitive ones
        and the chain/hash bookkeeping fields managed by this base class."""
        base_excludes = {
            "id",
            "created_at",
            "metadata",
            "node_metadata",
            "content_hash",
            "chain_hash",
            "prev_chain_hash",
            "supersedes_id",
        } | self._sensitive_fields

        result = self.to_dict(mode="db")
        for f_name in base_excludes:
            result.pop(f_name, None)

        if include_supersedes and self.supersedes_id is not None:
            result["_supersedes_id"] = str(self.supersedes_id)

        return result

    def _compute_hashes(self) -> tuple[str, str]:
        """Compute content_hash and chain_hash from canonical Element output."""
        domain = self._domain_dict(include_supersedes=False)
        content_hash = _sha256_hex(_canonical_json(domain))

        payload = self._domain_dict(include_supersedes=True)
        payload_hash = _sha256_hex(_canonical_json(payload))
        chain_hash = _sha256_hex(payload_hash + self.prev_chain_hash)
        return content_hash, chain_hash

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def verify_content(self) -> bool:
        """Return True if domain fields match the stored content_hash.

        A False return means either the instance was mutated after construction
        or the stored hash was corrupted.
        """
        expected, _ = self._compute_hashes()
        return self.content_hash == expected

    def verify_chain_link(self) -> bool:
        """Return True if chain_hash is consistent with current domain fields
        and prev_chain_hash.

        This checks a *single* link.  Full chain verification requires
        iterating all nodes in order and calling this method on each, passing
        the predecessor's chain_hash as prev_chain_hash.
        """
        _, expected = self._compute_hashes()
        return self.chain_hash == expected

    # ------------------------------------------------------------------
    # Supersession
    # ------------------------------------------------------------------

    def supersede(self, **updated_fields) -> "ImmutableEvidenceNode":
        """Return a new node of the same subclass with corrected fields.

        The new node's supersedes_id points back to this node.  This node
        is never modified.  The caller is responsible for persisting both
        nodes; no storage interaction occurs here.

        The new node starts a fresh chain link: its prev_chain_hash is
        set to this node's chain_hash.
        """
        init_kwargs = self.model_dump(
            mode="python",
            exclude={"id", "created_at", "content_hash", "chain_hash"},
        )

        init_kwargs.update(updated_fields)
        init_kwargs["supersedes_id"] = self.id
        init_kwargs["prev_chain_hash"] = self.chain_hash

        return type(self)(**init_kwargs)

    # ------------------------------------------------------------------
    # Safe serialization
    # ------------------------------------------------------------------

    def to_audit_dict(self) -> dict:
        """Return a dict safe for export: sensitive fields are absent.

        Includes chain bookkeeping fields so that an external verifier can
        reconstruct and check the chain without access to sensitive data.
        """
        result = self.to_dict(mode="json")
        result["node_id"] = result.pop("id")
        result["created_at"] = self.created_datetime.isoformat()
        result.pop("metadata", None)
        for f_name in self._sensitive_fields:
            result.pop(f_name, None)
        return result
```

### 2. Chain bootstrap — the genesis node

The first node in any chain has no predecessor.  Its `prev_chain_hash` is set to the sentinel
`GENESIS_HASH = "0" * 64`.  This value is fixed and publicly known; it is not a secret.  Its
purpose is to anchor the chain: any subsequent node's `chain_hash` depends on this sentinel,
so the first node is as tamper-evident as any later one.

When a subsequent node is appended, it receives `prev_chain_hash = predecessor.chain_hash`.
A chain of N nodes can be verified in O(N) by iterating in insertion order and calling
`verify_chain_link()` on each node.

```python
from lionagi.protocols.generic.pile import Pile


def verify_chain(nodes: Pile[ImmutableEvidenceNode]) -> bool:
    """Verify integrity of an ordered chain of nodes.

    Returns True iff every node's chain_hash is consistent with its
    domain fields and its predecessor's chain_hash.  The first node
    must have prev_chain_hash == GENESIS_HASH.
    """
    expected_prev = GENESIS_HASH
    for node in nodes:
        if node.prev_chain_hash != expected_prev:
            return False
        if not node.verify_chain_link():
            return False
        expected_prev = node.chain_hash
    return True
```

### 3. How EvidenceRef and Claim acquire these properties

**EvidenceRef** (ADR-0033) is currently a Pydantic model.  It acquires hash-chain properties by
inheriting from `ImmutableEvidenceNode` via a thin adapter mixin.  Domain fields (`kind`,
`ref`, `note`, `timestamp`) participate in `content_hash` as before; any field declared in
`_sensitive_fields` is excluded.

```python
from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

EvidenceKind = Literal[
    "message", "user_statement", "tool_result", "artifact",
    "url", "file", "model_inference", "human_assertion",
]


class EvidenceRef(ImmutableEvidenceNode):
    """A single structured evidence reference, tamper-evident.

    Corresponds to the EvidenceRef defined in ADR-0033; extends it with
    chain-hash integrity.
    """

    kind: EvidenceKind = "tool_result"
    ref: str = ""           # URI, message ID, artifact ID, etc.
    note: str = ""          # Human-readable annotation
    # Raw input is excluded from hashes and audit exports because it may
    # contain API keys, PII, or other secrets passed to a tool.
    raw_input: dict | None = Field(default=None, exclude=True)

    _sensitive_fields: ClassVar[set[str]] = {"raw_input"}
```

**Claim** (ADR-0039) follows the same pattern.  The claim's evidence list becomes a list of
`EvidenceRef` node IDs rather than inline objects; this keeps the Claim node's own hash stable
even as the referenced evidence grows.

```python
from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from lionagi.protocols.generic.pile import Pile

ClaimStatus = Literal["observed", "verified", "disputed", "superseded"]


class Claim(ImmutableEvidenceNode):
    """A durable learned fact backed by at least one EvidenceRef node.

    The lifecycle status field participates in content_hash, so a status
    transition produces a new node (via supersede()) rather than mutating
    the existing one.  This preserves the full status history in the chain.
    """

    subject: str = ""           # Entity the claim is about
    predicate: str = ""         # Relation or property
    value: str = ""             # Asserted value
    status: ClaimStatus = "observed"
    evidence: Pile[EvidenceRef] = Field(
        default_factory=lambda: Pile(item_type={EvidenceRef}, strict_type=True)
    )
    # The prompt or instruction that caused this claim to be formed.
    # Excluded because it may contain sensitive context from the session.
    source_prompt: str | None = Field(default=None, exclude=True)

    _sensitive_fields: ClassVar[set[str]] = {"source_prompt"}

    def transition(self, new_status: ClaimStatus) -> "Claim":
        """Return a new Claim node with updated status (supersedes this one).

        Valid transitions follow the lifecycle in ADR-0039:
          observed → verified | disputed
          verified → disputed | superseded
          disputed → verified | superseded
          superseded → (terminal)
        """
        _valid: dict[ClaimStatus, set[ClaimStatus]] = {
            "observed": {"verified", "disputed"},
            "verified": {"disputed", "superseded"},
            "disputed": {"verified", "superseded"},
            "superseded": set(),
        }
        if new_status not in _valid[self.status]:
            raise ValueError(
                f"Cannot transition Claim from {self.status!r} to {new_status!r}"
            )
        return self.supersede(status=new_status)
```

### 4. Application-layer enforcement vs. DB-layer enforcement

`ImmutableEvidenceNode` provides Python-level tamper evidence: the hashes are computed at
construction and can be verified at any time.  If application code mutates an attribute after
construction, `verify_content()` will return `False` on the next check.

DB-level triggers that block `UPDATE` and `DELETE` on the underlying table are a **v2 / KHive
concern**, not a v1 library concern.  The reasons:

1. lionagi is a library; it ships no migrations and makes no assumptions about the storage
   backend.  Different deployments use SQLite, Postgres, or in-process dicts.
2. Trigger DDL is storage-specific.  An in-process `MemoryKnowledgeStore` has no concept of
   a DB trigger.
3. The Python-level guarantee is sufficient for library mode: evidence is tamper-*evident* (you
   can detect mutation) even if not tamper-*proof* (a sufficiently privileged process could still
   modify storage directly).  Tamper-proof enforcement requires the governed storage layer that
   KHive will provide.

The `KnowledgeStore` protocol (ADR-0039) should document that a conforming implementation MUST
persist `ImmutableEvidenceNode` instances without modifying their `content_hash`, `chain_hash`,
or `prev_chain_hash` fields.  This is a protocol contract, not a compiled guarantee — which is
the correct level of enforcement for a library.

### 5. Chain ownership and session scope

Each `Branch` session maintains its own evidence chain.  Chain continuity is the responsibility
of the component that creates new nodes:

```python
from pydantic import ConfigDict, Field

from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile
from lionagi.session.branch import Branch


class EvidenceChain(Element):
    """Lightweight in-process chain accumulator.

    The KnowledgeStore implementation is responsible for persisting nodes
    and for reconstructing the chain_tip on reload. Branch integration uses
    the existing managers: messages via branch.msgs, actions via branch.acts,
    models via branch.mdls, and audit output via branch._log_manager.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    tip_hash: str = GENESIS_HASH
    nodes: Pile[ImmutableEvidenceNode] = Field(
        default_factory=lambda: Pile(
            item_type={ImmutableEvidenceNode},
            strict_type=False,
        )
    )

    @classmethod
    def for_branch(
        cls,
        branch: Branch,
        *,
        tip_hash: str = GENESIS_HASH,
    ) -> "EvidenceChain":
        """Create a branch-scoped chain without adding a parallel manager."""
        chain = cls(tip_hash=tip_hash)
        branch.metadata["evidence_chain_id"] = str(chain.id)
        branch.metadata["evidence_chain_tip"] = tip_hash
        return chain

    def append(
        self,
        node_cls: type[ImmutableEvidenceNode],
        *,
        branch: Branch | None = None,
        **kwargs,
    ) -> ImmutableEvidenceNode:
        """Construct and chain a new node, returning the sealed instance."""
        node = node_cls(prev_chain_hash=self.tip_hash, **kwargs)
        self.tip_hash = node.chain_hash
        self.nodes.include(node)

        if branch is not None:
            branch.metadata["evidence_chain_tip"] = self.tip_hash
            branch._log_manager.log(
                {
                    "event": "evidence_node_appended",
                    "evidence": node.to_audit_dict(),
                }
            )

        return node

    def verify(self) -> bool:
        """Verify all accumulated nodes form a valid chain."""
        return verify_chain(self.nodes)
```

A `KnowledgeStore` implementation that persists nodes reloads `tip_hash` from the last stored
node's `chain_hash`.  If the store is empty, `tip_hash` starts at `GENESIS_HASH`.

## Consequences

**Positive**

- Every `EvidenceRef` and `Claim` node carries a cryptographic fingerprint that proves its
  content has not changed since construction.  An external auditor can verify the chain with
  no access to the storage backend's internals.
- `content_hash` enables deduplication: two tool-result nodes recording the same output will
  produce the same `content_hash`, allowing stores to avoid redundant insertions without
  suppressing the chain entry.
- `_sensitive_fields` prevents raw tool inputs (API keys, user PII) from appearing in audit
  exports or hash computations, separating operational safety from audit correctness.
- Supersession via `supersede()` preserves full history.  A corrected `Claim` does not destroy
  the original — it chains a new node that points back.  [ADR-0042](ADR-0042-task-certificate.md)
  can sign over a chain tip with confidence that the underlying evidence is traceable.
- The `EvidenceChain` accumulator is zero-dependency: it works in-process with no storage,
  making it suitable for testing and for Library mode.

**Negative**

- SHA-256 hashing at construction adds roughly 0.1–0.5 ms per node, depending on domain field
  size.  This is acceptable for evidence-grade records (tool calls, claims) which are far
  less frequent than log events.  The DataLogger's high-volume `Log` entries are explicitly
  out of scope (see Non-Goals and [ADR-0049](ADR-0049-log-tier-governance.md)).
- Implementing `verify_chain()` on a loaded store requires fetching nodes in insertion order.
  Stores that do not preserve insertion order (e.g., unordered key-value stores) cannot support
  chain verification without additional ordering metadata.
- Because status transitions on `Claim` produce new nodes rather than mutating the existing one,
  a store accumulates one node per lifecycle transition.  For long-lived claims that go through
  many transitions this adds storage overhead.  In practice the lifecycle has four states, so
  the maximum per-claim node count is four.
- The Python-level immutability contract relies on discipline: nothing prevents application code
  from assigning to fields post-construction in the current `dataclass` implementation.  A v2
  hardening pass should add `__setattr__` protection or switch to `frozen=True` where Pydantic
  compatibility allows.

## Non-Goals

Explicitly out of scope for this ADR:

- **Full Merkle trees**: Not needed for v1.  A linear hash chain suffices to detect insertion,
  deletion, or mutation of any single node.  Merkle trees enable efficient proofs over large
  subsets; that capability belongs to a future KHive indexing ADR.
- **Cryptographic signing**: Digital signatures (ECDSA, Ed25519) over chain tips or individual
  nodes are covered by [ADR-0042](ADR-0042-task-certificate.md) (Task Certificate).  This ADR
  establishes the hash structure that signing will cover, not the signing mechanism itself.
- **Distributed ledger semantics**: Blockchain-style consensus, fork resolution, and distributed
  chain synchronization are not relevant to the single-process or single-tenant deployment
  scenarios lionagi targets in v1.
- **DB-level triggers**: PostgreSQL `BEFORE UPDATE` / `BEFORE DELETE` triggers that enforce
  immutability at the storage layer are a KHive v1 concern.  The `KnowledgeStore` protocol
  contract documents the expectation; enforcement is left to governed store implementations.
- **Multi-tenant chain isolation**: Ensuring that chain nodes from different tenants cannot be
  interleaved requires tenant-scoped chain roots and storage-layer row security.  This is KHive
  territory (see also ADR-0039's note on multi-tenant concerns).
- **Retroactive chain repair**: If a chain is found to be invalid (e.g., due to a store bug),
  this ADR does not define a repair protocol.  Invalid chains should be flagged and escalated;
  repair requires operator intervention.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Plain DataLogger entries (append-only dict list) | No tamper detection. An agent process with file-system write access can overwrite log entries silently. `verify_content()` would never be called because there is no hash to verify against. |
| DB-level triggers only, no application-layer hashes | Not portable: triggers are Postgres-specific and cannot protect in-process stores (SQLite, MemoryStore). Library mode would have no evidence integrity guarantees. |
| Timestamp-only ordering (no hash chain) | Timestamps can be spoofed or back-dated. They establish ordering only if the clock is trusted. A hash chain establishes ordering independently of wall time. |
| Full Merkle tree from the start | Provides efficient subset proofs but requires a tree-construction algorithm, a root-hash management layer, and more complex verification. The operational benefit over a linear chain is negligible at v1 scale (thousands of nodes per session, not billions). |
| Event sourcing (append-only event log, derived state) | Provides complete history but requires projections for current state, event schema versioning, and substantial infrastructure investment. The supersession pattern achieves the correction requirement with far less complexity. |
| Bidirectional supersession pointers (`superseded_by_id` on original) | Requires either a two-phase write (insert new node, then update original) or a transaction. Backward-only `supersedes_id` allows a single atomic INSERT with no mutation of the original node, which aligns with the immutability contract. prior research makes this decision for the same reason. |

## References

- [ADR-0033](ADR-0033-unified-entity-state-model.md) — defines `EvidenceRef` (8 kinds) and `NormalizedState`; this ADR adds hash-chain integrity to `EvidenceRef`
- [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md) — defines `Claim` and `KnowledgeStore` protocol; this ADR adds hash-chain integrity to `Claim`
- [ADR-0042](ADR-0042-task-certificate.md) — Task Certificate signs over a chain tip; depends on `ImmutableEvidenceNode.chain_hash` being stable
- [ADR-0049](ADR-0049-log-tier-governance.md) — Log Tier Governance classifies records as MUTABLE / PROTECTED / IMMUTABLE; `ImmutableEvidenceNode` is the mechanism backing the IMMUTABLE tier
- [ADR-0028](ADR-0028-status-reason-model.md) — StatusReason model; evidence nodes may carry status reasons as domain fields
- [ADR-0029](ADR-0029-artifact-contract.md) — Artifact contract; artifacts referenced by `EvidenceRef(kind="artifact")` benefit from chain provenance
- prior governance research `01_design/006-evidence-chain-cep/ADR-006-evidence-chain-cep.md` — source pattern for hash-chained evidence; D1 (chain hash formula), D2 (backward-only supersession)
- prior governance research `01_design/003-immutability/ADR-003-immutability.md` — source pattern for insert-only semantics, `_allowed_update_fields`, supersession over mutation
- prior governance research `01_design/002-entity/ADR-002-entity.md` — source pattern for dual hashing (`content_hash` + `integrity_hash`) and `_sensitive_fields` ClassVar
- NIST FIPS 180-4 — SHA-256 specification
