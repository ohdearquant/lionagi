# ADR-0052: Policy Resolution and Staged Release

**Status**: proposed
**Date**: 2026-05-26
**Depends on**: [ADR-0044](ADR-0044-tool-gates.md) (policies select which gates run), [ADR-0047](ADR-0047-agent-charter.md) (charter pins policy_release_version), [ADR-0050](ADR-0050-operation-context.md) (active policy version captured in operation evidence), [ADR-0051](ADR-0051-tool-registry-allowlists.md) (registry entries are policy-scoped)
**Related**: [ADR-0041](ADR-0041-immutable-evidence-nodes.md) (bundle hash follows the same SHA-256 pattern), [ADR-0042](ADR-0042-task-certificate.md) (certificate records active policy version), [ADR-0045](ADR-0045-break-glass-protocol.md) (break-glass resolves against a named policy), [ADR-0046](ADR-0046-jit-tool-grant.md) (JIT grants are policy-scoped)

## Context

lionagi's agent governance today is captured in `AgentConfig` (`lionagi/agent/config.py`). An
`AgentConfig` carries a `PermissionPolicy` with allowlist, denylist, or confirm mode; a set of
`hook_handlers`; and a list of tool names. This is per-agent and flat: there is exactly one
permission policy per agent, it has no version, and there is no mechanism for resolving what
happens when multiple policies could apply to a single operation.

That flat model is adequate for single-agent, single-operator use. It breaks down as soon as scope
widens. A tool call in a governed session has at least three relevant policy dimensions: what the
tenant (or organization) requires by default, what the role of the calling agent requires, and what
the specific resource (the tool being called) requires. These three can conflict. lionagi today
has no mechanism to resolve that conflict, no representation of policy scope, and no way to version
the policy so that an audit can reconstruct what rules were in force at execution time.

Cross-cutting principle #1 — **fail-closed is the universal default** — means that the absence of
a resolution mechanism is not neutral. Unresolved conflicts must deny, not silently proceed. And
cross-cutting principle #3 — **every constraint must be enforced, not just documented** — means
the resolution algorithm must be code-backed, not a documentation convention. The gap is structural:
without `PolicyBundle`, `PolicyScope`, `ScopedPolicy`, and `PolicyResolver`, the framework cannot
enforce precedence, cannot version policy state, and cannot record which policy version was active
in evidence.

### The applicable prior governance research insight

prior research establishes *lex specialis* as the resolution rule: the most specific applicable
policy wins. Specificity is scored by scope level — resource-level policies score higher than
role-level, role-level higher than tenant-level, tenant-level higher than global. When the
highest-scoring candidates are tied (two resource-level policies both matching the same tool call),
the action is blocked. prior research extends this with a staged release model: policies are
immutable once published, released through `CANARY → ROLLING → ACTIVE` stages, and protected by a
Two-Key Model requiring separate Legal (what is required) and Engineering (how it is enforced)
authorship. Translated to lionagi: tenant becomes agent scope, action handler becomes tool call,
charter becomes the session-binding vehicle.

### Why lionagi needs this

Consider an agent in a governed deployment with role `reviewer`, operating for tenant `acme`, that
calls a `write_pr` tool. The deployment has four active policies:

- A global default policy that restricts agents to read-only operations.
- A tenant policy for `acme` that allows reviewers to comment but not merge.
- A role policy for `reviewer` that blocks direct merges and requires comment evidence.
- A resource policy for the `write_pr` tool that requires a JIT grant (ADR-0046) before execution.

Without a resolution algorithm, the framework cannot determine which of these four policies governs
this specific call. It must either apply all of them (combinatorial and potentially contradictory),
apply the first match (order-dependent and non-deterministic), or deny by default. Only the
most-specific-wins rule with DENY-on-tie gives the auditable, order-independent answer: the
resource-level policy `write_pr_requires_jit` governs, because `resource` scope has the highest
specificity score. The other three policies remain in force for calls they are most-specific for.

## Decision

We introduce `PolicyBundle`, `PolicyScope`, `ScopedPolicy`, `PolicyRelease`, and `PolicyResolver`
as the policy resolution and lifecycle layer for lionagi. When multiple policies apply to an
operation, resolution follows most-specific-wins: resource (score 3) > role (score 2) > tenant
(score 1) > global (score 0). Ties at the top score → DENY (fail-closed). Policy bundles are
versioned, immutable once published, and released in stages. Sessions pin to a release version at
start; mid-session policy changes do not apply until the next session.

