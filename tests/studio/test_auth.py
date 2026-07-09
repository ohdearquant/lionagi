# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Attack-driven tests for bearer-token protection on GET routes.

The middleware uses a default-deny posture: when a token is configured, every
path not in the explicit public allowlist returns 401. The only public path is
/health.
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
    import lionagi.studio.app as app_mod
    import lionagi.studio.services.invocations as inv_mod
    import lionagi.studio.services.sessions as sess_mod
    import lionagi.studio.services.stats as stats_mod

    fake_db = tmp_path / "state.db"

    # Redirect all DB-backed services to the throw-away path so the app stays hermetic.
    for mod in (stats_mod, inv_mod, sess_mod):
        if hasattr(mod, "DEFAULT_DB_PATH"):
            monkeypatch.setattr(mod, "DEFAULT_DB_PATH", fake_db)
        if hasattr(mod, "_DB"):
            monkeypatch.setattr(mod, "_DB", str(fake_db))

    # A fresh app instance (via create_app()) instead of importlib.reload(app_mod):
    # reload mutates the shared module singleton every other importer holds a
    # reference to, which is both a data race under xdist and re-executes
    # module-level side effects (CORS regex compilation, route
    # re-registration) on a namespace other code still imports.
    app = app_mod.create_app()
    return TestClient(app, raise_server_exceptions=False, base_url="http://127.0.0.1:8765")


@pytest.mark.integration
class TestGetInvocationsAuthGuard:
    """GET /api/invocations must be gated by the bearer token."""

    def test_unauthenticated_request_is_rejected(self, monkeypatch, tmp_path):
        """GET /api/invocations/ without a token returns 401 when auth is configured."""
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
        """GET /api/sessions/ without a token returns 401 when auth is configured."""
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
    """Every data-returning GET prefix must be 401 when a token is configured."""

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
