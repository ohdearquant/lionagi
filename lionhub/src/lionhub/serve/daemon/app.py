# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The daemon's HTTP application and the one fixed seam a Studio application attaches through.

:func:`create_daemon_app` builds the root application: the listener's admission middleware, the
daemon's own routes, ``/health`` and the lifespan that owns the scheduler, the reconnect channel,
the transcript mirror and every other process resource. Called with no attachment it serves the
daemon API alone and imports no application module. Called with an attachment, such as
``lionagi.studio.app.attach_studio``, it lets that application add its routes and its startup and
shutdown hooks to the same process (ADR-0138 F1 and F2).
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import os
import re
from collections.abc import Awaitable, Callable
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from lionagi._errors import LionError
from lionagi.version import __version__
from lionhub.orchestration.state_store import StoreNotAddressableError
from lionhub.serve.daemon._host import admitted_host_names, normalize_host
from lionhub.serve.daemon._traceback_dump import arm_traceback_dump, disarm_traceback_dump
from lionhub.serve.daemon.compatibility import check_attachment_compatibility
from lionhub.serve.daemon.launch_access import (
    PAIRING_REDEEM_PATH,
    begin_serving,
    launch_bearer,
    mint_pairing_code,
)
from lionhub.serve.daemon.reconnect import (
    ReconnectChannel,
    default_socket_path,
    port_proof,
    unix_sockets_supported,
)
from lionhub.serve.daemon.routes import (
    DAEMON_ROUTE_MODULES,
    RouteModule,
    iter_studio_routes,
    load_route_modules,
)
from lionhub.serve.daemon.settings import CORS_ORIGINS, HOST