### 1. `PolicyBundle` — the versioned policy unit

A `PolicyBundle` is the atomic policy artifact. It specifies which gates must run, which models
and evidence kinds are permitted, scope of tool registry, and session limits. It carries two
authorship fields implementing the Two-Key Model: `authored_by` (who wrote the policy — the
"Legal" role) and `implemented_by` (who wrote the enforcement code — the "Engineering" role).
These must be different actors. A SHA-256 hash over all governance fields enables tamper detection.

```python
# lionagi/protocols/governance/policy.py

from __future__ import annotations

import hashlib
import json
from typing import ClassVar, Literal

from pydantic import ConfigDict, Field, model_validator

from lionagi.agent.permissions import PermissionPolicy
from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile


PolicyDecision = Literal["allow", "deny", "escalate"]


class PolicyRuleRef(Element):
    """Reference to a gate/rule that participates in policy enforcement."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )

    gate_id: str
    enforcement: Literal["required", "disabled"]
    justification: str | None = None


class PolicyEvidenceRef(Element):
    """Evidence emitted while resolving or enforcing a policy bundle."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )

    evidence_id: str
    evidence_kind: str
    policy_version_active: str
    metadata_digest: str | None = None


class PolicyBundle(Element):
    """Versioned, immutable unit of policy backed by PermissionPolicy.

    A PolicyBundle specifies the enforcement contract for an operation: which
    gates must run (ADR-0044), which tool registry scope applies (ADR-0051),
    which models and evidence kinds are permitted, the PermissionPolicy rules
    to apply, and optional session limits.

    Two-Key Model:
        authored_by  — the actor who declared WHAT the policy requires (policy
                       author / Legal role). Does not write enforcement code.
        implemented_by — the actor who delivered HOW it is enforced (gate
                        implementer / Engineering role). Does not decide policy.
    Neither can produce a valid, active bundle alone.

    hash — SHA-256 of all fields except hash itself. A bundle whose hash does
    not match its content is rejected before use.

    Attributes:
        bundle_id:              Unique identifier for this policy bundle.
        version:                Monotonically increasing integer per bundle_id.
        permission_policy:      Existing lionagi PermissionPolicy used for the
                                final allow/deny/escalate decision.
        rules:                  Pile of PolicyRuleRef gate/rule records. Rules
                                whose enforcement is "required" replace the old
                                gates_required list; "disabled" replaces the
                                old gates_disabled list.
        registry_scope:         Which tool registry scope applies (ADR-0051).
        allowed_models:         iModel identifiers permitted under this policy.
        allowed_evidence_kinds: Subset of the 8 EvidenceRef kinds (ADR-0033)
                                permitted in evidence emitted under this bundle.
        max_session_duration_sec: Optional hard cap on session wall time.
        authored_by:            Identity of the policy author (Two-Key Key 1).
        implemented_by:         Identity of the gate implementer (Two-Key Key 2).
        hash:                   SHA-256 over all fields except hash itself.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )

    bundle_id: str
    version: int = Field(ge=1)
    permission_policy: PermissionPolicy = Field(
        default_factory=PermissionPolicy.deny_all
    )
    rules: Pile[PolicyRuleRef] = Field(
        default_factory=lambda: Pile(item_type=PolicyRuleRef, strict_type=True)
    )
    evidence_refs: Pile[PolicyEvidenceRef] = Field(
        default_factory=lambda: Pile(item_type=PolicyEvidenceRef, strict_type=True)
    )
    registry_scope: str           # RegistryScope string; see ADR-0051
    allowed_models: tuple[str, ...] = ()
    allowed_evidence_kinds: tuple[str, ...] = ()
    max_session_duration_sec: int | None = Field(default=None, ge=0)
    authored_by: str
    implemented_by: str
    hash: str = ""

    @model_validator(mode="after")
    def _validate_two_key_model(self) -> "PolicyBundle":
        if self.authored_by == self.implemented_by:
            raise ValueError(
                f"PolicyBundle '{self.bundle_id}': authored_by and implemented_by "
                "must be different actors. The Two-Key Model requires separation "
                "between the policy author (what is required) and the gate "
                "implementer (how it is enforced). Same actor in both roles "
                "defeats the control."
            )
        return self

    @property
    def gates_required(self) -> tuple[str, ...]:
        return tuple(r.gate_id for r in self.rules if r.enforcement == "required")

    @property
    def gates_disabled(self) -> tuple[str, ...]:
        return tuple(r.gate_id for r in self.rules if r.enforcement == "disabled")

    def verify_hash(self) -> bool:
        """Return True if self.hash matches recomputed hash of content fields."""
        return self.hash == _compute_bundle_hash(self)


def _compute_bundle_hash(bundle: PolicyBundle) -> str:
    """Compute SHA-256 over all ratification-relevant fields.

    Excludes: hash (the field being computed).
    """
    content = bundle.to_dict(mode="db", exclude={"hash", "permission_policy"})
    content["permission_policy"] = {
        "mode": bundle.permission_policy.mode,
        "allow": bundle.permission_policy.allow,
        "deny": bundle.permission_policy.deny,
        "escalate": bundle.permission_policy.escalate,
    }
    serialized = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()
```

