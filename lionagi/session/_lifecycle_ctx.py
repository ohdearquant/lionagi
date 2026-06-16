# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Task-scoped lifecycle suppression via ContextVar (see docs/reference/testing-state-session.md)."""

from contextvars import ContextVar

__all__ = ("suppress_lifecycle_var",)

suppress_lifecycle_var: ContextVar[bool] = ContextVar("suppress_run_lifecycle", default=False)
