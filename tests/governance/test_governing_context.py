# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from lionagi.governance.certificate import CertificateGrade
from lionagi.governance.context import GoverningContext
from lionagi.governance.errors import GovernanceViolationError
from lionagi.governance.gates import Enforcement, GatePolicy, GateVerdict


def _allow_policy(tool):
    return GatePolicy(
        target_tool=tool, enforcement=Enforcement.HARD, gate_id="block", gate_fn=lambda t, c: False
    )


def _deny_policy(tool):
    return GatePolicy(
        target_tool=tool, enforcement=Enforcement.HARD, gate_id="block", gate_fn=lambda t, c: True
    )


def _advisory_policy(tool):
    return GatePolicy(
        target_tool=tool,
        enforcement=Enforcement.ADVISORY,
        gate_id="warn",
        gate_fn=lambda t, c: True,
    )


class TestGoverningContext:
    def test_check_allow(self):
        ctx = GoverningContext("t1", [_allow_policy("tool_a")])
        r = ctx.check("tool_a")
        assert r.verdict is GateVerdict.ALLOW

    def test_check_deny_raises(self):
        ctx = GoverningContext("t1", [_deny_policy("tool_a")])
        with pytest.raises(GovernanceViolationError) as exc:
            ctx.check("tool_a")
        assert "block" in str(exc.value)

    def test_check_deny_no_raise(self):
        ctx = GoverningContext("t1", [_deny_policy("tool_a")], raise_on_deny=False)
        r = ctx.check("tool_a")
        assert r.denied()

    def test_advisory_does_not_raise(self):
        ctx = GoverningContext("t1", [_advisory_policy("tool_a")])
        r = ctx.check("tool_a")
        assert r.verdict is GateVerdict.ADVISORY

    def test_evidence_chain_grows(self):
        ctx = GoverningContext("t1")
        ctx.check("any_tool")
        ctx.record({"custom": "event"})
        assert ctx.evidence_chain.node_count == 2

    def test_complete_produces_full_cert(self):
        ctx = GoverningContext("t1", [_allow_policy("t")])
        ctx.check("t")
        cert = ctx.complete()
        assert cert.grade == CertificateGrade.FULL
        assert cert.op_count == 1
        assert cert.ops_allowed == 1

    def test_complete_produces_failed_cert(self):
        ctx = GoverningContext("t1", [_deny_policy("t")], raise_on_deny=False)
        ctx.check("t")
        cert = ctx.complete()
        assert cert.grade == CertificateGrade.FAILED
        assert cert.ops_denied == 1

    def test_complete_produces_partial_cert(self):
        ctx = GoverningContext("t1", [_advisory_policy("t")])
        ctx.check("t")
        cert = ctx.complete()
        assert cert.grade == CertificateGrade.PARTIAL
        assert cert.ops_advisory == 1

    def test_complete_evidence_chain_head_matches(self):
        ctx = GoverningContext("t1")
        ctx.record({"x": 1})
        cert = ctx.complete()
        assert cert.evidence_chain_head == ctx.evidence_chain.head_hash()

    def test_no_policies_all_allowed(self):
        ctx = GoverningContext("t1")
        for i in range(5):
            ctx.check(f"tool_{i}")
        cert = ctx.complete()
        assert cert.grade == CertificateGrade.FULL
        assert cert.ops_allowed == 5

    def test_complete_flags_tampered_chain(self):
        ctx = GoverningContext("t1", [_allow_policy("t")])
        ctx.check("t")
        # Tamper with the recorded evidence after the fact.
        node = ctx.evidence_chain.nodes()[0]
        object.__setattr__(node, "tier", "MUTABLE")
        cert = ctx.complete()
        assert cert.chain_verified is False
        assert cert.grade == CertificateGrade.FAILED

    def test_gate_tally_in_cert(self):
        policies = [
            GatePolicy(
                target_tool="t",
                enforcement=Enforcement.ADVISORY,
                gate_id="warn",
                gate_fn=lambda *_: True,
            ),
        ]
        ctx = GoverningContext("t1", policies)
        ctx.check("t")
        ctx.check("t")
        cert = ctx.complete()
        assert cert.gate_results_summary.get("warn", 0) == 2