### 2. `PolicyScope` — specificity scoring

`PolicyScope` encodes where a policy applies and scores its specificity. The scoring follows
lex specialis: a policy scoped to a specific resource (tool_id) is more specific than one scoped
to a role, which is more specific than one scoped to a tenant, which is more specific than a
global default.

```python
class PolicyScope(Element):
    """Describes the scope at which a policy applies and its specificity score.

    Specificity scoring (most to least specific):
        resource  → 3  (applies to a specific tool_id)
        role      → 2  (applies to agents with a named role)
        tenant    → 1  (applies to all agents in a tenant)
        global    → 0  (applies everywhere; baseline default)

    scope_value is the matched identifier:
        resource:  the tool_id
        role:      the role name (e.g. "reviewer")
        tenant:    the tenant_id (e.g. "acme")
        global:    "*"

    Higher specificity always wins. Ties at the highest specificity → DENY.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )

    _SPECIFICITY_MAP: ClassVar[dict[str, int]] = {
        "resource": 3,
        "role": 2,
        "tenant": 1,
        "global": 0,
    }

    scope_type: Literal["resource", "role", "tenant", "global"]
    scope_value: str
    specificity: int = Field(ge=0, le=3)   # resource=3, role=2, tenant=1, global=0

    @model_validator(mode="after")
    def _validate_specificity(self) -> "PolicyScope":
        expected = self._SPECIFICITY_MAP[self.scope_type]
        if self.specificity != expected:
            raise ValueError(
                f"PolicyScope scope_type '{self.scope_type}' requires "
                f"specificity={expected}, got {self.specificity}"
            )
        return self
```

### 3. `ScopedPolicy` — a bundle bound to a scope with a validity window

```python
class ScopedPolicy(Element):
    """A PolicyBundle bound to a PolicyScope with a validity window.

    valid_from and valid_until are Unix timestamps. A ScopedPolicy is applicable
    only when: valid_from <= at < valid_until (or valid_until is None).

    policy_id uniquely identifies this scoped binding. Multiple ScopedPolicies
    may reference the same PolicyBundle (same bundle_id) with different scopes.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )

    policy_id: str
    bundle: PolicyBundle
    scope: PolicyScope
    valid_from: float
    valid_until: float | None = None

    def is_valid_at(self, at: float) -> bool:
        """Return True if this policy is temporally active at timestamp `at`."""
        if at < self.valid_from:
            return False
        if self.valid_until is not None and at >= self.valid_until:
            return False
        return True
```

### 4. `PolicyResolutionError` — the fail-closed signal

