# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Engine factory for the StateDB backend — normalises URLs and creates AsyncEngine instances."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from bisect import bisect_left
from collections.abc import Generator
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse, urlunparse

from lionagi._paths import LIONAGI_HOME

_log = logging.getLogger(__name__)


def _busy_timeout_from_env() -> int:
    """The sqlite busy_timeout (ms) this deployment asked for, or the default.

    The default (5000) is sized for the test suite -- a test holding a write
    lock deliberately should fail fast, not wait out a production timeout --
    and for years was silently also the production value. This is a
    deployment property, read from the environment so a daemon's launch
    config can set it high while the test suite sets nothing. An unusable,
    zero, or negative value falls back to the default and logs a warning,
    rather than refusing to start over a malformed tuning knob; zero would
    mean "never wait", turning every momentary lock into an error.
    """
    raw = os.environ.get("LIONAGI_SQLITE_BUSY_TIMEOUT_MS")
    if raw is None:
        return 5000
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value <= 0:
        _log.warning(
            "LIONAGI_SQLITE_BUSY_TIMEOUT_MS=%r is not a positive integer; using 5000",
            raw,
        )
        return 5000
    return value


# sqlite busy_timeout (ms) applied to every connection this process opens against
# the store, whether through this engine or through the Studio connection helper.
# A module attribute rather than a frozen local because the pragma listeners read
# it at connection time — tests that need a different wait retune it here.
SQLITE_BUSY_TIMEOUT_MS = _busy_timeout_from_env()

_busy_timeout_announced = False


def announce_busy_timeout() -> None:
    """Log once per process what busy_timeout this process's connections use,
    and whether it came from the environment or the built-in default.

    Recomputed at call time rather than recorded at import, since
    ``SQLITE_BUSY_TIMEOUT_MS`` is writable (tests retune it) and a
    provenance captured at import would keep claiming the environment's
    answer after something else replaced it.
    """
    global _busy_timeout_announced
    if _busy_timeout_announced:
        return
    _busy_timeout_announced = True

    effective = SQLITE_BUSY_TIMEOUT_MS
    raw = os.environ.get("LIONAGI_SQLITE_BUSY_TIMEOUT_MS")
    if raw is None:
        _log.info(
            "sqlite busy_timeout is %dms, the built-in default: "
            "LIONAGI_SQLITE_BUSY_TIMEOUT_MS is not set for this process",
            effective,
        )
        return
    try:
        asked = int(raw)
    except ValueError:
        asked = None
    if asked is not None and asked > 0 and asked == effective:
        _log.info(
            "sqlite busy_timeout is %dms, from LIONAGI_SQLITE_BUSY_TIMEOUT_MS",
            effective,
        )
    elif asked is None or asked <= 0:
        # Also warned when the value was first read, which happens at import —
        # possibly before this process configured logging at all. Repeating it
        # here is the difference between a warning that was emitted and one that
        # was seen.
        _log.warning(
            "sqlite busy_timeout is %dms: LIONAGI_SQLITE_BUSY_TIMEOUT_MS=%r is not usable",
            effective,
            raw,
        )
    else:
        _log.info(
            "sqlite busy_timeout is %dms, set in-process; "
            "LIONAGI_SQLITE_BUSY_TIMEOUT_MS asks for %dms",
            effective,
            asked,
        )


def has_wal_reset_fix(version_info: tuple[int, ...]) -> bool:
    """Whether a SQLite version is fixed or predates WAL; see the runtime WAL policy."""
    # Likely affected range 3.7.0-3.51.2 and the branch-specific backports below:
    # https://www.sqlite.org/wal.html#walresetbug
    # https://www.sqlite.org/releaselog/3_51_3.html
    v = tuple(version_info[:3])
    if v >= (3, 51, 3):
        return True
    if v[:2] == (3, 44) and v >= (3, 44, 6):
        return True
    if v[:2] == (3, 50) and v >= (3, 50, 7):
        return True
    return v < (3, 7, 0)  # predates WAL entirely; journal_mode=WAL is not honoured


_wal_reset_warning_emitted = False


def _warn_if_wal_reset_unfixed() -> None:
    """Warn once about possible WAL use on a runtime without a known WAL-reset fix."""
    global _wal_reset_warning_emitted
    if _wal_reset_warning_emitted or has_wal_reset_fix(sqlite3.sqlite_version_info):
        return
    _wal_reset_warning_emitted = True
    _log.warning(
        "Linked SQLite %s may be exposed to the WAL-reset corruption race "
        "(fixed in 3.51.3; backported to 3.44.6 and 3.50.7). Concurrent WAL connections "
        "may trigger this race. Compatibility opens leave journal_mode unchanged "
        "and refuse non-WAL stores. Use a Python runtime linked to a fixed "
        "SQLite; see the exposure conditions and temporary serialization limits at "
        "https://github.com/ohdearquant/lionagi/blob/main/"
        "docs/internals/runtime.md#sqlite-wal-reset-policy.",
        sqlite3.sqlite_version,
    )


_NOT_STOPPED = object()


class _RollbackOnCloseConnection(sqlite3.Connection):
    """End a transaction before native close can defer it behind retained cursors."""

    def close(self) -> None:
        try:
            in_transaction = self.in_transaction
        except sqlite3.ProgrammingError:
            # Preserve sqlite3's idempotent close (and its wrong-thread error).
            return super().close()
        try:
            if in_transaction:
                self.rollback()
        except Exception:
            _log.warning("Could not roll back SQLite transaction before closing", exc_info=True)
        finally:
            super().close()


