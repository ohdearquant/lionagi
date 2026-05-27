# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the SoDEnforcer separation-of-duties engine.

Covers all scenarios mandated by the governance SoD spec:
- Same actor author + approve → SoDViolation
- Different actors author + approve → no violation
- Scope isolation: SESSION and TASK scopes isolate between distinct scope_ids
- GLOBAL scope collapses all scope_ids into one
- No rules loaded → no violations
- SoD inactive → no violations
- clear_scope removes recorded actions
- Multiple conflicting roles in a rule (role_a / role_b pairing)
- record_action + check is idempotent (same action twice by same actor is fine)
- check() does not mutate state (non-recording)
- Cross-rule independence (two rules, independent violation detection)
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import pytest

from lionagi.protocols.governance.sod import (
    ActorRecord,
    SoDEnforcer,
    SoDViolation,
)
from lionagi.protocols.governance.targets import SoDRule

# ──────────────────────────────── Helpers ────────────────────────────────────


def _rule(
    role_a: str,
    role_b: str,
    *,
    scope: str = "session",
    conflict_type: str = "approval_chain",
) -> SoDRule:
    return SoDRule(
        conflict_type=conflict_type,
        role_a=role_a,
        role_b=role_b,
        scope=scope,
    )


def _enforcer(
    *rules: SoDRule,
    active: bool = True,
) -> SoDEnforcer:
    return SoDEnforcer(list(rules), sod_active=active)


# ──────────────────────────── SoDViolation model ─────────────────────────────


class TestSoDViolation:
    def test_fields_accessible(self):
        v = SoDViolation(
            rule_id="rule-1",
            conflicting_role="approver",
            prior_role="author",
            actor_id="agent-x",
            prior_actor_id="agent-x",
            scope_id="sess-1",
            justification="test",
        )
        assert v.rule_id == "rule-1"
        assert v.conflicting_role == "approver"
        assert v.prior_role == "author"
        assert v.actor_id == "agent-x"
        assert v.scope_id == "sess-1"

    def test_str_contains_key_info(self):
        v = SoDViolation(
            rule_id="rule-1",
            conflicting_role="approver",
            prior_role="author",
            actor_id="agent-x",
            prior_actor_id="agent-x",
            scope_id="sess-1",
            justification="test",
        )
        s = str(v)
        assert "rule-1" in s
        assert "agent-x" in s
        assert "approver" in s
        assert "author" in s


# ──────────────────────── Core violation detection ───────────────────────────


class TestSamActorViolation:
    """Same actor in both conflicting roles triggers a violation."""

    def test_author_then_approve_same_actor(self):
        enforcer = _enforcer(_rule("author", "approver"))
        enforcer.record_action("alice", "author", "write", scope_id="s1")

        violation = enforcer.check("alice", "approver", "approve", scope_id="s1")
        assert violation is not None
        assert isinstance(violation, SoDViolation)
        assert violation.actor_id == "alice"
        assert violation.conflicting_role == "approver"
        assert violation.prior_role == "author"

    def test_approve_then_author_same_actor(self):
        """Violation symmetric: B→A same as A→B."""
        enforcer = _enforcer(_rule("author", "approver"))
        enforcer.record_action("bob", "approver", "approve", scope_id="s1")

        violation = enforcer.check("bob", "author", "write", scope_id="s1")
        assert violation is not None
        assert violation.actor_id == "bob"
        assert violation.conflicting_role == "author"
        assert violation.prior_role == "approver"

    def test_violation_reports_scope_id(self):
        enforcer = _enforcer(_rule("author", "approver"))
        enforcer.record_action("carol", "author", "write", scope_id="flow-99")

        violation = enforcer.check("carol", "approver", "approve", scope_id="flow-99")
        assert violation is not None
        assert violation.scope_id == "flow-99"


