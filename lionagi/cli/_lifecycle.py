# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Exception classification and exit-code mapping for CLI run loops."""

from __future__ import annotations

EXIT_CODE_BY_STATUS: dict[str, int] = {
    "completed": 0,
    "failed": 1,
    "timed_out": 124,
    "aborted": 130,
    "cancelled": 143,
}


def classify_exception(exc: BaseException) -> str:
    from lionagi._errors import TimeoutError as LionTimeoutError

    if isinstance(exc, KeyboardInterrupt):
        return "aborted"
    if isinstance(exc, (TimeoutError, LionTimeoutError)):
        return "timed_out"
    from lionagi.ln.concurrency.errors import cancelled_exc_classes

    if isinstance(exc, cancelled_exc_classes()):
        return "cancelled"
    return "failed"