@functools.cache
def _stop_once_class(cls: type) -> type:
    """A subclass of the aiosqlite connection class *cls* that stops once.

    Every ``stop()`` returns the first call's future, and once stopped, the
    connection refuses new work with "no active connection", the error aiosqlite
    gives once its sqlite3 connection is gone.
    """

    class StopOnceConnection(cls):
        _stops_once = True

        def stop(self):
            future = getattr(self, "_first_stop", _NOT_STOPPED)
            if future is _NOT_STOPPED:
                future = self._first_stop = super().stop()
            return future

        async def _execute(self, fn, *args, **kwargs):
            if getattr(self, "_first_stop", _NOT_STOPPED) is not _NOT_STOPPED:
                raise ValueError("no active connection")
            return await super()._execute(fn, *args, **kwargs)

        @property
        def _conn(self):
            # SQLAlchemy sets the isolation level by queueing work on the driver itself, past
            # _execute; behind a stop that work would never run. The driver thread still reads
            # the connection for work queued before the stop, which runs as it did; any other
            # thread is refused, so interrupt() is refused after a stop too.
            if getattr(self, "_first_stop", _NOT_STOPPED) is not _NOT_STOPPED and (
                threading.current_thread() is not getattr(self, "_thread", None)
            ):
                raise ValueError("no active connection")
            return super()._conn

    return StopOnceConnection


def _stop_aiosqlite_once(driver_connection) -> None:
    """Let an aiosqlite connection be stopped and then closed without hanging.

    When a cancel breaks a statement, SQLAlchemy discards the connection: it asks
    for a graceful ``close()``, which queues behind the statement still running on
    the driver thread, and if a second cancel interrupts that wait it falls back
    to ``stop()``. aiosqlite 0.22.1 ends ``close()`` with its own ``stop()`` and
    waits for that future, but the worker thread exits at the first stop it takes
    from its queue, so the second one never runs and the cleanup waits forever,
    holding the pooled connection and, in a StateDB write, the write lock. Every
    later ``stop()`` here returns the first one's future.

    The same interruption leaves SQLAlchemy's connection unmarked, so it goes on
    to roll back through the stopped driver. aiosqlite answers that with
    "Connection closed", which SQLAlchemy does not recognise as a dead connection,
    so the pooled connection is never returned. Work refused after a stop raises
    "no active connection" instead, the error aiosqlite gives once its sqlite3
    connection is gone, which SQLAlchemy reads as a lost connection when a
    statement raises it.

    Releases before 0.22.1 have no ``stop()`` for SQLAlchemy to fall back to, so
    they are left alone.
    """
    import aiosqlite

    cls = type(driver_connection)
    if not isinstance(driver_connection, aiosqlite.Connection) or getattr(
        cls, "_stops_once", False
    ):
        return
    if not callable(getattr(cls, "stop", None)):
        return
    try:
        driver_connection.__class__ = _stop_once_class(cls)
    except TypeError:
        # A layout this subclass cannot take (slots added upstream): keep the
        # driver's own behaviour, and say so.
        _log.warning(
            "%s cannot take the stop-once subclass; a second cancel during a"
            " connection's cleanup can leave that cleanup waiting forever",
            cls.__qualname__,
        )


# How long a failed open waits for its driver worker to exit. The worker finishes
# the failed connector and then runs the stop the driver queued, so it normally
# exits at once; a connector that never returns (a hung filesystem) would hold it,
# and past this bound the open fails with that worker still running.
_FAILED_OPEN_WORKER_JOIN_SECONDS = 5.0


