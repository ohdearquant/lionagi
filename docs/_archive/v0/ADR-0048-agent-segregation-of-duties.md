# ADR-0048: Agent Segregation of Duties (SoD)

**Status**: proposed
**Date**: 2026-05-26
**Depends on**: [ADR-0047](ADR-0047-agent-charter.md) (SoD rules are charter constraints; charter enforces them at role-assignment time), [ADR-0046](ADR-0046-jit-tool-grant.md) (JIT grant issuer and consumer must be distinct actors), [ADR-0044](ADR-0044-tool-gates.md) (SoD enforcement surfaces as a HARD gate on role assignment), [ADR-0042](ADR-0042-task-certificate.md) (certificates require multi-actor attestation; SoD ensures those actors are genuinely distinct)
**Related**: [ADR-0041](ADR-0041-immutable-evidence-nodes.md) (exemption evidence is an immutable node), [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md) (branch.verify claims cannot use self-produced evidence), [ADR-0033](ADR-0033-unified-entity-state-model.md) (EvidenceRef carries actor_id for independence checks)

## Context

lionagi's multi-agent orchestration — `Session.flow()`, `li o fanout`, `li o flow` — assembles
pipelines of Branch actors. The framework assigns roles (implementer, reviewer, approver, auditor)
to branches at flow construction time. Today there is no mechanism that prevents the same branch
from holding roles that are structurally incompatible. A flow that creates an "implementer" step
and a "reviewer" step can assign both to `branch_a` with no warning. The branch reviews its own
output. The review passes. The task certificate records attestation by two roles — but both
attestations came from the same actor.

This is the central problem that Segregation of Duties (SoD) addresses. The cross-cutting
principle **"Every constraint must be enforced, not just documented"** is the direct motivation:
governance policies that require a reviewer to be independent of the author are common and
intuitive, but without runtime enforcement they are aspirational text. An autonomous LLM-driven
workflow does not read the policy document. It reads the code.

The consequence is not hypothetical. In a ReAct loop with a write step and a verify step, if both
steps run in the same branch, the branch is evaluating evidence it produced. In a multi-step
approval chain where the same branch holds two approval roles, a single compromise or error
propagates through both approvals. These failure modes are not caught by ADR-0044 gate checks
(which guard tool use, not role occupancy) or by ADR-0042 certificate multi-attestation (which
counts role signatures, not actor distinctness).

### The applicable prior governance research insight

Prior research introduced SoD as a *data-driven gate at grant time* for role assignments: the
same actor cannot both initiate and approve a state-changing action; cannot produce evidence and
then audit it; cannot grant privileged access and use it. The lion translation replaces "person"
with "branch actor" (identified by `ln_id`, not by model name), "role grant" with "role
assignment in a flow step", and "external authority approval" with "external attestation by a
human or a distinct designated authority agent". The five conflict types carry over verbatim
because they are structural — they describe the shape of the bypass, not the domain in which
it occurs.

### Why lionagi needs this

Consider a governed code-review flow: `session.flow()` constructs three steps — write, review,
merge. An orchestrator under load reuses `branch_a` for both write and review because it is
available and capable. Without SoD enforcement, `branch_a` submits code and then approves it.
The `TaskCertificate` records a "reviewer" attestation, which is technically true. The evidence
chain is intact. The audit trail looks clean. But the independence property that makes review
meaningful is absent. With SoD enforcement, the role assignment of `branch_a` to "reviewer" is
rejected at flow construction time — before any code is written — with a structured error naming
the conflict type (`TRANSACTION_DUAL_CONTROL`) and the existing role (`implementer`). The
orchestrator must select a different branch for the reviewer step, or fail closed.

## Decision

We introduce a `SoDPolicy` schema and a `assert_sod_independence` function that checks role
assignments against a versioned conflict matrix at assignment time, rejects conflicting
assignments with structured errors, and emits evidence when an exemption is in effect.

### 1. Conflict taxonomy

Five conflict types are defined, translated from prior research to agent terms:

| Conflict Type | Agent description | Canonical example |
|---|---|---|
| `TRANSACTION_DUAL_CONTROL` | Same actor both initiates and approves a state-changing action | branch writes code; branch reviews it |
| `RECORD_CUSTODY` | Same actor both creates evidence and audits it | an agent that proposes a file deletion cannot also be the agent that logs the deletion event |
| `AUDIT_INDEPENDENCE` | An actor's own outputs cannot be sole verification evidence | branch.verify() using only evidence from the same branch |
| `APPROVAL_CHAIN` | Same actor appears more than once in an approval chain | branch is both L1 and L2 approver in a multi-step sign-off |
| `ACCESS_CONTROL` | Same actor both grants tool access and uses the granted tool | branch issues a JIT grant (ADR-0046) and then exercises it |

### 2. Schema

```python
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import ConfigDict, Field

from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile


Scope = Literal["session", "task", "global"]

FROZEN_ELEMENT_CONFIG = ConfigDict(
    arbitrary_types_allowed=True,
    use_enum_values=True,
    populate_by_name=True,
    extra="forbid",
    frozen=True,
)


class ConflictType(str, Enum):
    TRANSACTION_DUAL_CONTROL = "transaction_dual_control"
    RECORD_CUSTODY            = "record_custody"
    AUDIT_INDEPENDENCE        = "audit_independence"
    APPROVAL_CHAIN            = "approval_chain"
    ACCESS_CONTROL            = "access_control"


class SoDDuty(Element):
    """A duty label that can be referenced by role and rule records."""

    model_config = FROZEN_ELEMENT_CONFIG

    label: str
    description: str | None = None


class SoDRole(Element):
    """A role label plus its governed duty set."""

    model_config = FROZEN_ELEMENT_CONFIG

    label: str
    duties: Pile[SoDDuty] = Field(
        default_factory=lambda: Pile(item_type=SoDDuty, strict_type=True)
    )


class SoDRule(Element):
    """One conflict pair within a policy.

    Both ``role_a`` and ``role_b`` are role labels as used in flow step
    definitions (e.g., "implementer", "reviewer", "approver", "auditor").
    The pair is bidirectional: (A, B) implies (B, A) - checked by
    ``assert_sod_independence`` via symmetric lookup.

    ``scope`` controls how far the actor registry extends:
    - "session" - conflict within a single Session object
    - "task"    - conflict within a named task / flow run
    - "global"  - conflict across all sessions on this node
    """

    model_config = FROZEN_ELEMENT_CONFIG

    rule_id: str
    conflict_type: ConflictType
    role_a: str
    role_b: str
    scope: Scope

    # Time-bounded exemption - requires external attestation, never self-issued.
    exemption_until: float | None = None          # Unix timestamp; None = no active exemption
    exemption_attestation: str | None = None      # Human or designated authority evidence ID


class SoDPolicy(Element):
    """Versioned set of SoD rules active for a session or flow.

    ``policy_id`` is stable across versions; ``version`` increments on any
    rule change.  The policy in effect at assignment time is recorded in the
    rejection evidence and in the TaskCertificate (ADR-0042).
    """

    model_config = FROZEN_ELEMENT_CONFIG

    policy_id: str
    version: int
    max_exemption_days: int = Field(default=365, ge=1, le=365)
    roles: Pile[SoDRole] = Field(
        default_factory=lambda: Pile(item_type=SoDRole, strict_type=True)
    )
    rules: Pile[SoDRule] = Field(
        default_factory=lambda: Pile(item_type=SoDRule, strict_type=True)
    )
```

### 3. Role registry

The assignment side tracks which actor currently holds which roles:

```python
from __future__ import annotations

import time

from pydantic import ConfigDict, Field

from lionagi.protocols.action.manager import ActionManager
from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.log import DataLogger
from lionagi.protocols.generic.pile import Pile
from lionagi.protocols.messages.manager import MessageManager
from lionagi.service.manager import iModelManager
from lionagi.session.branch import Branch


class RoleAssignment(Element):
    """A single actor-to-role binding."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )

    actor_id: str   # Branch.id / ln_id - NOT model name; see §6 for multi-LLM note
    role: str
    scope: Scope
    assigned_at: float = Field(default_factory=time.time)

    @classmethod
    def from_branch(cls, branch: Branch, role: str, scope: Scope) -> "RoleAssignment":
        """Bind a Branch actor while recording its active manager context."""
        return cls(
            actor_id=str(branch.id),
            role=role,
            scope=scope,
            metadata=_branch_manager_metadata(branch),
        )


class SoDCheckEvidence(Element):
    """First-class evidence for pass, denial, or exemption use."""

    model_config = FROZEN_ELEMENT_CONFIG

    actor_id: str
    requested_role: str
    scope: Scope
    policy_id: str
    policy_version: int
    passed: bool
    rule_id: str | None = None
    conflict_type: ConflictType | None = None
    conflicting_role: str | None = None
    exemption_attestation: str | None = None
    manager_snapshot: dict[str, object] = Field(default_factory=dict)


class SoDRegistry(Element):
    """Mutable registry of active role assignments for a scope.

    Populated at flow construction time.  ``assert_sod_independence``
    reads from this registry before writing to it.
    """

    assignments: Pile[RoleAssignment] = Field(
        default_factory=lambda: Pile(item_type=RoleAssignment, strict_type=True)
    )
    evidence: Pile[SoDCheckEvidence] = Field(
        default_factory=lambda: Pile(item_type=SoDCheckEvidence, strict_type=True)
    )

    def roles_for(self, actor_id: str, scope: str) -> list[str]:
        return [
            a.role
            for a in self.assignments
            if a.actor_id == actor_id and a.scope == scope
        ]

    def register(self, assignment: RoleAssignment) -> None:
        self.assignments.include(assignment)

    def register_branch(self, branch: Branch, role: str, scope: Scope) -> RoleAssignment:
        assignment = RoleAssignment.from_branch(branch, role=role, scope=scope)
        self.register(assignment)
        return assignment


def _branch_manager_metadata(branch: Branch | None) -> dict[str, object]:
    """Capture the existing Branch manager surfaces used by SoD."""
    if branch is None:
        return {}

    message_manager: MessageManager = branch.msgs
    action_manager: ActionManager = branch.acts
    imodel_manager: iModelManager = branch.mdls
    log_manager: DataLogger = branch._log_manager

    return {
        "message_count": len(message_manager.messages),
        "registered_tools": sorted(action_manager.registry.keys()),
        "chat_model": getattr(imodel_manager.chat, "model", None),
        "parse_model": getattr(imodel_manager.parse, "model", None),
        "log_count": len(log_manager.logs),
    }
```

### 4. Runtime check

`assert_sod_independence` is called at every role assignment — concretely, at the point where
`Session.flow()` (or `li o flow`) binds a branch to a step. It returns the assignment on success,
raises `SoDConflictError` on conflict, and emits an exemption evidence reference if an exemption
is active.

