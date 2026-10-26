from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import os
import re
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

from lionagi._errors import LionError
from lionagi.version import __version__

from ._host import admitted_host_names, normalize_host
from ._traceback_dump import arm_traceback_dump, disarm_traceback_dump
from .config import CORS_ORIGINS, HOST
from .launch_access import (
    PAIRING_REDEEM_PATH,
    begin_serving,
    launch_bearer,
    mint_pairing_code,
)
from .reconnect import ReconnectChannel, default_socket_path, port_proof, unix_sockets_supported
from .registry import iter_studio_routes, load_studio_route_modules
from .services._db import StoreNotAddressableError

_log = logging.getLogger(__name__)

# Reachable without a bearer token regardless of LIONAGI_STUDIO_AUTH_TOKEN;
# deliberately just liveness probes. /health also signs a reconnect challenge,
# which proves which daemon answers and reveals nothing else.
_PUBLIC_PATHS = frozenset({"/health"})

# A challenge is 32 lowercase hex digits, the form ``li studio`` sends.
_RECONNECT_CHALLENGE = re.compile(r"[0-9a-f]{32}")

# FastAPI built-in schema/docs routes that are NOT under /api but expose API
# shape and must be bearer-guarded in token mode, just like /api/*.
_GUARDED_NON_API_PATHS = frozenset(
    {
        "/openapi.json",
        "/docs",
        "/redoc",
        "/docs/oauth2-redirect",
    }
)


# What a no-token daemon can promise about an Operator decision, and what it cannot:
# the decision routes admit any local caller, so this is a workflow safeguard rather
# than isolation. Stated on the startup warning and on both decision routes' API
# descriptions, which are the surfaces a reader meets before trusting the prompt.
_OPEN_MODE_DECISION_NOTICE = (
    "Without LIONAGI_STUDIO_AUTH_TOKEN, any process that can reach the daemon's listener, "
    "including programs run by other local accounts, can allow or deny Operator proposals. "
    "The permission prompt is a workflow safeguard, not isolation from local programs; "
    "set a token on a shared machine."
)

# With no token configured, the answer to an API request from a browser origin that is
# neither loopback nor the daemon's own.
_REMOTE_ORIGIN_REFUSAL = (
    "This Studio daemon has no LIONAGI_STUDIO_AUTH_TOKEN, so it accepts a page on another "
    "origin only with the bearer `li studio` pairs it with. Run `li studio` to open Studio."
)


def _origin_accepted_without_token(origins: list[str], raw_host: str) -> bool:
    """Whether a request may use the API when no token is configured, by its Origin.

    A request with no Origin header is accepted here and then judged by its
    ``Sec-Fetch-Site`` header (``_fetch_site_accepted_without_origin``), because a
    browser sends no Origin on some cross-site GET requests. A browser origin is accepted
    when its host is loopback (``localhost``, 127.0.0.0/8 or ``::1``) on any port, or
    when it is the daemon's own
    origin: its authority equals the request's Host, as for the SPA a daemon bound to
    another address serves itself. A request that carries an Origin from another site,
    ``null`` included, is refused. CORS only decides which pages may read a response; a
    page on an allowed origin can still act, so it cannot stand in for this check.
    """
    if not origins:
        return True
    if len(origins) > 1:
        return False
    origin = origins[0].strip()
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if (
        parts.scheme not in ("http", "https")
        or not parts.netloc
        or "@" in parts.netloc
        or parts.path
        or parts.query
        or parts.fragment
    ):
        return False
    if parts.netloc.lower() == (raw_host or "").strip().lower():
        return True
    host = parts.hostname
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    # An IPv4-mapped IPv6 address is judged as its IPv4 address on every Python version.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def _fetch_site_accepted_without_origin(sec_fetch_sites: list[str]) -> bool:
    """Whether a request with no Origin header may use the API when no token is configured.

    A browser sends no Origin on a GET or HEAD it makes outside CORS: an image, a script or
    a top-level navigation, from any site. It does send ``Sec-Fetch-Site``, and only
    ``none`` (a typed address or a bookmark) and ``same-origin`` (the SPA this daemon
    serves) are accepted. A request without the header, from a command-line or scripted
    client or an older browser, is accepted. ``same-site``, ``cross-site``, any other value
    and a second header are refused.
    """
    if not sec_fetch_sites:
        return True
    if len(sec_fetch_sites) > 1:
        return False
    return sec_fetch_sites[0].strip().lower() in ("none", "same-origin")