class _SQLiteConnectionAwaitable:
    """Keep a failed driver connection until its worker has exited, within a bound.

    Reads the driver's private ``Connection._thread``, present in the aiosqlite
    releases the dependency range admits.
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    @property
    def daemon(self) -> bool:
        return self._connection._thread.daemon

    @daemon.setter
    def daemon(self, value: bool) -> None:
        # Older SQLAlchemy versions set daemon on the awaitable itself.
        self._connection._thread.daemon = value

    def __await__(self) -> Generator[Any, None, Any]:
        return self._connect().__await__()

    async def _connect(self) -> Any:
        try:
            return await self._connection
        except BaseException as failed_open:
            from lionagi.ln.concurrency import CancelScope, run_sync

            worker = self._connection._thread
            if worker.ident is not None:
                # The driver's failed await has already queued its stop. Join off-loop,
                # retaining this connection through a cancellation during the cleanup.
                interrupted = None
                with CancelScope(shield=True):
                    joining = asyncio.create_task(
                        run_sync(worker.join, _FAILED_OPEN_WORKER_JOIN_SECONDS)
                    )
                    while not joining.done():
                        try:
                            await asyncio.shield(joining)
                        except asyncio.CancelledError as exc:
                            interrupted = exc
                    joining.result()
                if worker.is_alive():
                    _log.warning(
                        "SQLite driver worker still running %.1f s after a failed"
                        " open; leaving it running",
                        _FAILED_OPEN_WORKER_JOIN_SECONDS,
                    )
                if interrupted is not None:
                    raise interrupted from failed_open
            raise


class _OwnedSQLiteDriver:
    """A per-engine driver view; the imported aiosqlite module is unchanged."""

    def __init__(self, driver: Any) -> None:
        self._driver = driver

    def __getattr__(self, name: str) -> Any:
        return getattr(self._driver, name)

    def connect(self, *args: Any, **kwargs: Any) -> _SQLiteConnectionAwaitable:
        return _SQLiteConnectionAwaitable(self._driver.connect(*args, **kwargs))


def _configure_sqlite_module(url: str, kwargs: dict[str, Any]) -> None:
    if not url.startswith("sqlite+aiosqlite://") or kwargs.get("module") is not None:
        return
    from sqlalchemy.dialects.sqlite.aiosqlite import SQLiteDialect_aiosqlite

    dbapi = SQLiteDialect_aiosqlite.import_dbapi()
    dbapi.aiosqlite = _OwnedSQLiteDriver(dbapi.aiosqlite)
    kwargs["module"] = dbapi


def _json_serializer(obj):
    if isinstance(obj, uuid.UUID):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _dumps_with_uuid(value):
    """Serializer for every JSON bind on every engine this module builds.

    ``allow_nan=False`` makes the encoder raise on inf, -inf and nan instead of
    writing the non-standard ``Infinity``/``NaN`` literals into durable storage,
    where nothing downstream can read them back as JSON. It is a flag on the
    encode pass that already runs, not an extra traversal of the payload.
    """
    return json.dumps(value, default=_json_serializer, allow_nan=False)


# A value with no scheme is a filesystem path. That is the documented case and
# almost always what was meant. But `user:secret@host/db` with the scheme left
# off is also scheme-less, and resolving it as a path creates a database file
# whose *name* carries the credential, at a location nobody chose, while the
# server it was meant to reach is never contacted. Nothing about that failure
# announces itself: the store opens, it is empty, and the daemon runs.
#
# The shape being matched is a URL's userinfo prefix, `something:something@`
# before any slash. A filename may legally contain an `@` — `user@host.db` is
# an odd but valid name — so the colon before it is what separates a credential
# from a filename, and this must not fire on the latter. Nothing beginning with
# `./` or `/` can match, which is the way to spell a path that really does look
# like this. A secret holding a `/`, a `:` or whitespace is not matched, so such
# a value is still read as a path.
_CREDENTIALED_USERINFO = re.compile(r"^[^\s:/@]+:[^\s:/@]+@([^\s/@]+)")


def _reject_schemeless_credentials(value: str) -> None:
    """Refuse a scheme-less value shaped like a credentialed connection string.

    Refusing rather than warning, because there is no reading of this value
    under which resolving it is the right thing to do. Every outcome of going
    ahead is wrong: the server named in it is not contacted, the store that
    does open is empty, and the credential is written into a file name where it
    outlives the process that was misconfigured. A warning leaves all three in
    place and asks somebody to be reading the log at the right moment.
    """
    match = _CREDENTIALED_USERINFO.match(value)
    if match is None:
        return
    raise ValueError(
        "The state store URL has no scheme, so it would be read as a filesystem "
        f"path and a SQLite file created from it. It has the shape of a connection "
        f"string to {match.group(1)} with credentials in front, so the credentials "
        "would become part of a file name on disk and the server would never be "
        "contacted. Add the scheme (postgresql:// for a server), or prefix the "
        "value with ./ if it really is a relative path."
    )


def normalize_state_db_url(value: str | Path | None) -> str:
    """Resolve *value* to a fully-qualified async SQLAlchemy URL string."""
    if value is None:
        db_path = (LIONAGI_HOME / "state.db").resolve()
        return f"sqlite+aiosqlite:///{db_path}"

    if isinstance(value, Path):
        return f"sqlite+aiosqlite:///{value.resolve()}"

    s = str(value)

    if s == ":memory:":
        return "sqlite+aiosqlite:///:memory:"

    if "://" not in s:
        _reject_schemeless_credentials(s)
        return f"sqlite+aiosqlite:///{Path(s).resolve()}"

    if s.startswith("sqlite+aiosqlite://") or s.startswith("postgresql+asyncpg://"):
        return s

    if s.startswith("sqlite:///"):
        return "sqlite+aiosqlite:" + s[len("sqlite:") :]

    if s.startswith("postgres://") or s.startswith("postgresql://"):
        parsed = urlparse(s)
        replaced = parsed._replace(scheme="postgresql+asyncpg")
        return urlunparse(replaced)

    return s


# A URL's password as SQLAlchemy reads it runs from the user's `:` to the first
# `@`; written unencoded it may hold a `/`, `?`, `#` or `:`, and the user name an
# `@`, as SQLAlchemy reads them. A parser that splits the user from the host at a
# later `@` reads it further, urlsplit to the last `@` before the first `/`, `?`
# or `#`; the span to the last `@` before whitespace covers every such reading
# in which neither the user name nor the password holds whitespace and is masked
# too, and an `@` after the host, in a query value say, takes the host with it.
# In prose whitespace ends the URL. A scheme is tried only where a run of scheme
# characters begins, so no stretch of text is scanned once per position in it.
_URL_USER = re.compile(r"(?<![\w+.-])[.-]*[\w+][\w+.-]*://[^\s:/]*:")

# The same for a URL given alone, with no prose after it to end the password:
# whitespace and line breaks are part of it. A dialect name may hold or start
# with `_` (`oracle+cx_oracle`), as SQLAlchemy reads it. C0 control characters
# and spaces before the scheme are passed over, as urlsplit strips them, and so is
# a tab or line break inside the scheme or its `://`, which urlsplit removes
# wherever it stands.
_WHOLE_URL_USER = re.compile(r"[\x00-\x20]*[\w+][\w+.\t\r\n-]*:[\t\r\n]*/[\t\r\n]*/[^:/]*:")

# A `user:secret@` credential outside a URL. The secret runs to the first `@`
# and may not contain whitespace, a slash, a backslash, a quote or a second
# colon, which is what keeps this off `sqlite:///path` (no `@`) and off an
# ordinary `scheme://host` (no colon before the host).
_CREDENTIAL_IN_TEXT = re.compile(r"(?<![\w.+~%-])([\w.+~%-]+):([^\s:@/\\'\"]+)@")

# A query parameter's key, found at every `?` and `&`, so one standing inside
# another's value is found too. libpq and asyncpg read a `password` parameter as
# the password, and a key ending in `password` is masked too, as the keyword
# reading below masks one. The key is compared percent-decoded, as SQLAlchemy
# decodes it, and in any case, which is wider than SQLAlchemy's exact match. SQLAlchemy reads
# the value to the next `&`, a `#`, a space or a carriage return included, and
# ends it at a newline; a URL given alone has it masked to the next `&` whatever
# it holds, and prose to the next `&` or whitespace. A tab or line break in the
# key is dropped before the compare, as urlsplit drops it wherever it stands, so
# asyncpg reads `?\rpassword=` as the password. Only the key: in prose a value,
# like a password, still ends at whitespace, a tab or line break included. That
# whitespace is ASCII only: a no-break or other Unicode space, which the drivers
# read as part of the value, stays inside it.
_QUERY_KEY = re.compile(r"(?=[?&]((?:[^?&=\s]|[\t\r\n])*)=)")
_KEY_BREAK = re.compile(r"[\t\r\n]")
_VALUE_END = re.compile(r"[&\s]", re.ASCII)
_WHOLE_VALUE_END = re.compile(r"&")
_SPACE = re.compile(r"\s")

# A key ending in `password`, in any case, written `key=value` outside a URL
# query: a keyword/value connection string (`host=db password=secret`), an
# environment assignment (`PGPASSWORD=secret`), a flag (`--db-password=secret`).
# After a `?` the key is a query parameter, read above instead. Whitespace
# around the `=` is passed over, as libpq passes it over. The value is read the
# way libpq reads a connection string's value and the way a shell reads a word,
# once with `$'..'` quoting and once without it, since not every shell has it,
# and masked to the end of the longest reading and on to the next space, tab or
# line feed:
#   libpq: quoted, to the first `'` a backslash does not escape; unquoted, to
#   the first whitespace a backslash does not escape, so a `;` or `)` is in it.
#   a shell: to the first space, tab, line feed or one of `|&;)<>` that is
#   neither escaped nor quoted, so a `,`, `]` or `}` is in it. A `'..'` part
#   runs to the next `'` and has no escapes; a `$'..'` part, where a shell has
#   that quoting, runs to the next `'` a backslash does not escape.
#   `ab'cd ef'gh` is one word, and `'a\'b'c d'` reads as `a\bc d`. A backtick
#   part runs to the next backtick a backslash does not escape. A `".."` part
#   runs to the next `"`, and a `${..}` part to the next `}`, that is neither
#   escaped nor quoted nor inside a part it holds, so `"a b"` and `${PW:-a b}`
#   are each one part. In a `${..}` part a `"` or `${` opens a part of its own,
#   and so does a `{`, to its own `}`, as zsh reads it (bash and dash end the
#   part at the first `}`). In a `".."` part a `${` does, an escape and a
#   backtick part are read, and a `'` or `$'` is a plain character; in a `${..}`
#   part inside it bash reads a `'` as a quote, and other shells as a plain
#   character, so the two shell readings take each way. Where some parts end,
#   only a shell's command grammar finds (a `case` pattern's `)`, a comment, a
#   here-document), and none of these is read: a `$(..)` or `$[..]` part, bash's
#   `${ ..; }` and `${| ..; }`, a `<(..)` or `>(..)` part outside a `".."` part
#   and, for bash, in a `${..}` part inside one, and in the word itself whatever
#   an unquoted `(` opens (zsh's `=(..)`, an extended glob's `@(..)`) each run to
#   the end of the text. So `$(cat pwfile) psql` is masked whole.
# After a `&` the key is a query parameter, read above, or follows a shell's `&`,
# so it is read here as a shell reads it too, but no further than the next `&`,
# where the query parameter's value ends, and masked on to the next space, tab,
# line feed or `&`. So a value holding a quoted or escaped `&` is masked only up
# to that `&`.
# libpq separates its keywords with whitespace only, so its reading starts no
# value there.
# A quote that is never closed runs to the end of the text. Every reading is
# ASCII, so a no-break or other Unicode space stays inside the value. Python's
# string literals are read none of these ways: Python joins `'a' 'b c'` into one
# string and keeps the backslash in `r'a\'b c'`, and both are masked only to the
# space. A key standing inside another key's value, as in
# `password=ab;password='c d'`, is read as well.
_PASSWORD_KEY = re.compile(r"(?<![\w.?-])[\w.-]*password\s*=\s*", re.IGNORECASE | re.ASCII)
_BLANK = re.compile(r"[ \t\n]")
_BLANK_OR_AMPERSAND = re.compile(r"[ \t\n&]")

# Each reading, in its plain state, outside quotes and escapes, reads up to the
# next character that may end that state (its stop pattern). A part may start
# there: an escape, a `'..'` or `$'..'` part or a backtick part (its part
# pattern), after which it is plain again; or an opening, whose part a reading
# of its own reads inside, to the character that closes it, after which this
# reading is plain again past that character. Where neither starts, the reading
# ends: the value at whitespace, or a backslash that ends the text, for libpq,
# and at a space, tab, line feed or one of `|&;)<>` for a shell; a part read
# inside at the `"` or `}` that closes it. A part read to the end of the text
# is not read at all.
_LIBPQ_QUOTED = re.compile(r"'(?:\\.|[^'\\])*'?", re.DOTALL)
_LIBPQ_STOP = re.compile(r"[\s\\]", re.ASCII)
_LIBPQ_PART = re.compile(r"\\.", re.DOTALL)
_SHELL_PART = re.compile(r"""\\.?|'[^']*'?|`(?:\\.|[^`\\])*`?""", re.DOTALL)
_BASH_PART = re.compile(r"""\\.?|'[^']*'?|`(?:\\.|[^`\\])*`?|\$'(?:\\.|[^'\\])*'?""", re.DOTALL)
_QUOTED_PART = re.compile(r"""\\.?|`(?:\\.|[^`\\])*`?""", re.DOTALL)
# what an opening hands a part read to the end of the text, which no reading here
# reads: where it ends, only a shell's command grammar finds
_REST = "the rest of the text"
_REST_QUOTED = {"$(": _REST, "$[": _REST, **dict.fromkeys(("${ ", "${\t", "${\n", "${|"), _REST)}
_REST_UNQUOTED = {**_REST_QUOTED, "<(": _REST, ">(": _REST, "(": _REST}
_SHELL_OPENINGS = {**_REST_UNQUOTED, '"': "shell quoted", "${": "shell expansion"}
_BASH_OPENINGS = {**_REST_UNQUOTED, '"': "bash quoted", "${": "bash expansion"}
# in a `${..}` part zsh reads an unquoted `{` as opening a part to its own `}`, where
# bash and dash end the part at the first `}`; the part is read zsh's way, the longer.
# The bash reading reads it that way too, so there it can mask past the `}` where bash
# ends the word, never short of it
_SHELL_EXPANSION_OPENINGS = {**_SHELL_OPENINGS, "{": "shell expansion"}
_BASH_EXPANSION_OPENINGS = {**_BASH_OPENINGS, "{": "bash expansion"}
# name: (stop pattern, part pattern, the reading each opening's part is read by). A
# reading meets an opening only where its stop pattern stops: a `(` alone is one in
# the word itself, not in a `${..}` part.
_READINGS: dict[str, tuple[re.Pattern[str], re.Pattern[str], dict[str, str]]] = {
    "libpq": (_LIBPQ_STOP, _LIBPQ_PART, {}),
    "shell": (re.compile(r"""[ \t\n|&;()<>\\'"`]|\$[({\[]"""), _SHELL_PART, _SHELL_OPENINGS),
    "shell expansion": (
        re.compile(r"""[{}\\'"`]|\$[({\[]|[<>]\("""),
        _SHELL_PART,
        _SHELL_EXPANSION_OPENINGS,
    ),
    "shell quoted": (
        re.compile(r"""[\\"`]|\$[({\[]"""),
        _QUOTED_PART,
        {**_REST_QUOTED, "${": "shell quoted expansion"},
    ),
    "shell quoted expansion": (
        re.compile(r"""[}\\"`]|\$[({\[]"""),
        _QUOTED_PART,
        {**_REST_QUOTED, '"': "shell quoted", "${": "shell quoted expansion"},
    ),
    "bash": (re.compile(r"""[ \t\n|&;()<>\\'"`]|\$[('{\[]"""), _BASH_PART, _BASH_OPENINGS),
    "bash expansion": (
        re.compile(r"""[{}\\'"`]|\$[('{\[]|[<>]\("""),
        _BASH_PART,
        _BASH_EXPANSION_OPENINGS,
    ),
    "bash quoted": (
        re.compile(r"""[\\"`]|\$[({\[]"""),
        _QUOTED_PART,
        {**_REST_QUOTED, "${": "bash quoted expansion"},
    ),
    # bash reads a `<(` or `>(` here as a process substitution, which other shells
    # inside `".."` read as plain characters
    "bash quoted expansion": (
        re.compile(r"""[}\\'"`]|\$[('{\[]|[<>]\("""),
        _BASH_PART,
        {
            **_REST_QUOTED,
            "<(": _REST,
            ">(": _REST,
            '"': "bash quoted",
            "${": "bash quoted expansion",
        },
    ),
}


