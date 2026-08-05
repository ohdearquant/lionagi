# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Engine factory for the StateDB backend — normalises URLs and creates AsyncEngine instances."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from lionagi._paths import LIONAGI_HOME

_log = logging.getLogger(__name__)


def _busy_timeout_from_env() -> int:
    """The sqlite busy_timeout (ms) this deployment asked for, or the default.

    The default of 5000 is sized for the test suite — a test that deliberately
    holds a write lock should fail fast, not wait out a production-grade
    timeout — and for years it was also silently the production value. On a
    large store contended by more than one long-lived process, five seconds is
    the difference between a write that waits and a write that reports
    "database is locked" after having already waited as long as it was allowed
    to. That is a deployment property, so it comes from the environment: a
    daemon's launch config sets it high, the suite sets nothing and keeps the
    fast failure.

    An unusable value falls back to the default and says so. Refusing to start
    over a malformed tuning knob would trade a slower lock wait for no daemon
    at all, which is not a trade the knob's owner asked for. Zero and negative
    values are refused the same way: busy_timeout=0 means "never wait", which
    turns every momentary lock into an error and is never what a deployment
    that bothered to set the variable wants.
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
    """Say once per process what busy_timeout this process's connections use.

    The value is a deployment property that lives in one config file's env
    block, so it is set once per config and every new config that spawns a
    lionagi process starts at the built-in default. Against a large contended
    store that default is the difference between a write that waits and a write
    that reports "database is locked", and until now nothing said which one was
    in effect — it could only be inferred by reading the config that launched
    the process, from outside the process.

    The source is recomputed here rather than recorded when the module was
    imported, so the line describes what is actually in effect at the moment a
    connection is about to use it. That matters because the module attribute is
    writable: tests retune it, and a provenance captured at import would keep
    claiming the environment's answer after something else had replaced it.
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


# A `user:secret@` credential, wherever it appears. The secret runs to the
# first `@` and may not contain whitespace, a slash, a quote or a second colon,
# which is what keeps this off `sqlite:///path` (no `@`) and off an ordinary
# `scheme://host` (no colon before the host).
_CREDENTIAL_IN_TEXT = re.compile(r"(?<![\w.+~%-])([\w.+~%-]+):([^\s:@/\\'\"]+)@")


def _mask_secret(secret: str) -> str:
    """The one mask token. Long secrets keep a 6-char prefix so an operator can
    tell two of them apart; short ones show nothing but their length, because a
    prefix of a short secret is most of it."""
    prefix = secret[:6] if len(secret) >= 12 else ""
    return f"{prefix}…[{len(secret)} chars]"


def mask_credentials(text: str) -> str:
    """Return *text* with the secret in every ``user:secret@`` masked.

    For prose rather than for a URL: an error message that quotes the store it
    failed to open carries the credential just as plainly as the URL field
    beside it, and masking only the field closes one of two channels onto the
    same secret.

    Text is scanned rather than parsed because the strings that reach here are
    not URLs and cannot be made into them. Two consequences worth stating: a
    password containing a literal ``@`` is masked only up to that character,
    and a secret passed as a bare argument rather than inside a URL is not
    matched at all. Both are under-masking, so neither is a reason to skip the
    pass, and both are why this is a backstop rather than the only control.

    Masking is idempotent: the token it writes contains a space, and a space
    cannot appear inside a secret this pattern will match.
    """
    return _CREDENTIAL_IN_TEXT.sub(lambda m: f"{m.group(1)}:{_mask_secret(m.group(2))}@", text)


def mask_db_url(url: str) -> str:
    """Return *url* with any password replaced by the first-6-chars mask.

    The structured pass comes first, and text scanning is the fallback for the
    URLs it cannot decompose. A string with no scheme parses as a path rather
    than as a URL, so ``urlparse`` reports no password for it and the
    credential would otherwise be returned verbatim by the function whose whole
    job is to remove it. Such a value is refused as a store setting, which is
    not a reason to drop the arm: what reaches here is whatever a caller was
    handed, including a driver quoting its own connection string, an older log
    line, and the ``./``-prefixed path that spelling makes acceptable.
    """
    try:
        parsed = urlparse(url)
        if not parsed.password:
            return mask_credentials(url)
        masked = _mask_secret(parsed.password)
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
        announce_busy_timeout()
        kwargs: dict = {"echo": False, "json_serializer": _dumps_with_uuid}
        kwargs.update(overrides)
        engine = create_async_engine(url, **kwargs)

        def _apply_pragmas(dbapi_conn, _connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
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
        cursor.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA query_only = 1")
        cursor.close()

    listen(engine.sync_engine, "connect", _apply_readonly_pragmas)
    return engine
