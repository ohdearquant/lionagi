# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared CLI utilities: exit-code mapping, exception classification, PID liveness, entity resolution."""

from __future__ import annotations

import os
import socket
import threading
from collections.abc import Sequence
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

# Deliberately outside the map above: not a run status, since no run has
# started. See docs/internals/cli.md for the exit-code/exception-classification contract.
EXIT_CODE_ENVIRONMENT_ERROR = 78

# Process-wide (not thread-local/ContextVar) because run_async drives each
# command on its own thread with its own event loop, and this flag must
# survive across that boundary. See docs/internals/cli.md for the full
# rationale and the accepted overlap-invocation limitation.
_allocation_lock = threading.Lock()
_invocations_in_flight = 0
_run_allocated = False


def mark_run_allocated() -> None:
    """Record that a run directory now exists for this invocation."""
    global _run_allocated
    with _allocation_lock:
        _run_allocated = True


def begin_invocation() -> None:
    """Enter an invocation, resetting the marker when it is safe to.

    The question the flag answers is whether *this* invocation allocated a run,
    so a process that calls the entry point more than once - the test suite
    does, and so would any in-process embedding - must not carry the first run's
    allocation into the second. Resetting unconditionally would do that, but it
    would also let a second invocation erase a first one's allocation while the
    first is still running, so the reset is skipped whenever anything else is in
    flight.
    """
    global _invocations_in_flight, _run_allocated
    with _allocation_lock:
        if _invocations_in_flight == 0:
            _run_allocated = False
        _invocations_in_flight += 1


def end_invocation() -> None:
    """Leave an invocation, so a later one can reset the marker again."""
    global _invocations_in_flight
    with _allocation_lock:
        if _invocations_in_flight > 0:
            _invocations_in_flight -= 1


def clear_run_allocation() -> None:
    """Reset both the marker and the in-flight count.

    For tests and embeddings that need a known starting state, rather than for
    the entry point, which goes through `begin_invocation`.
    """
    global _run_allocated, _invocations_in_flight
    with _allocation_lock:
        _run_allocated = False
        _invocations_in_flight = 0


def run_was_allocated() -> bool:
    with _allocation_lock:
        return _run_allocated


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


def recorded_pid_is_foreign(metadata: dict[str, Any] | None) -> bool:
    """Whether a run's recorded pid belongs to a different machine.

    A pid is only a name inside one host's pid space. When several hosts share
    a state store, every local check applied to a remote row answers about
    whichever unrelated local process happens to hold that number: it reads as
    a real answer, and it is about the wrong process. So this is asked before
    any pid on such a row is signalled or believed.

    Absence of ``pid_host`` is not foreignness. Rows written before the marker
    existed cannot be placed either way, and treating "unknown origin" as
    "another machine" would stop every one of them from being cancelled or
    swept. The callers that go on to act still have their own identity checks;
    this only removes the rows where those checks could not mean anything.
    """
    if not isinstance(metadata, dict):
        return False
    host = metadata.get("pid_host")
    return isinstance(host, str) and bool(host) and host != socket.gethostname()


# Stands in for a recorded identity mode that is present but not a string.
# Deliberately a string, so every caller's "is this a mode I know" comparison
# keeps working unchanged, and deliberately one no writer produces.
UNRECOGNIZED_IDENTITY_MODE = "<unrecognized>"


def recorded_identity_mode(metadata: dict[str, Any] | None) -> str | None:
    """The run's recorded process identity mode, or None if none was recorded.

    The distinction this preserves is between a row that never carried the
    marker and a row that carries one this code cannot read. Only the first is
    a legacy row that has to be judged by the other checks; the second names a
    stop-and-liveness protocol living somewhere else, and is exactly the case
    every caller here refuses.

    An ``isinstance(mode, str)`` test at the call site collapses the two, and
    it collapses them in the permissive direction: a mode recorded as a number
    or a nested object reads as absent, and the row is then treated as an
    ordinary local one whose pid can be signalled and whose silence can be
    read as death. So the type check happens here, once, and a present
    non-string comes back as `UNRECOGNIZED_IDENTITY_MODE` rather than as None.

    Absence is decided by whether the key is there, not by whether its value
    is None. A row carrying an explicit null is a row that recorded something
    this code cannot read, which is the second case and not the first, and
    reading the value alone puts exactly that row back on the permissive
    branch the paragraph above exists to close.
    """
    if not isinstance(metadata, dict):
        return None
    if "process_identity_mode" not in metadata:
        return None
    mode = metadata["process_identity_mode"]
    return mode if isinstance(mode, str) else UNRECOGNIZED_IDENTITY_MODE


