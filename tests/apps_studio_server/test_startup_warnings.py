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

import lionagi.state.db as state_db_mod

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")

import anyio  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_STUDIO_APP_LOGGER = "lionagi.studio.app"


# Handler spy infrastructure


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


# Client factory


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

    Bare TestClient(app, base_url="http://127.0.0.1:8765") construction does NOT trigger the lifespan.
    """
    import lionagi.studio.app as app_mod

    fake_db = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)

    # A fresh app instance (via create_app()) instead of importlib.reload(app_mod):
    # reload mutates the shared module singleton every other importer holds a
    # reference to, which is both a data race under xdist and re-executes
    # module-level side effects (CORS regex compilation, route
    # re-registration) on a namespace other code still imports.
    app = app_mod.create_app()
    _neutralize_heavy_lifespan(monkeypatch)
    with TestClient(app, raise_server_exceptions=False, base_url="http://127.0.0.1:8765") as client:
        yield client


@contextmanager
def _bare_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Generator[TestClient]:
    """Non-lifespan client for CORS header tests.

    Attaches the TestClient's blocking portal directly (one thread serving
    every request in the `with` block) instead of entering the client, which
    would drive the full lifespan -- scheduler start, DB reconciliation, WAL
    checkpoint -- unwanted here since these tests only assert on CORS
    response headers.
    """
    import lionagi.studio.app as app_mod

    fake_db = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)

    app = app_mod.create_app()
    client = TestClient(app, raise_server_exceptions=False, base_url="http://127.0.0.1:8765")
    with anyio.from_thread.start_blocking_portal(**client.async_backend) as portal:
        client.portal = portal
        try:
            yield client
        finally:
            client.portal = None


# Warning: no auth token


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


# Escalated warning: 0.0.0.0 bind without token


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


class TestEscalatedWarningNamesEveryNonLoopbackBind:
    @pytest.mark.parametrize("bind_host", ["0.0.0.0", "::", "192.168.1.5"])
    def test_a_non_loopback_bind_warns_that_other_machines_can_reach_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bind_host: str
    ) -> None:
        """A bind on anything but loopback reaches other machines, so the warning
        names the address it bound and says those machines can reach its pages.
        An IPv6 wildcard is one of them, not a quieter case."""
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("LIONAGI_STUDIO_HOST", bind_host)

        with _spy_logger(_STUDIO_APP_LOGGER) as records:
            with _lifespan_client(monkeypatch, tmp_path):
                pass

        warnings = records.warnings()
        named = [m for m in warnings if bind_host in m]
        assert named, f"no warning names the bind address {bind_host!r}; got {warnings!r}"
        assert any("other machines" in m for m in named), (
            f"the warning for {bind_host!r} does not say other machines can reach it: {named!r}"
        )

    @pytest.mark.parametrize("bind_host", ["127.0.0.1", "::1", "localhost"])
    def test_a_loopback_bind_keeps_the_local_only_warning(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bind_host: str
    ) -> None:
        """A loopback bind is reachable from this machine only, and the warning
        still says so -- loopback is not a mildly-wider interface."""
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("LIONAGI_STUDIO_HOST", bind_host)

        with _spy_logger(_STUDIO_APP_LOGGER) as records:
            with _lifespan_client(monkeypatch, tmp_path):
                pass

        warnings = records.warnings()
        assert any("on this machine" in m for m in warnings), (
            f"no local-only warning for the loopback bind {bind_host!r}; got {warnings!r}"
        )
        assert not any("other machines" in m for m in warnings), (
            f"a loopback bind {bind_host!r} warned that other machines can reach it: {warnings!r}"
        )


# CORS method set is bounded (not '*')


class TestTheWarningNamesTheCanonicalBindValue:
    def test_a_loopback_name_in_any_case_warns_local_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`LOCALHOST` is the loopback name; the classification and the message
        both read the value in canonical form. Read as written it was a name the
        daemon could not classify, so it warned that other machines could reach a
        daemon bound to loopback."""
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("LIONAGI_STUDIO_HOST", "LOCALHOST")

        with _spy_logger(_STUDIO_APP_LOGGER) as records:
            with _lifespan_client(monkeypatch, tmp_path):
                pass

        warnings = records.warnings()
        assert any("on this machine" in m for m in warnings), (
            f"no local-only warning for LOCALHOST; got {warnings!r}"
        )
        assert not any("other machines" in m for m in warnings), (
            f"LOCALHOST warned that other machines can reach it: {warnings!r}"
        )

    @pytest.mark.parametrize("bind_host", ["", "   ", "[]", "[ ]"])
    def test_a_blank_bind_warns_that_every_interface_is_bound(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bind_host: str
    ) -> None:
        """A blank value binds every interface, so the warning says so rather than
        naming a host that is not there: `on host []` named the brackets, and a
        bracketed blank was not read as blank at all."""
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("LIONAGI_STUDIO_HOST", bind_host)

        with _spy_logger(_STUDIO_APP_LOGGER) as records:
            with _lifespan_client(monkeypatch, tmp_path):
                pass

        warnings = records.warnings()
        assert any("on host every interface" in m for m in warnings), (
            f"no every-interface warning for {bind_host!r}; got {warnings!r}"
        )

    @pytest.mark.parametrize(
        ("bind_host", "reaches_other"),
        [
            ("localhost", False),
            ("LOCALHOST", False),
            ("[localhost]", False),
            ("sub.localhost", False),
            ("127.0.0.1", False),
            ("127.0.0.2", False),
            ("[::1]", False),
            ("::ffff:127.0.0.1", False),
            ("0.0.0.0", True),
            ("[::]", True),
            ("[2001:db8::1]", True),
            (" 192.168.1.5 ", True),
            ("::ffff:192.168.1.5", True),
            ("[]", True),
            ("", True),
        ],
    )
    def test_the_classifier_reads_the_value_in_canonical_form(self, bind_host, reaches_other):
        """`_host_reaches_other_machines` says whether a bind is reachable from
        another machine. Read as written, `LOCALHOST` was a name it could not
        classify -- and an unclassifiable bind must not read as safe -- while a
        bracketed wildcard it could strip. Read through the normalizer, the case
        does not decide the answer and the address decides it in any spelling.
        """
        from lionagi.studio.app import _host_reaches_other_machines

        assert _host_reaches_other_machines(bind_host) is reaches_other

    @pytest.mark.parametrize(
        ("bind_host", "named"),
        [
            ("[2001:db8::1]", "2001:db8::1"),
            ("2001:0DB8::1", "2001:db8::1"),
            (" 192.168.1.5 ", "192.168.1.5"),
            ("::ffff:192.168.1.5", "192.168.1.5"),
        ],
    )
    def test_a_non_loopback_bind_is_named_in_canonical_form(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bind_host: str, named: str
    ) -> None:
        """The warning names the address the bind reaches, not the spelling it was
        written in, and it is a non-loopback one however it was written."""
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("LIONAGI_STUDIO_HOST", bind_host)

        with _spy_logger(_STUDIO_APP_LOGGER) as records:
            with _lifespan_client(monkeypatch, tmp_path):
                pass

        warnings = records.warnings()
        assert any(f"on host {named} " in m for m in warnings), (
            f"no warning names {named!r} for a bind written {bind_host!r}; got {warnings!r}"
        )
        assert any("other machines" in m for m in warnings), (
            f"the warning for {bind_host!r} does not say other machines can reach it"
        )

    @pytest.mark.parametrize("bind_host", ["[::1]", "127.0.0.1", "::ffff:127.0.0.1", "[localhost]"])
    def test_a_loopback_bind_in_any_spelling_warns_local_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bind_host: str
    ) -> None:
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("LIONAGI_STUDIO_HOST", bind_host)

        with _spy_logger(_STUDIO_APP_LOGGER) as records:
            with _lifespan_client(monkeypatch, tmp_path):
                pass

        warnings = records.warnings()
        assert any("on this machine" in m for m in warnings), (
            f"no local-only warning for the loopback bind {bind_host!r}; got {warnings!r}"
        )
        assert not any("other machines" in m for m in warnings), (
            f"a loopback bind {bind_host!r} warned that other machines can reach it"
        )


