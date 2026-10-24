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

import lionagi.state.db as state_db_mod


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

    fake_db = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)

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

        assert any(origin == "https://lion-studio.khive.ai" for origin in config_mod.CORS_ORIGINS)

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

    def test_the_wildcard_bind_host_is_refused_on_an_api_origin_request(
        self, monkeypatch, tmp_path, make_client
    ):
        """A page whose origin is the wildcard it was bound to does not get the
        API: `::` is not a name a browser opens Studio at, so a request naming it
        as its own Host is refused before any origin is considered."""
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("LIONAGI_STUDIO_HOST", "::")
        client = make_client()

        resp = client.get(
            "/api/plugins", headers={"Host": "[::]:8765", "Origin": "http://[::]:8765"}
        )

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
        ("bind_host", "refused_hosts"),
        [
            ("0.0.0.0", ["0.0.0.0:8765", "0.0.0.0"]),
            ("::", ["[::]:8765", "[::]"]),
        ],
    )
    def test_an_unspecified_bind_host_is_refused_for_every_spelling(
        self, monkeypatch, tmp_path, make_client, bind_host, refused_hosts
    ):
        """An unspecified address (`0.0.0.0`, `::`) binds every interface; it is
        never a name a page is opened at, so it is not an admitted Host in either
        spelling. Admitting it would let any page reach the API by naming the
        wildcard, and would leave the published-URL checks unable to fail."""
        monkeypatch.setenv("LIONAGI_STUDIO_HOST", bind_host)
        client = make_client()

        for refused in refused_hosts:
            resp = client.get("/health", headers={"Host": refused})
            assert resp.status_code == 400, f"{refused} was admitted with {bind_host!r} set"

    @pytest.mark.parametrize(
        ("bind_host", "admitted", "refused"),
        [
            (
                "[2001:db8::1]",
                ["[2001:db8::1]:8765", "[2001:0db8::1]:8765", "[2001:DB8::1]"],
                ["[2001:db8::2]:8765", "192.168.1.5:8765", "2001:db8::1:8765"],
            ),
            (
                "2001:0db8::1",
                ["[2001:db8::1]:8765", "[2001:0db8::1]:8765"],
                ["[2001:db8::2]:8765"],
            ),
            (
                " 192.168.1.5 ",
                ["192.168.1.5:8765", "192.168.1.5"],
                ["192.168.1.6:8765", "localhost:8765.example.com"],
            ),
            (
                "[192.168.1.5]",
                ["192.168.1.5:8765"],
                ["192.168.1.6:8765"],
            ),
            (
                "[fe80::1]",
                ["[fe80::1]:8765", "[fe80:0000::1]:8765"],
                ["[fe80::2]:8765", "fe80::1:8765"],
            ),
            (
                "::ffff:192.168.1.5",
                ["192.168.1.5:8765", "[::ffff:c0a8:105]:8765", "[::ffff:7f00:1]"],
                # A dotted address inside brackets stays refused at the grammar, and
                # the mapped address of another IPv4 address is another address.
                ["192.168.1.6:8765", "[::ffff:c0a8:106]:8765", "[::ffff:192.168.1.5]:8765"],
            ),
        ],
    )
    def test_a_bind_address_admits_its_own_spellings_and_nothing_else(
        self, monkeypatch, tmp_path, make_client, bind_host, admitted, refused
    ):
        """The Host guard judges the address a Host names, not how it is spelled.

        A bind written with brackets, in a non-canonical IPv6 spelling or as an
        IPv4-mapped address used to widen nothing, because the raw value was added
        to the admitted set while the request's Host was parsed into another
        string: a browser sending the address it was given got 400.
        """
        monkeypatch.setenv("LIONAGI_STUDIO_HOST", bind_host)
        client = make_client()

        for host in admitted:
            resp = client.get("/health", headers={"Host": host})
            assert resp.status_code == 200, f"{host!r} was refused with {bind_host!r} set"
        for host in refused:
            resp = client.get("/health", headers={"Host": host})
            assert resp.status_code == 400, f"{host!r} was admitted with {bind_host!r} set"

    @pytest.mark.parametrize(
        "bind_host",
        ["[2001:db8::1]", " 192.168.1.5 ", "::ffff:192.168.1.5", "[fe80::1]", ""],
    )
    def test_a_bind_address_still_admits_the_loopback_names(
        self, monkeypatch, tmp_path, make_client, bind_host
    ):
        """The bind address is added to the loopback names, not instead of them."""
        monkeypatch.setenv("LIONAGI_STUDIO_HOST", bind_host)
        client = make_client()

        for host in ("localhost:8765", "127.0.0.1:8765", "[::1]:8765"):
            resp = client.get("/health", headers={"Host": host})
            assert resp.status_code == 200, f"{host!r} was refused with {bind_host!r} set"

    @pytest.mark.parametrize(
        ("bind_host", "refused"),
        [
            ("[0:0:0:0:0:0:0:0]", ["[::]:8765", "[::]", "[0:0:0:0:0:0:0:0]:8765"]),
            ("[0.0.0.0]", ["0.0.0.0:8765", "0.0.0.0", "[0.0.0.0]:8765"]),
            ("::ffff:0.0.0.0", ["0.0.0.0:8765", "0.0.0.0", "[::ffff:0.0.0.0]:8765"]),
            ("[]", ["[]:8765", "0.0.0.0:8765"]),
        ],
    )
    def test_an_unspecified_bind_host_is_unspecified_in_every_spelling(
        self, monkeypatch, tmp_path, make_client, bind_host, refused
    ):
        """A wildcard written another way is still a wildcard: it binds every
        interface, so it is still not a Host a page is opened at, and it must not
        widen the admitted set to include its own spelling."""
        monkeypatch.setenv("LIONAGI_STUDIO_HOST", bind_host)
        client = make_client()

        for host in refused:
            resp = client.get("/health", headers={"Host": host})
            assert resp.status_code == 400, f"{host!r} was admitted with {bind_host!r} set"

    @pytest.mark.parametrize(
        ("bind_host", "host_header"),
        [
            ("[2001:db8::1]", "[2001:db8::1]:8765"),
            ("2001:0db8::1", "[2001:0db8::1]:8765"),
            ("::ffff:192.168.1.5", "[::ffff:c0a8:105]:8765"),
            (" 192.168.1.5 ", "192.168.1.5:8765"),
        ],
    )
    def test_a_tokenless_origin_naming_the_request_host_is_accepted(
        self, monkeypatch, tmp_path, make_client, bind_host, host_header
    ):
        """A page the daemon itself serves sends its own authority as its Origin.

        The Origin rule already unwraps an IPv4-mapped address on its loopback
        branch, so the same-origin branch is the one exercised here: it compares
        the Origin's authority with the request's Host, as the SPA on a daemon
        bound to another address does.
        """
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("LIONAGI_STUDIO_HOST", bind_host)
        client = make_client()

        resp = client.get(
            _UNROUTED, headers={"Host": host_header, "Origin": f"http://{host_header}"}
        )

        assert resp.status_code == 404, resp.text

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
    """Every state-changing /api request must declare application/json.

    The rule covers JSON-shaped bodies and empty action routes alike, closing
    both forms of simple-request (no-preflight) CSRF.
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

        fake_db = tmp_path / "state.db"
        monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
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

    def test_empty_body_post_without_json_content_type_is_rejected(
        self, monkeypatch, tmp_path, make_client
    ):
        """Bodyless mutators are still cross-site form-submit targets.

        Require the same non-simple JSON content type as body-carrying writes;
        otherwise enable/disable/trigger/cancel endpoints bypass the preflight
        that protects an unauthenticated hosted Studio.
        """
        client = make_client()

        resp = client.post("/api/schedules/some-id/trigger")
        assert resp.status_code == 415
        assert resp.json()["detail"] == "Content-Type must be application/json"

    def test_empty_body_post_with_json_content_type_reaches_handler(
        self, monkeypatch, tmp_path, make_client
    ):
        client = make_client()

        resp = client.post(
            "/api/schedules/some-id/trigger",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code != 415

    def test_get_requests_are_never_gated_by_content_type(self, monkeypatch, tmp_path, make_client):
        client = make_client()

        resp = client.get("/api/sessions/", headers={"Content-Type": "text/plain"})
        assert resp.status_code != 415


@pytest.mark.integration
class TestAuthenticatedIdentity:
    def test_identity_requires_bearer_and_returns_only_identity_and_version(
        self, monkeypatch, tmp_path, make_client
    ):
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "desktop-launch-token")
        client = make_client()

        assert client.get("/api/identity").status_code == 401
        assert (
            client.get(
                "/api/identity",
                headers={"Authorization": "Bearer wrong-token"},
            ).status_code
            == 401
        )

        response = client.get(
            "/api/identity",
            headers={"Authorization": "Bearer desktop-launch-token"},
        )

        from lionagi.version import __version__

        assert response.status_code == 200
        assert response.json() == {
            "identity": "lionagi-studio",
            "version": __version__,
        }

    def test_identity_refuses_on_a_daemon_that_configured_no_token(
        self, monkeypatch, tmp_path, make_client
    ):
        """A 200 here must mean the caller authenticated, not merely that
        something on the port answers to the name. The bearer middleware
        skips entirely when no token is set, so without a route-level refusal
        a stale unauthenticated daemon holding the port passes the handshake.
        """
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        client = make_client()

        assert client.get("/api/identity").status_code == 401
        assert (
            client.get(
                "/api/identity",
                headers={"Authorization": "Bearer any-token-at-all"},
            ).status_code
            == 401
        )


_HOSTED = "https://lion-studio.khive.ai"
# A dev page's address as Vite reports it; not one of the default CORS origins.
_DEV_PAGE = "http://127.0.0.1:3000"
# The Host a dev page's requests carry: Vite's proxy (changeOrigin false) keeps the page's Host.
_DEV_PAGE_HOST = "127.0.0.1:3000"
_LOOPBACK = {"Host": "127.0.0.1:8765"}
# No route answers here, so an accepted request is a 404 and a refused one is a 401.
_UNROUTED = "/api/no-such-route"
_CONFIRM = "/api/operator/conversations/c-1/proposals/p-1/confirm"
_DECISION = "/api/operator/conversations/c-1/proposals/p-1/decision"


@pytest.mark.integration
class TestRemoteOriginNeedsToken:
    """With no token configured, a browser origin that is neither loopback nor the
    daemon's own is refused on every gated path; command-line and loopback callers
    are not."""

    @pytest.fixture(autouse=True)
    def _no_token(self, monkeypatch):
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
        monkeypatch.delenv("LIONAGI_STUDIO_HOST", raising=False)

    def test_the_hosted_origin_is_refused_on_a_read_and_says_why(self, make_client):
        client = make_client()

        resp = client.get(_UNROUTED, headers={**_LOOPBACK, "Origin": _HOSTED})

        assert resp.status_code == 401
        assert "LIONAGI_STUDIO_AUTH_TOKEN" in resp.json()["detail"]
        # CORS still answers, so the page can read the refusal instead of a network error.
        assert resp.headers.get("access-control-allow-origin") == _HOSTED

    def test_the_hosted_origin_is_refused_on_schema_paths(self, make_client):
        client = make_client()

        assert (
            client.get("/openapi.json", headers={**_LOOPBACK, "Origin": _HOSTED}).status_code == 401
        )
        assert client.get("/openapi.json", headers=_LOOPBACK).status_code == 200

    @pytest.mark.parametrize("path", [_CONFIRM, _DECISION])
    def test_the_hosted_origin_cannot_approve_an_operator_proposal(self, make_client, path):
        client = make_client()

        refused = client.post(path, json={}, headers={**_LOOPBACK, "Origin": _HOSTED})
        local = client.post(path, json={}, headers={**_LOOPBACK, "Origin": "http://localhost:5173"})

        assert refused.status_code == 401
        assert local.status_code != 401

    def test_health_and_the_spa_shell_stay_public(self, make_client):
        client = make_client()

        assert client.get("/health", headers={**_LOOPBACK, "Origin": _HOSTED}).status_code == 200
        assert client.get("/runs/abc", headers={**_LOOPBACK, "Origin": _HOSTED}).status_code != 401

    def test_a_request_with_no_origin_is_accepted(self, make_client):
        client = make_client()

        assert client.get(_UNROUTED, headers=_LOOPBACK).status_code == 404

    @pytest.mark.parametrize("site", ["none", "same-origin", "Same-Origin"])
    def test_a_request_with_no_origin_from_this_daemon_or_typed_is_accepted(
        self, make_client, site
    ):
        client = make_client()

        resp = client.get(_UNROUTED, headers={**_LOOPBACK, "Sec-Fetch-Site": site})

        assert resp.status_code == 404

    @pytest.mark.parametrize("site", ["cross-site", "Cross-Site", "same-site", "", "unknown"])
    def test_a_page_whose_browser_sent_no_origin_is_refused(self, make_client, site):
        """An image, a script or a navigation from another page carries no Origin;
        its Sec-Fetch-Site still says where it came from."""
        client = make_client()

        read = client.get(_UNROUTED, headers={**_LOOPBACK, "Sec-Fetch-Site": site})
        schema = client.get("/openapi.json", headers={**_LOOPBACK, "Sec-Fetch-Site": site})

        assert read.status_code == 401
        assert "LIONAGI_STUDIO_AUTH_TOKEN" in read.json()["detail"]
        assert schema.status_code == 401

    def test_two_sec_fetch_site_headers_are_refused(self, make_client):
        client = make_client()

        resp = client.get(
            _UNROUTED,
            headers=[
                ("Host", "127.0.0.1:8765"),
                ("Sec-Fetch-Site", "same-origin"),
                ("Sec-Fetch-Site", "cross-site"),
            ],
        )

        assert resp.status_code == 401

    def test_a_loopback_origin_is_judged_by_its_origin_not_its_fetch_site(self, make_client):
        """A development page on localhost calling the daemon on 127.0.0.1 is
        cross-site to the browser and sends its Origin."""
        client = make_client()

        resp = client.get(
            _UNROUTED,
            headers={
                **_LOOPBACK,
                "Origin": "http://localhost:5173",
                "Sec-Fetch-Site": "cross-site",
            },
        )

        assert resp.status_code == 404

    def test_a_cross_site_navigation_still_loads_the_spa_shell(self, make_client):
        client = make_client()

        resp = client.get("/runs/abc", headers={**_LOOPBACK, "Sec-Fetch-Site": "cross-site"})

        assert resp.status_code != 401

    @pytest.mark.parametrize(
        "origin",
        [
            "http://localhost:5173",
            "http://LOCALHOST:3000",
            "http://127.0.0.1:8765",
            "http://127.9.8.7:1",
            "http://[::1]:5173",
            "http://[::ffff:7f00:1]:5173",
            "https://localhost",
        ],
    )
    def test_a_loopback_origin_on_any_port_is_accepted(self, make_client, origin):
        client = make_client()

        assert client.get(_UNROUTED, headers={**_LOOPBACK, "Origin": origin}).status_code == 404

    @pytest.mark.parametrize(
        "origin",
        [
            "null",
            "",
            "file://",
            "chrome-extension://abcdef",
            "http://localhost.example.com",
            "http://127.0.0.1.example.com",
            "http://user@localhost:5173",
            "http://localhost:5173/path",
            "http://10.0.0.5:8765",
            "http://[::ffff:a00:5]:8765",
            "http://127.1:8765",
            "http://[::1",
        ],
    )
    def test_any_other_origin_is_refused(self, make_client, origin):
        client = make_client()

        resp = client.get(_UNROUTED, headers={**_LOOPBACK, "Origin": origin})

        assert resp.status_code == 401

    def test_two_origin_headers_are_refused(self, make_client):
        client = make_client()

        resp = client.get(
            _UNROUTED,
            headers=[
                ("Host", "127.0.0.1:8765"),
                ("Origin", "http://localhost:5173"),
                ("Origin", _HOSTED),
            ],
        )

        assert resp.status_code == 401

    def test_a_refused_origin_cannot_update_a_session(self, make_client):
        client = make_client()
        path = "/api/sessions/no-such-session"
        body = {"display_name": "renamed"}

        refused = client.put(
            path, headers={**_LOOPBACK, "Origin": "http://10.0.0.5:8765"}, json=body
        )
        accepted = client.put(
            path, headers={**_LOOPBACK, "Origin": "http://localhost:5173"}, json=body
        )

        assert refused.status_code == 401
        assert accepted.status_code != 401

    def test_the_daemons_own_origin_is_accepted_on_a_configured_bind_host(
        self, monkeypatch, make_client
    ):
        """A daemon bound to another address serves its own SPA; that page's
        requests carry the daemon's own origin and are accepted. Another port on
        the same address is another origin and is refused."""
        monkeypatch.setenv("LIONAGI_STUDIO_HOST", "192.168.1.5")
        client = make_client()
        own = {"Host": "192.168.1.5:8765"}

        assert (
            client.get(_UNROUTED, headers={**own, "Origin": "http://192.168.1.5:8765"}).status_code
            == 404
        )
        assert (
            client.get(_UNROUTED, headers={**own, "Origin": "http://192.168.1.5:9999"}).status_code
            == 401
        )

    def test_the_startup_warning_states_the_rule(self, caplog):
        import lionagi.studio.app as app_mod

        with caplog.at_level("WARNING", logger=app_mod.__name__):
            app_mod._emit_startup_warnings()

        assert "Sec-Fetch-Site none/same-origin pass" in caplog.text
        assert "Origin outside loopback/the daemon needs the paired bearer" in caplog.text


@pytest.mark.integration
class TestRemoteOriginWithToken:
    """With a token configured the bearer check applies to every caller, as before."""

    @pytest.fixture(autouse=True)
    def _token(self, monkeypatch):
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "studio-test-token")
        monkeypatch.delenv("CORS_ORIGINS", raising=False)

    def test_the_hosted_origin_with_the_bearer_is_accepted(self, make_client):
        client = make_client()

        resp = client.get(
            _UNROUTED,
            headers={**_LOOPBACK, "Origin": _HOSTED, "Authorization": "Bearer studio-test-token"},
        )

        assert resp.status_code == 404

    @pytest.mark.parametrize("auth", [None, "Bearer wrong", "studio-test-token"])
    def test_the_hosted_origin_without_the_bearer_is_refused(self, make_client, auth):
        client = make_client()
        headers = {**_LOOPBACK, "Origin": _HOSTED}
        if auth is not None:
            headers["Authorization"] = auth

        resp = client.get(_UNROUTED, headers=headers)

        assert resp.status_code == 401
        assert resp.json() == {"detail": "Unauthorized"}

    def test_a_loopback_origin_still_needs_the_bearer(self, make_client):
        client = make_client()

        resp = client.get(_UNROUTED, headers={**_LOOPBACK, "Origin": "http://localhost:5173"})

        assert resp.status_code == 401

    def test_the_bearer_alone_decides_whatever_the_fetch_site(self, make_client):
        client = make_client()
        cross = {**_LOOPBACK, "Sec-Fetch-Site": "cross-site"}

        accepted = client.get(
            _UNROUTED, headers={**cross, "Authorization": "Bearer studio-test-token"}
        )
        refused = client.get(_UNROUTED, headers={**_LOOPBACK, "Sec-Fetch-Site": "same-origin"})

        assert accepted.status_code == 404
        assert refused.status_code == 401


@pytest.mark.parametrize(
    ("origins", "host", "accepted"),
    [
        ([], "127.0.0.1:8765", True),
        (["http://localhost:5173"], "127.0.0.1:8765", True),
        (["http://[::1]"], "127.0.0.1:8765", True),
        (["https://lion-studio.khive.ai"], "127.0.0.1:8765", False),
        (["http://192.168.1.5:8765"], "192.168.1.5:8765", True),
        (["http://192.168.1.5:8765"], "127.0.0.1:8765", False),
        (["http://[::1"], "127.0.0.1:8765", False),
        (["http://localhost:5173", "http://localhost:5173"], "127.0.0.1:8765", False),
    ],
)
def test_origin_accepted_without_token(origins, host, accepted):
    from lionagi.studio.app import _origin_accepted_without_token

    assert _origin_accepted_without_token(origins, host) is accepted


@pytest.mark.parametrize(
    ("sites", "accepted"),
    [
        ([], True),
        (["none"], True),
        (["same-origin"], True),
        ([" SAME-ORIGIN "], True),
        (["same-site"], False),
        (["cross-site"], False),
        ([""], False),
        (["same-origin", "same-origin"], False),
    ],
)
def test_fetch_site_accepted_without_origin(sites, accepted):
    from lionagi.studio.app import _fetch_site_accepted_without_origin

    assert _fetch_site_accepted_without_origin(sites) is accepted


_PAIR = "/api/studio/pair"
_MINT = "/api/studio/pairing-codes"
_DISCONNECT = "/api/studio/disconnect"


@pytest.fixture()
def fresh_launch(monkeypatch):
    """A process with no launch bearer and no codes yet."""
    import lionagi.studio.launch_access as launch_access

    monkeypatch.setattr(launch_access, "_bearer", None)
    monkeypatch.setattr(launch_access, "_codes", {})
    monkeypatch.setattr(launch_access, "_capabilities", {}, raising=False)
    monkeypatch.setattr(launch_access, "_launch_codes", {}, raising=False)
    monkeypatch.setattr(launch_access, "_launch_origins", set())
    return launch_access


@pytest.mark.integration
class TestPairing:
    """A page on the hosted origin gets the launch bearer for a one-time code."""

    @pytest.fixture(autouse=True)
    def _no_token(self, monkeypatch, fresh_launch):
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
        monkeypatch.delenv("LIONAGI_STUDIO_HOST", raising=False)

    def _pair(self, client, code, origin=_HOSTED, host=None):
        headers = dict(_LOOPBACK)
        if host is not None:
            headers["Host"] = host
        if origin is not None:
            headers["Origin"] = origin
        return client.post(_PAIR, json={"code": code}, headers=headers)

    def test_the_hosted_origin_is_refused_without_the_bearer_and_accepted_with_it(
        self, make_client, fresh_launch
    ):
        client = make_client()
        code = fresh_launch.mint_pairing_code()

        before = client.get(_UNROUTED, headers={**_LOOPBACK, "Origin": _HOSTED})
        paired = self._pair(client, code)
        token = paired.json()["token"]
        after = client.get(
            _UNROUTED,
            headers={**_LOOPBACK, "Origin": _HOSTED, "Authorization": f"Bearer {token}"},
        )

        assert before.status_code == 401
        assert paired.status_code == 200
        assert paired.headers.get("access-control-allow-origin") == _HOSTED
        assert after.status_code == 404

    def test_a_spent_code_is_refused(self, make_client, fresh_launch):
        client = make_client()
        code = fresh_launch.mint_pairing_code()

        assert self._pair(client, code).status_code == 200
        second = self._pair(client, code)

        assert second.status_code == 401
        assert "li studio" in second.json()["detail"]

    def test_an_expired_code_is_refused(self, make_client, fresh_launch, monkeypatch):
        client = make_client()
        code = fresh_launch.mint_pairing_code()
        now = fresh_launch.time.monotonic()
        monkeypatch.setattr(
            fresh_launch.time, "monotonic", lambda: now + fresh_launch.PAIRING_CODE_TTL_S + 1
        )

        assert self._pair(client, code).status_code == 401

    def test_a_code_this_daemon_did_not_issue_is_refused(self, make_client, fresh_launch):
        client = make_client()
        fresh_launch.mint_pairing_code()

        assert self._pair(client, "not-a-code").status_code == 401

    @pytest.mark.parametrize("origin", ["https://evil.example", "http://10.0.0.5:8765", None])
    def test_pairing_refuses_an_origin_it_does_not_allow_and_keeps_the_code(
        self, make_client, fresh_launch, origin
    ):
        """Any other page that came by a code cannot pair with it, and the code
        stays good for the page it was minted for."""
        client = make_client()
        code = fresh_launch.mint_pairing_code()

        refused = self._pair(client, code, origin=origin)

        assert refused.status_code == 403
        assert self._pair(client, code).status_code == 200

    def test_a_page_on_the_origin_a_launcher_named_pairs_and_its_neighbour_does_not(
        self, make_client, fresh_launch
    ):
        """`li studio --dev` names its page's exact origin, which CORS need not allow."""
        client = make_client()
        code = fresh_launch.mint_pairing_code(origins=[_DEV_PAGE], for_launch=True)

        refused = self._pair(client, code, origin="http://127.0.0.1:3001", host="127.0.0.1:3001")
        paired = self._pair(client, code, origin=_DEV_PAGE, host=_DEV_PAGE_HOST)

        assert refused.status_code == 403
        assert paired.status_code == 200
        principal = fresh_launch.resolve_decision_capability(
            paired.json()["decision_token"], origin=_DEV_PAGE
        )
        assert principal is not None
        assert principal.origin == _DEV_PAGE

    def test_a_code_minted_for_no_named_origin_admits_no_new_page(self, make_client, fresh_launch):
        client = make_client()
        code = fresh_launch.mint_pairing_code(for_launch=True)

        assert not fresh_launch._launch_origins
        assert self._pair(client, code, origin=_DEV_PAGE, host=_DEV_PAGE_HOST).status_code == 403
        assert self._pair(client, code).status_code == 200

    def test_a_page_address_written_another_way_pairs_from_the_origin_its_browser_sends(
        self, make_client, fresh_launch
    ):
        client = make_client()
        code = fresh_launch.mint_pairing_code(origins=[f"{_DEV_PAGE}/"], for_launch=True)

        assert self._pair(client, code, origin=_DEV_PAGE, host=_DEV_PAGE_HOST).status_code == 200

    def test_a_named_page_asking_the_daemon_directly_is_refused_before_the_code_is_spent(
        self, make_client, fresh_launch
    ):
        """CORS answers no page on a named origin it does not allow, so that page
        could not read a bearer the route handed it."""
        client = make_client()
        code = fresh_launch.mint_pairing_code(origins=[_DEV_PAGE], for_launch=True)

        direct = self._pair(client, code, origin=_DEV_PAGE)
        proxied = self._pair(client, code, origin=_DEV_PAGE, host=_DEV_PAGE_HOST)

        assert direct.status_code == 403
        assert "access-control-allow-origin" not in direct.headers
        assert proxied.status_code == 200

    def test_the_launch_bearer_is_not_in_the_environment(self, make_client, fresh_launch):
        import os

        client = make_client()
        token = self._pair(client, fresh_launch.mint_pairing_code()).json()["token"]

        assert token not in os.environ.values()
        assert "LIONAGI_STUDIO_AUTH_TOKEN" not in os.environ

    def test_a_request_with_no_origin_from_another_site_is_accepted_with_the_launch_bearer(
        self, make_client, fresh_launch
    ):
        client = make_client()
        token = self._pair(client, fresh_launch.mint_pairing_code()).json()["token"]
        cross = {**_LOOPBACK, "Sec-Fetch-Site": "cross-site"}

        accepted = client.get(_UNROUTED, headers={**cross, "Authorization": f"Bearer {token}"})
        refused = client.get(_UNROUTED, headers={**cross, "Authorization": "Bearer not-the-one"})

        assert accepted.status_code == 404
        assert refused.status_code == 401

    def test_a_wrong_bearer_from_the_hosted_origin_is_refused(self, make_client, fresh_launch):
        client = make_client()
        self._pair(client, fresh_launch.mint_pairing_code())

        resp = client.get(
            _UNROUTED,
            headers={**_LOOPBACK, "Origin": _HOSTED, "Authorization": "Bearer not-the-one"},
        )

        assert resp.status_code == 401

    @pytest.mark.parametrize(
        "headers",
        [
            _LOOPBACK,
            {**_LOOPBACK, "Origin": "http://localhost:5173"},
            {**_LOOPBACK, "Origin": _HOSTED},
            {**_LOOPBACK, "Sec-Fetch-Site": "none"},
            {**_LOOPBACK, "Sec-Fetch-Site": "cross-site"},
            {**_LOOPBACK, "X-Studio-Decision-Token": "anything"},
        ],
        ids=["command-line", "dev-page", "hosted-page", "typed", "cross-site", "decision-header"],
    )
    def test_no_http_request_can_mint_a_code(self, make_client, headers):
        """The mint route is retired: whatever headers a request carries, an
        admitted one answers 410 and a page on another origin never gets a code.

        This replaces the test that pinned a command-line mint: that route is how
        a program that can only speak HTTP obtained a code, and so a bearer."""
        client = make_client()

        resp = client.post(_MINT, json={}, headers=headers)

        assert resp.status_code in (401, 410)
        if resp.status_code == 410:
            assert "code" not in resp.json()
            assert "li studio" in resp.json()["detail"]

    def test_pairing_returns_a_decision_capability_bound_to_the_page_origin(
        self, make_client, fresh_launch
    ):
        client = make_client()

        resp = self._pair(client, fresh_launch.mint_pairing_code())

        assert resp.status_code == 200
        body = resp.json()
        assert body["token"]
        principal = fresh_launch.resolve_decision_capability(body["decision_token"], origin=_HOSTED)
        assert principal is not None
        assert principal.origin == _HOSTED
        assert principal.scope == "operator:decide"
        assert (
            fresh_launch.resolve_decision_capability(
                body["decision_token"], origin="http://localhost:5173"
            )
            is None
        )

    def test_an_explicit_disconnect_revokes_the_capability(self, make_client, fresh_launch):
        client = make_client()
        capability = self._pair(client, fresh_launch.mint_pairing_code()).json()["decision_token"]

        revoked = client.post(
            _DISCONNECT,
            json={},
            headers={
                **_LOOPBACK,
                "Origin": _HOSTED,
                "Authorization": f"Bearer {fresh_launch.launch_bearer()}",
                "X-Studio-Decision-Token": capability,
            },
        )
        after = fresh_launch.resolve_decision_capability(capability, origin=_HOSTED)

        assert revoked.status_code == 200
        assert revoked.json() == {"revoked": True}
        assert after is None