def _bearer_matches(presented: str, token: str) -> bool:
    """Constant-time comparison of a presented Authorization header against the
    expected bearer. `compare_digest` only accepts ASCII-only str, so a header
    carrying non-ASCII bytes is a mismatch — same as before, not a 500.
    """
    try:
        return hmac.compare_digest(presented, f"Bearer {token}")
    except TypeError:
        return False


def _collect_cors_methods(application: FastAPI) -> list[str]:
    """Derive the CORS method allowlist from the app's actual route table
    (hardcoding silently omits FastAPI's auto-generated HEAD routes).
    """
    methods: set[str] = {"OPTIONS"}
    for route in application.routes:
        route_methods = getattr(route, "methods", None)
        if route_methods:
            methods.update(route_methods)
    return sorted(methods)


def _host_reaches_other_machines(bind_host: str) -> bool:
    """Whether a bind on *bind_host* is reachable from another machine.

    True for anything that is not loopback: a wildcard (`0.0.0.0`, `::`) binds
    every interface and a concrete address is reachable from the network it sits
    on. `localhost` and every loopback address (`127.0.0.0/8`, `::1`) are the
    only names that stay on this machine. An unparseable name is treated as
    reachable, since a bind the daemon cannot classify must not read as safe.

    Read through `normalize_host`, so ``LOCALHOST`` is the loopback name and a
    bind written in brackets or in a non-canonical spelling is judged as the
    address it names.
    """
    host = normalize_host(bind_host)
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


def _emit_startup_warnings() -> None:
    """Emit security warnings once at startup — no-op if conditions are safe."""
    token = os.getenv("LIONAGI_STUDIO_AUTH_TOKEN")
    if not token:
        bind_host = normalize_host(os.getenv("LIONAGI_STUDIO_HOST", HOST))
        # A bind on anything but loopback is reachable from other machines, so the
        # warning has to name the address and say who can reach it; naming only
        # 0.0.0.0 understated `::`, which binds every interface too. The name is the
        # canonical one, so a bracketed `[]` reads as blank and a spelling in
        # brackets reads as the address it names.
        if _host_reaches_other_machines(bind_host):
            where = bind_host or "every interface"
            _log.warning(
                f"Studio running WITHOUT authentication on host {where} — API "
                "requests with neither Origin nor Sec-Fetch-Site are accepted from "
                "other machines that can reach that address. This is unsafe in "
                "containers or cloud deployments. Set LIONAGI_STUDIO_AUTH_TOKEN to "
                "require a bearer token. " + _OPEN_MODE_DECISION_NOTICE
            )
        else:
            _log.warning(
                "Studio running WITHOUT authentication on loopback (reachable on this "
                "machine only) — requests with neither Origin nor "
                "Sec-Fetch-Site are accepted (CLI, scripts, indistinguishable browsers); "
                "without Origin, Sec-Fetch-Site none/same-origin pass, "
                "same-site/cross-site/other/repeated values need the paired bearer; "
                "Origin outside loopback/the daemon needs the paired bearer. "
                "Header admission does not prove locality. "
                "Set LIONAGI_STUDIO_AUTH_TOKEN to require a bearer token. "
                + _OPEN_MODE_DECISION_NOTICE
            )

    if "*" in CORS_ORIGINS:
        _log.warning(
            "CORS is configured with a wildcard origin ('*'). "
            "Set CORS_ORIGINS to a comma-separated list of allowed origins "
            "to restrict cross-origin access."
        )


