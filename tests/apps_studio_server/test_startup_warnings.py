# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for startup safety warnings and bounded CORS methods.

Log capture uses a handler spy instead of caplog: the FastAPI lifespan runs
in a thread spawned by TestClient, and caplog's root-logger handler is not
guaranteed to be in place before the lifespan starts. A Logger-attached spy
handler receives records from any thread.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")

from fastapi.testclient import TestClient  # noqa: E402

_STUDIO_APP_LOGGER = "lionagi.studio.app"


# ---------------------------------------------------------------------------
# Handler spy infrastructure
# ---------------------------------------------------------------------------


class _RecordList(list["logging.LogRecord"]):
    def messages_at_or_above(self, level: int) -> list[str]:
        return [r.getMessage() for r in self if r.levelno >= level]

    def warnings(self) -> list[str]:
        return self.messages_at_or_above(logging.WARNING)


class _SpyHandler(logging.Handler):
    """Spy handler: collects every LogRecord emitted to the attached logger."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: _RecordList = _RecordList()

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        self.records.append(record)


@contextmanager
def _spy_logger(name: str) -> Generator[_RecordList]:
    logger = logging.getLogger(name)
    spy = _SpyHandler()
    orig_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(spy)
    try:
        yield spy.records
    finally:
        logger.removeHandler(spy)
        logger.setLevel(orig_level)


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------


async def _anoop(*args: object, **kwargs: object) -> None:
    return None


def _neutralize_heavy_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub out the lifespan's heavy startup/shutdown side-effects.

    These tests only assert on _emit_startup_warnings() (runs first). The full
    lifespan spins a scheduler tick loop, three DB reapers (one shells out to ps
    and reads the real state.db), and WAL checkpoints — that footprint caused
    consistent OOM kills in xdist workers and leaked writes into DEFAULT_DB_PATH.
    The scheduler, reconciliation, and checkpoint paths are covered elsewhere.
    """
    import lionagi.studio.scheduler.engine as engine_mod
    import lionagi.studio.services.db_maintenance as db_maintenance_mod
    import lionagi.studio.services.launches as launches_mod
    import lionagi.studio.services.lifecycle as lifecycle_mod

    monkeypatch.setattr(engine_mod.scheduler, "start", _anoop)
    monkeypatch.setattr(engine_mod.scheduler, "stop", _anoop)
    monkeypatch.setattr(lifecycle_mod, "run_startup_reconciliation", _anoop)
    monkeypatch.setattr(db_maintenance_mod, "checkpoint_state_db", _anoop)
    monkeypatch.setattr(launches_mod, "shutdown_launches", _anoop)