# Boot time is read from the OS on each side of the comparison and can drift by
# a little across clock adjustments and suspend/resume cycles. A real reboot
# moves it by far more than this, so the tolerance costs nothing and avoids
# reading jitter as a reboot.
#
# It lives here, beside the host check, rather than next to either caller,
# because both the kill path and the liveness probe answer the same question
# about the same recorded value. Two copies of a number that has to agree is
# how one of them ends up being a process create-time tolerance instead.
BOOT_TIME_TOLERANCE = 5.0


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
    """A short id prefix matched more than one record.

    Carries the colliding ids so every CLI surface can tell the user what to
    disambiguate between instead of silently acting on one of them. `table` is
    None when the collision is across kinds rather than inside one, since the
    candidates then name their own kind and a single table would misdescribe it.
    """

    def __init__(self, id_or_short: str, table: str | None, candidates: list[str]) -> None:
        self.id_or_short = id_or_short
        self.table = table
        self.candidates = list(candidates)
        shown = self.candidates[:_CANDIDATES_SHOWN]
        listed = ", ".join(shown)
        if len(self.candidates) > len(shown):
            listed += ", ..."
        what = f"more than one {table} record" if table else "records of more than one kind"
        super().__init__(
            f"ambiguous id prefix {id_or_short!r} — matches {what} "
            f"({listed}); use a longer prefix or the full id"
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

    row = await _fetch_exact_row(db, table, id_or_short)
    if row is not None:
        return row

    rows = await _fetch_prefix_rows(db, table, id_or_short)
    if not rows:
        return None
    if len(rows) > 1:
        raise AmbiguousIdError(id_or_short, table, [r["id"] for r in rows])
    return rows[0]


async def _fetch_exact_row(db: Any, table: str, id_or_short: str) -> dict[str, Any] | None:
    return await db.fetch_one(
        f"SELECT * FROM {table} WHERE id = ?",  # noqa: S608
        (id_or_short,),
    )


async def _fetch_prefix_rows(db: Any, table: str, id_or_short: str) -> list[dict[str, Any]]:
    """Rows of *table* whose id starts with *id_or_short*, case-sensitively.

    LIKE alone is not enough: on the default backend it compares ASCII
    case-insensitively, so an upper-cased prefix would match a lower-cased id
    while the exact comparison beside it would not. It is kept as the first
    predicate because it is the one an index on `id` can use; the substring
    equality after it is what actually decides.
    """
    return await db.fetch_all(
        f"SELECT * FROM {table} WHERE id LIKE ? ESCAPE '\\' "  # noqa: S608
        f"AND substr(id, 1, ?) = ? "
        f"ORDER BY id LIMIT {_CANDIDATES_SHOWN + 1}",
        (_like_prefix_pattern(id_or_short), len(id_or_short), id_or_short),
    )


async def resolve_entity(
    db: Any, id_or_short: str, tables: Sequence[str] = _SEARCH_ORDER
) -> tuple[str, str, dict[str, Any]] | None:
    """Find the one record holding *id_or_short*, across every entity kind.

    An exact id is a primary key and settles it outright, in *tables* order.
    A prefix does not: it is a guess, and a guess that fits a session and an
    invocation equally well has no correct winner. Search order cannot break
    that tie, because ordering is about where to look first, not about which of
    two equally good matches the caller meant. So prefixes are gathered from
    every kind and a collision across kinds raises `AmbiguousIdError`, exactly
    as a collision inside one does. The alternative is a resolver that rejects
    ambiguity in one direction and silently picks in the other, which teaches
    callers to trust a prefix that resolves.

    *tables* narrows which kinds are considered, for callers that answer about
    a subset. It is the kinds a caller searches, not the policy it searches
    them under: a caller with its own list still gets the same exact-first,
    refuse-a-collision behaviour, which is the whole point of it living here.
    """
    id_or_short = id_or_short.strip()
    if not id_or_short:
        return None

    for table in tables:
        row = await _fetch_exact_row(db, table, id_or_short)
        if row is not None:
            return table, _TABLE_TO_ENTITY_TYPE[table], db._row_to_dict(row)

    hits: list[tuple[str, dict[str, Any]]] = []
    for table in tables:
        hits.extend((table, row) for row in await _fetch_prefix_rows(db, table, id_or_short))

    if not hits:
        return None
    if len(hits) > 1:
        kinds = {table for table, _ in hits}
        raise AmbiguousIdError(
            id_or_short,
            _TABLE_TO_ENTITY_TYPE[next(iter(kinds))] if len(kinds) == 1 else None,
            [
                row["id"] if len(kinds) == 1 else f"{_TABLE_TO_ENTITY_TYPE[table]} {row['id']}"
                for table, row in hits
            ],
        )

    table, row = hits[0]
    return table, _TABLE_TO_ENTITY_TYPE[table], db._row_to_dict(row)
