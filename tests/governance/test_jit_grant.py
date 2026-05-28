# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.governance.jit_grant (ADR-0046)."""

from __future__ import annotations

import threading
import time

import pytest

from lionagi.governance.gates import GateResult, GateVerdict
from lionagi.governance.jit_grant import (
    JITGrantStore,
    PermitToken,
    check_jit_grant,
    jit_gate_override,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store() -> JITGrantStore:
    return JITGrantStore()


def _issue(store: JITGrantStore, **kwargs) -> PermitToken:
    defaults = dict(
        tool_id="tool.read_secrets",
        grantee_role="analyst",
        grantor="admin",
        reason="audit review",
    )
    defaults.update(kwargs)
    return store.issue(**defaults)


# ---------------------------------------------------------------------------
# PermitToken — basic field assertions
# ---------------------------------------------------------------------------


class TestPermitTokenFields:
    def test_token_id_is_uuid_string(self):
        store = _store()
        token = _issue(store)
        import uuid

        uuid.UUID(token.token_id)  # raises if invalid

    def test_tool_id_stored(self):
        store = _store()
        token = _issue(store, tool_id="tool.deploy")
        assert token.tool_id == "tool.deploy"

    def test_grantee_role_stored(self):
        store = _store()
        token = _issue(store, grantee_role="operator")
        assert token.grantee_role == "operator"

    def test_grantor_stored(self):
        store = _store()
        token = _issue(store, grantor="sysadmin")
        assert token.grantor == "sysadmin"

    def test_reason_stored(self):
        store = _store()
        token = _issue(store, reason="compliance audit")
        assert token.reason == "compliance audit"

    def test_max_uses_default_is_one(self):
        store = _store()
        token = _issue(store)
        assert token.max_uses == 1

    def test_uses_remaining_equals_max_uses_on_issue(self):
        store = _store()
        token = _issue(store, max_uses=3)
        assert token.uses_remaining == 3

    def test_scope_default_is_session(self):
        store = _store()
        token = _issue(store)
        assert token.scope == "session"

    def test_scope_can_be_flow(self):
        store = _store()
        token = _issue(store, scope="flow")
        assert token.scope == "flow"

    def test_scope_can_be_global(self):
        store = _store()
        token = _issue(store, scope="global")
        assert token.scope == "global"

    def test_revoked_false_on_issue(self):
        store = _store()
        token = _issue(store)
        assert token.revoked is False

    def test_created_at_is_recent(self):
        before = time.time()
        store = _store()
        token = _issue(store)
        after = time.time()
        assert before <= token.created_at <= after

    def test_expires_at_is_future(self):
        store = _store()
        token = _issue(store, ttl_seconds=300)
        assert token.expires_at > time.time()

    def test_is_active_fresh_token(self):
        store = _store()
        token = _issue(store)
        assert token.is_active()

    def test_is_active_expired_token(self):
        store = _store()
        token = _issue(store, ttl_seconds=-1)
        assert not token.is_active()


# ---------------------------------------------------------------------------
# Single-use consumption
# ---------------------------------------------------------------------------


class TestSingleUseConsumption:
    def test_first_consume_returns_true(self):
        store = _store()
        token = _issue(store)
        ok = store.consume(token.token_id, token.tool_id, token.grantee_role)
        assert ok is True

    def test_second_consume_of_single_use_returns_false(self):
        store = _store()
        token = _issue(store)
        store.consume(token.token_id, token.tool_id, token.grantee_role)
        second = store.consume(token.token_id, token.tool_id, token.grantee_role)
        assert second is False

    def test_uses_remaining_decremented_after_consume(self):
        store = _store()
        token = _issue(store)
        store.consume(token.token_id, token.tool_id, token.grantee_role)
        updated = store.get(token.token_id)
        assert updated.uses_remaining == 0

    def test_consume_wrong_tool_id_returns_false(self):
        store = _store()
        token = _issue(store)
        ok = store.consume(token.token_id, "wrong.tool", token.grantee_role)
        assert ok is False

    def test_consume_wrong_role_returns_false(self):
        store = _store()
        token = _issue(store)
        ok = store.consume(token.token_id, token.tool_id, "wrong_role")
        assert ok is False

    def test_consume_nonexistent_token_returns_false(self):
        store = _store()
        ok = store.consume("00000000-0000-0000-0000-000000000000", "tool.x", "role_y")
        assert ok is False


# ---------------------------------------------------------------------------
# Multi-use tokens
# ---------------------------------------------------------------------------


class TestMultiUseTokens:
    def test_three_uses_all_succeed(self):
        store = _store()
        token = _issue(store, max_uses=3)
        for _ in range(3):
            ok = store.consume(token.token_id, token.tool_id, token.grantee_role)
            assert ok is True

    def test_fourth_use_on_max_three_returns_false(self):
        store = _store()
        token = _issue(store, max_uses=3)
        for _ in range(3):
            store.consume(token.token_id, token.tool_id, token.grantee_role)
        fourth = store.consume(token.token_id, token.tool_id, token.grantee_role)
        assert fourth is False

    def test_uses_remaining_decrements_correctly(self):
        store = _store()
        token = _issue(store, max_uses=3)
        store.consume(token.token_id, token.tool_id, token.grantee_role)
        store.consume(token.token_id, token.tool_id, token.grantee_role)
        updated = store.get(token.token_id)
        assert updated.uses_remaining == 1


# ---------------------------------------------------------------------------
# Expiration
# ---------------------------------------------------------------------------


class TestExpiration:
    def test_expired_token_consume_returns_false(self):
        store = _store()
        token = _issue(store, ttl_seconds=-1)  # already expired
        ok = store.consume(token.token_id, token.tool_id, token.grantee_role)
        assert ok is False

    def test_expired_token_not_in_list_active(self):
        store = _store()
        _issue(store, ttl_seconds=-1)
        assert store.list_active() == []

    def test_active_token_appears_in_list_active(self):
        store = _store()
        token = _issue(store)
        active = store.list_active()
        assert any(t.token_id == token.token_id for t in active)


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


class TestRevocation:
    def test_revoke_returns_true_first_time(self):
        store = _store()
        token = _issue(store)
        assert store.revoke(token.token_id) is True

    def test_revoke_twice_returns_false(self):
        store = _store()
        token = _issue(store)
        store.revoke(token.token_id)
        assert store.revoke(token.token_id) is False

    def test_consume_after_revoke_returns_false(self):
        store = _store()
        token = _issue(store)
        store.revoke(token.token_id)
        ok = store.consume(token.token_id, token.tool_id, token.grantee_role)
        assert ok is False

    def test_revoked_token_not_in_list_active(self):
        store = _store()
        token = _issue(store)
        store.revoke(token.token_id)
        active = store.list_active()
        assert not any(t.token_id == token.token_id for t in active)

    def test_revoke_nonexistent_returns_false(self):
        store = _store()
        assert store.revoke("no-such-token") is False


# ---------------------------------------------------------------------------
# list_active filtering
# ---------------------------------------------------------------------------


class TestListActiveFiltering:
    def test_filter_by_role(self):
        store = _store()
        _issue(store, grantee_role="analyst")
        _issue(store, grantee_role="operator")
        analysts = store.list_active(role="analyst")
        assert all(t.grantee_role == "analyst" for t in analysts)
        assert len(analysts) == 1

    def test_filter_by_tool_id(self):
        store = _store()
        _issue(store, tool_id="tool.read")
        _issue(store, tool_id="tool.write")
        reads = store.list_active(tool_id="tool.read")
        assert all(t.tool_id == "tool.read" for t in reads)
        assert len(reads) == 1

    def test_filter_by_role_and_tool(self):
        store = _store()
        _issue(store, grantee_role="analyst", tool_id="tool.read")
        _issue(store, grantee_role="analyst", tool_id="tool.write")
        _issue(store, grantee_role="operator", tool_id="tool.read")
        results = store.list_active(role="analyst", tool_id="tool.read")
        assert len(results) == 1
        assert results[0].grantee_role == "analyst"
        assert results[0].tool_id == "tool.read"

    def test_no_filter_returns_all_active(self):
        store = _store()
        _issue(store, grantee_role="a")
        _issue(store, grantee_role="b")
        assert len(store.list_active()) == 2


# ---------------------------------------------------------------------------
# cleanup_expired
# ---------------------------------------------------------------------------


class TestCleanupExpired:
    def test_cleanup_removes_expired_only(self):
        store = _store()
        expired = _issue(store, ttl_seconds=-1)
        active = _issue(store, ttl_seconds=300)
        removed = store.cleanup_expired()
        assert removed == 1
        assert store.get(expired.token_id) is None
        assert store.get(active.token_id) is not None

    def test_cleanup_returns_count(self):
        store = _store()
        _issue(store, ttl_seconds=-1)
        _issue(store, ttl_seconds=-1)
        assert store.cleanup_expired() == 2

    def test_cleanup_no_expired_returns_zero(self):
        store = _store()
        _issue(store)
        assert store.cleanup_expired() == 0


# ---------------------------------------------------------------------------
# check_jit_grant
# ---------------------------------------------------------------------------


class TestCheckJitGrant:
    def test_returns_token_when_valid_grant_exists(self):
        store = _store()
        _issue(store, tool_id="tool.secret", grantee_role="auditor")
        token = check_jit_grant(store, "tool.secret", "auditor")
        assert token is not None
        assert token.tool_id == "tool.secret"

    def test_returns_none_when_no_grant(self):
        store = _store()
        result = check_jit_grant(store, "tool.secret", "auditor")
        assert result is None

    def test_returns_none_when_role_mismatch(self):
        store = _store()
        _issue(store, tool_id="tool.secret", grantee_role="auditor")
        result = check_jit_grant(store, "tool.secret", "operator")
        assert result is None

    def test_token_consumed_after_check(self):
        store = _store()
        token = _issue(store, tool_id="tool.secret", grantee_role="auditor")
        check_jit_grant(store, "tool.secret", "auditor")
        # Second check should find nothing
        assert check_jit_grant(store, "tool.secret", "auditor") is None
        # And the stored copy should have uses_remaining == 0
        updated = store.get(token.token_id)
        assert updated.uses_remaining == 0


# ---------------------------------------------------------------------------
# jit_gate_override
# ---------------------------------------------------------------------------


def _deny_result(gate_id: str = "gate.sod") -> GateResult:
    return GateResult(
        verdict=GateVerdict.DENY,
        justification="SoD rule blocks tool",
        gate_id=gate_id,
        policy_ref="policy.v1",
    )


def _allow_result() -> GateResult:
    return GateResult(
        verdict=GateVerdict.ALLOW,
        justification="All gates passed",
        gate_id="",
    )


def _advisory_result() -> GateResult:
    return GateResult(
        verdict=GateVerdict.ADVISORY,
        justification="Advisory flag",
        gate_id="gate.advisory",
    )


class TestJitGateOverride:
    def test_deny_with_valid_grant_returns_allow(self):
        store = _store()
        _issue(store, tool_id="tool.x", grantee_role="analyst")
        result = jit_gate_override(store, _deny_result(), "tool.x", "analyst")
        assert result.verdict == GateVerdict.ALLOW

    def test_override_justification_contains_token_id(self):
        store = _store()
        token = _issue(store, tool_id="tool.x", grantee_role="analyst")
        result = jit_gate_override(store, _deny_result(), "tool.x", "analyst")
        assert token.token_id in result.justification

    def test_override_evidence_ref_is_token_id(self):
        store = _store()
        token = _issue(store, tool_id="tool.x", grantee_role="analyst")
        result = jit_gate_override(store, _deny_result(), "tool.x", "analyst")
        assert result.evidence_ref == token.token_id

    def test_deny_without_grant_still_deny(self):
        store = _store()
        result = jit_gate_override(store, _deny_result(), "tool.x", "analyst")
        assert result.verdict == GateVerdict.DENY

    def test_allow_passes_through_unchanged(self):
        store = _store()
        _issue(store, tool_id="tool.x", grantee_role="analyst")
        original = _allow_result()
        result = jit_gate_override(store, original, "tool.x", "analyst")
        assert result is original

    def test_advisory_passes_through_unchanged(self):
        store = _store()
        _issue(store, tool_id="tool.x", grantee_role="analyst")
        original = _advisory_result()
        result = jit_gate_override(store, original, "tool.x", "analyst")
        assert result is original

    def test_override_preserves_gate_id(self):
        store = _store()
        _issue(store, tool_id="tool.x", grantee_role="analyst")
        deny = _deny_result(gate_id="gate.hard")
        result = jit_gate_override(store, deny, "tool.x", "analyst")
        assert result.gate_id == "gate.hard"

    def test_override_preserves_policy_ref(self):
        store = _store()
        _issue(store, tool_id="tool.x", grantee_role="analyst")
        deny = _deny_result()
        result = jit_gate_override(store, deny, "tool.x", "analyst")
        assert result.policy_ref == "policy.v1"

    def test_grant_consumed_after_override(self):
        store = _store()
        token = _issue(store, tool_id="tool.x", grantee_role="analyst")
        jit_gate_override(store, _deny_result(), "tool.x", "analyst")
        # Second override attempt must fail
        result = jit_gate_override(store, _deny_result(), "tool.x", "analyst")
        assert result.verdict == GateVerdict.DENY
        updated = store.get(token.token_id)
        assert updated.uses_remaining == 0


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_consume_single_use_only_one_wins(self):
        """With a single-use token, exactly one thread should consume it."""
        store = _store()
        token = _issue(store, max_uses=1)
        results: list[bool] = []
        barrier = threading.Barrier(10)

        def consume_once():
            barrier.wait()
            ok = store.consume(token.token_id, token.tool_id, token.grantee_role)
            results.append(ok)

        threads = [threading.Thread(target=consume_once) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(1 for r in results if r) == 1, (
            f"Expected exactly one success, got {sum(1 for r in results if r)}"
        )

    def test_concurrent_check_jit_single_use_only_one_wins(self):
        """check_jit_grant across threads: only one thread consumes a single-use permit."""
        store = _store()
        _issue(store, tool_id="tool.z", grantee_role="role_z", max_uses=1)
        consumed: list[bool] = []
        barrier = threading.Barrier(5)

        def try_check():
            barrier.wait()
            token = check_jit_grant(store, "tool.z", "role_z")
            consumed.append(token is not None)

        threads = [threading.Thread(target=try_check) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(consumed) == 1, f"Expected exactly one successful grant, got {sum(consumed)}"
