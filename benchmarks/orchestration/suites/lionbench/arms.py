"""khive-injection bench arms (M0/M1/M2) — declarative config layer + the run
manifest's injection bookkeeping (INJECTION_DESIGN.md §3, §7 build-plan item 3,
§9 gate record R1/R2).

One ``ArmConfig`` = one arm = one toggle; adapters and the sandbox entry never
branch on which arm is running, they just get a different
``KhiveInjectionPolicy`` via ``ArmConfig.to_policy()``. M0 is injection off.
M1/M2 pin a namespace (the fallback isolation mechanism until khive's
recall/compose surface accepts a snapshot id, see ``KhiveInjectionProvider``'s
``snapshot_id`` guard) — an enabled arm without a namespace is rejected at
config-construction time, not silently allowed to contaminate the live store.
M2 additionally turns writeback on and resets its namespace between
instances; ``reset_record`` shapes that reset's manifest entry and folds a
failed reset into ``injection_effective`` exactly like a dead substrate
(INJECTION_DESIGN.md §6, §9 R2) — the actual reset call (kg delete /
memory-prune verbs, never file operations) is performed by whoever drives the
run; this module only validates the arm and shapes the record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lionagi.tools.khive_injection import KhiveInjectionPolicy, WritebackPolicy

_VALID_ARM_NAMES = ("M0", "M1", "M2")


@dataclass(frozen=True)
class ArmConfig:
    """One bench-arm config block. Construction validates the namespace-pinning
    rule: any arm with ``enabled=True`` must carry a ``namespace`` — an M1/M2
    arm config parsed without one is a contamination risk, not a valid arm."""

    name: str
    enabled: bool
    writeback: bool = False
    namespace: str | None = None
    cadence: str = "first_turn"

    def __post_init__(self):
        if self.name not in _VALID_ARM_NAMES:
            raise ValueError(f"arm name must be one of {_VALID_ARM_NAMES}, got {self.name!r}")
        if self.enabled and not self.namespace:
            raise ValueError(
                f"arm {self.name!r}: enabled khive-injection arms require an explicit "
                "namespace — an unpinned M1/M2 arm reads and writes the live khive "
                "store and contaminates the M0/M1/M2 comparison (INJECTION_DESIGN.md §3)."
            )

    def to_policy(self, *, profile_id: str) -> KhiveInjectionPolicy:
        """The real ``KhiveInjectionPolicy`` this arm drives the provider with."""
        return KhiveInjectionPolicy(
            profile_id=profile_id,
            enabled=self.enabled,
            namespace=self.namespace,
            cadence=self.cadence,
            writeback=WritebackPolicy(enabled=self.writeback),
        )


def m0_arm() -> ArmConfig:
    """Injection off — the control arm."""
    return ArmConfig(name="M0", enabled=False)


def m1_arm(namespace: str) -> ArmConfig:
    """Injection on, writeback off — read-only against a pinned namespace."""
    return ArmConfig(name="M1", enabled=True, writeback=False, namespace=namespace)


def m2_arm(namespace: str) -> ArmConfig:
    """Injection on, writeback on — the full flywheel, against a pinned
    namespace reset between instances (see ``reset_record``)."""
    return ArmConfig(name="M2", enabled=True, writeback=True, namespace=namespace)


def build_arm(name: str, namespace: str | None = None) -> ArmConfig:
    """CLI-facing constructor: dispatch to the named arm factory."""
    if name == "M0":
        return m0_arm()
    if name == "M1":
        if not namespace:
            raise ValueError("M1 requires --namespace")
        return m1_arm(namespace)
    if name == "M2":
        if not namespace:
            raise ValueError("M2 requires --namespace")
        return m2_arm(namespace)
    raise ValueError(f"arm name must be one of {_VALID_ARM_NAMES}, got {name!r}")


def reset_record(arm: ArmConfig, *, ok: bool, detail: str = "") -> dict:
    """Manifest record for M2's between-instance namespace reset.

    The reset itself is performed elsewhere, via kg delete / memory-prune
    verbs only (never file operations, INJECTION_DESIGN.md §9 R2) — this
    function only shapes the manifest entry. A reset that silently failed
    contaminates the arm exactly like a dead substrate; ``injection_manifest``
    below forces ``injection_effective=False`` when ``ok`` is falsy."""
    if arm.name != "M2":
        raise ValueError(f"namespace reset only applies to the M2 arm, got {arm.name!r}")
    return {"namespace": arm.namespace, "reset_ok": ok, "detail": detail}


def _report_failed(report: Any) -> list[str]:
    if isinstance(report, dict):
        return list(report.get("failed") or [])
    return list(getattr(report, "failed", None) or [])


def _report_fired(report: Any) -> list[dict]:
    if isinstance(report, dict):
        return list(report.get("fired") or [])
    return list(getattr(report, "fired", None) or [])


def injection_manifest(arm: ArmConfig, reports: list[Any], *, reset: dict | None = None) -> dict:
    """The run manifest's injection block for one (instance, arm) cell.

    ``reports`` is the per-turn ``ProviderReport`` sequence (or dict-shaped
    equivalents) from ``branch.last_context_report`` across the run's turns.
    ``injection_effective`` is False the moment ANY turn recorded a failed
    provider — a khive daemon outage degrades M1/M2 to M0 silently at run
    time but must show up loud here (INJECTION_DESIGN.md §6): a bench run
    with a dead substrate is invalidated, not silently scored as a clean arm.
    """
    if not arm.enabled:
        return {"arm": arm.name, "injection_effective": None, "providers_fired": []}

    any_failed = any(_report_failed(r) for r in reports)
    providers_fired: list[dict] = []
    for r in reports:
        providers_fired.extend(_report_fired(r))

    block = {
        "arm": arm.name,
        "injection_effective": not any_failed,
        "providers_fired": providers_fired,
    }
    if arm.name == "M2":
        if reset is None:
            raise ValueError("M2 arm requires a reset record (see reset_record)")
        block["namespace_reset"] = reset
        if not reset.get("reset_ok", False):
            block["injection_effective"] = False
    return block
