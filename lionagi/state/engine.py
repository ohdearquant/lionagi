# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Engine factory for the StateDB backend — normalises URLs and creates AsyncEngine instances."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from lionagi._paths import LIONAGI_HOME

_log = logging.getLogger(__name__)

# sqlite busy_timeout (ms) applied to every connection; kept low so tests that
# deliberately hold a write lock fail fast instead of waiting the full default.
_SQLITE_BUSY_TIMEOUT_MS = 5000


def has_wal_reset_fix(version_info: tuple[int, ...]) -> bool:
    """Whether a linked SQLite carries the fix for the WAL-reset corruption race
    (all WAL-mode releases 3.7.0-3.51.2; fixed in 3.51.3, backported to 3.44.6
    and 3.50.7). See docs/internals/runtime.md."""
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
    """Warn once per process when WAL is about to be enabled on a library that
    still carries the WAL-reset corruption race."""
    global _wal_reset_warning_emitted
    if _wal_reset_warning_emitted or has_wal_reset_fix(sqlite3.sqlite_version_info):
        return
    _wal_reset_warning_emitted = True
    _log.warning(
        "Linked SQLite %s predates the fix for the WAL-reset corruption race "
        "(fixed in 3.51.3; backported to 3.44.6 and 3.50.7). WAL is still enabled "
        "because concurrent readers and writers depend on it; upgrade the SQLite "
        "library to remove the exposure.",
        sqlite3.sqlite_version,
    )


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
# like this.
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


def mask_db_url(url: str) -> str:
    """Return *url* with any password replaced by the first-6-chars mask."""
    try:
        parsed = urlparse(url)
        if not parsed.password:
            return url
        pw = parsed.password
        prefix = pw[:6] if len(pw) >= 12 else ""
        masked = f"{prefix}…[{len(pw)} chars]"
        user_info = f"{parsed.username}:{masked}"
        host_part = parsed.hostname or ""
        if parsed.port:
            host_part = f"{host_part}:{parsed.port}"
        netloc = f"{user_info}@{host_part}"
        replaced = parsed._replace(netloc=netloc)
        return urlunparse(replaced)
    except Exception:  # noqa: BLE001
        return "<url-mask-error>"


def dialect_of(url: str) -> str:
    """Return 'sqlite' or 'postgresql' for the given URL."""
    if url.startswith("sqlite"):
        return "sqlite"
    if url.startswith("postgresql") or url.startswith("postgres"):
        return "postgresql"
    scheme = url.split("+")[0].split(":")[0].lower()
    return scheme


def make_engine(url: str, **overrides):
    """Create an AsyncEngine for *url*. SQLite gets a busy_timeout-first pragma
    listener; PostgreSQL gets pool_pre_ping and sslmode→ssl arg translation."""
    from sqlalchemy.event import listen
    from sqlalchemy.ext.asyncio import create_async_engine

    dialect = dialect_of(url)

    if dialect == "sqlite":
        _warn_if_wal_reset_unfixed()
        kwargs: dict = {"echo": False, "json_serializer": _dumps_with_uuid}
        kwargs.update(overrides)
        engine = create_async_engine(url, **kwargs)

        def _apply_pragmas(dbapi_conn, _connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
            cursor.execute("PRAGMA journal_mode = WAL")
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

    ro_url = f"{_SQLITE_ASYNC_PREFIX}file:{raw_path}?mode=ro&uri=true"
    kwargs: dict = {"echo": False, "json_serializer": _dumps_with_uuid}
    kwargs.update(overrides)
    engine = create_async_engine(ro_url, **kwargs)

    def _apply_readonly_pragmas(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA query_only = 1")
        cursor.close()

    listen(engine.sync_engine, "connect", _apply_readonly_pragmas)
    return engine