class _ValueEnds:
    """Where each reading of a keyword's value ends, for values starting
    anywhere in *text*.

    A reading is in its plain state where a value starts and after each part
    it reads, and from a position in that state it ends in the same place
    whichever value it came from. So each reading notes, for each position it
    passed in that state, where it ended, and a later value's reading that
    starts, or resumes after a part, at a noted position ends there too. A
    reading inside a quoted part opened it at the same quote whichever value
    it came from, and passed that quote in its plain state, so no quoted part
    is read twice either. What an opening's part holds is read by a reading of
    its own, which ends at the character that closes the part and is noted
    the same way; a part read to the end of the text is not read. However
    many values overlap, each position is read a bounded number of times.
    The readings waiting on a part are kept in a list, not on the call
    stack, so parts nested however deep are read.
    """

    def __init__(self, text: str) -> None:
        self._text = text
        self._noted: dict[str, list[int | None]] = {}
        # 1 where `_noted` holds an end, so the next noted position is found by
        # `bytearray.find`, not by stepping through the list
        self._is_noted: dict[str, bytearray] = {}
        self._ends: list[int] = []
        self._found: dict[re.Pattern[str], list[int]] = {}

    def next_match(self, pattern: re.Pattern[str], pos: int) -> int:
        """Where *pattern* next matches at or after *pos*, or the end of the text:
        all matches are found once, so a search per value does not rescan."""
        found = self._found.get(pattern)
        if found is None:
            found = self._found[pattern] = [m.start() for m in pattern.finditer(self._text)]
            found.append(len(self._text))
        return found[bisect_left(found, pos)]

    def libpq(self, start: int) -> int:
        if quoted := _LIBPQ_QUOTED.match(self._text, start):
            # another quoted value can start in this one only at the quote that
            # closes it, which no backslash escapes, so no quoted part is read twice
            return quoted.end()
        return self._read(start, "libpq")

    def shell(self, start: int) -> int:
        return self._read(start, "shell")

    def bash(self, start: int) -> int:
        return self._read(start, "bash")

    @staticmethod
    def _plain_to(
        text: str, pos: int, stops: re.Pattern[str], is_noted: bytearray
    ) -> tuple[int, int | None]:
        """Where a reading in its plain state at *pos* goes next: ``(stop, None)``
        at the next character its stop pattern matches, or the end of the text, or
        ``(pos, seen)`` at a position it passed in that state for an earlier value,
        if that comes first. The text is searched in windows that double, so the
        cost follows the distance to whichever comes first, not to a stop far past
        a position already read."""
        n = len(text)
        lo, width = pos, 64
        while True:
            hi = min(lo + width, n + 1)
            seen = is_noted.find(1, lo, hi)
            # a stop is at most two characters, so one starting before `hi` is found
            found = stops.search(text, lo, min(hi + 1, n))
            first = found.start() if found and found.start() < hi else None
            if seen != -1 and (first is None or seen <= first):
                return pos, seen
            if first is not None:
                return first, None
            if hi > n:
                return n, None
            lo, width = hi, width * 2

    def _read(self, start: int, reading: str) -> int:
        text = self._text
        waiting: list[tuple[str, list[tuple[int, int]]]] = []
        passed: list[tuple[int, int]] = []
        pos = start
        while True:
            if reading == _REST:
                end = len(text)
            else:
                noted = self._noted.get(reading)
                if noted is None:
                    noted = self._noted[reading] = [None] * (len(text) + 1)
                    self._is_noted[reading] = bytearray(len(text) + 1)
                is_noted = self._is_noted[reading]
                if (known := noted[pos]) is None:
                    stops, part, openings = _READINGS[reading]
                    stop, seen = self._plain_to(text, pos, stops, is_noted)
                    if seen is not None:
                        # plain up to a position this reading passed in the same state
                        # for an earlier value, so it ends where that one did
                        passed.append((pos, seen - 1))
                        end = self._ends[noted[seen]]  # type: ignore[index]
                    else:
                        passed.append((pos, stop))
                        if opening := next((o for o in openings if text.startswith(o, stop)), None):
                            waiting.append((reading, passed))
                            reading, passed, pos = openings[opening], [], stop + len(opening)
                            continue
                        if m := part.match(text, stop):
                            pos = m.end()
                            continue
                        end = stop
                else:
                    end = self._ends[known]
                self._ends.append(end)
                for first, last in passed:
                    noted[first : last + 1] = [len(self._ends) - 1] * (last + 1 - first)
                    is_noted[first : last + 1] = b"\x01" * (last + 1 - first)
            if not waiting:
                return end
            # past the character that closes the part, or at the end of the text if
            # none does
            reading, passed = waiting.pop()
            pos = min(end + 1, len(text))


