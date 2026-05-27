# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Policy resolution engine for Charter DSL v0.

Implements the *most-specific-wins* algorithm described in ADR-0052:

    resource (3) > role (2) > tenant (1) > global (0)

On a tie at the same specificity level the resolution always returns DENY
(fail-closed, cross-cutting principle #1).  The same applies when no rule
matches and the charter default is ``"deny"``.

Tenant scope is a **named label only** — lionagi does not implement tenant
isolation, tenant storage, or tenant middleware.  ``tenant`` merely occupies
position 1 in the specificity hierarchy so that commercial plug-ins can
attach rules without touching the open-source core.

Typical usage::

    from lionagi.protocols.governance.dsl import (
        PermissionResolution,
        PermissionRule,
        PermissionsDef,
    )
    from lionagi.protocols.governance.resolution import (
        PermissionVerdict,
        PolicyResolver,
    )

    permissions = PermissionsDef(
        default="deny",
        resolution=PermissionResolution(tie="deny"),
        allow=[
            PermissionRule(
                rule_id="global-read",
                scope="global",
                tools=["reader"],
                because="all agents may read",
            ),
            PermissionRule(
                rule_id="role-writer-edit",
                scope="role",
                roles=["writer"],
                tools=["editor"],
                because="writers may edit",
            ),
        ],
        deny=[],
    )

    resolver = PolicyResolver(permissions)
    result = resolver.resolve(tool_id="reader", role="analyst")
    assert result.verdict == PermissionVerdict.ALLOW

References:
    ADR-0052 — Policy Resolution and Staged Release
    ADR-0044 — Tool Gates (gates selected by active policy)
    ADR-0047 — Agent Charter (charter pins policy_release_version)
"""

from __future__ import annotations

from enum import IntEnum
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from lionagi.protocols.governance.dsl import PermissionRule, PermissionsDef

__all__ = [
    "PermissionVerdict",
    "PolicyResolver",
    "ResolutionResult",
    "ScopeLevel",
]


# ---------------------------------------------------------------------------
# Scope scoring
# ---------------------------------------------------------------------------


class ScopeLevel(IntEnum):
    """Specificity scores for the four permission scope levels.

    Higher value = more specific = takes precedence over lower values.

    ``TENANT`` is a hook point for commercial offerings — lionagi itself
    does not implement tenant isolation.  The integer position (1) only
    matters for the resolution ordering.
    """

    GLOBAL = 0
    TENANT = 1
    ROLE = 2
    RESOURCE = 3

    @classmethod
    def from_scope_name(cls, name: str) -> ScopeLevel:
        """Return the ``ScopeLevel`` for a DSL scope name.

        Args:
            name: One of ``"global"``, ``"tenant"``, ``"role"``,
                  ``"resource"``.

        Raises:
            KeyError: If ``name`` is not a recognised scope.
        """
        mapping: dict[str, ScopeLevel] = {
            "global": cls.GLOBAL,
            "tenant": cls.TENANT,
            "role": cls.ROLE,
            "resource": cls.RESOURCE,
        }
        try:
            return mapping[name]
        except KeyError:
            valid = sorted(mapping)
            raise KeyError(f"Unknown scope {name!r}; valid scopes: {valid}") from None


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


class PermissionVerdict(str):
    """String enum-style constants for policy decisions.

    Using ``str`` subclass rather than ``enum.Enum`` keeps JSON
    serialisation trivial and lets tests use plain string comparisons.
    """

    ALLOW = "allow"
    DENY = "deny"

    def __new__(cls, value: str) -> PermissionVerdict:
        if value not in (cls.ALLOW, cls.DENY):
            raise ValueError(f"PermissionVerdict must be 'allow' or 'deny', got {value!r}")
        return super().__new__(cls, value)


# Pre-built singletons so callers can do ``PermissionVerdict.ALLOW`` or
# compare with the string ``"allow"`` interchangeably.
PermissionVerdict.ALLOW = PermissionVerdict("allow")  # type: ignore[attr-defined]
PermissionVerdict.DENY = PermissionVerdict("deny")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class ResolutionResult(BaseModel):
    """Outcome of a single policy resolution call.

    Attributes:
        verdict:          ``"allow"`` or ``"deny"``.
        matching_rule_id: ``rule_id`` of the winning rule, or ``None`` when
                          no rule matched (default verdict applied).
        scope_level:      Integer specificity of the winning rule, or ``-1``
                          when the default verdict was applied.
        justification:    Human-readable summary of why this verdict was
                          reached, suitable for audit logs.
    """

    model_config = ConfigDict(frozen=True)

    verdict: str = Field(description="'allow' or 'deny'.")
    matching_rule_id: str | None = Field(
        default=None,
        description="rule_id of the winning PermissionRule, if any.",
    )
    scope_level: int = Field(
        default=-1,
        description=(
            "Specificity score of the winning rule (3=resource, 2=role, "
            "1=tenant, 0=global, -1=default applied)."
        ),
    )
    justification: str = Field(
        default="",
        description="Audit-grade explanation of the verdict.",
    )


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


class _Candidate(NamedTuple):
    """Intermediate representation used during resolution."""

    rule: PermissionRule
    verdict: str  # "allow" or "deny"
    score: int  # ScopeLevel integer


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class PolicyResolver:
    """Most-specific-wins policy resolver for ``PermissionsDef``.

    Implements ADR-0052 §5 for the Charter DSL v0 permissions block.
    The resolver is constructed once from a ``PermissionsDef`` and is
    then called for each (tool_id, role) pair that needs a decision.

    Algorithm:
        1. Collect all allow- and deny-rule candidates that match the
           request context ``(tool_id, role, tenant_id)``.
        2. If no candidates → apply ``permissions.default`` verdict.
        3. Sort candidates by specificity score (descending).
        4. Identify the maximum specificity score among candidates.
        5. Collect all candidates at that maximum score.
        6. If exactly one candidate at max score → return its verdict.
        7. If more than one candidate at max score (tie):
               a. If all agree on the same verdict → return that verdict.
               b. If they disagree → DENY (fail-closed, ADR-0052 §8 step 6).
           Note: the ``resolution.tie`` field from the DSL can override
           step 7b; ``"allow"`` overrides fail-closed but should never
           appear in a production charter.

    Matching rules for each scope:
        ``resource`` — rule has ``tools`` (or ``resources``) and
                       ``tool_id`` is in that list.
        ``role``     — rule has ``roles`` and ``role`` is in that list.
        ``tenant``   — label only; ``tenant_id`` must be in rule ``roles``
                       (roles field is reused as the scope value list in
                       DSL v0 for tenant rules) *OR* the rule has an empty
                       ``roles`` list (matches all).
        ``global``   — always matches.

    Empty ``tools`` / ``resources`` in a non-resource-scope rule means
    the rule applies to all tools.  Empty ``roles`` in a role-scope rule
    means the rule applies to all roles.  This makes it straightforward
    to write broad global defaults and narrow resource overrides.

    Args:
        permissions: Parsed ``PermissionsDef`` from a Charter document.
    """

    def __init__(self, permissions: PermissionsDef) -> None:
        self._permissions = permissions
        # Pre-index rules by scope for O(1) bucket lookup.
        self._allow_by_scope: dict[str, list[PermissionRule]] = {
            "global": [],
            "tenant": [],
            "role": [],
            "resource": [],
        }
        self._deny_by_scope: dict[str, list[PermissionRule]] = {
            "global": [],
            "tenant": [],
            "role": [],
            "resource": [],
        }
        for rule in permissions.allow:
            self._allow_by_scope[rule.scope].append(rule)
        for rule in permissions.deny:
            self._deny_by_scope[rule.scope].append(rule)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(
        self,
        tool_id: str,
        role: str,
        tenant_id: str = "",
    ) -> ResolutionResult:
        """Return the most-specific-wins verdict for ``(tool_id, role)``.

        Args:
            tool_id:   Identifier of the tool being invoked.
            role:      Role of the calling agent (e.g. ``"reviewer"``).
            tenant_id: Optional tenant scope label.  lionagi does not
                       use this for any isolation — it is only checked
                       against tenant-scoped rules' matching criteria.

        Returns:
            ``ResolutionResult`` with the final verdict and audit metadata.
        """
        candidates = self._collect_candidates(tool_id, role, tenant_id)

        if not candidates:
            return self._default_result(tool_id, role)

        # Sort descending by specificity score.
        candidates.sort(key=lambda c: c.score, reverse=True)
        top_score = candidates[0].score
        top_candidates = [c for c in candidates if c.score == top_score]

        if len(top_candidates) == 1:
            winner = top_candidates[0]
            return ResolutionResult(
                verdict=winner.verdict,
                matching_rule_id=winner.rule.rule_id,
                scope_level=winner.score,
                justification=(
                    f"Rule '{winner.rule.rule_id}' (scope={winner.rule.scope}, "
                    f"specificity={winner.score}) matched tool='{tool_id}' "
                    f"role='{role}': {winner.rule.because or 'no rationale'}"
                ),
            )

        # Tie — multiple rules at the same specificity.
        verdicts = {c.verdict for c in top_candidates}
        if len(verdicts) == 1:
            # All agree — return the unanimous verdict.
            winner = top_candidates[0]
            ids = ", ".join(c.rule.rule_id for c in top_candidates)
            return ResolutionResult(
                verdict=winner.verdict,
                matching_rule_id=winner.rule.rule_id,
                scope_level=top_score,
                justification=(
                    f"Unanimous tie at specificity={top_score} among "
                    f"[{ids}]: verdict='{winner.verdict}'"
                ),
            )

        # Conflicting tie — apply tie-break policy.
        tie_verdict = self._permissions.resolution.tie
        ids = ", ".join(c.rule.rule_id for c in top_candidates)
        return ResolutionResult(
            verdict=tie_verdict,
            matching_rule_id=None,
            scope_level=top_score,
            justification=(
                f"Conflicting tie at specificity={top_score} among [{ids}]: "
                f"tie-break policy='{tie_verdict}' applied"
            ),
        )

    def explain(
        self,
        tool_id: str,
        role: str,
        tenant_id: str = "",
    ) -> list[ResolutionResult]:
        """Return all matching rule candidates sorted by specificity (desc).

        Unlike ``resolve``, this method does not stop at the most specific
        winner — it returns a ``ResolutionResult`` for every candidate rule
        that matches the request context.  Useful for debugging why a
        verdict was (or was not) produced.

        Args:
            tool_id:   Identifier of the tool being invoked.
            role:      Role of the calling agent.
            tenant_id: Optional tenant scope label.

        Returns:
            List of ``ResolutionResult`` objects sorted by
            ``scope_level`` descending.  Empty list if no rules match.
        """
        candidates = self._collect_candidates(tool_id, role, tenant_id)
        candidates.sort(key=lambda c: c.score, reverse=True)
        results: list[ResolutionResult] = []
        for cand in candidates:
            results.append(
                ResolutionResult(
                    verdict=cand.verdict,
                    matching_rule_id=cand.rule.rule_id,
                    scope_level=cand.score,
                    justification=(
                        f"Rule '{cand.rule.rule_id}' (scope={cand.rule.scope}, "
                        f"specificity={cand.score}) matched tool='{tool_id}' "
                        f"role='{role}': {cand.rule.because or 'no rationale'}"
                    ),
                )
            )
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _collect_candidates(
        self,
        tool_id: str,
        role: str,
        tenant_id: str,
    ) -> list[_Candidate]:
        """Return all rule candidates that match the request context."""
        candidates: list[_Candidate] = []

        # --- resource scope (score=3) ---
        score = ScopeLevel.RESOURCE
        for rule in self._allow_by_scope["resource"]:
            if self._rule_matches_resource(rule, tool_id):
                candidates.append(_Candidate(rule, "allow", int(score)))
        for rule in self._deny_by_scope["resource"]:
            if self._rule_matches_resource(rule, tool_id):
                candidates.append(_Candidate(rule, "deny", int(score)))

        # --- role scope (score=2) ---
        score = ScopeLevel.ROLE
        for rule in self._allow_by_scope["role"]:
            if self._rule_matches_role(rule, role) and self._rule_matches_tool_filter(
                rule, tool_id
            ):
                candidates.append(_Candidate(rule, "allow", int(score)))
        for rule in self._deny_by_scope["role"]:
            if self._rule_matches_role(rule, role) and self._rule_matches_tool_filter(
                rule, tool_id
            ):
                candidates.append(_Candidate(rule, "deny", int(score)))

        # --- tenant scope (score=1) ---
        # Tenant is a scope label only — no isolation, no storage.
        score = ScopeLevel.TENANT
        for rule in self._allow_by_scope["tenant"]:
            if self._rule_matches_tenant(rule, tenant_id) and self._rule_matches_tool_filter(
                rule, tool_id
            ):
                candidates.append(_Candidate(rule, "allow", int(score)))
        for rule in self._deny_by_scope["tenant"]:
            if self._rule_matches_tenant(rule, tenant_id) and self._rule_matches_tool_filter(
                rule, tool_id
            ):
                candidates.append(_Candidate(rule, "deny", int(score)))

        # --- global scope (score=0) ---
        score = ScopeLevel.GLOBAL
        for rule in self._allow_by_scope["global"]:
            # Global rules always match.  Optional tool filter still applies.
            if self._rule_matches_tool_filter(rule, tool_id):
                candidates.append(_Candidate(rule, "allow", int(score)))
        for rule in self._deny_by_scope["global"]:
            if self._rule_matches_tool_filter(rule, tool_id):
                candidates.append(_Candidate(rule, "deny", int(score)))

        return candidates

    @staticmethod
    def _rule_matches_resource(rule: PermissionRule, tool_id: str) -> bool:
        """A resource-scope rule matches when ``tool_id`` is in its tool list.

        Both ``tools`` and ``resources`` fields are checked — the DSL
        allows either name for resource-scope rules.
        """
        targets = set(rule.tools or []) | set(rule.resources or [])
        # Empty targets at resource scope means "match nothing" — a resource
        # rule with no tool specified is invalid and treated conservatively.
        if not targets:
            return False
        return tool_id in targets

    @staticmethod
    def _rule_matches_role(rule: PermissionRule, role: str) -> bool:
        """A role-scope rule matches when ``role`` is in ``rule.roles``.

        Empty ``roles`` on a role-scope rule matches all roles.
        """
        if not rule.roles:
            return True
        return role in rule.roles

    @staticmethod
    def _rule_matches_tenant(rule: PermissionRule, tenant_id: str) -> bool:
        """A tenant-scope rule matches when ``tenant_id`` is in ``rule.roles``.

        In DSL v0 the ``roles`` field doubles as the scope value list for
        tenant rules (there is no dedicated ``tenants`` field).  Empty list
        matches all tenants.  Providing a non-empty ``tenant_id`` of ``""``
        matches only rules with an empty roles list.
        """
        if not rule.roles:
            return True
        return tenant_id in rule.roles

    @staticmethod
    def _rule_matches_tool_filter(rule: PermissionRule, tool_id: str) -> bool:
        """Check whether an optional tool filter on a non-resource rule matches.

        For non-resource-scope rules (global, role, tenant) an empty ``tools``
        list means the rule applies to all tools.  If ``tools`` is non-empty
        the ``tool_id`` must be present.
        """
        if not rule.tools:
            return True
        return tool_id in rule.tools

    def _default_result(self, tool_id: str, role: str) -> ResolutionResult:
        """Return a ``ResolutionResult`` based on ``permissions.default``."""
        verdict = self._permissions.default
        return ResolutionResult(
            verdict=verdict,
            matching_rule_id=None,
            scope_level=-1,
            justification=(
                f"No rule matched tool='{tool_id}' role='{role}': default='{verdict}' applied"
            ),
        )
