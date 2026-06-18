# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from lionagi.governance.errors import GovernanceViolationError
from lionagi.governance.gates import Enforcement, GateExecutor, GatePolicy, GateVerdict


def _always_fires(tool_name, ctx):
    return True


def _never_fires(tool_name, ctx):
    return False


def make_policy(target, enforcement=Enforcement.HARD, gate_fn=_always_fires, gate_id="g1"):
    return GatePolicy(target_tool=target, enforcement=enforcement, gate_id=gate_id, gate_fn=gate_fn)


class TestGateVerdict:
    def test_allow_when_no_policies(self):
        ex = GateExecutor([])
        r = ex.evaluate("my_tool")
        assert r.verdict is GateVerdict.ALLOW

    def test_allow_when_no_matching_policy(self):
        ex = GateExecutor([make_policy("other_tool")])
        r = ex.evaluate("my_tool")
        assert r.verdict is GateVerdict.ALLOW

    def test_hard_deny_when_gate_fires(self):
        ex = GateExecutor([make_policy("my_tool", Enforcement.HARD, _always_fires)])
        r = ex.evaluate("my_tool")
        assert r.verdict is GateVerdict.DENY
        assert r.denied() is not False  # sanity
        assert r.denied()

    def test_advisory_when_gate_fires_softly(self):
        ex = GateExecutor([make_policy("my_tool", Enforcement.ADVISORY, _always_fires)])
        r = ex.evaluate("my_tool")
        assert r.verdict is GateVerdict.ADVISORY

    def test_allow_when_gate_fn_returns_false(self):
        ex = GateExecutor([make_policy("my_tool", Enforcement.HARD, _never_fires)])
        r = ex.evaluate("my_tool")
        assert r.verdict is GateVerdict.ALLOW

    def test_hard_policy_short_circuits(self):
        fired = []

        def record(tool, ctx):
            fired.append(tool)
            return True

        policies = [
            make_policy("t", Enforcement.HARD, record, "g1"),
            make_policy("t", Enforcement.HARD, record, "g2"),
        ]
        ex = GateExecutor(policies)
        r = ex.evaluate("t")
        assert r.verdict is GateVerdict.DENY
        assert len(fired) == 1  # short-circuit after first hard deny

    def test_multiple_advisories_collapsed(self):
        policies = [
            make_policy("t", Enforcement.ADVISORY, _always_fires, "a1"),
            make_policy("t", Enforcement.ADVISORY, _always_fires, "a2"),
        ]
        ex = GateExecutor(policies)
        r = ex.evaluate("t")
        assert r.verdict is GateVerdict.ADVISORY
        assert "2 advisory" in r.justification

    def test_elapsed_ms_is_nonnegative(self):
        ex = GateExecutor([make_policy("t")])
        r = ex.evaluate("t")
        assert r.elapsed_ms >= 0.0

    def test_to_evidence_dict_shape(self):
        ex = GateExecutor([])
        r = ex.evaluate("x")
        d = r.to_evidence_dict()
        assert d["event"] == "gate_result"
        assert "verdict" in d and "gate_id" in d


class TestGovernanceViolationError:
    def test_raises_with_gate_id(self):
        from lionagi.governance.gates import GateResult

        result = GateResult(verdict=GateVerdict.DENY, justification="blocked", gate_id="g99")
        err = GovernanceViolationError(result)
        assert "g99" in str(err)
        assert err.result is result