# What a masked secret becomes. No part of it survives: a prefix of a password
# is still part of the password.
_MASK = "***"


def mask_credentials(text: str) -> str:
    """Return *text* with every secret it finds replaced by ``***``.

    For prose as well as a URL -- an error message that quotes the store it
    failed to open carries the credential as plainly as the URL field beside it.
    A span is masked when one of the readings below takes it for a secret: a
    URL's password to the first ``@`` and to the last ``@`` before whitespace,
    the value of each query parameter whose key ends in ``password`` to the next
    ``&`` or whitespace, the value of a key ending in ``password`` written
    ``key=value`` anywhere else, as a keyword/value connection string, an
    environment assignment or a flag writes it, and a ``user:secret@`` outside a
    URL. Spans that overlap or touch are masked as one, so what stands between
    two of them is masked too. A URL's password holding whitespace is not
    matched here. Such a keyword's value is masked to the end of the longest of
    its readings, libpq's and a shell's with and without ``$'..'`` quoting,
    quotes, backslash escapes and a shell's parameter expansions included, a
    command substitution or any other part whose end only a shell's command
    grammar finds taken to the end of the text, and on to the next space, tab
    or line feed; a Python string literal is read none of these ways. Otherwise, outside a
    URL, text is scanned, not parsed: a secret holding whitespace, a slash, a
    backslash or a quote is not matched, one holding ``:`` keeps the part
    before its last ``:``, one holding ``@`` keeps the part after its first
    ``@``, and a secret passed as a separate argument (``--password secret``)
    isn't matched at all.
    """
    return _mask_spans(text, whole=False)