```python
class PolicyConflict(Element):
    """Ambiguity record emitted when equally specific policies match."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )

    reason: Literal["no_applicable_policy", "specificity_tie", "hash_mismatch"]
    conflicting_policies: Pile[ScopedPolicy] = Field(
        default_factory=lambda: Pile(item_type=ScopedPolicy, strict_type=True)
    )
    detail: str


class PolicyResolutionResult(Element):
    """Evidence-grade result of policy resolution for one operation."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )

    operation_ref: str
    tool_id: str
    role: str
    tenant_id: str
    at: float
    decision: PolicyDecision
    selected_policy: ScopedPolicy | None = None
    bundle: PolicyBundle | None = None
    candidates: Pile[ScopedPolicy] = Field(
        default_factory=lambda: Pile(item_type=ScopedPolicy, strict_type=True)
    )
    conflicts: Pile[PolicyConflict] = Field(
        default_factory=lambda: Pile(item_type=PolicyConflict, strict_type=True)
    )
    evidence_refs: Pile[PolicyEvidenceRef] = Field(
        default_factory=lambda: Pile(item_type=PolicyEvidenceRef, strict_type=True)
    )

    @property
    def policy_version_active(self) -> str | None:
        if self.bundle is None:
            return None
        return f"{self.bundle.bundle_id}@v{self.bundle.version}"


class PolicyResolutionError(PermissionError):
    """Raised when policy resolution cannot produce an unambiguous result.

    Resolution fails for exactly three reasons:
        1. No applicable policy exists — no matching scoped policy covers
           the (tool_id, role, tenant_id, at) tuple.
        2. The top-scoring candidates are tied — two or more ScopedPolicies
           at the same (highest) specificity both match.
        3. Bundle hash verification fails — the selected bundle has been
           tampered with.

    All three cases result in DENY (fail-closed). Callers MUST NOT proceed
    with the operation when this exception is raised.
    """

    def __init__(
        self,
        message: str,
        *,
        result: PolicyResolutionResult | None = None,
    ) -> None:
        super().__init__(message)
        self.result = result
```

### 5. `PolicyResolver` — the resolution algorithm

