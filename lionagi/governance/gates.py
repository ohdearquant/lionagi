# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tool gate evaluation: policy-driven ALLOW / ADVISORY / DENY verdicts."""

from __future__ import annotations

import enum
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

__all__ = [
    "Enforcement",
    "GatePolicy",
    "GateResult",
    "GateVerdict",
    "GateExecutor",
]


class Enforcement(str, enum.Enum):
    HARD = "hard"
    SOFT = "soft"
    ADVISORY = "advisory"


class GatePolicy(BaseModel):
    """A single policy rule binding a gate callable to a tool name."""

    target_tool: str
    enforcement: Enforcement = Enforcement.HARD
    gate_id: str = ""
    gate_fn: Callable[[str, Any], bool] | None = None

    model_config = {"arbitrary_types_allowed": True}

    def matches(self, tool_name: str) -> bool:
        return self.target_tool == tool_name

    def evaluate(self, tool_name: str, ctx: Any) -> bool:
        """Return True when the gate fires (i.e. wants to block)."""
        if self.gate_fn is None:
            return True
        return bool(self.gate_fn(tool_name, ctx))


class GateVerdict(str, enum.Enum):
    ALLOW = "allow"
    DENY = "deny"
    ADVISORY = "advisory"


class GateResult(BaseModel):
    verdict: GateVerdict
    justification: str
    gate_id: str
    elapsed_ms: float = 0.0

    def denied(self) -> bool:
        return self.verdict is GateVerdict.DENY

    def to_evidence_dict(self) -> dict[str, Any]:
        return {
            "event": "gate_result",
            "verdict": self.verdict.value,
            "gate_id": self.gate_id,
            "justification": self.justification,
            "elapsed_ms": self.elapsed_ms,
        }


class GateExecutor:
    """Evaluates a list of GatePolicy rules for one tool invocation.

    Matching is by exact target_tool name.  A HARD policy that fires short-
    circuits with DENY.  SOFT/ADVISORY policies are collected; if any fired
    the final verdict is ADVISORY.  No match → ALLOW.
    """

    def __init__(self, policies: list[GatePolicy]) -> None:
        self.policies = policies

    def evaluate(self, tool_name: str, ctx: Any = None) -> GateResult:
        start = time.perf_counter()
        advisories: list[GateResult] = []

        for policy in self.policies:
            if not policy.matches(tool_name):
                continue
            if not policy.evaluate(tool_name, ctx):
                continue
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            if policy.enforcement is Enforcement.HARD:
                return GateResult(
                    verdict=GateVerdict.DENY,
                    justification=f"Hard gate {policy.gate_id!r} blocks {tool_name!r}",
                    gate_id=policy.gate_id,
                    elapsed_ms=elapsed_ms,
                )
            advisories.append(
                GateResult(
                    verdict=GateVerdict.ADVISORY,
                    justification=f"Advisory gate {policy.gate_id!r} flagged {tool_name!r}",
                    gate_id=policy.gate_id,
                    elapsed_ms=elapsed_ms,
                )
            )

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        if advisories:
            return GateResult(
                verdict=GateVerdict.ADVISORY,
                justification=f"{len(advisories)} advisory gate(s) flagged {tool_name!r}",
                gate_id=advisories[-1].gate_id,
                elapsed_ms=elapsed_ms,
            )
        return GateResult(
            verdict=GateVerdict.ALLOW,
            justification=f"All gates passed for {tool_name!r}",
            gate_id="",
            elapsed_ms=elapsed_ms,
        )
