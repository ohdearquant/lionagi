# ADR-0042: Task Certificate — Signed Proof of Process Adherence

**Status**: proposed
**Date**: 2026-05-26
**Depends on**: [ADR-0041](ADR-0041-immutable-evidence-nodes.md) (certificates are evidence nodes; chain hash required)
**Related**: [ADR-0044](ADR-0044-tool-gates.md) (gates passed/failed recorded in certificate), [ADR-0045](ADR-0045-break-glass-protocol.md) (break-glass path produces DEGRADED certificate), [ADR-0050](ADR-0050-operation-context.md) (operation context serialized into certificate), [ADR-0033](ADR-0033-unified-entity-state-model.md) (EvidenceRef substrate), [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md) (evidence as first-class artifact)

## Context

lionagi today has no concept of *task completion proof*. A `Branch` session ends and the
operator is left with: a list of messages, `DataLogger` entries, and the final assistant
response. These artifacts assert *what happened*, but none of them assert *how the task was
governed*. There is no single artifact that answers:

- Were all required tool gates checked before execution?
- Was any gate bypassed via break-glass?
- Which policy version governed this run?
- Is the evidence chain for this task intact and unmodified?

This gap has concrete consequences. KHive's audit surface — and any operator deploying lionagi
for consequential workloads — needs a tamper-evident record that proves process was followed,
independently of whether the outcome was correct. Without this, "the agent said it followed the
rules" is the only evidence available, which is not evidence at all.

The cross-cutting principle **"Evidence is first-class, not logs"** applies directly: a DataLogger
entry recording that a gate check occurred is operational telemetry. A `TaskCertificate`
asserting that all gates passed, bound to the policy version active at execution time and
hash-chained to the evidence nodes produced during the task, is governed evidence. The
distinction is the same as the difference between a log line and a signed receipt.

The cross-cutting principle **"Every constraint must be enforced, not just documented"** also
applies: a governance policy that says "all HARD gates must pass" but produces no verifiable
artifact of that assertion is not enforced — it is aspirational. The certificate is what makes
adherence verifiable post-hoc.

### The applicable prior governance research insight

