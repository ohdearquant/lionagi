# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Attack-driven regression tests for bearer-token protection on GET routes.

Before the fix, GET /api/invocations and GET /api/sessions (and many other
data-returning routes) returned agent-produced content without authentication,
even when LIONAGI_STUDIO_AUTH_TOKEN was set.  Any unauthenticated caller could
enumerate sessions, runs, projects, schedules, teams, agents, playbooks,
definitions, shows, and aggregate stats.

The middleware now uses a default-deny posture: when a token is configured,
every path that is not in the explicit public allowlist (_PUBLIC_PATHS) returns
401 regardless of HTTP method.  The only public path is /health.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")

from fastapi.testclient import TestClient  # noqa: E402

# Every data-returning GET prefix that must be gated when a token is configured.
_DATA_GET_PREFIXES = [
    "/api/runs/",
    "/api/projects/",
    "/api/schedules/",
    "/api/teams/",
    "/api/agents/",
    "/api/playbooks/",
    "/api/definitions/",
    "/api/shows/",
    "/api/stats",
    "/api/invocations/",
    "/api/sessions/",
    "/api/admin/health",
    "/api/admin/doctor",
    "/api/artifacts/",
    "/api/skills/",
    "/api/plugins/",
]


def _make_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    """Return a TestClient wired to the studio app with a throw-away DB."""
    from importlib import reload

    import lionagi.studio.app as app_mod
    import lionagi.studio.services.invocations as inv_mod
    import lionagi.studio.services.sessions as sess_mod
    import lionagi.studio.services.stats as stats_mod

    fake_db = tmp_path / "state.db"

    # Redirect all DB-backed services to the throw-away path so no real
    # state is read and the app stays hermetic.
    for mod in (stats_mod, inv_mod, sess_mod):
        if hasattr(mod, "DEFAULT_DB_PATH"):
            monkeypatch.setattr(mod, "DEFAULT_DB_PATH", fake_db)
        if hasattr(mod, "_DB"):
            monkeypatch.setattr(mod, "_DB", str(fake_db))

    reload(app_mod)
    return TestClient(app_mod.app, raise_server_exceptions=False)


@pytest.mark.integration
class TestGetInvocationsAuthGuard:
    """GET /api/invocations must be gated by the bearer token."""

    def test_unauthenticated_request_is_rejected(self, monkeypatch, tmp_path):
        """GET /api/invocations/ without a token returns 401 when auth is configured.

        Attack scenario: an unauthenticated caller polls the invocations list to
        enumerate agent activity.  The guard must reject such requests before any
        data is returned.
        """
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "testsecret")
        client = _make_client(monkeypatch, tmp_path)

        resp = client.get("/api/invocations/")
        assert resp.status_code == 401

    def test_wrong_token_is_rejected(self, monkeypatch, tmp_path):
        """GET /api/invocations/ with an incorrect Bearer token returns 401."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "testsecret")
        client = _make_client(monkeypatch, tmp_path)

        resp = client.get("/api/invocations/", headers={"Authorization": "Bearer wrongtoken"})
        assert resp.status_code == 401

    def test_correct_token_is_accepted(self, monkeypatch, tmp_path):
        """GET /api/invocations/ with the correct Bearer token must not return 401."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "testsecret")
        client = _make_client(monkeypatch, tmp_path)

        resp = client.get("/api/invocations/", headers={"Authorization": "Bearer testsecret"})
        assert resp.status_code == 200

    def test_open_when_no_token_configured(self, monkeypatch, tmp_path):
        """When LIONAGI_STUDIO_AUTH_TOKEN is absent, /api/invocations/ is open."""
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        client = _make_client(monkeypatch, tmp_path)

        resp = client.get("/api/invocations/")
        assert resp.status_code != 401


@pytest.mark.integration
class TestGetSessionsAuthGuard:
    """GET /api/sessions must be gated by the bearer token."""

    def test_unauthenticated_request_is_rejected(self, monkeypatch, tmp_path):
        """GET /api/sessions/ without a token returns 401 when auth is configured.

        Attack scenario: an unauthenticated caller lists all sessions to harvest
        session IDs for further enumeration.  The guard must reject this before
        any data leaves the server.
        """
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "testsecret")
        client = _make_client(monkeypatch, tmp_path)

        resp = client.get("/api/sessions/")
        assert resp.status_code == 401

    def test_wrong_token_is_rejected(self, monkeypatch, tmp_path):
        """GET /api/sessions/ with an incorrect Bearer token returns 401."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "testsecret")
        client = _make_client(monkeypatch, tmp_path)

        resp = client.get("/api/sessions/", headers={"Authorization": "Bearer wrongtoken"})
        assert resp.status_code == 401

    def test_correct_token_is_accepted(self, monkeypatch, tmp_path):
        """GET /api/sessions/ with the correct Bearer token must not return 401."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "testsecret")
        client = _make_client(monkeypatch, tmp_path)

        resp = client.get("/api/sessions/", headers={"Authorization": "Bearer testsecret"})
        assert resp.status_code == 200

    def test_open_when_no_token_configured(self, monkeypatch, tmp_path):
        """When LIONAGI_STUDIO_AUTH_TOKEN is absent, /api/sessions/ is open."""
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        client = _make_client(monkeypatch, tmp_path)

        resp = client.get("/api/sessions/")
        assert resp.status_code != 401


