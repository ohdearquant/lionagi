# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Hosted-mode hardening: default CORS allowlist, Host-header validation
(DNS-rebinding defense), and JSON Content-Type enforcement on state-changing
/api requests (simple-request CSRF defense).
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")

import anyio  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@contextmanager
def _entered_client(app: FastAPI, **kwargs: object) -> Generator[TestClient]:
    """Attach a TestClient's blocking portal directly, without driving the
    ASGI lifespan.

    ``TestClient.__enter__()`` is the only public way to get a persistent
    portal (one background thread serving every request) instead of
    ``_portal_factory()``'s per-request spin-up/spin-down thread, but
    entering it also always runs the app's lifespan -- scheduler start, DB
    reconciliation, WAL checkpoint. These hardening tests exercise
    middleware, not startup behavior, so the lifespan is deliberately never
    driven here; attaching the portal directly gets the one-thread-per-test
    win without it.
    """
    client = TestClient(app, **kwargs)
    with anyio.from_thread.start_blocking_portal(**client.async_backend) as portal:
        client.portal = portal
        try:
            yield client
        finally:
            client.portal = None


@pytest.fixture()
def make_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Factory fixture: each call builds a fresh app (via create_app(), not
    importlib.reload) plus an entered client, all torn down when the test
    ends. A fresh app instance, built with the monkeypatched DB path already
    in place, replaces reloading lionagi.studio.app: reload mutates the
    shared module singleton every other importer holds a reference to,
    which is a data race under xdist and re-executes module-level side
    effects (CORS regex compilation, route re-registration) on a namespace
    other code still imports.
    """
    import lionagi.studio.app as app_mod
    import lionagi.studio.services.invocations as inv_mod
    import lionagi.studio.services.sessions as sess_mod
    import lionagi.studio.services.stats as stats_mod

    fake_db = tmp_path / "state.db"
    for mod in (stats_mod, inv_mod, sess_mod):
        if hasattr(mod, "DEFAULT_DB_PATH"):
            monkeypatch.setattr(mod, "DEFAULT_DB_PATH", fake_db)
        if hasattr(mod, "_DB"):
            monkeypatch.setattr(mod, "_DB", str(fake_db))

    stack = ExitStack()

    def _factory() -> TestClient:
        app = app_mod.create_app()
        # base_url determines the Host header the TestClient sends for
        # relative paths; use the real default bind address so tests
        # exercise the same Host the daemon actually serves on (TestClient
        # otherwise defaults to the fictitious "testserver" host, which the
        # Host-check would reject).
        return stack.enter_context(
            _entered_client(app, raise_server_exceptions=False, base_url="http://127.0.0.1:8765")
        )

    yield _factory
    stack.close()


@pytest.mark.integration
class TestHostedCorsOrigin:
    """The hosted static SPA's origin must be in the default CORS allowlist."""

    def test_hosted_origin_in_default_allowlist(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
        import lionagi.studio.config as config_mod

        assert "https://lion-studio.khive.ai" in config_mod.CORS_ORIGINS

    def test_hosted_origin_gets_cors_headers(self, monkeypatch, tmp_path, make_client):
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
        client = make_client()

        resp = client.get(
            "/health",
            headers={
                "Origin": "https://lion-studio.khive.ai",
                "Host": "127.0.0.1:8765",
            },
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "https://lion-studio.khive.ai"


@pytest.mark.integration
class TestHostHeaderValidation:
    """DNS-rebinding defense: only loopback (any port) and the configured bind
    host are accepted as Host header values."""

    def test_hosted_flow_host_and_origin_both_pass(self, monkeypatch, tmp_path, make_client):
        """Pin test: the exact hosted-page flow -- a request from the browser
        tab at https://lion-studio.khive.ai talking to the local daemon at
        127.0.0.1:8765 -- must be accepted by both the Host check and CORS."""
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
        client = make_client()

        resp = client.get(
            "/health",
            headers={
                "Host": "127.0.0.1:8765",
                "Origin": "https://lion-studio.khive.ai",
            },
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "https://lion-studio.khive.ai"

    def test_localhost_any_port_accepted(self, monkeypatch, tmp_path, make_client):
        client = make_client()

        resp = client.get("/health", headers={"Host": "localhost:59999"})
        assert resp.status_code == 200

    def test_bracketed_ipv6_loopback_accepted(self, monkeypatch, tmp_path, make_client):
        client = make_client()

        resp = client.get("/health", headers={"Host": "[::1]:8765"})
        assert resp.status_code == 200

    def test_rebound_host_is_rejected(self, monkeypatch, tmp_path, make_client):
        """A DNS-rebinding attempt (Host pointed at an attacker domain while
        the connection is actually to the local daemon) must be rejected."""
        client = make_client()

        resp = client.get("/health", headers={"Host": "evil.example.com"})
        assert resp.status_code == 400
        assert "Invalid Host header" in resp.json()["detail"]

    def test_rebound_host_rejected_for_api_paths_too(self, monkeypatch, tmp_path, make_client):
        client = make_client()

        resp = client.get("/api/sessions/", headers={"Host": "evil.example.com"})
        assert resp.status_code == 400

    def test_configured_non_loopback_bind_host_accepted(self, monkeypatch, tmp_path, make_client):
        """When the operator explicitly binds to a routable host/hostname,
        that value (with any port) is accepted, alongside loopback."""
        monkeypatch.setenv("LIONAGI_STUDIO_HOST", "studio-box.local")
        client = make_client()

        resp = client.get("/health", headers={"Host": "studio-box.local:8765"})
        assert resp.status_code == 200

    def test_wildcard_bind_host_does_not_widen_allowlist(self, monkeypatch, tmp_path, make_client):
        """Binding to 0.0.0.0 (all interfaces) must not itself become an
        accepted Host value -- browsers never send Host: 0.0.0.0."""
        monkeypatch.setenv("LIONAGI_STUDIO_HOST", "0.0.0.0")
        client = make_client()

        resp = client.get("/health", headers={"Host": "0.0.0.0:8765"})
        assert resp.status_code == 400

    @pytest.mark.parametrize(
        "malformed_host",
        [
            "127.0.0.1:8765.evil.com",
            "127.0.0.1:8765:evil",
            "[::1]evil.com",
            "localhost:badport",
        ],
    )
    def test_malformed_host_authority_is_rejected(
        self, monkeypatch, tmp_path, malformed_host, make_client
    ):
        """Authorities that Python/Starlette's URL parser would normalize
        into an accepted loopback hostname must be rejected outright by the
        strict Host-header grammar."""
        client = make_client()

        resp = client.get("/health", headers={"Host": malformed_host})
        assert resp.status_code == 400
        assert "Invalid Host header" in resp.json()["detail"]

    def test_non_preflight_options_with_bad_host_is_rejected(
        self, monkeypatch, tmp_path, make_client
    ):
        """An OPTIONS request without Origin/Access-Control-Request-Method
        is not a real CORS preflight and must still get its Host checked."""
        client = make_client()

        resp = client.options(
            "/api/sessions/",
            headers={"Host": "evil.example.com"},
        )
        assert resp.status_code == 400
        assert "Invalid Host header" in resp.json()["detail"]

    def test_real_cors_preflight_still_succeeds(self, monkeypatch, tmp_path, make_client):
        """A genuine CORS preflight (Origin + Access-Control-Request-Method)
        from the hosted SPA's origin must still succeed."""
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
        client = make_client()

        resp = client.options(
            "/health",
            headers={
                "Origin": "https://lion-studio.khive.ai",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code in (200, 204)
        assert resp.headers.get("access-control-allow-origin") == "https://lion-studio.khive.ai"

    def test_preflight_with_bad_host_is_rejected(self, monkeypatch, tmp_path, make_client):
        """Even a well-formed CORS preflight from an allowed origin must be
        rejected when the Host header is invalid: Host validation runs
        outside CORSMiddleware, so a rebound request can't fish a successful
        preflight response out of the daemon."""
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
        client = make_client()

        resp = client.options(
            "/health",
            headers={
                "Host": "evil.example.com",
                "Origin": "https://lion-studio.khive.ai",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code == 400
        assert "Invalid Host header" in resp.json()["detail"]
        assert resp.headers.get("access-control-allow-origin") is None


@pytest.mark.integration
class TestJsonContentTypeEnforcement:
    """State-changing /api requests must declare application/json when they
    carry a body -- closes the simple-request (no-preflight) JSON CSRF gap
    that FastAPI's own body parsing (which ignores Content-Type) leaves open.
    """

    def test_text_plain_body_on_bodyless_route_is_rejected(
        self, monkeypatch, tmp_path, make_client
    ):
        """Representative body-less, side-effecting route: enabling a
        schedule takes only a path param, so FastAPI would otherwise execute
        it regardless of Content-Type. A text/plain simple-request body must
        now be rejected before the handler runs."""
        client = make_client()

        resp = client.post(
            "/api/schedules/some-id/enable",
            content='{"pwn": true}',
            headers={"Content-Type": "text/plain"},
        )
        assert resp.status_code == 415
        assert resp.json()["detail"] == "Content-Type must be application/json"

    async def test_chunked_text_plain_body_is_rejected(self, monkeypatch, tmp_path):
        """A streamed body with no Content-Length (Transfer-Encoding: chunked)
        must not slip past the Content-Type gate just because the middleware
        can't see a Content-Length header."""
        httpx = pytest.importorskip("httpx", reason="httpx not installed")

        import lionagi.studio.app as app_mod
        import lionagi.studio.services.invocations as inv_mod
        import lionagi.studio.services.sessions as sess_mod
        import lionagi.studio.services.stats as stats_mod

        fake_db = tmp_path / "state.db"
        for mod in (stats_mod, inv_mod, sess_mod):
            if hasattr(mod, "DEFAULT_DB_PATH"):
                monkeypatch.setattr(mod, "DEFAULT_DB_PATH", fake_db)
            if hasattr(mod, "_DB"):
                monkeypatch.setattr(mod, "_DB", str(fake_db))
        app = app_mod.create_app()

        async def _chunks():
            yield b'{"pwn": true}'

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as ac:
            resp = await ac.post(
                "/api/schedules/some-id/enable",
                content=_chunks(),
                headers={"Content-Type": "text/plain", "Transfer-Encoding": "chunked"},
            )
        assert resp.status_code == 415
        assert resp.json()["detail"] == "Content-Type must be application/json"

    def test_normal_json_path_still_works(self, monkeypatch, tmp_path, make_client):
        """A route with a required Pydantic body, sent the way the SPA sends
        it (application/json), must not be blocked by the new middleware --
        it should reach validation/business logic, not get stopped at 415."""
        client = make_client()

        resp = client.post(
            "/api/projects/",
            json={"name": "x"},
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code != 415

    def test_empty_body_post_from_spa_shape_still_works(self, monkeypatch, tmp_path, make_client):
        """The SPA sends several POSTs with no body and no Content-Type at all
        (e.g. schedule trigger/enable/disable, invocation cancel). These must
        stay reachable -- the middleware only gates requests that *carry* a
        body, so a genuinely empty POST must pass through to the handler
        (here surfacing as 404 for a nonexistent id, not 415)."""
        client = make_client()

        resp = client.post("/api/schedules/some-id/trigger")
        assert resp.status_code != 415

    def test_get_requests_are_never_gated_by_content_type(self, monkeypatch, tmp_path, make_client):
        client = make_client()

        resp = client.get("/api/sessions/", headers={"Content-Type": "text/plain"})
        assert resp.status_code != 415