prior research introduced the *decision certificate* pattern for people-operations workflows
(terminations, adverse actions, PIP initiations). The core insight translates directly to agent
governance: the certificate is the primary proof artifact — it proves *process* adherence, not
*outcome* correctness. prior governance research further established that certificates are never revoked, only
superseded. Revocation creates audit ambiguity ("did this decision happen?"); supersession
preserves the full correction trail ("this decision happened; a later decision is now
authoritative"). In lionagi terms: if a task is re-run under a corrected policy, the original
certificate is superseded, not erased. Both records remain.

### Why lionagi needs this

Consider a governed ReAct loop that calls `write_file` three times. Each call is guarded by a
HARD gate requiring path allowlist membership (ADR-0044). One call is blocked; the agent invokes
break-glass (ADR-0045) to proceed. The session ends. Forty-eight hours later, an auditor asks:
"Did the agent stay within its path allowlist?" Today, the only answer is "check the logs." With
`TaskCertificate`, the answer is: "Here is the certificate for task X. `defensibility` is
`DEGRADED`. `gates_failed` lists the blocked gate. `break_glass_invoked` is `True`. The evidence
chain head is verifiable against the immutable evidence nodes in ADR-0041." The audit takes
seconds, not forensic reconstruction.

## Decision

We introduce `TaskCertificate`, a specialized `ImmutableEvidenceNode` (ADR-0041) that is emitted
when an agent task completes, encoding the process record: which gates passed, which failed,
whether break-glass was invoked, the hash-chain head of the evidence produced, the policy version
active at execution time, and any human or agent attestations collected.

### 1. State Machine

A `TaskCertificate` traverses four states. Transitions are irreversible.

```text
PROVISIONAL → GATED → MINTED
                         ↓
                    SUPERSEDED
```

A fifth terminal variant, `BREAK_GLASS`, branches from `GATED` when the task completed via
emergency override:

```text
PROVISIONAL → BREAK_GLASS → MINTED (defensibility: DEGRADED)
                                ↓
                           SUPERSEDED
```

State semantics:

| State | Meaning |
|-------|---------|
| `PROVISIONAL` | Task started; certificate emitted but no completion claim yet. Agent is still executing. |
| `GATED` | All required tool gates have passed. Task has completed normally. Pending final minting. |
| `BREAK_GLASS` | Task completed via emergency override (ADR-0045). Not all gates passed normally. Defensibility is DEGRADED regardless of outcome. |
| `MINTED` | Certificate is final. Content is sealed. Hash is recorded in the immutable evidence chain (ADR-0041). |
| `SUPERSEDED` | A newer certificate for the same `task_id` has been minted. This certificate is preserved and remains queryable; it is no longer the authoritative record. |

Once a certificate reaches `MINTED` or `SUPERSEDED`, its content fields are immutable. The
certificate object may only gain a `superseded_by` pointer; no other field changes.

### 2. TaskCertificate Dataclass

```python
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import ClassVar, Optional
from uuid import UUID

from pydantic import ConfigDict, Field

from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile


class CertificateState(str, Enum):
    PROVISIONAL = "PROVISIONAL"
    GATED = "GATED"
    BREAK_GLASS = "BREAK_GLASS"
    MINTED = "MINTED"
    SUPERSEDED = "SUPERSEDED"


class Defensibility(str, Enum):
    FULL = "FULL"          # All required gates passed; no emergency overrides
    DEGRADED = "DEGRADED"  # Break-glass was invoked; process was bypassed


class GateRecord(Element):
    """One gate evaluation result, recorded at certificate minting time."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
    )

    gate_id: str
    gate_kind: str          # "HARD" | "SOFT" | "ADVISORY"  (ADR-0044)
    tool_name: str
    passed: bool
    override_invoked: bool  # True if break-glass was used for this gate
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    policy_version: str     # Policy version active when this gate ran (ADR-0050)


class Attestation(Element):
    """
    A signed assertion by a human or agent that they reviewed and approved
    the task outcome.  Non-repudiable: once recorded, never removed.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
    )

    attestor_id: str              # Agent ID, user ID, or external principal
    attestor_kind: str            # "human" | "agent" | "system"
    statement: str                # Free-text or structured assertion
    attested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    signature: Optional[str] = None  # Optional: base64 SHA-256 HMAC or external sig


class EvidenceRef(Element):
    """
    Reference to an evidence node in the ADR-0041 chain.

    This mirrors the EvidenceRef substrate from ADR-0033/ADR-0041 and is
    Pile-managed here so certificates reference evidence rather than copying it.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
    )

    evidence_id: UUID
    evidence_kind: str
    chain_hash: str


class TaskCertificate(Element):
    """
    Immutable proof of process adherence for a single governed task.

    A TaskCertificate is a specialized ImmutableEvidenceNode (ADR-0041).
    It does NOT assert outcome correctness — it asserts that the process
    defined by the active policy was followed (or, if defensibility is
    DEGRADED, that it was bypassed with an auditable record).

    Fields are sealed at MINTED state.  Only superseded_by may be written
    post-minting, and only by the minting subsystem.
    """
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
        validate_assignment=True,
    )

    _mutable_after_seal: ClassVar[set[str]] = {"state", "superseded_by"}

    # Identity
    task_id: str = ""           # Caller-supplied task identifier
    session_id: str = ""        # lionagi Session.ln_id
    branch_id: str = ""         # lionagi Branch.ln_id

    # Lifecycle
    state: CertificateState = CertificateState.PROVISIONAL
    defensibility: Defensibility = Defensibility.FULL

    # Evidence linkage (ADR-0041)
    evidence_chain_head: Optional[str] = None   # SHA-256 hash of last evidence node
    evidence_node_count: int = 0                 # How many evidence nodes produced
    evidence_refs: Pile[EvidenceRef] = Field(
        default_factory=lambda: Pile(item_type={EvidenceRef}, strict_type=True)
    )

    # Gate results (ADR-0044)
    gates_passed: Pile[GateRecord] = Field(
        default_factory=lambda: Pile(item_type={GateRecord}, strict_type=True)
    )
    gates_failed: Pile[GateRecord] = Field(
        default_factory=lambda: Pile(item_type={GateRecord}, strict_type=True)
    )
    break_glass_invoked: bool = False

    # Attestations
    attestations: Pile[Attestation] = Field(
        default_factory=lambda: Pile(item_type={Attestation}, strict_type=True)
    )

    # Policy provenance (ADR-0050)
    policy_version_active: str = ""     # Policy version string at task start
    operation_context_hash: Optional[str] = None  # SHA-256 of serialized OperationContext

    # Temporal record
    emitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    gated_at: Optional[datetime] = None
    minted_at: Optional[datetime] = None

    # Supersession chain
    supersedes: Optional[UUID] = None         # Certificate this one replaces
    superseded_by: Optional[UUID] = None      # Certificate that replaces this one

    @property
    def certificate_id(self) -> UUID:
        """Compatibility alias for Element.id."""
        return self.id

    def __setattr__(self, name: str, value) -> None:
        sealed = getattr(self, "state", None) in (
            CertificateState.MINTED,
            CertificateState.SUPERSEDED,
        )
        if sealed and name not in self._mutable_after_seal:
            raise AttributeError("Minted certificates are immutable.")
        super().__setattr__(name, value)

    def content_hash(self) -> str:
        """
        SHA-256 of the certificate's immutable fields, excluding superseded_by.
        Used to verify the certificate has not been tampered with post-minting.
        """
        payload = self.to_dict(mode="db")
        payload.pop("superseded_by", None)
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()

    @property
    def is_authoritative(self) -> bool:
        """False if this certificate has been superseded."""
        return self.superseded_by is None

    @property
    def is_sealed(self) -> bool:
        """True if certificate content is immutable (MINTED or SUPERSEDED)."""
        return self.state in (CertificateState.MINTED, CertificateState.SUPERSEDED)
```

### 3. Minting Requirements

A certificate may only transition to `MINTED` when all of the following conditions hold:

1. **All HARD gates passed** — no `GateRecord` in `gates_failed` has `gate_kind == "HARD"`,
   unless `break_glass_invoked is True` (in which case `state` must be `BREAK_GLASS` and
   `defensibility` must be `DEGRADED`).
2. **Evidence chain intact** — `evidence_chain_head` is set and the chain is walkable back to
   the first evidence node emitted for this `task_id`. If any node in the chain is missing or
   its hash does not match, minting is refused.
3. **No unresolved SOFT gate overrides** — any SOFT gate that was overridden must have a
   corresponding `Attestation` with `attestor_kind != "system"` (i.e., a human or non-self
   agent attested the override).
4. **Policy version recorded** — `policy_version_active` is non-empty.
5. **State machine consistency** — state must be `GATED` or `BREAK_GLASS` before minting.

If any condition fails, `branch.mint_certificate()` raises `CertificateMintingError` with a
structured reason. The certificate remains in its pre-minting state; it is not discarded. The
operator may resolve the condition (e.g., supply a missing attestation) and retry.

```python
from pydantic import ConfigDict

from lionagi.protocols.generic.element import Element
from lionagi.protocols.governance.certificate import (
    TaskCertificate,
    CertificateState,
    Attestation,
)


class CertificateMintingFailure(Element):
    """Structured minting failure record suitable for logging and evidence."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
    )

    certificate_id: str
    reason: str
    requirement: str | None = None


class CertificateMintingError(Exception):
    """Raised when minting preconditions are not satisfied."""

    def __init__(self, failure: CertificateMintingFailure) -> None:
        self.failure = failure
        super().__init__(f"Cannot mint {failure.certificate_id}: {failure.reason}")
```

### 4. Branch and Session API

```python
from datetime import datetime, timezone

from lionagi.protocols.action.manager import ActionManager
from lionagi.protocols.generic.log import DataLogger
from lionagi.protocols.generic.pile import Pile
from lionagi.protocols.messages.manager import MessageManager
from lionagi.protocols.governance.certificate import Attestation, TaskCertificate


# Existing Branch managers are the integration point.
actions: ActionManager = branch.acts
messages: MessageManager = branch.msgs
audit: DataLogger = branch._log_manager

# On Branch — task-level operations
cert: TaskCertificate = await branch.emit_certificate(task_id="task-abc-123")
# Returns PROVISIONAL certificate immediately.
# The implementation extends ActionManager with a Pile[TaskCertificate] store
# and uses MessageManager progression to bind the task-flow transcript.
actions.certificates.include(cert)
cert.metadata["message_progression"] = messages.progression.to_dict(mode="json")
audit.log(cert)

cert = await branch.mint_certificate(
    task_id="task-abc-123",
    attestation=Attestation(
        attestor_id="user:ocean",
        attestor_kind="human",
        statement="Reviewed ReAct trace; process adherence confirmed.",
        attested_at=datetime.now(timezone.utc),
    ),
)
# Validates minting requirements; transitions GATED → MINTED.
# Raises CertificateMintingError if any precondition fails.
audit.log(cert)

# On Session — cross-branch retrieval
cert: TaskCertificate | None = session.get_certificate(task_id="task-abc-123")
# Returns the authoritative (non-superseded) certificate for task_id.
# Returns None if no certificate has been minted for that task.

all_certs: Pile[TaskCertificate] = session.list_certificates(
    task_id="task-abc-123",
    include_superseded=True,
)
# Returns all certificates for task_id, including superseded ones, ordered oldest-first.

new_cert = session.supersede_certificate(
    task_id="task-abc-123",
    reason="Policy version 2.1 invalidated gate configuration used in original run.",
)
# Mints a new PROVISIONAL certificate for the same task_id.
# Marks the current authoritative certificate as SUPERSEDED.
# Sets new_cert.supersedes = old_cert.certificate_id.
```

### 5. The Supersession Doctrine

Certificates are never revoked. The distinction matters:

- **Revocation** implies "this record should not have existed; treat it as if it never happened."
  This creates legal and audit ambiguity: was the process followed or not? Regulators and
  auditors cannot work with erasure.
- **Supersession** implies "this record happened; a newer record is now the authoritative
  reference; the old record is preserved as part of the audit trail."

If a task is re-run because the first run was executed under a defective policy version,
the correct action is: mint a new certificate for the same `task_id` (which supersedes the
original), with `supersedes` pointing back to the original certificate's `certificate_id`.
Both certificates remain in storage. Queries for the authoritative certificate return the
new one; queries with `include_superseded=True` return the full chain.

### 6. Post-Hoc Verification

Any party with access to the evidence store may verify a minted certificate:

```python
from lionagi.protocols.generic.pile import Pile
from lionagi.protocols.governance.certificate import (
    CertificateVerificationFinding,
    CertificateVerificationResult,
    verify_certificate,
)


result: CertificateVerificationResult = await verify_certificate(
    certificate=cert,
    evidence_store=session.evidence_store,
)
# Returns CertificateVerificationResult with fields:
#   chain_intact: bool        — evidence chain hashes all match
#   policy_consistent: bool   — gate decisions match policy_version_active rules
#   content_unmodified: bool  — cert.content_hash() matches stored hash at minting time
#   findings: Pile[CertificateVerificationFinding] — anomalies found, empty if clean
```

Verification re-walks the evidence chain starting from `evidence_chain_head`, verifying each
node's `prev_hash` pointer (ADR-0041). It then replays the gate decisions recorded in
`gates_passed` and `gates_failed` against the policy version stored in `policy_version_active`
(fetched from the policy store, ADR-0052). If the policy version is no longer available
(e.g., it was retired), verification returns `policy_consistent: None` (indeterminate) rather
than `False` — absence of the historical policy is not evidence of tampering.

### 7. Integration with Break-Glass (ADR-0045)

When break-glass is invoked during a governed task execution, the following occurs
automatically:

1. The pending certificate transitions from `GATED` to `BREAK_GLASS`.
2. `defensibility` is set to `DEGRADED`. This cannot be overridden by any attestation.
3. The gate that was bypassed is moved to `gates_failed` with `override_invoked=True`.
4. The break-glass event is recorded as an evidence node in the chain (ADR-0041).

A `BREAK_GLASS` certificate may still reach `MINTED` state. A `DEGRADED` certificate is a
legitimate, auditable record — it is not an error condition. It is the system correctly
recording that an exception was taken. The operator's audit surface (KHive) can filter for
`defensibility=DEGRADED` to surface tasks that require human review without rejecting them
outright.

### 8. Certificate Storage

`TaskCertificate` objects are stored as `ImmutableEvidenceNode` entries in the evidence store
(ADR-0041). The evidence node's `content_hash` field contains the value returned by
`TaskCertificate.content_hash()`. This means the certificate itself is hash-chained into the
same evidence store that records all other governed artifacts, providing a single integrity
surface.

Certificates are also indexed by `task_id` in a separate lookup table for efficient retrieval
by `session.get_certificate(task_id)`. The lookup table records only the `certificate_id`
and `task_id`; the full certificate content lives in the evidence store.

## Consequences

**Positive**

- Audit queries that previously required forensic log reconstruction are answered by a single
  `session.get_certificate(task_id)` call.
- Process adherence is verifiable post-hoc without access to the original session or branch —
  only the evidence store is required.
- The supersession chain preserves a complete correction history; corrections do not erase
  prior records.
- Break-glass paths are automatically promoted to auditable `DEGRADED` certificates; no
  special instrumentation is needed at the call site.
- Certificates compose with ADR-0041 (evidence nodes), ADR-0044 (gate records), and ADR-0050
  (operation context) without redundant storage — they reference, not duplicate.

**Negative**

- Storage growth is unbounded for long-lived KHive deployments. Superseded certificates are
  never deleted. Operators must provision accordingly.
- Adding `emit_certificate` / `mint_certificate` calls to every governed task path increases
  surface area for implementation bugs. The minting precondition checks must be exhaustive
  or they provide false assurance.
- Two-phase emission (PROVISIONAL at task start, MINTED at task end) complicates crash
  recovery: PROVISIONAL certificates for tasks that crashed without minting must be detected
  and flagged. A background reconciliation job is required.
- Verification requires the historical policy version to be available. Policy retirement
  without archival causes `policy_consistent: None` indeterminate results on old certificates.

## Non-Goals

Explicitly out of scope:

- **Outcome correctness verification**: A `MINTED` certificate with `defensibility=FULL`
  asserts that the process was followed. It does not assert that the task's output is correct,
  safe, or consistent with the operator's intent. Outcome verification is a separate concern
  (evaluation, human review).
- **Cryptographic signing with asymmetric keys (RSA/Ed25519)**: prior research uses
  RSA-4096. For v1 lionagi, SHA-256 content hashing and chain integrity (ADR-0041) are
  sufficient. Key management infrastructure for asymmetric signing is deferred to a future
  ADR targeting KHive enterprise deployments.
- **Human-signing UI**: The `Attestation` dataclass supports human attestations.
  The UI for collecting them is a KHive product concern, not a lionagi framework concern.
- **Multi-tenant certificate namespacing**: Certificates for different tenants sharing a
  KHive deployment must be isolated. Tenant namespacing is deferred to KHive's multi-tenancy
  layer.
- **Certificate revocation**: Deliberately absent. See supersession doctrine (section 5).
  Implementing revocation would undermine the audit guarantees this ADR is designed to provide.
- **Per-tool-call certificates**: Tool gates (ADR-0044) produce `GateRecord` entries that
  are aggregated into the task-level certificate. Per-call certificates are too granular
  for the audit use case and would produce certificate volumes that are unmanageable in
  practice.
- **Real-time certificate streaming**: Certificates are finalized at task completion.
  Streaming partial certificate state to external systems during execution is out of scope;
  that use case is served by the evidence stream (ADR-0041).

## Alternatives Considered

| Alternative | Why Rejected |
|------------|--------------|
| Store task status in session metadata | Session metadata is mutable and not tamper-evident. An agent (or a bug) can overwrite `session.metadata["task_status"] = "passed"` with no audit trail. This is logs, not evidence. |
| Per-tool certificates (one per `ActionRequest`) | Too granular. An agent making 30 tool calls would produce 30 certificates. Aggregating them into a coherent task-level record still requires a task certificate; per-tool certificates are redundant intermediate artifacts. |
| No certificates; rely on DataLogger + evidence nodes | DataLogger entries (ADR-0049) are mutable-tier by default. Evidence nodes (ADR-0041) record what happened but not whether the process was correctly followed. Neither alone asserts "all required gates passed under this policy version." The combination is not equivalent to a certificate. |
| Certificate revocation instead of supersession | Revocation creates legal ambiguity ("did this task happen?"). Supersession preserves the full audit trail while clearly marking which record is authoritative. Adopted from prior research which rejected revocation for the same reason. |
| Embed full evidence content in certificate | Evidence nodes can be arbitrarily large (file diffs, LLM responses). Embedding them in the certificate violates separation of concerns and makes storage unpredictable. The certificate references evidence via `evidence_chain_head`; retrieval traverses the chain (ADR-0041). |

## References

- [ADR-0041](ADR-0041-immutable-evidence-nodes.md) — certificates are evidence nodes; `evidence_chain_head` points into the immutable chain
- [ADR-0044](ADR-0044-tool-gates.md) — HARD/SOFT/ADVISORY gate records aggregated into certificate
- [ADR-0045](ADR-0045-break-glass-protocol.md) — break-glass invocation transitions certificate to BREAK_GLASS state with DEGRADED defensibility
- [ADR-0050](ADR-0050-operation-context.md) — `policy_version_active` and `operation_context_hash` sourced from OperationContext
- [ADR-0052](ADR-0052-policy-resolution.md) — policy version fetched for post-hoc verification replay
- [ADR-0033](ADR-0033-unified-entity-state-model.md) — `EvidenceRef` substrate; certificates produce an `EvidenceRef` of kind `artifact`
- [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md) — "Evidence is first-class, not logs" principle instantiated here
- prior governance research `01_design/007-decision-certificate/ADR-007-decision-certificate.md` — source pattern (decision certificate architecture, supersession doctrine, attestation records)
