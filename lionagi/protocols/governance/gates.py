# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Governance gate evaluation types and executor (P17).

GateResult is the canonical result type for gate evaluation.
GovernanceViolationError is raised on HARD-enforcement DENY.
GateExecutor evaluates GateRegistration lists from compiled charters.
"""

from __future__ import annotations

import enum
import time
from typing import Any

from pydantic import BaseModel

from lionagi.protocols.governance.dsl import Enforcement
from lionagi.protocols.governance.targets import GateRegistration

__all__ = [
    "GateVerdict",
    "GateResult",
    "GovernanceViolationError",
    "GateExecutor",
]


class GateVerdict(str, enum.Enum):
    ALLOW = "allow"
    DENY = "deny"
    ADVISORY = "advisory"


class GateResult(BaseModel):
    verdict: GateVerdict
    justification: str
    gate_id: str
    policy_ref: str | None = None
    evidence_ref: str | None = None
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "justification": self.justification,
            "gate_id": self.gate_id,
            "policy_ref": self.policy_ref,
            "evidence_ref": self.evidence_ref,
            "elapsed_ms": self.elapsed_ms,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GateResult:
        return cls(
            verdict=GateVerdict(d["verdict"]),
            justification=d["justification"],
            gate_id=d["gate_id"],
            policy_ref=d.get("policy_ref"),
            evidence_ref=d.get("evidence_ref"),
            elapsed_ms=d.get("elapsed_ms", 0.0),
        )


class GovernanceViolationError(Exception):
    def __init__(self, result: GateResult) -> None:
        self.result = result
        super().__init__(f"Gate {result.gate_id} denied: {result.justification}")


class GateExecutor:
    """Evaluates compiled GateRegistrations for a tool invocation.

    Matching is by exact target_tool name. HARD-enforcement matches short-circuit
    with DENY. SOFT/ADVISORY matches are collected and returned as ADVISORY.
    ALLOW is returned when no matching registrations fire a DENY or ADVISORY.
    """

    def __init__(self, registrations: list[GateRegistration]) -> None:
        self.registrations = registrations

    def evaluate(self, tool_name: str, ctx: Any) -> GateResult:
        start = time.perf_counter()
        advisory_results: list[GateResult] = []

        for reg in self.registrations:
            if reg.target_tool != tool_name:
                continue
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            if reg.enforcement == Enforcement.HARD:
                return GateResult(
                    verdict=GateVerdict.DENY,
                    justification=(f"Hard gate '{reg.gate_function}' blocks tool '{tool_name}'"),
                    gate_id=reg.gate_function,
                    policy_ref=reg.charter_ref or None,
                    elapsed_ms=elapsed_ms,
                )
            # SOFT or ADVISORY — collect and continue
            advisory_results.append(
                GateResult(
                    verdict=GateVerdict.ADVISORY,
                    justification=(
                        f"Advisory gate '{reg.gate_function}' flagged tool '{tool_name}'"
                    ),
                    gate_id=reg.gate_function,
                    policy_ref=reg.charter_ref or None,
                    elapsed_ms=elapsed_ms,
                )
            )

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        if advisory_results:
            last = advisory_results[-1]
            return GateResult(
                verdict=GateVerdict.ADVISORY,
                justification=(
                    f"{len(advisory_results)} advisory gate(s) flagged for '{tool_name}'"
                ),
                gate_id=last.gate_id,
                policy_ref=last.policy_ref,
                elapsed_ms=elapsed_ms,
            )
        return GateResult(
            verdict=GateVerdict.ALLOW,
            justification=f"All gates passed for tool '{tool_name}'",
            gate_id="",
            elapsed_ms=elapsed_ms,
        )