class TestDifferentActorsNoViolation:
    """Different actors in conflicting roles: no violation."""

    def test_author_approver_different_actors(self):
        enforcer = _enforcer(_rule("author", "approver"))
        enforcer.record_action("alice", "author", "write", scope_id="s1")

        violation = enforcer.check("bob", "approver", "approve", scope_id="s1")
        assert violation is None

    def test_record_both_different_actors_then_check(self):
        enforcer = _enforcer(_rule("author", "approver"))
        enforcer.record_action("alice", "author", "write", scope_id="s1")
        enforcer.record_action("bob", "approver", "approve", scope_id="s1")

        # Alice tries another role — allowed because she never acted as approver
        violation = enforcer.check("alice", "author", "edit", scope_id="s1")
        assert violation is None

        # Bob tries same rule B side — allowed because he is approver not author
        violation = enforcer.check("bob", "approver", "re-approve", scope_id="s1")
        assert violation is None


# ──────────────────────────── Scope isolation ────────────────────────────────


class TestSessionScopeIsolation:
    """SESSION scope records are isolated per scope_id."""

    def test_different_sessions_independent(self):
        enforcer = _enforcer(_rule("author", "approver", scope="session"))
        enforcer.record_action("alice", "author", "write", scope_id="sess-A")

        # Same actor, same rule, but different scope_id → no violation
        violation = enforcer.check("alice", "approver", "approve", scope_id="sess-B")
        assert violation is None

    def test_same_session_triggers_violation(self):
        enforcer = _enforcer(_rule("author", "approver", scope="session"))
        enforcer.record_action("alice", "author", "write", scope_id="sess-A")

        violation = enforcer.check("alice", "approver", "approve", scope_id="sess-A")
        assert violation is not None


class TestTaskScopeIsolation:
    """TASK scope (maps to 'task' in the DSL) isolates per scope_id."""

    def test_different_tasks_independent(self):
        enforcer = _enforcer(_rule("author", "approver", scope="task"))
        enforcer.record_action("dave", "author", "write", scope_id="task-1")

        violation = enforcer.check("dave", "approver", "approve", scope_id="task-2")
        assert violation is None

    def test_same_task_triggers_violation(self):
        enforcer = _enforcer(_rule("author", "approver", scope="task"))
        enforcer.record_action("dave", "author", "write", scope_id="task-1")

        violation = enforcer.check("dave", "approver", "approve", scope_id="task-1")
        assert violation is not None


class TestGlobalScope:
    """GLOBAL scope collapses all scope_ids into one shared pool."""

    def test_global_conflict_crosses_scope_ids(self):
        enforcer = _enforcer(_rule("author", "approver", scope="global"))
        enforcer.record_action("eve", "author", "write", scope_id="sess-X")

        # Different scope_id but GLOBAL rule → still a conflict
        violation = enforcer.check("eve", "approver", "approve", scope_id="sess-Y")
        assert violation is not None

    def test_global_no_violation_different_actors(self):
        enforcer = _enforcer(_rule("author", "approver", scope="global"))
        enforcer.record_action("frank", "author", "write", scope_id="sess-X")

        violation = enforcer.check("grace", "approver", "approve", scope_id="sess-Y")
        assert violation is None


# ───────────────────────────── Empty/inactive ────────────────────────────────


class TestNoRulesNoViolations:
    """When no rules are loaded, check() always returns None."""

    def test_empty_rules(self):
        enforcer = SoDEnforcer([], sod_active=True)
        enforcer.record_action("alice", "author", "write", scope_id="s1")
        assert enforcer.check("alice", "approver", "approve", scope_id="s1") is None

    def test_empty_rules_any_action(self):
        enforcer = SoDEnforcer([])
        assert enforcer.check("x", "admin", "delete", scope_id="global") is None


class TestSoDInactive:
    """When sod_active=False, no violations are returned regardless of rules."""

    def test_inactive_bypasses_same_actor(self):
        rule = _rule("author", "approver")
        enforcer = _enforcer(rule, active=False)
        enforcer.record_action("alice", "author", "write", scope_id="s1")

        assert enforcer.check("alice", "approver", "approve", scope_id="s1") is None

    def test_inactive_flag_readable(self):
        enforcer = SoDEnforcer([], sod_active=False)
        assert enforcer.active is False

    def test_active_flag_readable(self):
        enforcer = SoDEnforcer([], sod_active=True)
        assert enforcer.active is True

    def test_rules_are_still_loaded_when_inactive(self):
        rule = _rule("author", "approver")
        enforcer = SoDEnforcer([rule], sod_active=False)
        assert len(enforcer.rules) == 1