```python
from __future__ import annotations

import time

from collections.abc import Awaitable, Callable

from lionagi.agent.config import AgentConfig
from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.log import DataLogger
from lionagi.session.branch import Branch


class SoDConflict(Element):
    """Element record for a denied role assignment."""

    model_config = FROZEN_ELEMENT_CONFIG

    actor_id: str
    requested_role: str
    conflicting_role: str
    conflict_type: ConflictType
    rule_id: str
    policy_id: str
    policy_version: int


class SoDConflictError(Exception):
    """Raised when a role assignment would violate an active SoD rule."""

    def __init__(self, conflict: SoDConflict):
        self.conflict = conflict
        super().__init__(str(self))

    def __str__(self) -> str:
        conflict = self.conflict
        return (
            f"SoD conflict [{conflict.rule_id}]: actor '{conflict.actor_id}' already holds "
            f"role '{conflict.conflicting_role}'; cannot assign '{conflict.requested_role}' "
            f"({conflict.conflict_type.value}) - policy {conflict.policy_id} "
            f"v{conflict.policy_version}"
        )


def assert_sod_independence(
    actor_id: str,
    requested_role: str,
    scope: str,
    registry: SoDRegistry,
    policy: SoDPolicy | None,
    branch: Branch | None = None,
) -> RoleAssignment:
    """Check and register a role assignment against the active SoD policy.

    Raises ``SoDConflictError`` if a conflict is found and no valid exemption
    is in effect.  Returns the new ``RoleAssignment`` on success and registers
    it in ``registry``.

    Fail-closed: if ``policy`` has no rules (empty Pile), the assignment
    proceeds - an empty policy is a deliberate choice, not an absent matrix.
    If ``policy`` itself is ``None``, assignment is denied.
    """

    if policy is None:
        raise PermissionError("SoD assignment denied: no active SoDPolicy")

    now = time.time()
    effective_actor_id = str(branch.id) if branch is not None else actor_id
    held_roles = registry.roles_for(effective_actor_id, scope)

    for rule in policy.rules:
        # Bidirectional check
        if rule.scope != scope:
            continue
        pairs = {(rule.role_a, rule.role_b), (rule.role_b, rule.role_a)}
        for held in held_roles:
            if (held, requested_role) in pairs:
                if _is_active_exemption(rule, now):
                    # Exemption active - proceed but emit evidence reference.
                    _emit_exemption_evidence(
                        actor_id=effective_actor_id,
                        rule=rule,
                        policy=policy,
                        registry=registry,
                        branch=branch,
                        requested_role=requested_role,
                        conflicting_role=held,
                    )
                    break  # exemption covers this conflict; continue to next rule

                conflict = SoDConflict(
                    actor_id=effective_actor_id,
                    requested_role=requested_role,
                    conflicting_role=held,
                    conflict_type=rule.conflict_type,
                    rule_id=rule.rule_id,
                    policy_id=policy.policy_id,
                    policy_version=policy.version,
                )
                evidence = SoDCheckEvidence(
                    actor_id=effective_actor_id,
                    requested_role=requested_role,
                    scope=scope,
                    policy_id=policy.policy_id,
                    policy_version=policy.version,
                    passed=False,
                    rule_id=rule.rule_id,
                    conflict_type=rule.conflict_type,
                    conflicting_role=held,
                    manager_snapshot=_branch_manager_metadata(branch),
                )
                registry.evidence.include(evidence)
                _record_sod_evidence(branch, evidence)
                raise SoDConflictError(conflict)

    assignment = (
        RoleAssignment.from_branch(branch, requested_role, scope)
        if branch is not None
        else RoleAssignment(actor_id=effective_actor_id, role=requested_role, scope=scope)
    )
    registry.register(assignment)

    evidence = SoDCheckEvidence(
        actor_id=effective_actor_id,
        requested_role=requested_role,
        scope=scope,
        policy_id=policy.policy_id,
        policy_version=policy.version,
        passed=True,
        manager_snapshot=_branch_manager_metadata(branch),
    )
    registry.evidence.include(evidence)
    _record_sod_evidence(branch, evidence)
    return assignment


def _is_active_exemption(rule: SoDRule, now: float) -> bool:
    return (
        rule.exemption_until is not None
        and rule.exemption_until > now
        and rule.exemption_attestation is not None
    )


def _emit_exemption_evidence(
    *,
    actor_id: str,
    rule: SoDRule,
    policy: SoDPolicy,
    registry: SoDRegistry,
    branch: Branch | None,
    requested_role: str,
    conflicting_role: str,
) -> SoDCheckEvidence:
    """Emit an immutable evidence node recording the exemption.

    Implementation writes an EvidenceRef (ADR-0033) of kind
    ``human_assertion`` referencing ``rule.exemption_attestation``.
    The evidence node captures: actor_id, rule_id, policy version,
    exemption_until, and the exemption attestation ID.  This node
    becomes part of the TaskCertificate evidence chain (ADR-0042).

    Full implementation lives in lionagi/agent/governance/sod.py.
    The signature is the contract; the evidence schema is EvidenceRef from
    ADR-0033.
    """
    evidence = SoDCheckEvidence(
        actor_id=actor_id,
        requested_role=requested_role,
        scope=rule.scope,
        policy_id=policy.policy_id,
        policy_version=policy.version,
        passed=True,
        rule_id=rule.rule_id,
        conflict_type=rule.conflict_type,
        conflicting_role=conflicting_role,
        exemption_attestation=rule.exemption_attestation,
        manager_snapshot=_branch_manager_metadata(branch),
    )
    registry.evidence.include(evidence)
    _record_sod_evidence(branch, evidence)
    return evidence


def _record_sod_evidence(branch: Branch | None, evidence: SoDCheckEvidence) -> None:
    if branch is None:
        return
    log_manager: DataLogger = branch._log_manager
    log_manager.log(evidence)


def sod_assignment_pre_hook(
    registry: SoDRegistry,
    policy: SoDPolicy,
) -> Callable[[str, str, dict], Awaitable[dict | None]]:
    """Existing AgentConfig/Tool pre-hook wrapper for role-assignment tools."""

    async def _hook(tool_name: str, action: str, args: dict) -> dict | None:
        if action != "assign_role" and tool_name != "assign_role":
            return None

        branch = args.get("branch")
        if not isinstance(branch, Branch):
            raise PermissionError("SoD assignment denied: missing Branch actor")

        assignment = assert_sod_independence(
            actor_id=str(branch.id),
            requested_role=args["role"],
            scope=args["scope"],
            registry=registry,
            policy=policy,
            branch=branch,
        )
        args["sod_assignment_id"] = str(assignment.id)
        return args

    return _hook


def install_sod_enforcement(
    config: AgentConfig,
    registry: SoDRegistry,
    policy: SoDPolicy,
) -> AgentConfig:
    """Register SoD through lionagi's existing hook system, not a bespoke registry."""
    config.pre("assign_role", sod_assignment_pre_hook(registry, policy))
    return config
```