def _start_claude_mirror() -> tuple[asyncio.Event, asyncio.Task] | tuple[None, None]:
    """Start the in-process Claude Code mirror tail if enabled; return (stop, task)."""
    from .config import (
        MIRROR_CLAUDE_ENABLED,
        MIRROR_CLAUDE_INTERVAL,
        MIRROR_CLAUDE_ROOT,
        MIRROR_CLAUDE_SINCE,
        MIRROR_CODEX_ROOT,
        MIRROR_IMPORT_AMBIENT,
        MIRROR_SOURCE,
    )

    if not MIRROR_CLAUDE_ENABLED:
        return None, None
    from lionagi.cli.mirror import CLAUDE_PROJECTS_DIR, CODEX_SESSIONS_DIR, mirror_forever

    claude_root = MIRROR_CLAUDE_ROOT
    codex_root = MIRROR_CODEX_ROOT
    if MIRROR_IMPORT_AMBIENT:
        claude_root = claude_root or CLAUDE_PROJECTS_DIR
        codex_root = codex_root or CODEX_SESSIONS_DIR

    want_claude = MIRROR_SOURCE in ("both", "claude") and claude_root is not None
    want_codex = MIRROR_SOURCE in ("both", "codex") and codex_root is not None
    if not want_claude and not want_codex:
        _log.info(
            "Studio transcript mirror not started: the selected profile has no configured sources"
        )
        return None, None
    source = "both" if want_claude and want_codex else "claude" if want_claude else "codex"

    stop = asyncio.Event()
    task = asyncio.create_task(
        mirror_forever(
            stop,
            root=claude_root,
            codex_root=codex_root,
            source=source,
            since=MIRROR_CLAUDE_SINCE,
            interval=MIRROR_CLAUDE_INTERVAL,
        ),
        name="claude-mirror-tail",
    )

    def _log_unexpected_exit(t: asyncio.Task) -> None:
        # Retained handle suppresses asyncio's own warning; surface it loudly instead.
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            _log.error(
                "Studio transcript mirror exited unexpectedly (%s)",
                type(exc).__name__,
            )

    task.add_done_callback(_log_unexpected_exit)
    _log.info(
        "Studio transcript mirror tail started (source=%s, since=%s)", source, MIRROR_CLAUDE_SINCE
    )
    return stop, task


async def _stop_claude_mirror(stop: asyncio.Event | None, task: asyncio.Task | None) -> None:
    """Signal the mirror tail to stop and await it, cancelling as a backstop."""
    if stop is None or task is None:
        return
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=10)
    except (asyncio.TimeoutError, TimeoutError):
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
    except Exception:  # noqa: BLE001
        # a failed tail must not block shutdown
        _log.warning("Claude mirror tail ended with error", exc_info=True)


def _start_reconnect_channel() -> ReconnectChannel | None:
    """Start the same-user reconnect socket before this daemon serves.

    A platform without unix sockets has no reconnect; the CLI then says to
    restart the daemon. A socket that cannot be created is logged and does not
    stop the daemon from serving: every HTTP route is unaffected.
    """
    if not unix_sockets_supported():
        return None
    channel = ReconnectChannel(default_socket_path(), mint_pairing_code)
    try:
        channel.start()
    except OSError as exc:
        _log.warning("Studio reconnect channel unavailable: %s", exc)
        return None
    return channel


async def _startup_warmup() -> None:
    """Deferred WAL checkpoint, off the critical path so /health serves as
    soon as reconciliation completes."""
    try:
        from .services.db_maintenance import checkpoint_state_db

        await checkpoint_state_db(actor="startup")
    except Exception:  # noqa: BLE001
        _log.warning("Startup WAL checkpoint failed (non-fatal)", exc_info=True)


async def _finalize_warmup(task: asyncio.Task | None) -> None:
    """Cancel the warmup task if still running, then await it (avoids an
    un-retrieved-task warning)."""
    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        _log.warning("Startup warmup task failed (non-fatal)", exc_info=True)


