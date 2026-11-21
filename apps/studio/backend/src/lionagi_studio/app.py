# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The Studio application, attached to the daemon in the daemon's process.

The daemon (:mod:`lionhub.serve.daemon.app`) owns the listener, the admission middleware, the
scheduler and every process resource. This module supplies the one fixed attachment,
:func:`attach_studio`: the application's route inventory, its frontend mount and its Operator
startup and shutdown (ADR-0138 F1). :func:`create_app` and :data:`app` are the daemon with Studio
attached, as ``li studio`` and ``uvicorn lionagi_studio.app:app`` serve it.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from starlette.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

from lionhub.serve.daemon.app import (  # noqa: F401 -- lifespan, validate_host_header: see below
    Attachment,
    DaemonServices,
    create_daemon_app,
    lifespan,
    validate_host_header,
)

from .registry import _STUDIO_ROUTE_MODULES

# ``lifespan`` and ``validate_host_header`` were defined here before the daemon moved to
# lionhub. They stay importable from this module so existing imports keep resolving, and
# are not part of its declared surface.
__all__ = ("app", "attach_studio", "create_app")


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


def attach_studio(application: FastAPI, services: DaemonServices) -> Attachment:
    """Attach the Studio application to the daemon's root application.

    Mounts the frontend when ``LIONAGI_STUDIO_FRONTEND_DIST`` names a built one, and returns the
    application's route inventory and its Operator startup and shutdown. ``services`` is the
    daemon's own launch and access services; Studio's handlers reach the same ones.
    """
    from .services.operator import operator_shutdown, operator_startup

    dist = _resolve_frontend_dist()
    if dist is not None:
        _mount_spa(application, dist)
    return Attachment(
        route_modules=_STUDIO_ROUTE_MODULES,
        startup=operator_startup,
        shutdown=operator_shutdown,
    )


def create_app() -> FastAPI:
    """Build a fresh daemon with Studio attached, so callers (notably tests) get a clean one
    without ``importlib.reload``."""
    return create_daemon_app(attach_studio)


app = create_app()