# ─────────────────────────── clear_scope ─────────────────────────────────────


class TestClearScope:
    """clear_scope removes all recorded actions for the given scope_id."""

    def test_clear_removes_violation(self):
        enforcer = _enforcer(_rule("author", "approver"))
        enforcer.record_action("alice", "author", "write", scope_id="s1")
        enforcer.clear_scope("s1")

        # After clearing, no prior record → no violation
        violation = enforcer.check("alice", "approver", "approve", scope_id="s1")
        assert violation is None

    def test_clear_only_removes_target_scope(self):
        enforcer = _enforcer(_rule("author", "approver"))
        enforcer.record_action("alice", "author", "write", scope_id="s1")
        enforcer.record_action("alice", "author", "write", scope_id="s2")
        enforcer.clear_scope("s1")

        # s2 still has a record
        assert enforcer.check("alice", "approver", "approve", scope_id="s2") is not None
        # s1 was cleared
        assert enforcer.check("alice", "approver", "approve", scope_id="s1") is None

    def test_clear_nonexistent_scope_is_noop(self):
        enforcer = _enforcer(_rule("author", "approver"))
        # Should not raise
        enforcer.clear_scope("nonexistent")

    def test_clear_global_scope(self):
        enforcer = _enforcer(_rule("author", "approver", scope="global"))
        enforcer.record_action("alice", "author", "write", scope_id="any")
        enforcer.clear_scope("__global__")

        violation = enforcer.check("alice", "approver", "approve", scope_id="any")
        assert violation is None


# ───────────────────────── Idempotency ───────────────────────────────────────


class TestIdempotency:
    """Same action recorded twice by same actor does not create spurious violations."""

    def test_same_action_twice_no_false_positive(self):
        enforcer = _enforcer(_rule("author", "approver"))
        enforcer.record_action("alice", "author", "write", scope_id="s1")
        enforcer.record_action("alice", "author", "write", scope_id="s1")

        # Alice is still only author; should still violate if she tries approver
        violation = enforcer.check("alice", "approver", "approve", scope_id="s1")
        assert violation is not None

    def test_check_does_not_record(self):
        """check() must be a pure read — it must not record the action."""
        enforcer = _enforcer(_rule("author", "approver"))
        # Check without recording
        enforcer.check("alice", "author", "write", scope_id="s1")
        # Now alice has not been recorded; bob should have no conflict
        violation = enforcer.check("alice", "approver", "approve", scope_id="s1")
        assert violation is None

    def test_record_then_check_idempotent(self):
        """Calling check() multiple times returns consistent results."""
        enforcer = _enforcer(_rule("author", "approver"))
        enforcer.record_action("alice", "author", "write", scope_id="s1")

        r1 = enforcer.check("alice", "approver", "approve", scope_id="s1")
        r2 = enforcer.check("alice", "approver", "approve", scope_id="s1")
        assert r1 is not None
        assert r2 is not None
        assert r1.rule_id == r2.rule_id


# ──────────────────── Multiple conflicting roles ──────────────────────────────