### 5. Worked example — code-review flow

A three-step flow: `write → review → merge`.

```text
Step 1: assign(branch=branch_a, actor=branch_a.id, role="implementer", scope="task:pr-42")
        -> _branch_manager_metadata captures branch_a.msgs, branch_a.acts,
           branch_a.mdls, and branch_a._log_manager
        -> held_roles(branch_a.id, "task:pr-42") = []
        -> no conflict -> RoleAssignment registered in SoDRegistry.assignments
        -> SoDCheckEvidence logged through DataLogger

Step 2: assign(branch=branch_a, actor=branch_a.id, role="reviewer", scope="task:pr-42")
        -> held_roles(branch_a.id, "task:pr-42") = ["implementer"]
        -> rule SOD-001: (implementer, reviewer, TRANSACTION_DUAL_CONTROL)
        -> no exemption active
        -> SoDConflictError: actor 'branch_a' already holds 'implementer';
           cannot assign 'reviewer' (transaction_dual_control)
        -> denial evidence stored in SoDRegistry.evidence and logged through DataLogger

        Orchestrator selects branch_b instead:
        assign(branch=branch_b, actor=branch_b.id, role="reviewer", scope="task:pr-42")
        -> held_roles(branch_b.id, "task:pr-42") = []
        -> no conflict -> assignment registered and evidence logged

Step 3: assign(branch=branch_c, actor=branch_c.id, role="merger", scope="task:pr-42")
        -> no conflict rules for (implementer|reviewer, merger)
        -> assignment registered and evidence logged
```

The TaskCertificate (ADR-0042) for this task records three distinct `actor_id` values for the
three attesting roles. The SoD check result at step 2 is preserved in the immutable evidence
chain (ADR-0041) as the rejection record.

### 6. Self-attestation prevention (AUDIT_INDEPENDENCE)

The `AUDIT_INDEPENDENCE` conflict type extends beyond flow role assignments to
`branch.verify()` from ADR-0039. A verification claim whose `actor_id` is `X` cannot rely
solely on evidence whose `actor_id` is also `X` within the same scope. This is the agent
analogue of the general principle that an actor cannot audit their own outputs.

At `branch.verify()` call time:

