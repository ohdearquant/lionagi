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

logger = logging.getLogger(__name__)

# sqlite busy_timeout (ms) applied to every connection. Tunable so tests that
# deliberately hold a write lock fail fast instead of waiting the full default.
_SQLITE_BUSY_TIMEOUT_MS = 5000

# SQLite has a long-standing WAL-reset bug -- present in every release from
# 3.7.0 (2010-07-21) through 3.51.2 -- where a checkpoint can skip all or
# part of a transaction, leaving the database file corrupt. Fixed in 3.51.3
# and later, with point-release backports for two earlier branches.
_SQLITE_WAL_RESET_FIX_VERSION = (3, 51, 3)
_SQLITE_WAL_RESET_FIX_BACKPORTS = ((3, 44, 6), (3, 50, 7))


def _sqlite_has_wal_reset_fix(version_info: tuple[int, ...]) -> bool:
    """Whether *version_info* (as from ``sqlite3.sqlite_version_info``)
    includes the documented WAL-reset corruption fix."""
    if version_info >= _SQLITE_WAL_RESET_FIX_VERSION:
        return True
    return any(
        version_info[:2] == (major, minor) and version_info[2] >= patch
        for major, minor, patch in _SQLITE_WAL_RESET_FIX_BACKPORTS
    )


# Logged at most once per process -- _apply_pragmas runs on every new
# connection, and this is a startup-time compatibility fact, not a per-
# connection event.
_wal_version_warning_emitted = False


def _json_serializer(obj):
    if isinstance(obj, uuid.UUID):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _dumps_with_uuid(value):
    return json.dumps(value, default=_json_serializer)


def normalize_state_db_url(value: str | Path | None) -> str:
    """Resolve *value* to a fully-qualified async SQLAlchemy URL string."""
    if value is None:
        db_path = (LIONAGI_HOME / "state.db").resolve()
        return f"sqlite+aiosqlite:///{db_path}"

    if isinstance(value, Path):
        return f"sqlite+aiosqlite:///{value.resolve()}"

    s = str(value)

    # Special-case SQLite in-memory shorthand.
    if s == ":memory:":
        return "sqlite+aiosqlite:///:memory:"

    # Bare filesystem path — no scheme detected.
    if "://" not in s:
        return f"sqlite+aiosqlite:///{Path(s).resolve()}"

    # Already fully-qualified async variants — leave unchanged.
    if s.startswith("sqlite+aiosqlite://") or s.startswith("postgresql+asyncpg://"):
        return s

    # sqlite:/// → sqlite+aiosqlite:/// (preserve original slash count)
    if s.startswith("sqlite:///"):
        return "sqlite+aiosqlite:" + s[len("sqlite:") :]

    # postgres:// or postgresql:// → postgresql+asyncpg://
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
        # first-6 prefix only when ≥6 chars stay hidden (never expose short secrets)
        prefix = pw[:6] if len(pw) >= 12 else ""
        masked = f"{prefix}…[{len(pw)} chars]"
        # Rebuild netloc without exposing the raw password.
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
    # Fall back to scheme prefix.
    scheme = url.split("+")[0].split(":")[0].lower()
    return scheme


def make_engine(url: str, **overrides):
    """Create an AsyncEngine for *url*. SQLite gets a busy_timeout-first pragma
    listener; PostgreSQL gets pool_pre_ping and sslmode→ssl arg translation."""
    from sqlalchemy.event import listen
    from sqlalchemy.ext.asyncio import create_async_engine

    dialect = dialect_of(url)

    if dialect == "sqlite":
        kwargs: dict = {"echo": False, "json_serializer": _dumps_with_uuid}
        kwargs.update(overrides)
        engine = create_async_engine(url, **kwargs)

        def _apply_pragmas(dbapi_conn, _connection_record):
            global _wal_version_warning_emitted
            if not _wal_version_warning_emitted and not _sqlite_has_wal_reset_fix(
                sqlite3.sqlite_version_info
            ):
                logger.warning(
                    "linked SQLite %s lacks the documented WAL-reset corruption fix "
                    "(fixed in %s, backported to %s) -- enabling WAL mode on this "
                    "library carries the documented corruption risk under concurrent "
                    "writers and checkpoint activity; upgrade SQLite if possible.",
                    sqlite3.sqlite_version,
                    ".".join(map(str, _SQLITE_WAL_RESET_FIX_VERSION)),
                    " / ".join(".".join(map(str, v)) for v in _SQLITE_WAL_RESET_FIX_BACKPORTS),
                )
                _wal_version_warning_emitted = True
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