@pytest.mark.integration
class TestCORSBoundedMethods:
    @contextmanager
    def _client(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Generator[TestClient]:
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        with _bare_client(monkeypatch, tmp_path) as client:
            yield client

    def test_options_preflight_returns_200_or_204(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """CORS OPTIONS preflight to /health must succeed (200 or 204)."""
        with self._client(monkeypatch, tmp_path) as client:
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
        with self._client(monkeypatch, tmp_path) as client:
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
        with self._client(monkeypatch, tmp_path) as client:
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
        with self._client(monkeypatch, tmp_path) as client:
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


# CORS wildcard origin warning


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


# CLI wires the resolved bind host into the warning source


class TestCLIHostWiring:
    """The CLI must export the resolved bind host into LIONAGI_STUDIO_HOST before
    uvicorn.run so the app's 0.0.0.0 escalation warning sees the actual host.
    """

    def test_backend_only_exports_resolved_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import lionagi.studio.cli as studio_cli

        monkeypatch.delenv("LIONAGI_STUDIO_HOST", raising=False)
        monkeypatch.setattr(studio_cli, "_ensure_apps_importable", lambda: True)

        captured: dict[str, str] = {}

        def _fake_uvicorn_run(_app: str, *, host: str, port: int, workers: int) -> None:
            # The export must have happened before uvicorn.run is invoked.
            captured["env_host"] = os.environ.get("LIONAGI_STUDIO_HOST", "")
            captured["bind_host"] = host
            captured["workers"] = workers

        import uvicorn

        monkeypatch.setattr(uvicorn, "run", _fake_uvicorn_run)

        rc = studio_cli._start_backend_only("0.0.0.0", 18765)

        assert rc == 0
        assert captured["bind_host"] == "0.0.0.0"
        # One process: a code minted before serving lives only in this one.
        assert captured["workers"] == 1
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

        def _fake_uvicorn_run(_app: str, *, host: str, port: int, workers: int) -> None:
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
