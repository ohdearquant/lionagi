# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Task-scoped run-lifecycle suppression via contextvars.

Using a ``ContextVar`` instead of a Branch-level boolean ensures suppression is
scoped to the *asyncio task* (or trio task) that set it.  asyncio copies the
context when spawning a new task, so nested coroutines in the SAME task inherit
the suppression flag while concurrent tasks on the SAME branch each get their
own copy and are therefore never affected.

Usage::

    # Suppress lifecycle signals for the duration of the call:
    token = _suppress_lifecycle_var.set(True)
    try:
        ...
    finally:
        _suppress_lifecycle_var.reset(token)

    # Check inside run():
    if _suppress_lifecycle_var.get():
        ...
"""

from contextvars import ContextVar

__all__ = ("suppress_lifecycle_var",)

suppress_lifecycle_var: ContextVar[bool] = ContextVar("suppress_run_lifecycle", default=False)