```python
class PolicyResolver:
    """Resolves scoped policies while preserving PermissionPolicy as authority.

    Resolution algorithm (most-specific-wins):
        1. Collect all ScopedPolicies whose scope matches the operation:
               - resource match: scope.scope_type == "resource" and
                 scope.scope_value == tool_id
               - role match:     scope.scope_type == "role" and
                 scope.scope_value == role
               - tenant match:   scope.scope_type == "tenant" and
                 scope.scope_value == tenant_id
               - global match:   scope.scope_type == "global"
           AND is_valid_at(at) == True.
        2. If no candidates: raise PolicyResolutionError (no policy → deny).
        3. Sort candidates by scope.specificity descending.
        4. Identify the maximum specificity score among candidates.
        5. Collect all candidates at that maximum specificity.
        6. If more than one: raise PolicyResolutionError (tie → deny).
        7. Verify winner.bundle.verify_hash(). If False: raise PolicyResolutionError
           (tampered bundle → deny).
        8. Delegate the final allow/deny/escalate decision to the selected
           bundle.permission_policy.check().
        9. Return a PolicyResolutionResult Element suitable for DataLogger.

    Cross-cutting principle #1: ambiguity at any step → deny.
    """

    def __init__(
        self,
        *,
        base_policy: PermissionPolicy,
        policies: Pile[ScopedPolicy] | None = None,
        release_version: str | None = None,
    ) -> None:
        self.base_policy = base_policy
        self.policies = policies or Pile(item_type=ScopedPolicy, strict_type=True)
        self.release_version = release_version

    def resolve(
        self,
        *,
        tool_id: str,
        action: str,
        args: dict,
        role: str,
        tenant_id: str,
        at: float,
        operation_ref: str = "",
    ) -> PolicyResolutionResult:
        """Return the most-specific applicable result for this operation.

        Args:
            tool_id:   The identifier of the tool being called.
            action:    The action string passed to PermissionPolicy.check().
            args:      Tool arguments passed to PermissionPolicy.check().
            role:      The calling agent's role.
            tenant_id: The tenant the agent belongs to.
            at:        Unix timestamp of the operation (use time.time()).

        Returns:
            PolicyResolutionResult containing the selected bundle and decision.

        Raises:
            PolicyResolutionError: If no policy applies, if the top candidates
                are tied, or if the winning bundle's hash fails verification.
        """
        candidates = self._find_applicable(tool_id, role, tenant_id, at)

        if not candidates:
            result = PolicyResolutionResult(
                operation_ref=operation_ref,
                tool_id=tool_id,
                role=role,
                tenant_id=tenant_id,
                at=at,
                decision="deny",
            )
            raise PolicyResolutionError(
                f"No applicable policy for tool='{tool_id}' role='{role}' "
                f"tenant='{tenant_id}' at={at:.3f} — fail closed. "
                "Register at least a global fallback policy.",
                result=result,
            )

        ordered = sorted(candidates, key=lambda c: c.scope.specificity, reverse=True)
        top_score = ordered[0].scope.specificity
        winners = [c for c in ordered if c.scope.specificity == top_score]

        if len(winners) > 1:
            ids = ", ".join(w.policy_id for w in winners)
            conflict = PolicyConflict(
                reason="specificity_tie",
                conflicting_policies=Pile(
                    collections=winners,
                    item_type=ScopedPolicy,
                    strict_type=True,
                ),
                detail=(
                    f"{len(winners)} policies tied at specificity={top_score}: {ids}"
                ),
            )
            result = PolicyResolutionResult(
                operation_ref=operation_ref,
                tool_id=tool_id,
                role=role,
                tenant_id=tenant_id,
                at=at,
                decision="deny",
                candidates=candidates,
                conflicts=Pile(
                    collections=[conflict],
                    item_type=PolicyConflict,
                    strict_type=True,
                ),
            )
            raise PolicyResolutionError(
                f"{len(winners)} policies tied at specificity={top_score} "
                f"for tool='{tool_id}' role='{role}' tenant='{tenant_id}': "
                f"[{ids}] — fail closed. Disambiguate by adjusting scope or "
                "superseding one with a more-specific bundle.",
                result=result,
            )

        winner = winners[0]
        if not winner.bundle.verify_hash():
            conflict = PolicyConflict(
                reason="hash_mismatch",
                conflicting_policies=Pile(
                    collections=[winner],
                    item_type=ScopedPolicy,
                    strict_type=True,
                ),
                detail=(
                    f"PolicyBundle '{winner.bundle.bundle_id}' "
                    f"v{winner.bundle.version} hash verification failed"
                ),
            )
            result = PolicyResolutionResult(
                operation_ref=operation_ref,
                tool_id=tool_id,
                role=role,
                tenant_id=tenant_id,
                at=at,
                decision="deny",
                selected_policy=winner,
                bundle=winner.bundle,
                candidates=candidates,
                conflicts=Pile(
                    collections=[conflict],
                    item_type=PolicyConflict,
                    strict_type=True,
                ),
            )
            raise PolicyResolutionError(
                f"PolicyBundle '{winner.bundle.bundle_id}' v{winner.bundle.version} "
                "hash verification failed — bundle may have been tampered with. "
                "Reject and investigate before proceeding.",
                result=result,
            )

        decision = winner.bundle.permission_policy.check(tool_id, action, args)
        return PolicyResolutionResult(
            operation_ref=operation_ref,
            tool_id=tool_id,
            role=role,
            tenant_id=tenant_id,
            at=at,
            decision=decision.behavior,
            selected_policy=winner,
            bundle=winner.bundle,
            candidates=candidates,
        )

    def _find_applicable(
        self,
        tool_id: str,
        role: str,
        tenant_id: str,
        at: float,
    ) -> Pile[ScopedPolicy]:
        """Return all ScopedPolicies that match the operation context."""
        matches = []
        for sp in self.policies:
            if not sp.is_valid_at(at):
                continue
            st = sp.scope.scope_type
            sv = sp.scope.scope_value
            if (
                (st == "resource" and sv == tool_id)
                or (st == "role" and sv == role)
                or (st == "tenant" and sv == tenant_id)
                or st == "global"
            ):
                matches.append(sp)
        return Pile(collections=matches, item_type=ScopedPolicy, strict_type=True)

    def to_pre_hook(self, *, branch=None):
        """Install through AgentConfig.security_pre or Tool.preprocessor chains."""

        async def policy_resolution_hook(
            tool_name: str, action: str, args: dict
        ) -> dict | None:
            import time

            branch_ref = branch or args.get("branch")
            role = args.get("role", "default")
            tenant_id = args.get("tenant_id", "global")
            result = self.resolve(
                tool_id=tool_name,
                action=action,
                args=args,
                role=role,
                tenant_id=tenant_id,
                at=time.time(),
                operation_ref=args.get("operation_ref", ""),
            )

            if branch_ref is not None:
                # Branch managers are the integration point:
                # ActionManager supplies the tool registry, iModelManager supplies
                # active model identity, and DataLogger records the Element result.
                _ = branch_ref.acts.registry.get(tool_name)
                model_name = getattr(branch_ref.chat_model, "model", None)
                if result.bundle and result.bundle.allowed_models:
                    if model_name not in result.bundle.allowed_models:
                        raise PermissionError(
                            f"Model '{model_name}' is not allowed by "
                            f"{result.policy_version_active}"
                        )
                branch_ref.metadata["policy_version_active"] = (
                    result.policy_version_active
                )
                branch_ref._log_manager.log(result)

            if result.decision == "allow":
                return None
            if result.decision == "escalate":
                raise PermissionError(
                    f"Permission escalation required under "
                    f"{result.policy_version_active}"
                )
            raise PermissionError(
                f"Permission denied under {result.policy_version_active}"
            )

        return policy_resolution_hook
```

