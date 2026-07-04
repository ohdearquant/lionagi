# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared CLI utilities: exit-code mapping, exception classification, PID liveness, entity resolution."""

from __future__ import annotations

import os
from typing import Any

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
    from lionagi.ln.concurrency.utils import SigtermInterrupt

    # SIGTERM is an external termination request, not an internal failure —
    # it lands in the same terminal bucket as a runtime-cancelled task
    # (same reason class, same exit code 143) rather than a new status.
    if isinstance(exc, SigtermInterrupt):
        return "cancelled"
    if isinstance(exc, cancelled_exc_classes()):
        return "cancelled"
    return "failed"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


_SEARCH_ORDER = ("sessions", "invocations", "plays", "shows")

_TABLE_TO_ENTITY_TYPE = {
    "sessions": "session",
    "invocations": "invocation",
    "plays": "play",
    "shows": "show",
}


async def resolve_entity(db: Any, id_or_short: str) -> tuple[str, str, dict[str, Any]] | None:
    id_or_short = id_or_short.strip()
    is_prefix = len(id_or_short) < 36

    for table in _SEARCH_ORDER:
        if is_prefix:
            row = await db.fetch_one(
                f"SELECT * FROM {table} WHERE id LIKE ?",  # noqa: S608
                (id_or_short + "%",),
            )
        else:
            row = await db.fetch_one(
                f"SELECT * FROM {table} WHERE id = ?",  # noqa: S608
                (id_or_short,),
            )
        if row is not None:
            entity_type = _TABLE_TO_ENTITY_TYPE[table]
            return table, entity_type, db._row_to_dict(row)

    return None
