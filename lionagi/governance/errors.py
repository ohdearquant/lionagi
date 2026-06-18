# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lionagi.governance.gates import GateResult

__all__ = [
    "GovernanceViolationError",
]


class GovernanceViolationError(Exception):
    def __init__(self, result: GateResult) -> None:
        self.result = result
        super().__init__(f"Gate {result.gate_id!r} denied: {result.justification}")