### 6. `PolicyRelease` — staged rollout lifecycle

A `PolicyRelease` bundles one or more `ScopedPolicy` records with a version and a status. The
status lifecycle gates deployment: a release does not affect production sessions until it reaches
`ACTIVE`. Sessions pin their release version at start; a release transitioning from `ROLLING` to
`ACTIVE` mid-session does not apply to already-running sessions.

```python
ReleaseStatus = Literal["DRAFT", "CANARY", "ROLLING", "ACTIVE", "SUPERSEDED"]


class PolicyRelease(Element):
    """A versioned, staged release of a set of ScopedPolicies.

    Status lifecycle:
        DRAFT     → authored, not yet deployed to any sessions
        CANARY    → active for <5% of new sessions (opt-in or random sample)
        ROLLING   → active for a named tenant group (explicit list)
        ACTIVE    → global default; applies to all new sessions
        SUPERSEDED → replaced by a newer release; no new sessions pin to this

    Pinning rule: a session records release_version at start. The PolicyResolver
    for that session is constructed from the bundles in that specific release.
    Transitions after session start (CANARY → ROLLING, ROLLING → ACTIVE) do not
    affect already-running sessions.

    Attributes:
        release_id:       Unique identifier for this release.
        release_version:  Human-readable version string (e.g. "2026.05.1").
        status:           Current lifecycle stage.
        policies:         The ScopedPolicies included in this release.
        canary_tenant_ids: Tenants opted into CANARY stage (empty = random 5%).
        rolling_tenant_ids: Tenants in the ROLLING cohort.
        released_at:      Unix timestamp of transition to CANARY or later.
        superseded_by:    release_id of the successor, once SUPERSEDED.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )

    release_id: str
    release_version: str
    status: ReleaseStatus
    policies: Pile[ScopedPolicy] = Field(
        default_factory=lambda: Pile(item_type=ScopedPolicy, strict_type=True)
    )
    canary_tenant_ids: tuple[str, ...]
    rolling_tenant_ids: tuple[str, ...]
    released_at: float | None
    superseded_by: str | None = None

    def resolver_for(
        self,
        tenant_id: str,
        *,
        base_policy: PermissionPolicy,
    ) -> PolicyResolver | None:
        """Return a PolicyResolver if this release applies to tenant_id.

        Returns None if the release is DRAFT or SUPERSEDED, or if the tenant
        is not yet in scope for the current status (e.g. not in canary cohort
        when status is CANARY).
        """
        if self.status in ("DRAFT", "SUPERSEDED"):
            return None
        if self.status == "CANARY":
            if self.canary_tenant_ids and tenant_id not in self.canary_tenant_ids:
                return None
        elif self.status == "ROLLING":
            if tenant_id not in self.rolling_tenant_ids:
                return None
        return PolicyResolver(
            base_policy=base_policy,
            policies=self.policies,
            release_version=self.release_version,
        )
```

### 7. Operation Context capture

When `PolicyResolver.resolve()` succeeds, the resolved `bundle_id` and `bundle.version` are
written into `OperationContext.policy_version_active` (ADR-0050). Every evidence node (ADR-0041)
emitted during the operation carries this value in its metadata. Auditors can reconstruct the
exact policy that governed any historical operation by looking up that bundle version in the
policy store.

```python
# lionagi/agent/governance/policy.py

from lionagi.agent.config import AgentConfig
from lionagi.agent.permissions import PermissionPolicy
from lionagi.session.branch import Branch


def attach_policy_release(
    *,
    config: AgentConfig,
    branch: Branch,
    release: PolicyRelease,
    tenant_id: str,
    base_policy: PermissionPolicy,
) -> PolicyResolver | None:
    """Attach policy resolution through the existing hook and manager surfaces."""
    resolver = release.resolver_for(tenant_id, base_policy=base_policy)
    if resolver is None:
        return None

    hook = resolver.to_pre_hook(branch=branch)
    config.hook_handlers.setdefault("security_pre:*", []).insert(0, hook)

    # Existing Branch managers remain authoritative:
    # - MessageManager carries policy metadata on operation messages.
    # - ActionManager provides the registered tool set the hook guards.
    # - iModelManager provides the active model identity checked by the hook.
    # - DataLogger receives PolicyResolutionResult Element records.
    branch.metadata["policy_release_version"] = release.release_version
    branch.metadata["policy_version_active"] = None
    branch.on_message_added.append(
        lambda message: message.metadata.setdefault(
            "policy_release_version", release.release_version
        )
    )
    return resolver
```

