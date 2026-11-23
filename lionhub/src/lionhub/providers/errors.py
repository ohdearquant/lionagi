# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Public error types raised by CLI providers and their worker lifecycle."""

from __future__ import annotations

from lionagi._provider_errors import (
    NestedAgentRefusedError,
    ProviderAdapterError,
    ProviderAuthError,
    ProviderCapacityError,
    ProviderContextError,
    ProviderEmptyTurnError,
    ProviderError,
    ProviderPermissionError,
    ProviderQuotaError,
    ProviderSafetyError,
    ProviderStreamDisconnectError,
    ProviderTeardownError,
    ProviderUnsupportedModelError,
    SubagentAdmissionRefusedError,
    WorkerLivenessError,
)

__all__ = (
    "ProviderError",
    "ProviderQuotaError",
    "ProviderAuthError",
    "ProviderContextError",
    "ProviderCapacityError",
    "ProviderUnsupportedModelError",
    "ProviderSafetyError",
    "ProviderStreamDisconnectError",
    "ProviderTeardownError",
    "ProviderPermissionError",
    "ProviderAdapterError",
    "ProviderEmptyTurnError",
    "WorkerLivenessError",
    "SubagentAdmissionRefusedError",
    "NestedAgentRefusedError",
)
