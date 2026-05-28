# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for governance tests.

Provides an autouse fixture that resets the operation-context contextvar
after every test.  GovernedFlowController.__init__ sets the var without
returning the reset token, so without this fixture the var leaks across
tests when they run in a single process (no-xdist or serial mode).
"""

from __future__ import annotations

import pytest

from lionagi.governance.context import (
    _operation_context_var,
)


@pytest.fixture(autouse=True)
def _reset_operation_context():
    """Reset the operation-context contextvar before and after each test."""
    token = _operation_context_var.set(None)
    yield
    _operation_context_var.reset(token)