### 8. Resolution algorithm — summary table

| Step | Action | On failure |
|------|--------|------------|
| 1 | Collect all ScopedPolicies matching (tool_id, role, tenant_id) with is_valid_at(at) | → step 2 |
| 2 | If no candidates | → DENY (PolicyResolutionError: "no applicable policy") |
| 3 | Sort by scope.specificity descending | — |
| 4 | Identify max specificity score | — |
| 5 | Collect all candidates at max score | → step 6 |
| 6 | If more than one winner | → DENY (PolicyResolutionError: "ambiguous tie") |
| 7 | Verify winner.bundle.verify_hash() | → DENY (PolicyResolutionError: "tampered bundle") |
| 8 | Return winner.bundle | — |

### 9. Worked example

An agent in `tenant=acme`, `role=reviewer`, calls `tool=write_pr` at Unix timestamp `t`.
The active `PolicyRelease` contains four `ScopedPolicy` records:

| policy_id | scope_type | scope_value | specificity | bundle_id |
|-----------|------------|-------------|-------------|-----------|
| `global-read-only` | global | `*` | 0 | `read_only` |
| `acme-reviewers-comment` | tenant | `acme` | 1 | `reviewers_can_comment` |
| `role-reviewer-no-merge` | role | `reviewer` | 2 | `reviewers_can_comment_no_merge` |
| `tool-write-pr-jit` | resource | `write_pr` | 3 | `write_pr_requires_jit` |

All four match the operation context. Sorted by specificity descending:
`tool-write-pr-jit` (3), `role-reviewer-no-merge` (2), `acme-reviewers-comment` (1),
`global-read-only` (0).

Maximum specificity = 3. Only one candidate at that score: `tool-write-pr-jit`. Hash verified.

Resolution result: `write_pr_requires_jit` applies. The gates declared in that bundle run
(including the JIT grant gate from ADR-0046). `OperationContext.policy_version_active` is set
to `"write_pr_requires_jit@v3"`. The other three bundles remain authoritative for operations
they are most-specific for.

### 10. Two-Key Model — enforcement

The structural separation in `PolicyBundle.__post_init__` (`authored_by != implemented_by`) is
a construction-time check. The policy authorship record is stored in the bundle and covered by
`bundle.hash`. Any post-creation modification that changes either authorship field or any gate
list is detectable via `verify_hash()` before the bundle is used.

Operationally, Two-Key means:

- A policy author can declare `gates_required: ["gate_write_pr_approval"]` in a bundle. They
  cannot ship that gate or modify its logic.
- A gate implementer can register `gate_write_pr_approval` in the gate registry (ADR-0044).
  They cannot include or exclude it from a bundle's `gates_required`.
- A bundle that lists a gate not present in the gate registry is rejected at the same point
  as a Charter with an unresolvable `gate_id` (ADR-0047, section 4).

## Consequences

**Positive**

- Deterministic resolution: any (tool_id, role, tenant_id, at) tuple resolves to exactly one
  bundle or raises. Order of policy registration has no effect on the outcome.
- Fail-closed by construction: every resolution failure path raises `PolicyResolutionError`,
  which callers must handle. There is no "default permit" fallback.
- Audit-grade traceability: `OperationContext.policy_version_active` and evidence node metadata
  record the exact policy bundle in force. Reconstructing governance history requires only the
  policy store and the operation context.
- Staged rollback: a bad release transitions from `ACTIVE` to `SUPERSEDED` and a prior release
  is re-activated. Sessions that pinned to the bad release complete under it (or abort); new
  sessions pin to the restored release.
- Two-Key separation: no single actor can produce a valid, deployed bundle that both declares
  a constraint and ships its enforcement. The hash makes that separation tamper-evident.

**Negative**

- Resolution complexity: operators must reason about scope levels and avoid accidental ties.
  Two resource-level policies for the same tool_id will always produce a DENY-on-tie until one
  is superseded or scoped differently.