@asynccontextmanager
async def lifespan(app_instance):
    from lionagi.cli._code_identity import snapshot_git_position

    from .daemon_log import daemon_log_handler
    from .scheduler.engine import scheduler
    from .services.lifecycle import run_startup_reconciliation
    from .services.operator import operator_shutdown, operator_startup

    # First, before anything else can run and long before the first request:
    # read where this process's code came from. The modules in memory were fixed
    # at import, but the checkout they came from is a directory anyone can move
    # afterwards. Only a reading taken now distinguishes "the tree moved under a
    # running daemon" from "this is the code we loaded" — deferred to the first
    # health request, the snapshot would describe whatever the tree had become by
    # then and report a commit this process never loaded, with a clean drift
    # verdict on top.
    await asyncio.to_thread(snapshot_git_position)

    # Off unless LIONAGI_STUDIO_TRACEBACK_DUMP names a path. Armed here rather
    # than later so a hang anywhere in the rest of startup is still captured.
    arm_traceback_dump()

    _emit_startup_warnings()
    # The second of the two settings-driven notify bootstrap points (CLI is the first).
    from lionagi.state.lifecycle.notify_settings import register_settings_terminal_callback

    register_settings_terminal_callback()
    # The scheduler's INFO lines (a schedule firing) print on stderr however the app is
    # served: li studio, python -m lionagi.studio, or uvicorn pointed at this module.
    with daemon_log_handler():
        await scheduler.start()
        # Corrects phantom/stale-status rows /api routes read directly; must precede serving.
        await run_startup_reconciliation()
        # Recover any accepted Operator turn that was interrupted by a prior daemon
        # exit before accepting new submissions.
        await operator_startup()
        # Codes and decision capabilities belong to the daemon that issued them.
        # Serving starts a new epoch unless a launcher minted this launch's code
        # just before starting this daemon in the same process.
        begin_serving()
        reconnect = _start_reconnect_channel()
        app_instance.state.reconnect_key = reconnect.key if reconnect is not None else None
        mirror_stop, mirror_task = _start_claude_mirror()
        # Pure maintenance; deferred so readiness isn't gated on it.
        warmup_task = asyncio.create_task(_startup_warmup(), name="studio-startup-warmup")
        try:
            yield
        finally:
            from lionagi.ln.concurrency import CancelScope, ExceptionGroup

            from .services.launches import shutdown_launches

            failures: list[Exception] = []

            async def finish(operation: Any) -> None:
                try:
                    await operation()
                except Exception as exc:
                    failures.append(exc)

            with CancelScope(shield=True):
                registry = getattr(app_instance.state, "session_tail_registry", None)
                if registry is not None:
                    await finish(registry.aclose)
                    app_instance.state.session_tail_registry = None
                try:
                    disarm_traceback_dump()
                except Exception as exc:
                    failures.append(exc)
                app_instance.state.reconnect_key = None
                if reconnect is not None:
                    await finish(lambda: asyncio.to_thread(reconnect.stop))
                await finish(lambda: _finalize_warmup(warmup_task))
                await finish(lambda: _stop_claude_mirror(mirror_stop, mirror_task))
                await finish(operator_shutdown)
                await finish(shutdown_launches)
                await finish(scheduler.stop)
            if failures:
                raise ExceptionGroup("Studio shutdown failed", failures)


# Strict Host-header authority grammar, deliberately stricter than
# `request.url.hostname`; see docs/internals/studio.md.
_HOST_AUTHORITY_RE = re.compile(r"^(?P<host>[A-Za-z0-9.\-]+)(?::(?P<port>\d{1,5}))?$")
_IPV6_AUTHORITY_RE = re.compile(r"^\[(?P<host>[0-9A-Fa-f:]+)\](?::(?P<port>\d{1,5}))?$")


def _parse_host_authority(raw_host: str) -> str | None:
    """Strictly parse a raw Host header value: normalized host (lowercased,
    brackets stripped for IPv6), or None if it doesn't match the grammar."""
    raw_host = (raw_host or "").strip()
    if not raw_host:
        return None
    match = (
        _IPV6_AUTHORITY_RE.match(raw_host)
        if raw_host.startswith("[")
        else _HOST_AUTHORITY_RE.match(raw_host)
    )
    if not match:
        return None
    host = match.group("host")
    return host.lower() if host else None


