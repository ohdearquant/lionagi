# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the break-glass emergency override mechanism.

Coverage plan
-------------
1. Activate → denied gate becomes allowed
2. Expiry → gates re-engage after the window closes
3. Deactivation → early termination works
4. Evidence chain → activation, override, deactivation all recorded
5. Scoped break-glass → only matching tool is overridden
6. Missing attestation fields → error raised
7. Disabled charter section → error raised
8. ALLOW / ADVISORY verdicts pass through unmodified
9. Override counter increments per override event
10. Check-override on expired session returns original DENY
"""

from __future__ import annotations

import time

import pytest

from lionagi.protocols.governance.breakglass import (
    BreakGlassDisabledError,
    BreakGlassMissingAttestationError,
    BreakGlassRecord,
    BreakGlassSession,
)
from lionagi.protocols.governance.dsl import (
    BreakGlassAttestation,
    BreakGlassDef,
    BreakGlassNotification,
    EvidenceDef,
)
from lionagi.protocols.governance.evidence import EvidenceChain, LogTier
from lionagi.protocols.governance.gates import GateResult, GateVerdict

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_charter(enabled: bool = True, expires_after: str = "15m") -> BreakGlassDef:
    return BreakGlassDef(
        enabled=enabled,
        expires_after=expires_after,
        attestation=BreakGlassAttestation(approver_role="ops-lead", requires_reason=True),
        temporary_grants=["deploy_tool"],
        notifications=[BreakGlassNotification(target="log", on_events=["activate", "deactivate"])],
        evidence=EvidenceDef(required=["activation_reason"]),
    )


def _deny_result(gate_id: str = "hard_gate") -> GateResult:
    return GateResult(
        verdict=GateVerdict.DENY,
        justification="Hard gate blocks tool",
        gate_id=gate_id,
    )


def _allow_result(gate_id: str = "allow_gate") -> GateResult:
    return GateResult(
        verdict=GateVerdict.ALLOW,
        justification="All gates passed",
        gate_id=gate_id,
    )


def _advisory_result(gate_id: str = "adv_gate") -> GateResult:
    return GateResult(
        verdict=GateVerdict.ADVISORY,
        justification="Advisory flagged",
        gate_id=gate_id,
    )


# ---------------------------------------------------------------------------
# 1. Activate → denied gate becomes ALLOW
# ---------------------------------------------------------------------------


class TestActivateOverridesDeny:
    def test_denied_gate_becomes_allow(self):
        session = BreakGlassSession(charter_break_glass=_make_charter())
        session.activate(attester_id="ops-42", reason="prod outage INC-9999")

        result = session.check_override(_deny_result())

        assert result.verdict == GateVerdict.ALLOW

    def test_override_justification_mentions_attester(self):
        session = BreakGlassSession(charter_break_glass=_make_charter())
        session.activate(attester_id="ops-42", reason="emergency fix")

        result = session.check_override(_deny_result())

        assert "ops-42" in result.justification

    def test_override_justification_mentions_original_denial(self):
        session = BreakGlassSession(charter_break_glass=_make_charter())
        session.activate(attester_id="ops-42", reason="emergency fix")
        original = _deny_result()

        result = session.check_override(original)

        assert original.justification in result.justification

    def test_override_preserves_gate_id(self):
        session = BreakGlassSession(charter_break_glass=_make_charter())
        session.activate(attester_id="ops-42", reason="fix")
        original = _deny_result(gate_id="classified_gate")

        result = session.check_override(original)

        assert result.gate_id == "classified_gate"

    def test_no_charter_session_still_overrides(self):
        """No charter → defaults apply (3600 s window); override still works."""
        session = BreakGlassSession()
        session.activate(attester_id="ops-42", reason="no-charter emergency")

        result = session.check_override(_deny_result())

        assert result.verdict == GateVerdict.ALLOW


# ---------------------------------------------------------------------------
# 2. Expiry → gates re-engage
# ---------------------------------------------------------------------------


class TestExpiry:
    def test_expired_session_does_not_override(self):
        session = BreakGlassSession()
        session.activate(attester_id="ops-42", reason="short test", duration_seconds=0)

        # duration_seconds=0 → expires_at == activated_at → already expired
        time.sleep(0.01)
        result = session.check_override(_deny_result())

        assert result.verdict == GateVerdict.DENY

    def test_is_active_returns_false_after_expiry(self):
        session = BreakGlassSession()
        session.activate(attester_id="ops-42", reason="short", duration_seconds=0)

        time.sleep(0.01)

        assert session.is_active() is False

    def test_is_active_returns_true_while_window_open(self):
        session = BreakGlassSession()
        session.activate(attester_id="ops-42", reason="open window", duration_seconds=300)

        assert session.is_active() is True


# ---------------------------------------------------------------------------
# 3. Deactivation
# ---------------------------------------------------------------------------


class TestDeactivation:
    def test_deactivate_ends_override(self):
        session = BreakGlassSession()
        session.activate(attester_id="ops-42", reason="will deactivate", duration_seconds=300)
        session.deactivate()

        result = session.check_override(_deny_result())

        assert result.verdict == GateVerdict.DENY

    def test_is_active_false_after_deactivate(self):
        session = BreakGlassSession()
        session.activate(attester_id="ops-42", reason="deactivate me", duration_seconds=300)
        session.deactivate()

        assert session.is_active() is False

    def test_deactivate_is_idempotent(self):
        """Calling deactivate twice must not raise."""
        session = BreakGlassSession()
        session.activate(attester_id="ops-42", reason="idempotent test", duration_seconds=300)
        session.deactivate()
        session.deactivate()  # second call — must be a no-op

    def test_deactivate_on_inactive_session_is_noop(self):
        """Deactivating before any activation must not raise."""
        session = BreakGlassSession()
        session.deactivate()  # no-op


# ---------------------------------------------------------------------------
# 4. Evidence chain recording
# ---------------------------------------------------------------------------


class TestEvidenceChainRecording:
    def test_activation_recorded_to_chain(self):
        chain = EvidenceChain()
        session = BreakGlassSession(evidence_chain=chain)
        session.activate(attester_id="ops-42", reason="audit test", duration_seconds=300)

        assert chain.node_count == 1
        node = list(chain.nodes)[0]
        assert node.content["event"] == "break_glass_activate"

    def test_override_recorded_to_chain(self):
        chain = EvidenceChain()
        session = BreakGlassSession(evidence_chain=chain)
        session.activate(attester_id="ops-42", reason="override audit", duration_seconds=300)
        session.check_override(_deny_result())

        # activation node + override node = 2
        assert chain.node_count == 2
        override_node = list(chain.nodes)[1]
        assert override_node.content["event"] == "break_glass_override"

    def test_deactivation_recorded_to_chain(self):
        chain = EvidenceChain()
        session = BreakGlassSession(evidence_chain=chain)
        session.activate(attester_id="ops-42", reason="deact audit", duration_seconds=300)
        session.deactivate()

        # activation + deactivation = 2
        assert chain.node_count == 2
        deact_node = list(chain.nodes)[1]
        assert deact_node.content["event"] == "break_glass_deactivate"

    def test_all_evidence_nodes_are_immutable_tier(self):
        chain = EvidenceChain()
        session = BreakGlassSession(evidence_chain=chain)
        session.activate(attester_id="ops-42", reason="tier test", duration_seconds=300)
        session.check_override(_deny_result())
        session.deactivate()

        for node in chain.nodes:
            assert node.tier == LogTier.IMMUTABLE.value or node.tier == LogTier.IMMUTABLE

    def test_chain_is_valid_after_full_lifecycle(self):
        chain = EvidenceChain()
        session = BreakGlassSession(evidence_chain=chain)
        session.activate(attester_id="ops-42", reason="full lifecycle", duration_seconds=300)
        session.check_override(_deny_result())
        session.deactivate()

        assert chain.verify().valid is True

    def test_override_node_records_attester_id(self):
        chain = EvidenceChain()
        session = BreakGlassSession(evidence_chain=chain)
        session.activate(attester_id="ops-42", reason="attester check", duration_seconds=300)
        session.check_override(_deny_result())

        override_node = list(chain.nodes)[1]
        assert override_node.content["attester_id"] == "ops-42"

    def test_activation_node_records_reason(self):
        chain = EvidenceChain()
        session = BreakGlassSession(evidence_chain=chain)
        session.activate(attester_id="ops-42", reason="INC-9999 outage", duration_seconds=300)

        act_node = list(chain.nodes)[0]
        assert act_node.content["reason"] == "INC-9999 outage"

    def test_multiple_overrides_each_recorded(self):
        chain = EvidenceChain()
        session = BreakGlassSession(evidence_chain=chain)
        session.activate(attester_id="ops-42", reason="multi-override", duration_seconds=300)

        session.check_override(_deny_result(gate_id="gate_a"))
        session.check_override(_deny_result(gate_id="gate_b"))
        session.check_override(_deny_result(gate_id="gate_c"))

        # activation (1) + 3 override nodes = 4
        assert chain.node_count == 4


# ---------------------------------------------------------------------------
# 5. Scoped break-glass
# ---------------------------------------------------------------------------


class TestScopedBreakGlass:
    def test_scope_matches_tool_override_applied(self):
        session = BreakGlassSession()
        session.activate(
            attester_id="ops-42",
            reason="scoped override",
            duration_seconds=300,
            scope="deploy_tool",
        )

        result = session.check_override(_deny_result(gate_id="deploy_tool"))

        assert result.verdict == GateVerdict.ALLOW

    def test_scope_does_not_match_original_deny_returned(self):
        session = BreakGlassSession()
        session.activate(
            attester_id="ops-42",
            reason="scoped override",
            duration_seconds=300,
            scope="deploy_tool",
        )

        result = session.check_override(_deny_result(gate_id="delete_tool"))

        assert result.verdict == GateVerdict.DENY

    def test_scope_all_overrides_any_tool(self):
        session = BreakGlassSession()
        session.activate(
            attester_id="ops-42", reason="all-scope", duration_seconds=300, scope="all"
        )

        for gate_name in ("deploy_tool", "delete_tool", "read_tool"):
            result = session.check_override(_deny_result(gate_id=gate_name))
            assert result.verdict == GateVerdict.ALLOW, f"Expected ALLOW for {gate_name}"

    def test_scoped_override_does_not_record_non_matching_check(self):
        chain = EvidenceChain()
        session = BreakGlassSession(evidence_chain=chain)
        session.activate(
            attester_id="ops-42",
            reason="scope-no-record",
            duration_seconds=300,
            scope="deploy_tool",
        )

        # check_override on a non-matching gate → no new node should be recorded
        session.check_override(_deny_result(gate_id="other_tool"))

        # only the activation node — no override node
        assert chain.node_count == 1


# ---------------------------------------------------------------------------
# 6. Missing attestation fields
# ---------------------------------------------------------------------------


class TestMissingAttestationFields:
    def test_empty_attester_id_raises(self):
        session = BreakGlassSession()
        with pytest.raises(BreakGlassMissingAttestationError):
            session.activate(attester_id="", reason="valid reason")

    def test_whitespace_only_attester_id_raises(self):
        session = BreakGlassSession()
        with pytest.raises(BreakGlassMissingAttestationError):
            session.activate(attester_id="   ", reason="valid reason")

    def test_empty_reason_raises(self):
        session = BreakGlassSession()
        with pytest.raises(BreakGlassMissingAttestationError):
            session.activate(attester_id="ops-42", reason="")

    def test_whitespace_only_reason_raises(self):
        session = BreakGlassSession()
        with pytest.raises(BreakGlassMissingAttestationError):
            session.activate(attester_id="ops-42", reason="  ")


# ---------------------------------------------------------------------------
# 7. Disabled charter section
# ---------------------------------------------------------------------------


class TestDisabledCharter:
    def test_disabled_charter_raises_on_activate(self):
        charter = _make_charter(enabled=False)
        session = BreakGlassSession(charter_break_glass=charter)

        with pytest.raises(BreakGlassDisabledError):
            session.activate(attester_id="ops-42", reason="should fail")

    def test_disabled_charter_no_evidence_written(self):
        chain = EvidenceChain()
        charter = _make_charter(enabled=False)
        session = BreakGlassSession(charter_break_glass=charter, evidence_chain=chain)

        with pytest.raises(BreakGlassDisabledError):
            session.activate(attester_id="ops-42", reason="should fail")

        # No evidence should have been written because activation was rejected
        assert chain.node_count == 0


# ---------------------------------------------------------------------------
# 8. Non-DENY verdicts pass through unmodified
# ---------------------------------------------------------------------------


class TestNonDenyPassThrough:
    def test_allow_verdict_unchanged(self):
        session = BreakGlassSession()
        session.activate(attester_id="ops-42", reason="passthru test", duration_seconds=300)
        original = _allow_result()

        result = session.check_override(original)

        assert result is original

    def test_advisory_verdict_unchanged(self):
        session = BreakGlassSession()
        session.activate(attester_id="ops-42", reason="passthru test", duration_seconds=300)
        original = _advisory_result()

        result = session.check_override(original)

        assert result is original

    def test_no_evidence_written_for_allow_passthru(self):
        chain = EvidenceChain()
        session = BreakGlassSession(evidence_chain=chain)
        session.activate(attester_id="ops-42", reason="no record for allow", duration_seconds=300)
        count_after_activate = chain.node_count

        session.check_override(_allow_result())

        assert chain.node_count == count_after_activate


# ---------------------------------------------------------------------------
# 9. Override counter increments
# ---------------------------------------------------------------------------


class TestOverrideCounter:
    def test_override_counter_starts_at_zero(self):
        session = BreakGlassSession()
        record = session.activate(attester_id="ops-42", reason="counter test", duration_seconds=300)
        assert record.override_count == 0

    def test_override_counter_increments(self):
        session = BreakGlassSession()
        session.activate(attester_id="ops-42", reason="counter test", duration_seconds=300)

        session.check_override(_deny_result(gate_id="g1"))
        session.check_override(_deny_result(gate_id="g2"))
        session.check_override(_deny_result(gate_id="g3"))

        assert session._record.override_count == 3

    def test_override_count_in_deactivation_evidence(self):
        chain = EvidenceChain()
        session = BreakGlassSession(evidence_chain=chain)
        session.activate(attester_id="ops-42", reason="counter deact", duration_seconds=300)

        session.check_override(_deny_result())
        session.check_override(_deny_result())
        session.deactivate()

        deact_node = list(chain.nodes)[-1]
        assert deact_node.content["override_count"] == 2


# ---------------------------------------------------------------------------
# 10. Duration / charter limit enforcement
# ---------------------------------------------------------------------------


class TestDurationLimits:
    def test_duration_exceeds_charter_limit_raises(self):
        charter = _make_charter(expires_after="15m")  # 900 s max
        session = BreakGlassSession(charter_break_glass=charter)

        with pytest.raises(ValueError, match="exceeds charter maximum"):
            session.activate(attester_id="ops-42", reason="too long", duration_seconds=901)

    def test_duration_at_charter_limit_accepted(self):
        charter = _make_charter(expires_after="15m")  # 900 s max
        session = BreakGlassSession(charter_break_glass=charter)

        record = session.activate(attester_id="ops-42", reason="at limit", duration_seconds=900)

        assert record.is_active() is True

    def test_default_duration_from_charter_expires_after(self):
        charter = _make_charter(expires_after="5m")  # 300 s
        session = BreakGlassSession(charter_break_glass=charter)

        record = session.activate(attester_id="ops-42", reason="default duration")

        delta = (record.expires_at - record.activated_at).total_seconds()
        assert abs(delta - 300) < 2  # allow up to 2 s of test wall-clock drift

    def test_no_charter_default_duration_is_3600s(self):
        session = BreakGlassSession()
        record = session.activate(attester_id="ops-42", reason="no-charter default")

        delta = (record.expires_at - record.activated_at).total_seconds()
        assert abs(delta - 3600) < 2
