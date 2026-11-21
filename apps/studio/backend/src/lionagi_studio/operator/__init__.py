# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Durable Studio Operator protocol.

The Operator conversation is deliberately separate from LionAGI runtime
``Session`` rows.  Runtime work launched by an Operator command still goes
through the ordinary Studio launch service and therefore appears in Runs.
"""

from .coordinator import (
    OperatorCoordinator,
    get_operator_coordinator,
    reset_operator_coordinator_for_testing,
)

__all__ = (
    "OperatorCoordinator",
    "get_operator_coordinator",
    "reset_operator_coordinator_for_testing",
)