async def validate_host_header(request: Request, call_next):
    """Reject requests whose Host header doesn't match an expected value —
    defends against DNS rebinding; see docs/internals/studio.md.
    """
    hostname = _parse_host_authority(request.headers.get("host", ""))
    # Both sides are compared in canonical form: the grammar above fixes what a
    # Host authority may look like, and `normalize_host` then says which address
    # it names. Without it a bind written in brackets, in a non-canonical IPv6
    # spelling or as an IPv4-mapped address widened nothing, so a browser's Host
    # for that very address was refused.
    if hostname is not None:
        hostname = normalize_host(hostname)
    allowed_hosts = admitted_host_names(os.getenv("LIONAGI_STUDIO_HOST", HOST))
    if hostname is None or hostname not in allowed_hosts:
        return JSONResponse(
            {"detail": f"Invalid Host header: {request.headers.get('host', '')!r}"},
            status_code=400,
        )
    return await call_next(request)


def _mount_studio_routes(application: FastAPI) -> None:
    """Add every route registered via the @studio_route decorator to
    `application`; importing the area modules here fires the decorators.
    """
    load_studio_route_modules()
    for _route in iter_studio_routes():
        application.add_api_route(
            f"/api{_route.path}",
            _route.handler,
            methods=[_route.method],
            **(
                {"response_model": _route.response_model}
                if _route.response_model is not None
                else {}
            ),
            dependencies=list(_route.dependencies),
            status_code=_route.status_code,
            tags=list(_route.tags),
            name=_route.name,
            summary=_route.summary,
            description=_route.description,
            **(
                {"response_class": _route.response_class}
                if _route.response_class is not None
                else {}
            ),
            responses=dict(_route.responses) if _route.responses is not None else None,
            include_in_schema=_route.include_in_schema,
        )


def _resolve_frontend_dist() -> Path | None:
    """Return the dist/ directory to serve, or None if absent (reads
    LIONAGI_STUDIO_FRONTEND_DIST; unset means API-only mode)."""
    env_override = os.environ.get("LIONAGI_STUDIO_FRONTEND_DIST")
    if not env_override:
        return None
    p = Path(env_override)
    return p if (p / "index.html").exists() else None


def _mount_spa(application: FastAPI, dist: Path) -> None:
    """Mount static assets and register an SPA 404 fallback. Uses a 404
    exception handler, not a catch-all route (which would intercept
    /api/shows before FastAPI's trailing-slash redirect fires).
    """
    assets_dir = dist / "assets"
    if assets_dir.is_dir():
        application.mount("/assets", StaticFiles(directory=str(assets_dir)), name="spa-assets")

    index_path = dist / "index.html"

    @application.exception_handler(404)
    async def _spa_fallback(request: Request, exc: Exception) -> FileResponse | JSONResponse:
        # /api/* paths stay 404 JSON here, not the SPA HTML shell.
        path = request.url.path
        if path.startswith("/api/") or path == "/api":
            # The API's own message, not a generic one. This handler is
            # registered for the STATUS, so it also receives every deliberate
            # HTTPException(404, detail=...) an API handler raises. Answering
            # those with a hardcoded "Not Found" replaced "invalid path
            # component" and "Show 'x' not found" with a string that says
            # nothing -- and only when a frontend was mounted, so the suite,
            # which runs API-only, could not see it and production got the
            # degraded message.
            return JSONResponse({"detail": getattr(exc, "detail", "Not Found")}, status_code=404)
        if request.method not in ("GET", "HEAD"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return FileResponse(
            str(index_path),
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
            },
        )