- Ambiguity burden is on policy authors: the DENY-on-tie rule is conservative. Legitimate
  scenarios that happen to tie require explicit disambiguation, which may require a release cycle.
- Multiple active versions: during CANARY and ROLLING stages, different tenants run against
  different policy versions. The policy store and audit tooling must support querying by
  bundle version, not just by current state.
- Two-Key coordination overhead: policy authors and gate implementers must coordinate each
  release. A bundle referencing a gate not yet registered blocks the release from advancing
  past DRAFT.

## Non-Goals

Explicitly out of scope:

- **Full conflict resolution with weights or priorities**: only specificity rank determines the
  winner. Weighted scoring, confidence-adjusted merging, or partial policy application are not
  supported. If weights are needed, supersede the lower-specificity policy with a new bundle
  at a higher specificity scope.
- **Automatic policy generation from regulations**: tooling that reads GDPR or HIPAA text and
  produces `PolicyBundle` records is out of scope for this ADR.
- **Runtime policy mutation**: a running session cannot modify its own policy. All changes must
  go through the release lifecycle (DRAFT → CANARY → ROLLING → ACTIVE) and apply only to
  sessions starting after the release reaches the applicable stage.
- **Policy composition or merging**: combining two bundles' `gates_required` lists into a
  synthetic bundle is not supported. Each bundle is the atomic governance unit; composition
  would re-introduce the conflict resolution problem this ADR solves.
- **Automatic release promotion**: advancing a release from CANARY to ROLLING to ACTIVE requires
  explicit human action (or an external approval gate). Automated promotion based on error rates
  is out of scope for this ADR.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| First-match wins | Resolution depends on registration order. Two deployments with identically scoped policies but different registration sequences produce different results. Non-deterministic; not audit-defensible. |
| Confidence-weighted merging | Assigns weights to each applicable policy and blends the result. Introduces a scoring function that is itself a policy decision, is difficult to audit, and violates the fail-closed principle — partial application of a policy that would deny is effectively a permit. |
| Single global policy (current state) | `AgentConfig.permissions` is a single flat policy per agent with no versioning and no scope hierarchy. Adequate for single-agent use; rejected for multi-agent deployments where role and resource overrides are required. |
| Most-specific-wins with permit-on-tie | The alternative to DENY-on-tie. Rejected because a tie indicates two equally-authoritative policies disagree. Picking one arbitrarily is non-deterministic. Denying forces policy authors to resolve the ambiguity explicitly, which is the correct outcome. |

## References

- [ADR-0041](ADR-0041-immutable-evidence-nodes.md) — SHA-256 hash-chain pattern; `PolicyBundle.hash` follows the same construction
- [ADR-0042](ADR-0042-task-certificate.md) — Task Certificate records the active policy version as part of the proof artifact
- [ADR-0044](ADR-0044-tool-gates.md) — `PolicyBundle.gates_required` references gate_ids from this registry; a bundle listing an absent gate is rejected
- [ADR-0045](ADR-0045-break-glass-protocol.md) — break-glass sessions resolve against a named policy bundle for DEGRADED defensibility
- [ADR-0046](ADR-0046-jit-tool-grant.md) — JIT grant gates appear in `PolicyBundle.gates_required`; the worked example shows `write_pr_requires_jit`
- [ADR-0047](ADR-0047-agent-charter.md) — `AgentCharter.policy_release_version` pins the charter to a specific release; the charter is the session-binding vehicle
- [ADR-0050](ADR-0050-operation-context.md) — `OperationContext.policy_version_active` records the resolved bundle; evidence nodes carry this field
- [ADR-0051](ADR-0051-tool-registry-allowlists.md) — `PolicyBundle.registry_scope` selects which tool registry scope governs tool access
- [ADR-0033](ADR-0033-unified-entity-state-model.md) — EvidenceRef 8 kinds; `PolicyBundle.allowed_evidence_kinds` is a subset of these
- `lionagi/agent/config.py` — `AgentConfig` and `PermissionPolicy`; library-mode policy remains here
- prior governance research `01_design/011-policy-resolution/ADR-011-policy-resolution.md` — lex specialis, deny-by-default, resource > role > tenant hierarchy
- prior governance research `01_design/017-policy-release/ADR-017-policy-release.md` — staged rollout (CANARY → TENANT_GROUP → GLOBAL), Two-Key Model, immutable releases