__all__ = ("Attach", "Attachment", "DaemonServices", "create_daemon_app", "lifespan")


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
    from lionhub.serve.daemon.settings import (
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
    from lionhub.serve.mirror import CLAUDE_PROJECTS_DIR, CODEX_SESSIONS_DIR, mirror_forever

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

    # Said before the task exists: nothing that can raise sits between creating the task
    # and handing it to the caller, whose shutdown is the only thing that stops it.
    _log.info(
        "Studio transcript mirror tail starting (source=%s, since=%s)", source, MIRROR_CLAUDE_SINCE
    )
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
    return stop, task


async def _stop_claude_mirror(stop: asyncio.Event | None, task: asyncio.Task | None) -> None:
    """Signal the mirror tail to stop and await it, cancelling as a backstop.

    Waits with :func:`asyncio.wait`, which returns when the tail ends however it ends, so a
    tail that something else cancelled is not mistaken for a cancellation of this shutdown.
    Only a cancellation of the caller propagates from here.
    """
    if stop is None or task is None:
        return
    stop.set()
    _done, pending = await asyncio.wait({task}, timeout=10)
    if pending:
        task.cancel()
        await asyncio.wait({task})
    if task.cancelled():
        return
    if task.exception() is not None:
        # a failed tail must not block shutdown
        _log.warning("Claude mirror tail ended with error", exc_info=task.exception())


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
        from lionhub.orchestration.scheduler.db_maintenance import checkpoint_state_db

        await checkpoint_state_db(actor="startup")
    except Exception:  # noqa: BLE001
        _log.warning("Startup WAL checkpoint failed (non-fatal)", exc_info=True)


async def _finalize_warmup(task: asyncio.Task | None) -> None:
    """Cancel the warmup task if still running, then await it (avoids an
    un-retrieved-task warning).

    The task's own cancellation ends it quietly; a cancellation of the caller propagates
    rather than being swallowed as if it were the task's.
    """
    if task is None:
        return
    if not task.done():
        task.cancel()
    await asyncio.wait({task})
    if task.cancelled():
        return
    if task.exception() is not None:
        _log.warning("Startup warmup task failed (non-fatal)", exc_info=task.exception())


async def _refuse_or_prepare_leg_host() -> None:
    """In leg-host mode, refuse an unsafe start, then remove worktrees a previous run left."""
    from lionhub.serve.daemon import leg_host

    if not leg_host.enabled():
        return
    bind_host = normalize_host(os.getenv("LIONAGI_STUDIO_HOST", HOST))
    refusals = leg_host.startup_refusals(host=bind_host)
    if refusals:
        raise RuntimeError("Leg-host mode refused to start: " + "; ".join(refusals))
    for repo in leg_host.load_repos().values():
        await leg_host.sweep_worktrees(repo, live=set())


#: The write routes a leg host serves: a launch and its cancellation. Every other write
#: answers 403 there, so a caller holding the bearer can launch, read and cancel, nothing more.
_LEG_HOST_WRITE_PATHS = re.compile(r"/api/launches/?|/api/invocations/[0-9a-f]{8,32}/cancel")


@asynccontextmanager
async def lifespan(app_instance):
    from lionhub.orchestration.scheduler.engine import scheduler
    from lionhub.orchestration.scheduler.lifecycle import run_startup_reconciliation
    from lionhub.serve.daemon.daemon_log import daemon_log_handler
    from lionhub.serve.diagnostics import snapshot_git_position

    attachment: Attachment | None = getattr(app_instance.state, "attachment", None)

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
    # A step below that fails before the daemon block disarms it again on the
    # way out; inside the block the shutdown sequence does. The disarm runs even
    # when arming itself raised: arming can set the timer and then fail to say so,
    # and disarming a dump that never armed does nothing.
    log_scope = ExitStack()
    try:
        arm_traceback_dump()

        _emit_startup_warnings()
        await _refuse_or_prepare_leg_host()
        # The second of the two settings-driven notify bootstrap points (CLI is the first).
        from lionagi.state.lifecycle.notify_settings import register_settings_terminal_callback

        register_settings_terminal_callback()
        # The scheduler's INFO lines (a schedule firing) print on stderr however the app is
        # served: li studio, python -m lionagi.studio, or uvicorn pointed at this module.
        from lionhub.serve.daemon import leg_host
        from lionhub.serve.daemon.daemon_log import SCHEDULER_LOGGER

        # A leg host runs only the legs its callers launch. The scheduler engine would fire
        # this account's schedules, and the mirror would copy this account's other
        # transcripts into the host's store, so neither starts there. Said on the daemon's
        # own stderr logger.
        quiet_host = leg_host.enabled()
        # Entered inside this block, so a log handler that cannot be installed is a failed
        # start like any other and the dump armed above is disarmed.
        log_scope.enter_context(daemon_log_handler())
    except BaseException as exc:
        disarm_failure: Exception | None = None
        try:
            disarm_traceback_dump()
        except Exception as failure:
            disarm_failure = failure
        if disarm_failure is not None:
            if isinstance(exc, Exception):
                from lionagi.ln.concurrency import ExceptionGroup

                raise ExceptionGroup("Studio startup failed", [exc, disarm_failure]) from None
            # A cancelled start stays a cancellation; what its cleanup hit is logged.
            _log.error(
                "Cleanup after an interrupted Studio start failed",
                exc_info=(type(disarm_failure), disarm_failure, disarm_failure.__traceback__),
            )
        raise

    with log_scope:
        # What startup has begun so far. A step that fails, or a cancellation, before
        # the daemon serves runs the shutdown sequence below for the reconnect socket,
        # mirror, warmup and attached application only if they were begun, so a failed
        # start leaves none of them, nor a scheduler, running in the process that tried
        # it. The launch and scheduler stops always run: each does nothing when nothing
        # was started, as on a leg host, where the scheduler never starts.
        attachment_entered = False
        reconnect: ReconnectChannel | None = None
        mirror_stop: asyncio.Event | None = None
        mirror_task: asyncio.Task | None = None
        warmup_task: asyncio.Task | None = None
        serving = False
        startup_error: BaseException | None = None
        try:
            if quiet_host:
                logging.getLogger(SCHEDULER_LOGGER).info(
                    "Leg host: the scheduler engine is not started"
                )
            else:
                await scheduler.start()
            # Corrects phantom/stale-status rows /api routes read directly; must precede
            # serving.
            await run_startup_reconciliation()
            # The attached application's startup, e.g. Studio recovering any accepted
            # Operator turn a prior daemon exit interrupted, before new submissions are
            # accepted. Marked before the call: a startup that fails part way may already
            # have started the application's tasks.
            if attachment is not None:
                attachment_entered = True
                await attachment.startup()
            # Codes and decision capabilities belong to the daemon that issued them.
            # Serving starts a new epoch unless a launcher minted this launch's code
            # just before starting this daemon in the same process.
            begin_serving()
            reconnect = _start_reconnect_channel()
            app_instance.state.reconnect_key = reconnect.key if reconnect is not None else None
            if quiet_host:
                logging.getLogger(SCHEDULER_LOGGER).info(
                    "Leg host: the transcript mirror is not started"
                )
            else:
                mirror_stop, mirror_task = _start_claude_mirror()
            # Pure maintenance; deferred so readiness isn't gated on it.
            warmup_task = asyncio.create_task(_startup_warmup(), name="studio-startup-warmup")
            serving = True
            yield
        except BaseException as exc:
            if not serving:
                startup_error = exc
            raise
        finally:
            from lionagi.ln.concurrency import CancelScope, ExceptionGroup
            from lionhub.serve.daemon.services.launches import shutdown_launches

            failures: list[Exception] = []
            interrupt: BaseException | None = None

            async def finish(operation: Any) -> None:
                # Every step runs whatever an earlier one raised. The shield holds off an
                # AnyIO cancellation, not a native task.cancel(): one that lands inside a
                # step is held here and re-raised after the last step, so it cannot cut the
                # shutdown short.
                nonlocal interrupt
                try:
                    await operation()
                except Exception as exc:
                    failures.append(exc)
                except BaseException as exc:
                    if interrupt is None:
                        interrupt = exc

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
                if attachment is not None and attachment_entered:
                    await finish(attachment.shutdown)
                await finish(shutdown_launches)
                await finish(scheduler.stop)
            if interrupt is not None:
                # The shutdown was itself interrupted. Every step still ran; the interrupt
                # propagates, and what the steps hit is logged rather than lost.
                for failure in failures:
                    _log.error(
                        "A Studio shutdown step failed during an interrupted shutdown",
                        exc_info=(type(failure), failure, failure.__traceback__),
                    )
                raise interrupt
            if failures:
                if startup_error is None:
                    raise ExceptionGroup("Studio shutdown failed", failures)
                if isinstance(startup_error, Exception):
                    raise ExceptionGroup("Studio startup failed", [startup_error, *failures])
                # A cancelled start stays a cancellation; what its cleanup hit is logged.
                for failure in failures:
                    _log.error(
                        "Cleanup after an interrupted Studio start failed",
                        exc_info=(type(failure), failure, failure.__traceback__),
                    )


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


@dataclass(frozen=True, slots=True)
class DaemonServices:
    """The daemon-owned services an attached application calls in this process.

    Each field is the daemon's own module, so an application reaches the one launch supervisor and
    the one identity and capability store the daemon runs; attaching creates no second scheduler,
    launch supervisor or capability store.
    """

    launches: ModuleType
    launch_access: ModuleType


@dataclass(frozen=True, slots=True)
class Attachment:
    """What an attached application gives the daemon.

    ``route_modules`` is the application's route inventory, composed with the daemon's in one
    deterministic order. ``startup`` runs after startup reconciliation and before the daemon
    serves; ``shutdown`` runs after the mirror stops and before detached launches shut down. Both
    run only when the application is attached, and ``shutdown`` only after ``startup`` began.
    """

    route_modules: tuple[RouteModule, ...]
    startup: Callable[[], Awaitable[object]]
    shutdown: Callable[[], Awaitable[None]]


#: The fixed attachment interface: called once with the root application and the daemon's
#: services before any process resource starts; returns the application's attachment.
Attach = Callable[[FastAPI, DaemonServices], Attachment]


def _mount_routes(application: FastAPI, *inventories: tuple[RouteModule, ...]) -> None:
    """Add every route the inventories register to ``application``, in their composed order.

    Importing an inventory's modules fires their ``studio_route`` decorators. Routes are added by
    their module's inventory position and then in the order the module registered them, so the
    order does not depend on which module some other code imported first, and a route module
    outside these inventories adds nothing even when it is imported in this process.
    """
    load_route_modules(*inventories)
    position = {entry.module: entry.position for inventory in inventories for entry in inventory}
    served = sorted(
        (route for route in iter_studio_routes() if route.handler.__module__ in position),
        key=lambda route: (position[route.handler.__module__], route.order),
    )
    for _route in served:
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


def create_daemon_app(attach: Attach | None = None) -> FastAPI:
    """Build the daemon's root application, with ``attach``'s application in it when given.

    ``attach`` is called once, here, before any process resource starts, so an application that
    cannot be built fails this call rather than a running daemon. First the compatibility manifest
    of the package that defines ``attach`` is read, and a missing, malformed or incompatible one
    is refused with :class:`~lionhub.serve.daemon.compatibility.IncompatibleApplicationError`
    before the attachment adds any route (ADR-0138 F7). With no ``attach`` the daemon serves its
    own API alone, reads no manifest, and the application's endpoints answer the normal
    missing-route 404.
    """
    if attach is not None:
        check_attachment_compatibility(attach)

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

    # Added before the bearer check, so it runs inside it: an unauthenticated caller
    # gets 401 and learns nothing about which routes a leg host serves.
    @application.middleware("http")
    async def refuse_leg_host_writes(request: Request, call_next):
        from lionhub.serve.daemon import leg_host

        if (
            request.method not in ("GET", "HEAD", "OPTIONS")
            and leg_host.enabled()
            and not _LEG_HOST_WRITE_PATHS.fullmatch(request.url.path)
        ):
            return JSONResponse(
                {"detail": "A leg host accepts only launches and their cancellation"},
                status_code=403,
            )
        return await call_next(request)

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

    attachment = None
    if attach is not None:
        from lionhub.serve.daemon import launch_access
        from lionhub.serve.daemon.services import launches

        attachment = attach(
            application, DaemonServices(launches=launches, launch_access=launch_access)
        )
    application.state.attachment = attachment
    _mount_routes(
        application,
        DAEMON_ROUTE_MODULES,
        *((attachment.route_modules,) if attachment is not None else ()),
    )

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

    # Every route and mount is in place before CORSMiddleware, so _collect_cors_methods sees them.
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
