# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared CLI utilities: exit-code mapping, exception classification, PID liveness, entity resolution."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

EXIT_CODE_BY_STATUS: dict[str, int] = {
    "completed": 0,
    # Completion-trust gate: loop exited clean but no commits ahead of base
    # and no artifacts were produced. Exits non-zero so scripts, CI, and
    # schedule on_fail chaining treat it as a failure rather than silently
    # trusting an empty run.
    "completed_empty": 1,
    "failed": 1,
    "timed_out": 124,
    "aborted": 130,
    "cancelled": 143,
}


def validate_cwd_exists(cwd: str | None, *, flag: str = "--cwd") -> str | None:
    """Fail fast when a user-supplied working directory doesn't exist.

    Every CLI surface that forwards a ``cwd``/``repo`` value to a CLI-backed
    agent spawn (claude/codex/gemini-code) must call this BEFORE allocating a
    run or starting the spawn, so a typo'd path produces a clear, immediate
    error instead of the provider layer silently creating the directory (or
    the spawn failing deep inside an opaque subprocess). Raises
    ``ConfigurationError`` (a ``ValueError`` subclass) naming both the path
    and the flag; a caller with no cwd override (``cwd`` falsy) gets it back
    unchanged.

    Returns the tilde-expanded path string, and callers must forward THAT:
    validating ``~/proj`` expanded while forwarding the literal would pass
    here and then fail deep in the provider layer, which never expands.
    """
    if not cwd:
        return cwd
    from lionagi._errors import ConfigurationError

    path = Path(cwd).expanduser()
    if not path.exists():
        raise ConfigurationError(f"{flag} path does not exist: {cwd!r}")
    if not path.is_dir():
        raise ConfigurationError(f"{flag} path is not a directory: {cwd!r}")
    return str(path)


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