class TestMultipleRules:
    """When multiple rules are loaded, each is checked independently."""

    def test_two_rules_first_violated(self):
        rule1 = _rule("author", "approver", conflict_type="approval_chain")
        rule2 = _rule("auditor", "executor", conflict_type="audit_independence")
        enforcer = _enforcer(rule1, rule2)

        enforcer.record_action("alice", "author", "write", scope_id="s1")
        violation = enforcer.check("alice", "approver", "approve", scope_id="s1")
        assert violation is not None

    def test_two_rules_second_violated(self):
        rule1 = _rule("author", "approver", conflict_type="approval_chain")
        rule2 = _rule("auditor", "executor", conflict_type="audit_independence")
        enforcer = _enforcer(rule1, rule2)

        enforcer.record_action("alice", "auditor", "audit", scope_id="s1")
        violation = enforcer.check("alice", "executor", "execute", scope_id="s1")
        assert violation is not None

    def test_two_rules_neither_violated(self):
        rule1 = _rule("author", "approver")
        rule2 = _rule("auditor", "executor")
        enforcer = _enforcer(rule1, rule2)

        enforcer.record_action("alice", "author", "write", scope_id="s1")
        enforcer.record_action("bob", "auditor", "audit", scope_id="s1")

        # Alice tries approver — she is author → violation (rule1)
        v1 = enforcer.check("alice", "approver", "approve", scope_id="s1")
        assert v1 is not None

        # Carol is unrelated — no violations
        v2 = enforcer.check("carol", "executor", "execute", scope_id="s1")
        assert v2 is None

    def test_three_way_coverage(self):
        """A scenario with 3+ actions modeled as two rules covering all pairs."""
        # author↔approver and author↔deployer
        rule1 = _rule("author", "approver")
        rule2 = _rule("author", "deployer")
        enforcer = _enforcer(rule1, rule2)

        enforcer.record_action("alice", "author", "write", scope_id="s1")

        assert enforcer.check("alice", "approver", "approve", scope_id="s1") is not None
        assert enforcer.check("alice", "deployer", "deploy", scope_id="s1") is not None
        assert enforcer.check("bob", "approver", "approve", scope_id="s1") is None
        assert enforcer.check("bob", "deployer", "deploy", scope_id="s1") is None


# ──────────────────────── Cross-scope rule with scopes ───────────────────────


class TestMixedScopeRules:
    """When rules with different scopes are loaded simultaneously."""

    def test_session_rule_does_not_bleed_into_other_session(self):
        enforcer = _enforcer(_rule("author", "approver", scope="session"))
        enforcer.record_action("alice", "author", "write", scope_id="sess-1")

        # sess-2 is isolated
        assert enforcer.check("alice", "approver", "approve", scope_id="sess-2") is None

    def test_global_rule_and_session_rule_independently_checked(self):
        global_rule = _rule("reviewer", "merger", scope="global")
        session_rule = _rule("author", "approver", scope="session")
        enforcer = _enforcer(global_rule, session_rule)

        enforcer.record_action("alice", "reviewer", "review", scope_id="sess-1")
        enforcer.record_action("alice", "author", "write", scope_id="sess-1")

        # Global rule fires across sess-2
        v1 = enforcer.check("alice", "merger", "merge", scope_id="sess-2")
        assert v1 is not None

        # Session rule does NOT fire in sess-2 (different session)
        v2 = enforcer.check("alice", "approver", "approve", scope_id="sess-2")
        assert v2 is None

        # Session rule DOES fire in sess-1
        v3 = enforcer.check("alice", "approver", "approve", scope_id="sess-1")
        assert v3 is not None


# ──────────────────────────── Thread safety ──────────────────────────────────


class TestThreadSafety:
    """Concurrent record_action calls must not corrupt internal state."""

    def test_concurrent_records_do_not_raise(self):
        rule = _rule("author", "approver")
        enforcer = _enforcer(rule)
        errors: list[Exception] = []

        def worker(actor_id: str, scope_id: str) -> None:
            try:
                enforcer.record_action(actor_id, "author", "write", scope_id=scope_id)
                enforcer.check(actor_id, "approver", "approve", scope_id=scope_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(f"agent-{i}", f"scope-{i}")) for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Unexpected exceptions: {errors}"


# ──────────────────────── ActorRecord namedtuple ──────────────────────────────


class TestActorRecord:
    def test_fields(self):
        rec = ActorRecord(actor_id="x", role="author", action="write")
        assert rec.actor_id == "x"
        assert rec.role == "author"
        assert rec.action == "write"

    def test_immutable(self):
        rec = ActorRecord(actor_id="x", role="author", action="write")
        with pytest.raises((AttributeError, TypeError)):
            rec.actor_id = "y"  # type: ignore[misc]
