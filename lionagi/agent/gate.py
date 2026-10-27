# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Compatibility exports for the core security-control verdict contract."""

from __future__ import annotations

from lionagi.protocols.action.gate import (
    ControlUnavailableError,
    GateDeniedError,
    GateEvaluator,
    GateResult,
    adapt_legacy_hook,
    adapt_session_gate,
    run_gate_pass,
)
from lionagi.protocols.action.gate import (
    logger as logger,
)

__all__ = (
    "ControlUnavailableError",
    "GateDeniedError",
    "GateEvaluator",
    "GateResult",
    "adapt_legacy_hook",
    "adapt_session_gate",
    "run_gate_pass",
)