```python
from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile


def check_audit_independence(
    verifier_actor_id: str,
    evidence_refs: Pile[Element],
    scope: str,
    policy: SoDPolicy,
    branch: Branch | None = None,
    registry: SoDRegistry | None = None,
) -> None:
    """Raise SoDConflictError if all evidence is self-produced.

    A verification is independent if at least one EvidenceRef was produced
    by a different actor_id.  An empty evidence Pile fails closed - a claim
    with no supporting evidence cannot be verified.
    """
    audit_rules = Pile(item_type=SoDRule, strict_type=True)
    for rule in policy.rules:
        if (
            rule.conflict_type == ConflictType.AUDIT_INDEPENDENCE
            and rule.scope == scope
        ):
            audit_rules.include(rule)

    if audit_rules.is_empty():
        return  # No audit independence rules; proceed

    rule = audit_rules[0]

    if evidence_refs.is_empty():
        _raise_audit_independence_conflict(
            verifier_actor_id=verifier_actor_id,
            scope=scope,
            policy=policy,
            rule=rule,
            branch=branch,
            registry=registry,
        )

    independent = any(
        (actor := _evidence_actor_id(ref)) is not None
        and actor != verifier_actor_id
        for ref in evidence_refs
    )
    if not independent:
        _raise_audit_independence_conflict(
            verifier_actor_id=verifier_actor_id,
            scope=scope,
            policy=policy,
            rule=rule,
            branch=branch,
            registry=registry,
        )

    evidence = SoDCheckEvidence(
        actor_id=verifier_actor_id,
        requested_role="verifier",
        scope=scope,
        policy_id=policy.policy_id,
        policy_version=policy.version,
        passed=True,
        rule_id=rule.rule_id,
        conflict_type=ConflictType.AUDIT_INDEPENDENCE,
        manager_snapshot=_branch_manager_metadata(branch),
    )
    if registry is not None:
        registry.evidence.include(evidence)
    _record_sod_evidence(branch, evidence)


def _evidence_actor_id(ref: Element) -> str | None:
    return getattr(ref, "actor_id", None) or ref.metadata.get("actor_id")


def _raise_audit_independence_conflict(
    *,
    verifier_actor_id: str,
    scope: str,
    policy: SoDPolicy,
    rule: SoDRule,
    branch: Branch | None,
    registry: SoDRegistry | None,
) -> None:
    conflict = SoDConflict(
        actor_id=verifier_actor_id,
        requested_role="verifier",
        conflicting_role="sole_evidence_producer",
        conflict_type=ConflictType.AUDIT_INDEPENDENCE,
        rule_id=rule.rule_id,
        policy_id=policy.policy_id,
        policy_version=policy.version,
    )
    evidence = SoDCheckEvidence(
        actor_id=verifier_actor_id,
        requested_role="verifier",
        scope=scope,
        policy_id=policy.policy_id,
        policy_version=policy.version,
        passed=False,
        rule_id=rule.rule_id,
        conflict_type=ConflictType.AUDIT_INDEPENDENCE,
        conflicting_role="sole_evidence_producer",
        manager_snapshot=_branch_manager_metadata(branch),
    )
    if registry is not None:
        registry.evidence.include(evidence)
    _record_sod_evidence(branch, evidence)
    raise SoDConflictError(conflict)
```

### 7. Exemptions

An exemption is time-bounded, requires an external attestation ID (referencing a human decision
record or a designated authority agent's evidence node), and is never self-issued. The
constraints are:

- `exemption_until` must be a future timestamp; past timestamps are treated as absent.
- `exemption_attestation` must reference a non-null evidence ID from an actor other than the
  actor seeking the exemption.
- An agent cannot grant its own exemption. The `exemption_attestation` actor and the exempted
  actor must differ — this is itself an `ACCESS_CONTROL` rule.
- Every exemption use emits an immutable evidence node (ADR-0041) binding the exemption
  attestation ID to the assignment record.

No permanent exemptions. An exemption that exceeds the policy's `max_exemption_days` (default
365; agent flows default 30) is rejected at SoDPolicy construction time, not at runtime.

### 8. Multi-LLM actor identity

Same model, different actors. Two branches both backed by `gpt-4o` are different actors if they
have different `ln_id` values. SoD operates on `actor_id = branch.ln_id`, not on
`model_id = branch.imodel.model`. This is intentional: the independence property is about
decision authority, not computational substrate. A system that treats same-model branches as the
same actor would break all practical multi-agent flows, since most orchestrators use a single
capable model for all workers. The compromise vector that SoD addresses is a single *decision
actor* controlling both sides of a dual-control gate — not two instances of the same model
reaching independent conclusions.

Conversely, two branches with different `ln_id` values but the same `ln_id` prefix (e.g.,
cloned branches) are still distinct actors unless the flow explicitly aliases them. Aliasing is
out of scope for this ADR.

## Consequences

**Positive**

- Prevents self-approval in autonomous flows: the most critical single-actor bypass in
  multi-agent governance is closed at role-assignment time, before any work begins.
- Audit-grade independence: TaskCertificates (ADR-0042) that carry multi-role attestation now
  guarantee the attesting actors were genuinely distinct at the time of assignment.
- Early detection: orchestrators receive a structured `SoDConflictError` at flow construction,
  not midway through execution, allowing recovery (substitute a different branch) rather than
  rollback.
- Evidence trail: every conflict check — pass or fail — produces a record that names the
  policy version, rule ID, and actors involved. Exemptions produce immutable nodes.
