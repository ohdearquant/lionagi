"""Unit tests for the khive-injection bench arm layer: M0/M1/M2 construction,
namespace-pinning validation, and the run manifest's injection bookkeeping."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from arms import (  # noqa: E402
    ArmConfig,
    build_arm,
    injection_manifest,
    m0_arm,
    m1_arm,
    m2_arm,
    reset_record,
)

# ---------------------------------------------------------------------------
# Arm construction + namespace-pinning validation
# ---------------------------------------------------------------------------


def test_m0_arm_is_disabled_with_no_namespace_required():
    arm = m0_arm()
    assert arm.name == "M0"
    assert arm.enabled is False
    assert arm.namespace is None


def test_m1_arm_requires_namespace_at_construction():
    with pytest.raises(ValueError, match="require an explicit namespace"):
        ArmConfig(name="M1", enabled=True, namespace=None)


def test_m2_arm_requires_namespace_at_construction():
    with pytest.raises(ValueError, match="require an explicit namespace"):
        ArmConfig(name="M2", enabled=True, writeback=True, namespace="")


def test_m1_arm_factory_produces_read_only_pinned_config():
    arm = m1_arm("bench-m1-ns")
    assert arm.enabled is True
    assert arm.writeback is False
    assert arm.namespace == "bench-m1-ns"


def test_m2_arm_factory_produces_writeback_on_pinned_config():
    arm = m2_arm("bench-m2-ns")
    assert arm.enabled is True
    assert arm.writeback is True
    assert arm.namespace == "bench-m2-ns"


def test_build_arm_dispatches_by_name():
    assert build_arm("M0").name == "M0"
    assert build_arm("M1", "ns").name == "M1"
    assert build_arm("M2", "ns").name == "M2"


def test_build_arm_m1_without_namespace_rejected():
    with pytest.raises(ValueError, match="requires --namespace"):
        build_arm("M1", None)


def test_build_arm_m2_without_namespace_rejected():
    with pytest.raises(ValueError, match="requires --namespace"):
        build_arm("M2", None)


def test_unknown_arm_name_rejected():
    with pytest.raises(ValueError, match="arm name must be one of"):
        ArmConfig(name="M3", enabled=False)


# ---------------------------------------------------------------------------
# ArmConfig.to_policy — the real KhiveInjectionPolicy an arm drives
# ---------------------------------------------------------------------------


def test_to_policy_carries_namespace_and_writeback():
    arm = m2_arm("bench-m2-ns")
    policy = arm.to_policy(profile_id="implementer-recall-v1")
    assert policy.enabled is True
    assert policy.namespace == "bench-m2-ns"
    assert policy.writeback.enabled is True


def test_m0_to_policy_is_disabled():
    policy = m0_arm().to_policy(profile_id="implementer-recall-v1")
    assert policy.enabled is False


# ---------------------------------------------------------------------------
# reset_record
# ---------------------------------------------------------------------------


def test_reset_record_only_for_m2():
    with pytest.raises(ValueError, match="only applies to the M2 arm"):
        reset_record(m1_arm("ns"), ok=True)


def test_reset_record_shape():
    rec = reset_record(m2_arm("ns"), ok=True, detail="pruned")
    assert rec == {"namespace": "ns", "reset_ok": True, "detail": "pruned"}


# ---------------------------------------------------------------------------
# injection_manifest — the manifest block per (instance, arm) cell
# ---------------------------------------------------------------------------


@dataclass
class _FakeReport:
    fired: list = field(default_factory=list)
    failed: list = field(default_factory=list)


def test_m0_arm_manifest_is_effective_none():
    block = injection_manifest(m0_arm(), reports=[])
    assert block == {"arm": "M0", "injection_effective": None, "providers_fired": []}


def test_m1_arm_manifest_effective_true_when_no_turn_failed():
    reports = [
        _FakeReport(fired=[{"provider_name": "khive_injection:x", "tokens": 120}]),
        _FakeReport(fired=[{"provider_name": "khive_injection:x", "tokens": 80}]),
    ]
    block = injection_manifest(m1_arm("ns"), reports)
    assert block["injection_effective"] is True
    assert block["providers_fired"] == [
        {"provider_name": "khive_injection:x", "tokens": 120},
        {"provider_name": "khive_injection:x", "tokens": 80},
    ]


def test_injection_effective_false_when_any_turn_provider_failed():
    reports = [
        _FakeReport(fired=[{"provider_name": "khive_injection:x", "tokens": 120}]),
        _FakeReport(failed=["khive_injection:x"]),
    ]
    block = injection_manifest(m1_arm("ns"), reports)
    assert block["injection_effective"] is False


def test_injection_manifest_accepts_dict_shaped_reports():
    reports = [{"fired": [{"provider_name": "p", "tokens": 5}], "failed": []}, {"failed": ["p"]}]
    block = injection_manifest(m1_arm("ns"), reports)
    assert block["injection_effective"] is False
    assert block["providers_fired"] == [{"provider_name": "p", "tokens": 5}]


def test_m2_arm_manifest_requires_reset_record():
    with pytest.raises(ValueError, match="requires a reset record"):
        injection_manifest(m2_arm("ns"), reports=[])


def test_m2_arm_manifest_forces_ineffective_on_failed_reset():
    reset = reset_record(m2_arm("ns"), ok=False, detail="reset verb not wired")
    block = injection_manifest(
        m2_arm("ns"),
        reports=[_FakeReport(fired=[{"provider_name": "p", "tokens": 10}])],
        reset=reset,
    )
    assert block["injection_effective"] is False
    assert block["namespace_reset"] == reset


def test_m2_arm_manifest_effective_true_when_reset_ok_and_no_failures():
    reset = reset_record(m2_arm("ns"), ok=True, detail="pruned")
    block = injection_manifest(
        m2_arm("ns"),
        reports=[_FakeReport(fired=[{"provider_name": "p", "tokens": 10}])],
        reset=reset,
    )
    assert block["injection_effective"] is True
    assert block["namespace_reset"] == reset


def test_enabled_arms_blocked_until_namespace_reads_exist():
    import pytest as _pytest

    from arms import m0_arm, m1_arm, m2_arm

    m0_arm().assert_runnable()
    with _pytest.raises(RuntimeError, match="namespace-scoped reads"):
        m1_arm(namespace="bench-m1").assert_runnable()
    with _pytest.raises(RuntimeError, match="namespace-scoped reads"):
        m2_arm(namespace="bench-m2").assert_runnable()
