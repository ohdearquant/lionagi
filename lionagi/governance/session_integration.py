# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Compatibility shim: re-exports from lionagi.session.governed_flow.

Tests and downstream code that import from lionagi.governance.session_integration
should migrate to lionagi.session.governed_flow, but this shim keeps them working.
"""

from lionagi.session.governed_flow import (  # noqa: F401
    _RAISE,
    _SKIP,
    _GovernedExecutor,
    _ungoverned_flow,
    governed_flow,
)

__all__ = ["governed_flow", "_GovernedExecutor", "_RAISE", "_SKIP", "_ungoverned_flow"]
