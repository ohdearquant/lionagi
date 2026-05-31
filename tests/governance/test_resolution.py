# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ADR-0052 most-specific-wins policy resolution engine.

Coverage goals:
    - All four scope levels (resource=3, role=2, tenant=1, global=0)
    - Specificity ordering: resource beats role, role beats global
    - Tie at same specificity: deny wins (fail-closed)
    - Tie with unanimous verdict: unanimous verdict wins
    - No matching rule: falls to PermissionsDef.default
    - Default "allow" mode
    - Multiple roles: agent matches if any role matches
    - ScopeLevel enum values and from_scope_name helper
    - PermissionVerdict constants
    - ResolutionResult fields
    - explain() returns all candidates sorted by specificity
    - Empty PermissionsDef: denies everything
    - Tool filter on global/role rules: empty=all, non-empty=exact match
    - Resource rule with empty tools: matches nothing (conservative)
    - Tenant scope as label only: matching by tenant_id value
    - deny rule beats allow rule at same scope (via tie-break → deny)
"""

import pytest

from lionagi.governance.dsl import (
    PermissionResolution,
    PermissionRule,
    PermissionsDef,
)
from lionagi.governance.resolution import (
    PermissionVerdict,
    PolicyResolver,
    ResolutionResult,
    ScopeLevel,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rule(
    rule_id: str,
    scope: str,
    *,
    roles: list[str] | None = None,
    tools: list[str] | None = None,
    resources: list[str] | None = None,
    action: str = "",
    because: str = "",
) -> PermissionRule:
    return PermissionRule(
        rule_id=rule_id,
        scope=scope,
        roles=roles or [],
        tools=tools or [],
        resources=resources or [],
        action=action,
        because=because,
    )


def _permissions(
    *,
    allow: list[PermissionRule] | None = None,
    deny: list[PermissionRule] | None = None,
    default: str = "deny",
    tie: str = "deny",
) -> PermissionsDef:
    return PermissionsDef(
        default=default,
        resolution=PermissionResolution(
            specificity_order=["resource", "role", "tenant", "global"],
            tie=tie,
        ),
        allow=allow or [],
        deny=deny or [],
    )


# ---------------------------------------------------------------------------
# ScopeLevel
# ---------------------------------------------------------------------------


class TestScopeLevel:
    def test_values(self) -> None:
        assert ScopeLevel.GLOBAL == 0
        assert ScopeLevel.TENANT == 1
        assert ScopeLevel.ROLE == 2
        assert ScopeLevel.RESOURCE == 3

    def test_ordering(self) -> None:
        assert ScopeLevel.GLOBAL < ScopeLevel.TENANT < ScopeLevel.ROLE < ScopeLevel.RESOURCE

    def test_from_scope_name_all(self) -> None:
        assert ScopeLevel.from_scope_name("global") == ScopeLevel.GLOBAL
        assert ScopeLevel.from_scope_name("tenant") == ScopeLevel.TENANT
        assert ScopeLevel.from_scope_name("role") == ScopeLevel.ROLE
        assert ScopeLevel.from_scope_name("resource") == ScopeLevel.RESOURCE

    def test_from_scope_name_unknown_raises(self) -> None:
        with pytest.raises(KeyError, match="Unknown scope"):
            ScopeLevel.from_scope_name("unknown")


# ---------------------------------------------------------------------------
# PermissionVerdict
# ---------------------------------------------------------------------------


class TestPermissionVerdict:
    def test_allow_constant(self) -> None:
        assert PermissionVerdict.ALLOW == "allow"

    def test_deny_constant(self) -> None:
        assert PermissionVerdict.DENY == "deny"

    def test_string_equality(self) -> None:
        assert PermissionVerdict.ALLOW == "allow"
        assert PermissionVerdict.DENY == "deny"

    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError, match="must be 'allow' or 'deny'"):
            PermissionVerdict("escalate")


# ---------------------------------------------------------------------------
# ResolutionResult
# ---------------------------------------------------------------------------


class TestResolutionResult:
    def test_fields_present(self) -> None:
        r = ResolutionResult(
            verdict="allow",
            matching_rule_id="rule-1",
            scope_level=3,
            justification="test",
        )
        assert r.verdict == "allow"
        assert r.matching_rule_id == "rule-1"
        assert r.scope_level == 3
        assert r.justification == "test"

    def test_default_fields(self) -> None:
        r = ResolutionResult(verdict="deny")
        assert r.matching_rule_id is None
        assert r.scope_level == -1
        assert r.justification == ""


# ---------------------------------------------------------------------------
# Basic allow / deny
# ---------------------------------------------------------------------------


class TestBasicAllow:
    def test_global_allow_rule_matches(self) -> None:
        perms = _permissions(
            allow=[_rule("global-allow", "global", because="allow all")],
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("any_tool", "any_role")
        assert result.verdict == "allow"
        assert result.matching_rule_id == "global-allow"
        assert result.scope_level == ScopeLevel.GLOBAL

    def test_global_deny_rule_matches(self) -> None:
        perms = _permissions(
            deny=[_rule("global-deny", "global")],
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("any_tool", "any_role")
        assert result.verdict == "deny"
        assert result.matching_rule_id == "global-deny"

    def test_role_allow_rule_matches_by_role(self) -> None:
        perms = _permissions(
            allow=[_rule("role-writer", "role", roles=["writer"])],
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("editor", "writer")
        assert result.verdict == "allow"
        assert result.matching_rule_id == "role-writer"

    def test_role_allow_rule_does_not_match_different_role(self) -> None:
        perms = _permissions(
            allow=[_rule("role-writer", "role", roles=["writer"])],
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("editor", "reviewer")
        # No match → default deny
        assert result.verdict == "deny"
        assert result.matching_rule_id is None

    def test_resource_allow_rule_matches_by_tool(self) -> None:
        perms = _permissions(
            allow=[_rule("res-reader", "resource", tools=["reader"])],
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("reader", "any_role")
        assert result.verdict == "allow"
        assert result.scope_level == ScopeLevel.RESOURCE

    def test_resource_deny_rule_matches_by_tool(self) -> None:
        perms = _permissions(
            deny=[_rule("res-deny-bash", "resource", tools=["bash"])],
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("bash", "any_role")
        assert result.verdict == "deny"
        assert result.scope_level == ScopeLevel.RESOURCE


# ---------------------------------------------------------------------------
# Specificity ordering: most-specific-wins
# ---------------------------------------------------------------------------


class TestSpecificityOrdering:
    def test_resource_beats_role(self) -> None:
        """A resource-scope DENY overrides a role-scope ALLOW."""
        perms = _permissions(
            allow=[_rule("role-allow", "role", roles=["writer"])],
            deny=[_rule("res-deny", "resource", tools=["editor"])],
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("editor", "writer")
        assert result.verdict == "deny"
        assert result.scope_level == ScopeLevel.RESOURCE

    def test_resource_beats_global(self) -> None:
        """A resource-scope ALLOW overrides a global DENY."""
        perms = _permissions(
            allow=[_rule("res-allow", "resource", tools=["reader"])],
            deny=[_rule("global-deny", "global")],
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("reader", "any_role")
        assert result.verdict == "allow"
        assert result.scope_level == ScopeLevel.RESOURCE

    def test_role_beats_global(self) -> None:
        """A role-scope ALLOW overrides a global DENY for the matching role."""
        perms = _permissions(
            allow=[_rule("role-allow-admin", "role", roles=["admin"])],
            deny=[_rule("global-deny-all", "global")],
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("any_tool", "admin")
        assert result.verdict == "allow"
        assert result.scope_level == ScopeLevel.ROLE

    def test_role_beats_global_only_for_matching_role(self) -> None:
        """The role-scope rule does not apply to non-admin roles."""
        perms = _permissions(
            allow=[_rule("role-allow-admin", "role", roles=["admin"])],
            deny=[_rule("global-deny-all", "global")],
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("any_tool", "analyst")
        # analyst doesn't match the role rule, so global deny wins
        assert result.verdict == "deny"
        assert result.scope_level == ScopeLevel.GLOBAL

    def test_specificity_chain_resource_over_role_over_global(self) -> None:
        """All three levels present — resource wins for its matching tool."""
        perms = _permissions(
            allow=[
                _rule("global-allow", "global"),
                _rule("role-allow-writer", "role", roles=["writer"]),
            ],
            deny=[_rule("res-deny-writer-tool", "resource", tools=["writer_tool"])],
        )
        resolver = PolicyResolver(perms)
        # writer calling writer_tool → resource deny wins
        result = resolver.resolve("writer_tool", "writer")
        assert result.verdict == "deny"
        assert result.scope_level == ScopeLevel.RESOURCE

        # writer calling other tool → role allow wins (score 2 > global score 0)
        result2 = resolver.resolve("reader", "writer")
        assert result2.verdict == "allow"
        assert result2.scope_level == ScopeLevel.ROLE


# ---------------------------------------------------------------------------
# Tie-breaking
# ---------------------------------------------------------------------------


class TestTieBreaking:
    def test_same_scope_conflicting_verdicts_deny_wins(self) -> None:
        """Two global rules, one allow and one deny → tie → deny-on-tie."""
        perms = _permissions(
            allow=[_rule("global-allow-reader", "global", tools=["reader"])],
            deny=[_rule("global-deny-reader", "global", tools=["reader"])],
            tie="deny",
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("reader", "any_role")
        assert result.verdict == "deny"
        assert result.matching_rule_id is None  # tie → no single winner

    def test_same_scope_unanimous_allow_wins(self) -> None:
        """Two global allow rules for the same tool → unanimous → allow."""
        perms = _permissions(
            allow=[
                _rule("global-allow-1", "global", tools=["reader"]),
                _rule("global-allow-2", "global", tools=["reader"]),
            ],
            tie="deny",
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("reader", "any_role")
        assert result.verdict == "allow"
        # First match used as matching_rule_id when unanimous
        assert result.matching_rule_id is not None

    def test_same_scope_unanimous_deny_wins(self) -> None:
        """Two global deny rules → unanimous → deny."""
        perms = _permissions(
            deny=[
                _rule("global-deny-1", "global", tools=["bash"]),
                _rule("global-deny-2", "global", tools=["bash"]),
            ],
            tie="deny",
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("bash", "any_role")
        assert result.verdict == "deny"

    def test_tie_policy_allow_overrides_fail_closed(self) -> None:
        """tie='allow' overrides fail-closed when there's a conflict at same level."""
        perms = _permissions(
            allow=[_rule("global-allow", "global", tools=["tool"])],
            deny=[_rule("global-deny", "global", tools=["tool"])],
            tie="allow",
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("tool", "any_role")
        assert result.verdict == "allow"

    def test_two_resource_rules_conflicting_deny_wins(self) -> None:
        """Two resource-scope rules at same score, conflicting → deny."""
        perms = _permissions(
            allow=[_rule("res-allow", "resource", tools=["tool"])],
            deny=[_rule("res-deny", "resource", tools=["tool"])],
            tie="deny",
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("tool", "any_role")
        assert result.verdict == "deny"
        assert result.scope_level == ScopeLevel.RESOURCE


# ---------------------------------------------------------------------------
# No matching rule → default
# ---------------------------------------------------------------------------


class TestDefaultFallback:
    def test_no_rule_default_deny(self) -> None:
        perms = _permissions(default="deny")
        resolver = PolicyResolver(perms)
        result = resolver.resolve("any_tool", "any_role")
        assert result.verdict == "deny"
        assert result.matching_rule_id is None
        assert result.scope_level == -1

    def test_no_rule_default_allow(self) -> None:
        perms = _permissions(default="allow")
        resolver = PolicyResolver(perms)
        result = resolver.resolve("any_tool", "any_role")
        assert result.verdict == "allow"
        assert result.matching_rule_id is None
        assert result.scope_level == -1

    def test_no_match_for_role_falls_to_default(self) -> None:
        perms = _permissions(
            allow=[_rule("role-allow-admin", "role", roles=["admin"])],
            default="deny",
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("tool", "analyst")
        assert result.verdict == "deny"
        assert result.matching_rule_id is None

    def test_resource_rule_wrong_tool_falls_to_default(self) -> None:
        perms = _permissions(
            allow=[_rule("res-allow-reader", "resource", tools=["reader"])],
            default="deny",
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("editor", "any_role")
        assert result.verdict == "deny"
        assert result.matching_rule_id is None


# ---------------------------------------------------------------------------
# Multiple roles
# ---------------------------------------------------------------------------


class TestMultipleRoles:
    def test_agent_matches_if_any_role_in_rule(self) -> None:
        perms = _permissions(
            allow=[_rule("multi-role", "role", roles=["admin", "reviewer", "writer"])],
        )
        resolver = PolicyResolver(perms)
        for role in ("admin", "reviewer", "writer"):
            result = resolver.resolve("tool", role)
            assert result.verdict == "allow", f"Expected allow for role={role}"

    def test_agent_denied_if_no_role_matches(self) -> None:
        perms = _permissions(
            allow=[_rule("multi-role", "role", roles=["admin", "reviewer"])],
            default="deny",
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("tool", "analyst")
        assert result.verdict == "deny"

    def test_empty_roles_on_role_scope_matches_any_role(self) -> None:
        """A role-scope rule with empty roles matches every role."""
        perms = _permissions(
            allow=[_rule("role-any", "role", roles=[])],
        )
        resolver = PolicyResolver(perms)
        for role in ("admin", "analyst", "guest", "some_random_role"):
            result = resolver.resolve("tool", role)
            assert result.verdict == "allow", f"Expected allow for role={role}"


# ---------------------------------------------------------------------------
# Tool filter on non-resource rules
# ---------------------------------------------------------------------------


class TestToolFilter:
    def test_global_rule_with_tool_filter_matches_specific_tool(self) -> None:
        perms = _permissions(
            allow=[_rule("global-allow-reader", "global", tools=["reader"])],
            default="deny",
        )
        resolver = PolicyResolver(perms)
        assert resolver.resolve("reader", "any_role").verdict == "allow"
        assert resolver.resolve("editor", "any_role").verdict == "deny"

    def test_global_rule_no_tool_filter_matches_all(self) -> None:
        perms = _permissions(
            allow=[_rule("global-allow-all", "global")],
        )
        resolver = PolicyResolver(perms)
        for tool in ("reader", "editor", "bash", "some_tool"):
            result = resolver.resolve(tool, "any_role")
            assert result.verdict == "allow"

    def test_role_rule_with_tool_filter(self) -> None:
        perms = _permissions(
            allow=[_rule("role-writer-editor", "role", roles=["writer"], tools=["editor"])],
            default="deny",
        )
        resolver = PolicyResolver(perms)
        assert resolver.resolve("editor", "writer").verdict == "allow"
        assert resolver.resolve("reader", "writer").verdict == "deny"


# ---------------------------------------------------------------------------
# Resource rule edge cases
# ---------------------------------------------------------------------------


class TestResourceRuleEdgeCases:
    def test_resource_rule_with_resources_field(self) -> None:
        """``resources`` field is checked in addition to ``tools``."""
        perms = _permissions(
            allow=[
                PermissionRule(
                    rule_id="res-via-resources",
                    scope="resource",
                    roles=[],
                    action="use",
                    because="test",
                    resources=["special_tool"],
                )
            ],
        )
        resolver = PolicyResolver(perms)
        assert resolver.resolve("special_tool", "any_role").verdict == "allow"

    def test_resource_rule_empty_tools_matches_nothing(self) -> None:
        """Resource rule with no tools/resources is conservative — matches nothing."""
        perms = _permissions(
            allow=[_rule("res-empty", "resource")],
            default="deny",
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("any_tool", "any_role")
        assert result.verdict == "deny"

    def test_resource_rule_union_of_tools_and_resources(self) -> None:
        perms = _permissions(
            allow=[
                PermissionRule(
                    rule_id="res-union",
                    scope="resource",
                    roles=[],
                    action="use",
                    because="test",
                    tools=["tool_a"],
                    resources=["tool_b"],
                )
            ],
        )
        resolver = PolicyResolver(perms)
        assert resolver.resolve("tool_a", "any_role").verdict == "allow"
        assert resolver.resolve("tool_b", "any_role").verdict == "allow"
        assert resolver.resolve("tool_c", "any_role").verdict == "deny"


# ---------------------------------------------------------------------------
# Tenant scope
# ---------------------------------------------------------------------------


class TestTenantScope:
    def test_tenant_rule_matches_by_tenant_id(self) -> None:
        """Tenant-scope rule matches when tenant_id is in rule.roles."""
        perms = _permissions(
            allow=[_rule("tenant-acme-allow", "tenant", roles=["acme"])],
            default="deny",
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("tool", "any_role", tenant_id="acme")
        assert result.verdict == "allow"
        assert result.scope_level == ScopeLevel.TENANT

    def test_tenant_rule_does_not_match_different_tenant(self) -> None:
        perms = _permissions(
            allow=[_rule("tenant-acme-allow", "tenant", roles=["acme"])],
            default="deny",
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("tool", "any_role", tenant_id="other")
        assert result.verdict == "deny"

    def test_tenant_rule_empty_roles_matches_all_tenants(self) -> None:
        """Empty roles on tenant rule means match any tenant."""
        perms = _permissions(
            allow=[_rule("tenant-any", "tenant", roles=[])],
        )
        resolver = PolicyResolver(perms)
        for tid in ("acme", "beta", ""):
            result = resolver.resolve("tool", "any_role", tenant_id=tid)
            assert result.verdict == "allow"

    def test_role_beats_tenant(self) -> None:
        """Role-scope DENY (score=2) beats tenant-scope ALLOW (score=1)."""
        perms = _permissions(
            allow=[_rule("tenant-allow", "tenant", roles=["acme"])],
            deny=[_rule("role-deny-analyst", "role", roles=["analyst"])],
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("tool", "analyst", tenant_id="acme")
        assert result.verdict == "deny"
        assert result.scope_level == ScopeLevel.ROLE


# ---------------------------------------------------------------------------
# Empty permissions
# ---------------------------------------------------------------------------


class TestEmptyPermissions:
    def test_empty_permissions_deny_everything(self) -> None:
        perms = _permissions(default="deny")
        resolver = PolicyResolver(perms)
        for tool in ("reader", "editor", "bash", "any_tool"):
            result = resolver.resolve(tool, "any_role")
            assert result.verdict == "deny", f"Expected deny for tool={tool}"

    def test_empty_permissions_no_matching_rule_id(self) -> None:
        perms = _permissions(default="deny")
        resolver = PolicyResolver(perms)
        result = resolver.resolve("tool", "role")
        assert result.matching_rule_id is None
        assert result.scope_level == -1


# ---------------------------------------------------------------------------
# explain()
# ---------------------------------------------------------------------------


class TestExplain:
    def test_explain_returns_all_candidates_sorted(self) -> None:
        perms = _permissions(
            allow=[
                _rule("global-allow", "global"),
                _rule("role-allow-writer", "role", roles=["writer"]),
                _rule("res-allow-editor", "resource", tools=["editor"]),
            ],
        )
        resolver = PolicyResolver(perms)
        results = resolver.explain("editor", "writer")
        assert len(results) == 3
        # Sorted descending by scope_level
        assert results[0].scope_level == ScopeLevel.RESOURCE
        assert results[1].scope_level == ScopeLevel.ROLE
        assert results[2].scope_level == ScopeLevel.GLOBAL

    def test_explain_returns_empty_when_no_candidates(self) -> None:
        perms = _permissions(default="deny")
        resolver = PolicyResolver(perms)
        results = resolver.explain("any_tool", "any_role")
        assert results == []

    def test_explain_includes_deny_rules(self) -> None:
        perms = _permissions(
            allow=[_rule("global-allow", "global")],
            deny=[_rule("res-deny-bash", "resource", tools=["bash"])],
        )
        resolver = PolicyResolver(perms)
        results = resolver.explain("bash", "any_role")
        verdicts = {r.verdict for r in results}
        assert "allow" in verdicts
        assert "deny" in verdicts

    def test_explain_each_result_has_matching_rule_id(self) -> None:
        perms = _permissions(
            allow=[
                _rule("role-allow", "role", roles=["admin"]),
            ],
        )
        resolver = PolicyResolver(perms)
        results = resolver.explain("tool", "admin")
        assert len(results) == 1
        assert results[0].matching_rule_id == "role-allow"

    def test_explain_vs_resolve_consistency(self) -> None:
        """The first result from explain() matches what resolve() returns."""
        perms = _permissions(
            allow=[
                _rule("global-allow", "global"),
                _rule("role-allow-admin", "role", roles=["admin"]),
                _rule("res-allow-reader", "resource", tools=["reader"]),
            ],
        )
        resolver = PolicyResolver(perms)
        resolve_result = resolver.resolve("reader", "admin")
        explain_results = resolver.explain("reader", "admin")

        # Top explain result should have same scope_level as resolve result
        assert explain_results[0].scope_level == resolve_result.scope_level
        assert explain_results[0].verdict == resolve_result.verdict


# ---------------------------------------------------------------------------
# Justification field
# ---------------------------------------------------------------------------


class TestJustification:
    def test_justification_contains_tool_and_role(self) -> None:
        perms = _permissions(
            allow=[_rule("global-allow", "global", because="all allowed")],
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("reader", "analyst")
        assert "reader" in result.justification
        assert "analyst" in result.justification

    def test_default_justification_mentions_default(self) -> None:
        perms = _permissions(default="deny")
        resolver = PolicyResolver(perms)
        result = resolver.resolve("tool", "role")
        assert "default" in result.justification.lower()

    def test_tie_justification_mentions_tie_break(self) -> None:
        perms = _permissions(
            allow=[_rule("global-allow", "global", tools=["tool"])],
            deny=[_rule("global-deny", "global", tools=["tool"])],
        )
        resolver = PolicyResolver(perms)
        result = resolver.resolve("tool", "any_role")
        assert "tie" in result.justification.lower()


# ---------------------------------------------------------------------------
# DSL validation integration
# ---------------------------------------------------------------------------


class TestDSLValidation:
    def test_wildcard_in_tool_rejected_by_dsl(self) -> None:
        with pytest.raises(ValueError, match="Wildcards are invalid"):
            PermissionRule(rule_id="wild", scope="resource", tools=["tool*"])

    def test_invalid_default_rejected_by_dsl(self) -> None:
        with pytest.raises(ValueError):
            PermissionsDef(default="escalate")

    def test_permissions_def_default_deny(self) -> None:
        perms = _permissions(default="deny")
        assert perms.default == "deny"
        assert perms.resolution.tie == "deny"


# ---------------------------------------------------------------------------
# ADR-0052 worked example
# ---------------------------------------------------------------------------


class TestADR0052WorkedExample:
    """Reproduces the worked example from ADR-0052 §10.

    Agent: tenant=acme, role=reviewer, tool=write_pr.

    Four rules in force:
        global-read-only  → global (0) → deny
        acme-tenant-allow → tenant (1) → allow
        role-reviewer     → role   (2) → deny
        tool-write-pr     → resource(3)→ allow

    Most specific wins: resource (3) → allow.
    """

    def setup_method(self) -> None:
        self.perms = _permissions(
            allow=[
                _rule("acme-tenant-allow", "tenant", roles=["acme"]),
                _rule("tool-write-pr", "resource", tools=["write_pr"]),
            ],
            deny=[
                _rule("global-read-only", "global"),
                _rule("role-reviewer-no-merge", "role", roles=["reviewer"]),
            ],
        )
        self.resolver = PolicyResolver(self.perms)

    def test_write_pr_resolved_by_resource_scope(self) -> None:
        result = self.resolver.resolve("write_pr", "reviewer", tenant_id="acme")
        assert result.verdict == "allow"
        assert result.scope_level == ScopeLevel.RESOURCE
        assert result.matching_rule_id == "tool-write-pr"

    def test_other_tool_resolved_by_role_scope(self) -> None:
        """For a tool not covered by a resource rule, role (score=2) wins."""
        result = self.resolver.resolve("other_tool", "reviewer", tenant_id="acme")
        assert result.verdict == "deny"
        assert result.scope_level == ScopeLevel.ROLE

    def test_non_reviewer_on_other_tool_resolved_by_tenant(self) -> None:
        """non-reviewer in acme → tenant allow (score=1) beats global deny (score=0)."""
        result = self.resolver.resolve("other_tool", "analyst", tenant_id="acme")
        assert result.verdict == "allow"
        assert result.scope_level == ScopeLevel.TENANT

    def test_non_acme_agent_on_other_tool_falls_to_global(self) -> None:
        """Agent outside acme → only global deny applies."""
        result = self.resolver.resolve("other_tool", "analyst", tenant_id="other_co")
        assert result.verdict == "deny"
        assert result.scope_level == ScopeLevel.GLOBAL

    def test_explain_returns_four_candidates_for_reviewer_write_pr(self) -> None:
        results = self.resolver.explain("write_pr", "reviewer", tenant_id="acme")
        assert len(results) == 4
        scores = [r.scope_level for r in results]
        assert scores == sorted(scores, reverse=True)