@pytest.mark.integration
class TestPairingWithToken:
    """With a configured token, pairing hands the page that token, and the launch
    bearer opens nothing."""

    @pytest.fixture(autouse=True)
    def _token(self, monkeypatch, fresh_launch):
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "studio-test-token")
        monkeypatch.delenv("CORS_ORIGINS", raising=False)

    def test_pairing_returns_the_configured_token_and_a_decision_capability(
        self, make_client, fresh_launch
    ):
        client = make_client()
        code = fresh_launch.mint_pairing_code()

        resp = client.post(_PAIR, json={"code": code}, headers={**_LOOPBACK, "Origin": _HOSTED})

        assert resp.status_code == 200
        body = resp.json()
        assert body["token"] == "studio-test-token"
        assert fresh_launch.resolve_decision_capability(body["decision_token"], origin=_HOSTED)

    def test_the_configured_token_issues_no_code_and_is_not_a_decision_capability(
        self, make_client
    ):
        """``LIONAGI_STUDIO_AUTH_TOKEN`` admits requests as today; it issues no
        code and is not a decision capability. A decision it admits is still
        accepted, unattributed (``test_operator_protocol``)."""
        client = make_client()
        bearer = {**_LOOPBACK, "Authorization": "Bearer studio-test-token"}

        assert client.post(_MINT, json={}, headers=bearer).status_code == 410
        import lionagi.studio.launch_access as launch_access

        assert (
            launch_access.resolve_decision_capability("studio-test-token", origin=_HOSTED) is None
        )

    def test_the_launch_bearer_is_not_accepted_in_token_mode(self, make_client, fresh_launch):
        client = make_client()
        fresh_launch.mint_pairing_code()
        launch = fresh_launch.launch_bearer()

        resp = client.get(
            _UNROUTED,
            headers={**_LOOPBACK, "Origin": _HOSTED, "Authorization": f"Bearer {launch}"},
        )

        assert resp.status_code == 401


# /health answers a reconnect challenge


def test_health_signs_a_challenge_with_the_port_the_request_reached(make_client):
    from lionagi.studio.reconnect import port_proof

    client = make_client()
    key = bytes(range(32))
    client.app.state.reconnect_key = key
    challenge = "0123456789abcdef" * 2

    body = client.get("/health", params={"challenge": challenge}).json()

    # The client's base URL is 127.0.0.1:8765, so the request reached port 8765.
    assert body == {"status": "ok", "proof": port_proof(key, challenge, 8765)}
    assert body["proof"] != port_proof(key, challenge, 8766)


def test_health_signs_nothing_without_a_key_or_for_a_malformed_challenge(make_client):
    client = make_client()
    client.app.state.reconnect_key = None
    assert client.get("/health", params={"challenge": "0" * 32}).json() == {"status": "ok"}

    client.app.state.reconnect_key = bytes(32)
    for malformed in ("", "0" * 31, "0" * 33, "g" * 32, "A" * 32, "0" * 31 + "\n"):
        resp = client.get("/health", params={"challenge": malformed})
        assert resp.json() == {"status": "ok"}, malformed
    assert client.get("/health").json() == {"status": "ok"}
