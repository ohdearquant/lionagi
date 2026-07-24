# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared CLI utilities: exit-code mapping, exception classification, PID liveness, entity resolution."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

EXIT_CODE_BY_STATUS: dict[str, int] = {
    "completed": 0,
    # Completion-trust gate: no commits/artifacts. See docs/internals/cli.md.
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

    # SIGTERM shares the cancelled bucket (exit 143), not a new status.
    # See docs/internals/cli.md.
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

# How many colliding ids an ambiguity message lists before it truncates.
_CANDIDATES_SHOWN = 5


class AmbiguousIdError(ValueError):
    """A short id prefix matched more than one row in one table.

    Carries the colliding ids so every CLI surface can tell the user what to
    disambiguate between instead of silently acting on one of them.
    """

    def __init__(self, id_or_short: str, table: str, candidates: list[str]) -> None:
        self.id_or_short = id_or_short
        self.table = table
        self.candidates = list(candidates)
        shown = self.candidates[:_CANDIDATES_SHOWN]
        listed = ", ".join(shown)
        if len(self.candidates) > len(shown):
            listed += ", ..."
        super().__init__(
            f"ambiguous id prefix {id_or_short!r} — matches more than one "
            f"{table} record ({listed}); use a longer prefix or the full id"
        )


def _like_prefix_pattern(id_or_short: str) -> str:
    """Escape LIKE metacharacters so a prefix is matched literally."""
    escaped = id_or_short.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


async def fetch_unique_row(db: Any, table: str, id_or_short: str) -> dict[str, Any] | None:
    """Resolve one id (or short prefix) to a single row of *table*.

    Exact id wins outright — it is the primary key, so it cannot be ambiguous.
    Otherwise the value is treated as a prefix, and a prefix matching more than
    one row raises `AmbiguousIdError` rather than picking one: a `LIKE` query
    plus a fetch-one has no cardinality check and no ordering rule, so the row
    it returns is whichever the engine happens to yield first. Rows are ordered
    by id so the candidate list an error reports is stable.

    Returns the raw row dict (JSON columns still encoded); callers that need
    decoded columns pass it through `db._row_to_dict`.
    """
    id_or_short = id_or_short.strip()
    if not id_or_short:
        return None

    row = await db.fetch_one(
        f"SELECT * FROM {table} WHERE id = ?",  # noqa: S608
        (id_or_short,),
    )
    if row is not None:
        return row

    rows = await db.fetch_all(
        f"SELECT * FROM {table} WHERE id LIKE ? ESCAPE '\\' "  # noqa: S608
        f"ORDER BY id LIMIT {_CANDIDATES_SHOWN + 1}",
        (_like_prefix_pattern(id_or_short),),
    )
    if not rows:
        return None
    if len(rows) > 1:
        raise AmbiguousIdError(id_or_short, table, [r["id"] for r in rows])
    return rows[0]


async def resolve_entity(db: Any, id_or_short: str) -> tuple[str, str, dict[str, Any]] | None:
    """Sweep the entity tables in order for the first one holding *id_or_short*.

    Raises `AmbiguousIdError` when the prefix collides inside a table. A prefix
    that matches one row in each of two different tables still resolves to the
    earlier table in `_SEARCH_ORDER` — that shadowing is the documented
    search-order contract, not a cardinality failure.
    """
    for table in _SEARCH_ORDER:
        row = await fetch_unique_row(db, table, id_or_short)
        if row is not None:
            entity_type = _TABLE_TO_ENTITY_TYPE[table]
            return table, entity_type, db._row_to_dict(row)

    return None