@contextmanager
def _lifespan_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Generator[TestClient]:
    """TestClient context manager that runs the lifespan (so startup warnings fire).

    Bare TestClient(app) construction does NOT trigger the lifespan.
    """
    from importlib import reload

    import lionagi.studio.app as app_mod
    import lionagi.studio.services.stats as stats_mod

    fake_db = tmp_path / "state.db"
    monkeypatch.setattr(stats_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(stats_mod, "_DB", str(fake_db))

    reload(app_mod)
    _neutralize_heavy_lifespan(monkeypatch)
    with TestClient(app_mod.app, raise_server_exceptions=False) as client:
        yield client


def _make_bare_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    """Non-lifespan client for CORS header tests."""
    from importlib import reload

    import lionagi.studio.app as app_mod
    import lionagi.studio.services.stats as stats_mod

    fake_db = tmp_path / "state.db"
    monkeypatch.setattr(stats_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(stats_mod, "_DB", str(fake_db))

    reload(app_mod)
    return TestClient(app_mod.app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Warning: no auth token
# ---------------------------------------------------------------------------


class TestNoAuthWarning:
    def test_warning_emitted_without_token(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Starting without LIONAGI_STUDIO_AUTH_TOKEN emits a WARNING."""
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("LIONAGI_STUDIO_HOST", "127.0.0.1")

        with _spy_logger(_STUDIO_APP_LOGGER) as records:
            with _lifespan_client(monkeypatch, tmp_path):
                pass

        auth_warnings = [m for m in records.warnings() if "LIONAGI_STUDIO_AUTH_TOKEN" in m]
        assert auth_warnings, (
            f"Expected a WARNING mentioning LIONAGI_STUDIO_AUTH_TOKEN; "
            f"got warning records: {records.warnings()!r}"
        )

    def test_warning_mentions_no_authentication(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The no-token warning must describe the risk, not just name the env var."""
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("LIONAGI_STUDIO_HOST", "127.0.0.1")

        with _spy_logger(_STUDIO_APP_LOGGER) as records:
            with _lifespan_client(monkeypatch, tmp_path):
                pass

        full_text = " ".join(records.warnings())
        assert "authentication" in full_text.lower(), (
            f"Warning must mention 'authentication'; got: {full_text!r}"
        )

    def test_no_warning_with_token_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When a token is configured, no unauthenticated-mode warning is emitted."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "supersecret")

        with _spy_logger(_STUDIO_APP_LOGGER) as records:
            with _lifespan_client(monkeypatch, tmp_path):
                pass

        auth_warnings = [m for m in records.warnings() if "LIONAGI_STUDIO_AUTH_TOKEN" in m]
        assert not auth_warnings, (
            f"Should not warn about auth when token is set; got: {auth_warnings!r}"
        )


# ---------------------------------------------------------------------------
# Escalated warning: 0.0.0.0 bind without token
# ---------------------------------------------------------------------------


class TestEscalatedWarningOnWildcardBind:
    def test_escalated_warning_on_0000_host(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Binding to 0.0.0.0 without a token emits an escalated (louder) warning."""
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("LIONAGI_STUDIO_HOST", "0.0.0.0")

        import lionagi.studio.config as config_mod

        monkeypatch.setattr(config_mod, "HOST", "0.0.0.0")

        with _spy_logger(_STUDIO_APP_LOGGER) as records:
            with _lifespan_client(monkeypatch, tmp_path):
                pass

        assert any("0.0.0.0" in m for m in records.warnings()), (
            f"Expected escalated warning mentioning '0.0.0.0'; got warnings: {records.warnings()!r}"
        )

    def test_no_0000_escalation_with_token_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """0.0.0.0 escalation must NOT fire when a token is configured."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "secure")
        monkeypatch.setenv("LIONAGI_STUDIO_HOST", "0.0.0.0")

        import lionagi.studio.config as config_mod

        monkeypatch.setattr(config_mod, "HOST", "0.0.0.0")

        with _spy_logger(_STUDIO_APP_LOGGER) as records:
            with _lifespan_client(monkeypatch, tmp_path):
                pass

        network_warnings = [m for m in records.warnings() if "0.0.0.0" in m]
        assert not network_warnings, (
            f"Should not warn about 0.0.0.0 when token is set; got: {network_warnings!r}"
        )


# ---------------------------------------------------------------------------
# CORS method set is bounded (not '*')
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestCORSBoundedMethods:
    def _client(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        return _make_bare_client(monkeypatch, tmp_path)

    def test_options_preflight_returns_200_or_204(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """CORS OPTIONS preflight to /health must succeed (200 or 204)."""
        client = self._client(monkeypatch, tmp_path)
        resp = client.options(
            "/health",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code in (200, 204), (
            f"OPTIONS preflight must succeed; got {resp.status_code}"
        )

    def test_allowed_methods_header_is_not_wildcard(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Access-Control-Allow-Methods must not be '*'."""
        client = self._client(monkeypatch, tmp_path)
        resp = client.options(
            "/health",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        allow_methods = resp.headers.get("access-control-allow-methods", "")
        assert allow_methods != "*", (
            f"CORS allow_methods must be an explicit list, not a wildcard; got: {allow_methods!r}"
        )

    def test_allowed_methods_covers_required_verbs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """GET, POST, PUT, PATCH, DELETE, OPTIONS must all be in the allowed set."""
        client = self._client(monkeypatch, tmp_path)
        resp = client.options(
            "/health",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
            },
        )
        raw = resp.headers.get("access-control-allow-methods", "")
        allowed = {m.strip().upper() for m in raw.split(",")}
        for verb in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            assert verb in allowed, (
                f"Expected {verb!r} in Access-Control-Allow-Methods; got header: {raw!r}"
            )

    def test_head_preflight_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """CORS preflight requesting HEAD must succeed — not 400.

        FastAPI auto-generates HEAD for GET routes/docs endpoints; a hardcoded
        allowlist that omits HEAD causes preflight 400 for those routes.
        """
        client = self._client(monkeypatch, tmp_path)
        resp = client.options(
            "/openapi.json",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "HEAD",
            },
        )
        assert resp.status_code in (200, 204), (
            f"HEAD preflight must succeed; got {resp.status_code}"
        )
        raw = resp.headers.get("access-control-allow-methods", "")
        allowed = {m.strip().upper() for m in raw.split(",")}
        assert "HEAD" in allowed, (
            f"Expected HEAD in Access-Control-Allow-Methods; got header: {raw!r}"
        )

    def test_allowlist_covers_every_served_route_method(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The CORS allowlist must cover every method served by the app's route table.

        Deriving the allowlist from app.routes keeps it in sync; this test fails
        if any served method escapes the allowlist.
        """
        import lionagi.studio.app as app_mod

        served: set[str] = set()
        for route in app_mod.app.routes:
            methods = getattr(route, "methods", None)
            if methods:
                served.update(m.upper() for m in methods)

        allowlist = {m.upper() for m in app_mod._collect_cors_methods(app_mod.app)}

        missing = served - allowlist
        assert not missing, (
            f"CORS allowlist is missing served method(s): {sorted(missing)}; "
            f"served={sorted(served)} allowlist={sorted(allowlist)}"
        )
        assert "HEAD" in allowlist, f"HEAD must be in the CORS allowlist; got {sorted(allowlist)}"


# ---------------------------------------------------------------------------
# CORS wildcard origin warning
# ---------------------------------------------------------------------------


class TestCORSWildcardOriginWarning:
    def test_warning_on_wildcard_cors_origin(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """CORS_ORIGINS containing '*' emits a startup WARNING."""
        import lionagi.studio.app as app_mod
        import lionagi.studio.config as config_mod

        monkeypatch.setattr(config_mod, "CORS_ORIGINS", ["*"])
        monkeypatch.setattr(app_mod, "CORS_ORIGINS", ["*"])

        with _spy_logger(_STUDIO_APP_LOGGER) as records:
            with _lifespan_client(monkeypatch, tmp_path):
                pass

        cors_warnings = [m for m in records.warnings() if "CORS" in m.upper()]
        assert cors_warnings, (
            f"Expected a CORS wildcard origin warning; got all warnings: {records.warnings()!r}"
        )

    def test_no_cors_warning_for_explicit_origins(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No CORS warning when origins are explicitly listed (not wildcard)."""
        import lionagi.studio.app as app_mod
        import lionagi.studio.config as config_mod

        explicit_origins = ["http://localhost:3000", "http://localhost:5173"]
        monkeypatch.setattr(config_mod, "CORS_ORIGINS", explicit_origins)
        monkeypatch.setattr(app_mod, "CORS_ORIGINS", explicit_origins)

        with _spy_logger(_STUDIO_APP_LOGGER) as records:
            with _lifespan_client(monkeypatch, tmp_path):
                pass

        cors_warnings = [m for m in records.warnings() if "CORS" in m.upper()]
        assert not cors_warnings, (
            f"Should not emit CORS warning for explicit origins; got: {cors_warnings!r}"
        )


# ---------------------------------------------------------------------------
# CLI wires the resolved bind host into the warning source
# ---------------------------------------------------------------------------


class TestCLIHostWiring:
    """The CLI must export the resolved bind host into LIONAGI_STUDIO_HOST before
    uvicorn.run so the app's 0.0.0.0 escalation warning sees the actual host.
    """

    def test_backend_only_exports_resolved_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import lionagi.studio.cli as studio_cli

        monkeypatch.delenv("LIONAGI_STUDIO_HOST", raising=False)
        monkeypatch.setattr(studio_cli, "_ensure_apps_importable", lambda: True)

        captured: dict[str, str] = {}

        def _fake_uvicorn_run(_app: str, *, host: str, port: int) -> None:
            # The export must have happened before uvicorn.run is invoked.
            captured["env_host"] = os.environ.get("LIONAGI_STUDIO_HOST", "")
            captured["bind_host"] = host

        import uvicorn

        monkeypatch.setattr(uvicorn, "run", _fake_uvicorn_run)

        rc = studio_cli._start_backend_only("0.0.0.0", 18765)

        assert rc == 0
        assert captured["bind_host"] == "0.0.0.0"
        assert captured["env_host"] == "0.0.0.0", (
            "CLI must export the resolved bind host into LIONAGI_STUDIO_HOST "
            "before uvicorn.run so the app's security warning sees it"
        )

    def test_start_local_overwrites_stale_host_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """_start_local must overwrite a stale LIONAGI_STUDIO_HOST before uvicorn.run."""
        import lionagi.studio.cli as studio_cli

        # Stale env claims 0.0.0.0; the CLI is about to bind 127.0.0.1.
        monkeypatch.setenv("LIONAGI_STUDIO_HOST", "0.0.0.0")
        # _start_local sets LIONAGI_STUDIO_FRONTEND_DIST raw in os.environ;
        # registering the var with monkeypatch restores its absence on teardown
        # so the path can't leak to other test files on this worker.
        monkeypatch.delenv("LIONAGI_STUDIO_FRONTEND_DIST", raising=False)
        monkeypatch.setattr(studio_cli.shutil, "which", lambda _name: "/usr/bin/node")
        monkeypatch.setattr(studio_cli, "_ensure_frontend_built", lambda *a, **k: True)
        monkeypatch.setattr(studio_cli, "_ensure_apps_importable", lambda: True)

        captured: dict[str, str] = {}

        def _fake_uvicorn_run(_app: str, *, host: str, port: int) -> None:
            captured["env_host"] = os.environ.get("LIONAGI_STUDIO_HOST", "")
            captured["bind_host"] = host

        import uvicorn

        monkeypatch.setattr(uvicorn, "run", _fake_uvicorn_run)

        rc = studio_cli._start_local("127.0.0.1", 18765, 3000, tmp_path, False)

        assert rc == 0
        assert captured["bind_host"] == "127.0.0.1"
        assert captured["env_host"] == "127.0.0.1", (
            "_start_local must overwrite a stale LIONAGI_STUDIO_HOST with the "
            "actual resolved bind host before uvicorn.run"
        )