def create_app() -> FastAPI:
    """Build and return a fresh Studio FastAPI app instance, so callers
    (notably tests) can get a clean one without `importlib.reload`."""
    # The served OpenAPI document is a surface that states a version, and a
    # client reading it to decide what the server supports gets whatever it
    # says. Left unset, FastAPI supplies its own default, which is the same
    # string for every release and so tells a client nothing.
    application = FastAPI(title="Lion Studio Server", version=__version__, lifespan=lifespan)

    @application.exception_handler(LionError)
    async def _lion_error_handler(request: Request, exc: LionError) -> JSONResponse:
        """Translate domain errors raised by service logic into HTTP responses."""
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.message},
        )

    @application.exception_handler(StoreNotAddressableError)
    async def _store_not_addressable_handler(
        request: Request, exc: StoreNotAddressableError
    ) -> JSONResponse:
        """Map a route asking for rows it structurally cannot fetch to 501.

        Handles exactly :class:`StoreNotAddressableError` and nothing wider --
        a path-resolution bug raising something else must not land here and be
        reported as this specific, permanent condition. Not 503: the store
        being server-backed does not change while this process runs, so
        retrying buys nothing and shouldn't be implied.
        """
        return JSONResponse(
            status_code=501,
            content={
                "detail": (
                    f"{request.url.path} cannot answer: the configured store "
                    f"is {exc.backend}-backed, and this route can only read "
                    "from a local SQLite file."
                ),
                "route": request.url.path,
                "backend": exc.backend,
            },
        )

    @application.middleware("http")
    async def require_studio_bearer_token(request: Request, call_next):
        # CORS preflight has no Authorization header by design; let it through.
        if request.method == "OPTIONS":
            return await call_next(request)
        token = os.getenv("LIONAGI_STUDIO_AUTH_TOKEN")
        path = request.url.path
        # /api/* and schema/docs are gated; non-API GET/HEAD (SPA shell,
        # hashed assets) stay public or the UI becomes unloadable.
        is_api = path == "/api" or path.startswith("/api/")
        is_guarded_non_api = path in _GUARDED_NON_API_PATHS
        is_public_static = (
            request.method in ("GET", "HEAD") and not is_api and not is_guarded_non_api
        )
        # The pairing exchange is how a page gets its bearer, so it cannot need one;
        # the one-time code it carries is the credential.
        is_pairing = request.method == "POST" and path == PAIRING_REDEEM_PATH
        if path in _PUBLIC_PATHS or is_public_static or is_pairing:
            return await call_next(request)
        presented = request.headers.get("authorization") or ""
        origins = request.headers.getlist("origin")
        if token:
            if not _bearer_matches(presented, token):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        elif not _origin_accepted_without_token(origins, request.headers.get("host", "")) or (
            not origins
            and not _fetch_site_accepted_without_origin(request.headers.getlist("sec-fetch-site"))
        ):
            # A page on another origin, or one whose browser sent no Origin, is
            # accepted with the bearer this launch handed it through pairing.
            bearer = launch_bearer()
            if bearer is None or not _bearer_matches(presented, bearer):
                return JSONResponse({"detail": _REMOTE_ORIGIN_REFUSAL}, status_code=401)
        return await call_next(request)

    @application.middleware("http")
    async def require_json_content_type(request: Request, call_next):
        """Require JSON media type on every state-changing /api request.

        This includes bodyless action routes: without the header, a cross-site
        simple POST can trigger them without a CORS preflight. See
        docs/internals/studio.md.
        """
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)
        path = request.url.path
        if not (path == "/api" or path.startswith("/api/")):
            return await call_next(request)
        content_type = request.headers.get("content-type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            return JSONResponse(
                {"detail": "Content-Type must be application/json"},
                status_code=415,
            )
        return await call_next(request)

    _mount_studio_routes(application)

    @application.get("/health")
    async def health(request: Request, challenge: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"status": "ok"}
        # A later `li studio` learns this start's key over the reconnect socket and checks
        # that the daemon on the port it was given is the one holding it. The proof covers the
        # port this request arrived on, so a listener relaying to this daemon from another
        # port cannot pass it on.
        key = getattr(request.app.state, "reconnect_key", None)
        server = request.scope.get("server")
        port = server[1] if server else None
        if (
            key is not None
            and challenge is not None
            and _RECONNECT_CHALLENGE.fullmatch(challenge)
            and isinstance(port, int)
        ):
            body["proof"] = port_proof(key, challenge, port)
        return body

    # Mount SPA before CORSMiddleware so _collect_cors_methods sees the Mount entry.
    dist = _resolve_frontend_dist()
    if dist is not None:
        _mount_spa(application, dist)

    # Starlette wraps middleware LIFO; added-after-CORS makes Host validation
    # OUTERMOST (checked before CORS answers preflight). See docs/internals/studio.md
    # for the full request order.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=_collect_cors_methods(application),
        allow_headers=["*"],
    )
    application.add_middleware(BaseHTTPMiddleware, dispatch=validate_host_header)

    return application


app = create_app()