def mask_db_url(url: str) -> str:
    """Return *url* with every secret it finds replaced by ``***``.

    *url* is normally a URL as the store holds it, scheme first. Beyond what
    ``mask_credentials`` masks, a query password's value runs to the next
    ``&``, and a URL that opens with its scheme has its password taken to
    the first ``@`` and to the last one, whatever it holds, whitespace and
    line breaks included; an ``@`` after the host moves that end and hides
    the host too. C0 control characters and spaces before the scheme, and
    a tab or line break inside it or its ``://``, are passed over as
    urlsplit passes them.
    """
    return _mask_spans(url, whole=True)


def _mask_spans(text: str, *, whole: bool) -> str:
    """Mask each span these readings take for a secret, all found before any
    is masked: each query value whose key ends in ``password``, each value of
    a key ending in ``password`` outside a query, then each URL password and
    ``user:secret@``, read on *text* and, when it has query values to mask,
    again on a view with them blanked, which takes out a delimiter such a
    value may hold that would stop a reading short. *whole* reads *text* as
    one URL given alone. The query and URL readings take one scan of the
    string plus a binary search per match, and the keyword readings read each
    position a bounded number of times however many values overlap
    (``_ValueEnds``), so the cost grows with the length of *text*."""
    ends = [m.start() for m in (_WHOLE_VALUE_END if whole else _VALUE_END).finditer(text)]
    values = []
    for m in _QUERY_KEY.finditer(text):
        if unquote(_KEY_BREAK.sub("", m.group(1))).lower().endswith("password"):
            start = m.end(1) + 1
            i = bisect_left(ends, start)
            stop = ends[i] if i < len(ends) else len(text)
            if stop > start:  # an empty value has nothing to mask
                values.append((start, stop))
    values = _merged(values)
    blanked = list(text)
    for start, end in values:
        blanked[start:end] = "x" * (end - start)
    spans = list(values)
    reads = _ValueEnds(text)
    pos = 0
    while m := _PASSWORD_KEY.search(text, pos):
        # to the end of the longest reading, then on to the next space, tab or line
        # feed, or, after a `&`, no further than the next `&`. An empty value, or an
        # empty pair of quotes, has nothing to mask. A key may stand inside this
        # value, so the search goes on from where the value starts.
        pos = m.end()
        after_ampersand = text[m.start() - 1 : m.start()] == "&"
        end = max(reads.shell(pos), reads.bash(pos), pos if after_ampersand else reads.libpq(pos))
        if after_ampersand:
            end = min(end, reads.next_match(_WHOLE_VALUE_END, pos))
        if end - pos <= 2 and text[pos:end] in ("", "''", '""'):
            continue
        stops = _BLANK_OR_AMPERSAND if after_ampersand else _BLANK
        spans.append((pos, reads.next_match(stops, end)))
    for s in (text, "".join(blanked)) if values else (text,):
        ats = [m.start() for m in re.finditer("@", s)]
        gaps = [m.start() for m in _SPACE.finditer(s)]
        for m in _URL_USER.finditer(s):
            i = bisect_left(gaps, m.end())
            first = bisect_left(ats, m.end())
            last = bisect_left(ats, gaps[i] if i < len(gaps) else len(s)) - 1
            if first <= last:  # an `@` before whitespace: to the first and to the last
                spans += [(m.end(), ats[first]), (m.end(), ats[last])]
        spans += [m.span(2) for m in _CREDENTIAL_IN_TEXT.finditer(s)]
        if whole and (m := _WHOLE_URL_USER.match(s)) and (first := s.find("@", m.end())) >= 0:
            spans += [(m.end(), first), (m.end(), s.rindex("@"))]
    out, pos = [], 0
    for start, end in _merged(spans):
        out += [text[pos:start], _MASK]
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _merged(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """*spans* sorted, with those that overlap or touch joined into one."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def dialect_of(url: str) -> str:
    """Return 'sqlite' or 'postgresql' for the given URL."""
    if url.startswith("sqlite"):
        return "sqlite"
    if url.startswith("postgresql") or url.startswith("postgres"):
        return "postgresql"
    scheme = url.split("+")[0].split(":")[0].lower()
    return scheme


def make_engine(url: str, *, before_settings=None, **overrides):
    """Create an AsyncEngine for *url*. SQLite gets a busy_timeout-first pragma
    listener; PostgreSQL gets pool_pre_ping and sslmode→ssl arg translation.

    On SQLite, *before_settings*, when given, is called with each new DBAPI connection after its
    busy_timeout and before the settings that change the file (``journal_mode`` first). A
    connection it refuses is closed and its error raised.
    """
    from sqlalchemy.event import listen
    from sqlalchemy.ext.asyncio import create_async_engine

    dialect = dialect_of(url)

    if dialect == "sqlite":
        announce_busy_timeout()
        kwargs: dict = {"echo": False, "json_serializer": _dumps_with_uuid}
        kwargs.update(overrides)
        kwargs["connect_args"] = {
            "factory": _RollbackOnCloseConnection,
            **kwargs.get("connect_args", {}),
        }
        _configure_sqlite_module(url, kwargs)
        engine = create_async_engine(url, **kwargs)

        def _apply_pragmas(dbapi_conn, connection_record):
            _stop_aiosqlite_once(connection_record.driver_connection)
            cursor = dbapi_conn.cursor()
            cursor.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
            if before_settings is not None:
                try:
                    before_settings(dbapi_conn)
                except BaseException:
                    # SQLAlchemy does not close a connection whose connect listener raised, and
                    # its driver thread would outlive the refusal.
                    try:
                        cursor.close()
                        dbapi_conn.close()
                    except Exception:  # noqa: S110 -- the refusal below is the error to report
                        pass
                    raise
            cursor.execute("PRAGMA journal_mode = WAL")
            if str(cursor.fetchone()[0]).lower() == "wal":
                _warn_if_wal_reset_unfixed()
            cursor.execute("PRAGMA synchronous = NORMAL")
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA cache_size = -64000")
            cursor.execute("PRAGMA wal_autocheckpoint = 1000")
            cursor.close()

        listen(engine.sync_engine, "connect", _apply_pragmas)
        return engine

    # PostgreSQL path.
    connect_args: dict = {}

    # Translate sslmode query param to asyncpg ssl argument.
    if "sslmode=" in url:
        match = re.search(r"sslmode=([^&]+)", url)
        if match:
            sslmode = match.group(1)
            if sslmode in ("require", "verify-ca", "verify-full"):
                import ssl as _ssl

                ctx = _ssl.create_default_context()
                if sslmode == "require":
                    ctx.check_hostname = False
                    ctx.verify_mode = _ssl.CERT_NONE
                connect_args["ssl"] = ctx
            elif sslmode == "disable":
                connect_args["ssl"] = False
            # Strip sslmode from url so asyncpg does not receive an unknown param.
            url = re.sub(r"[?&]sslmode=[^&]*", "", url).rstrip("?")

    kwargs = {"pool_pre_ping": True, "echo": False, "json_serializer": _dumps_with_uuid}
    if connect_args:
        kwargs["connect_args"] = connect_args
    kwargs.update(overrides)
    return create_async_engine(url, **kwargs)


def make_compatibility_engine(url: str, guard, **overrides):
    """AsyncEngine for a compatibility-mode open (ADR-0128) of an existing SQLite store.

    Each new connection gets connection-local settings only, reads the store's journal mode
    instead of setting it, and carries *guard*'s authorizer; a connection that cannot be set up
    that way is closed and the refusal raised. The driver's statement cache is off, so the
    authorizer decides on every prepare rather than once per cached statement.
    """
    from sqlalchemy.event import listen
    from sqlalchemy.ext.asyncio import create_async_engine

    dialect = dialect_of(url)
    if dialect != "sqlite":
        raise ValueError(f"make_compatibility_engine() only supports sqlite, got {dialect!r}")
    _warn_if_wal_reset_unfixed()
    announce_busy_timeout()
    kwargs: dict = {
        "echo": False,
        "json_serializer": _dumps_with_uuid,
        "connect_args": {"cached_statements": 0},
    }
    kwargs.update(overrides)
    _configure_sqlite_module(url, kwargs)
    engine = create_async_engine(url, **kwargs)

    def _configure(dbapi_conn, connection_record):
        try:
            _stop_aiosqlite_once(connection_record.driver_connection)
            dbapi_conn.isolation_level = None  # driver autocommit; the guard emits BEGIN IMMEDIATE
            cursor = dbapi_conn.cursor()
            cursor.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
            cursor.close()
            guard.configure(dbapi_conn, connection_record)
        except BaseException:
            # SQLAlchemy does not close a connection whose connect listener raised, and its
            # driver thread would outlive the refusal.
            try:
                dbapi_conn.close()
            except Exception:  # noqa: S110 -- the refusal below is the error to report
                pass
            raise

    listen(engine.sync_engine, "connect", _configure)
    guard.install(engine.sync_engine)
    return engine


_SQLITE_ASYNC_PREFIX = "sqlite+aiosqlite:///"


def make_readonly_engine(url: str, **overrides):
    """Read-only AsyncEngine over an existing SQLite file via URI `mode=ro`.
    SQLite only — see docs/internals/runtime.md for the read-only contract."""
    from sqlalchemy.event import listen
    from sqlalchemy.ext.asyncio import create_async_engine

    dialect = dialect_of(url)
    if dialect != "sqlite":
        raise ValueError(f"make_readonly_engine() only supports sqlite, got dialect={dialect!r}")
    if not url.startswith(_SQLITE_ASYNC_PREFIX):
        raise ValueError(f"unexpected sqlite URL shape for read-only open: {url!r}")

    raw_path = url[len(_SQLITE_ASYNC_PREFIX) :]
    if raw_path == ":memory:":
        raise ValueError("make_readonly_engine() requires an on-disk database, not :memory:")

    from sqlalchemy.engine import make_url

    # The file make_engine() opens for *url*, written as a file URI: written raw, SQLite reads a
    # '#' in the path as the start of a fragment, which also drops mode=ro, so another file was
    # created and opened writable, and reads '%' as the start of an escape.
    location = Path(make_url(url).database).absolute().as_uri()
    ro_url = f"{_SQLITE_ASYNC_PREFIX}{location}?mode=ro&uri=true"
    kwargs: dict = {"echo": False, "json_serializer": _dumps_with_uuid}
    kwargs.update(overrides)
    kwargs["connect_args"] = {
        "factory": _RollbackOnCloseConnection,
        **kwargs.get("connect_args", {}),
    }
    _configure_sqlite_module(ro_url, kwargs)
    engine = create_async_engine(ro_url, **kwargs)

    def _apply_readonly_pragmas(dbapi_conn, connection_record):
        _stop_aiosqlite_once(connection_record.driver_connection)
        cursor = dbapi_conn.cursor()
        cursor.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA query_only = 1")
        cursor.close()

    listen(engine.sync_engine, "connect", _apply_readonly_pragmas)
    return engine