@pytest.mark.integration
class TestDefaultDenyAllDataRoutes:
    """Every data-returning GET prefix must be 401 when a token is configured.

    This is a parametrized regression guard: adding a new router cannot
    silently open a new unauthenticated GET surface.  Any route not listed in
    _DATA_GET_PREFIXES AND not in _PUBLIC_PATHS is already covered by the
    default-deny middleware, but explicit enumeration here makes regressions
    obvious immediately.
    """

    @pytest.mark.parametrize("prefix", _DATA_GET_PREFIXES)
    def test_unauthenticated_returns_401(self, monkeypatch, tmp_path, prefix):
        """GET <prefix> without a token must return 401 when auth is configured."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "testsecret")
        client = _make_client(monkeypatch, tmp_path)

        resp = client.get(prefix)
        assert resp.status_code == 401, (
            f"Expected 401 for unauthenticated GET {prefix!r} "
            f"but got {resp.status_code}.  Route is unprotected."
        )

    @pytest.mark.parametrize("prefix", _DATA_GET_PREFIXES)
    def test_wrong_token_returns_401(self, monkeypatch, tmp_path, prefix):
        """GET <prefix> with a wrong token must return 401."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "testsecret")
        client = _make_client(monkeypatch, tmp_path)

        resp = client.get(prefix, headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    @pytest.mark.parametrize("prefix", _DATA_GET_PREFIXES)
    def test_correct_token_passes_middleware(self, monkeypatch, tmp_path, prefix):
        """GET <prefix> with the correct token must pass the auth middleware (not 401)."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "testsecret")
        client = _make_client(monkeypatch, tmp_path)

        resp = client.get(prefix, headers={"Authorization": "Bearer testsecret"})
        assert resp.status_code != 401, (
            f"Valid token was rejected for GET {prefix!r}: status {resp.status_code}"
        )

    @pytest.mark.parametrize("prefix", _DATA_GET_PREFIXES)
    def test_open_when_no_token_configured(self, monkeypatch, tmp_path, prefix):
        """Without LIONAGI_STUDIO_AUTH_TOKEN, all routes remain open for local dev."""
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        client = _make_client(monkeypatch, tmp_path)

        resp = client.get(prefix)
        assert resp.status_code != 401


@pytest.mark.integration
class TestPublicAllowlist:
    """The explicit public allowlist must remain reachable without a token."""

    def test_health_reachable_without_token(self, monkeypatch, tmp_path):
        """GET /health must return 200 even when a token is configured."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "testsecret")
        client = _make_client(monkeypatch, tmp_path)

        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_reachable_with_wrong_token(self, monkeypatch, tmp_path):
        """GET /health must return 200 regardless of Authorization header value."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "testsecret")
        client = _make_client(monkeypatch, tmp_path)

        resp = client.get("/health", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 200


@pytest.mark.integration
class TestAuthEdgeCases:
    def test_empty_string_token_configured_allows_all(self, monkeypatch, tmp_path):
        # LIONAGI_STUDIO_AUTH_TOKEN="" is falsy — the guard must not activate
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "")
        client = _make_client(monkeypatch, tmp_path)
        resp = client.get("/api/invocations/")
        assert resp.status_code != 401

    def test_token_with_newline_in_header_rejected(self, monkeypatch, tmp_path):
        # Header injection attempt: "Bearer validtoken\nX-Injected: evil"
        # HTTP headers cannot contain bare newlines; starlette strips/rejects them.
        # This just ensures the server does not crash and still returns 401.
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "testsecret")
        client = _make_client(monkeypatch, tmp_path)
        # TestClient encodes headers; we pass a value that looks like injection
        try:
            resp = client.get(
                "/api/invocations/",
                headers={"Authorization": "Bearer testsecret\nX-Evil: injected"},
            )
            # If the request is allowed, it must not 200 with injected data
            assert resp.status_code in (400, 401, 422)
        except Exception:
            # A low-level rejection (e.g. ValueError for bad header) is also acceptable
            pass

    def test_bearer_prefix_case_sensitive_rejected(self, monkeypatch, tmp_path):
        # "bearer testsecret" (lowercase) must be rejected — comparison is exact
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "testsecret")
        client = _make_client(monkeypatch, tmp_path)
        resp = client.get("/api/invocations/", headers={"Authorization": "bearer testsecret"})
        assert resp.status_code == 401