- Aligns with cross-cutting principle #3: SoD constraints are charter constraints
  (ADR-0047) which become runtime gates (ADR-0044); the chain is now complete.

**Negative**

- Orchestration complexity: flows that previously reused one powerful branch for all steps
  must now provision at minimum two branches for any dual-control workflow pair.
- Minimum actor count: a flow with `N` mutually exclusive roles requires `N` distinct actors.
  For flows with 4+ conflicting role pairs, this can force significant branch proliferation.
- Policy maintenance burden: new roles added to a flow require conflict analysis against the
  active policy before deployment. The conflict matrix is deliberately manual — no automatic
  generation (see Non-Goals).
- Exemption process friction: legitimate single-actor flows (e.g., a solo exploration session
  not subject to audit requirements) require either a policy with no conflicting rules or an
  attested exemption; there is no silent bypass.

## Non-Goals

Explicitly out of scope:

- **Human SoD**: this ADR governs agent actors (`Branch` instances) only. Human users have
  their own authentication and authorization layer; their SoD is out of scope for this ADR.
- **Automatic conflict matrix generation**: the conflict matrix is authored by humans and
  reviewed before activation. Automatic inference of conflicts from role capability overlap
  is deliberately excluded — it would produce false positives and obscure the policy intent.
- **Model-level isolation guarantees**: SoD does not assert that two branches backed by the
  same LLM are computationally independent. It asserts only that they are organizationally
  distinct actors with separate decision authority.
- **Retroactive conflict detection**: the check runs at assignment time. Flows already running
  are not interrupted by a new policy version; the policy in effect at assignment time governs
  the run. Policy changes take effect on the next flow construction.
- **Cross-session global conflict tracking at scale**: the "global" scope in `SoDRule` is
  supported within a single lionagi node. Distributed global conflict tracking (across
  horizontally scaled nodes) is out of scope.

## Alternatives Considered

| Alternative | Why Rejected |
|---|---|
| Trust actor_id only, no role conflict check | Doesn't catch role-level conflicts. Two distinct actors can still form a self-approval loop if the same human controls both branches. Role conflicts are structural, not identity-based alone. |
| Human-only SoD (out-of-band process) | Does not prevent autonomous self-approval in flows with no human in the loop. A governed flow with no human reviewer can still self-approve; the process rule has no enforcement point. |
| SoD check at execution time (not assignment time) | Too late. The conflicting role assignment exists in the system; the branch has already begun work in both roles. Execution-time detection leaves the system in an inconsistent state that requires rollback rather than clean substitution. |
| Static exclusion pairs hardcoded in flow runner | Inflexible and non-auditable. Adding a new conflict pair requires a code change and deployment. Cannot be versioned or superseded without a release. No exemption path for legitimate edge cases. |
| No SoD (rely on operator discipline) | Rejected outright. Autonomous systems do not have discipline. Any governance property that depends on operators manually checking role assignments before every flow construction is not a governance property — it is a hope. |
| OPA/Rego for SoD policy evaluation | Adds external infrastructure dependency for a check that is fundamentally a set-membership problem. OPA adds flexibility the conflict matrix does not need, at the cost of an operational component that must be kept available (fail-closed requirement from cross-cutting principle #1). |

## References

- [ADR-0047](ADR-0047-agent-charter.md) — SoD rules are charter constraints; charter is the
  binding mechanism
- [ADR-0046](ADR-0046-jit-tool-grant.md) — JIT grant issuer and consumer must satisfy
  `ACCESS_CONTROL` SoD rule
- [ADR-0044](ADR-0044-tool-gates.md) — role assignment is enforced as a HARD gate;
  `SoDConflictError` maps to gate-denied
- [ADR-0042](ADR-0042-task-certificate.md) — certificate multi-attestation is meaningful only
  if attesting actors are SoD-independent
- [ADR-0041](ADR-0041-immutable-evidence-nodes.md) — exemption use and conflict detection
  produce immutable evidence nodes
- [ADR-0039](ADR-0039-knowledge-substrate-minimal-interface.md) — `branch.verify()` AUDIT_INDEPENDENCE check
- [ADR-0033](ADR-0033-unified-entity-state-model.md) — EvidenceRef carries `actor_id`; used
  in `check_audit_independence`
