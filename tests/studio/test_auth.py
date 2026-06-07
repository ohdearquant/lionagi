# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Attack-driven regression tests for bearer-token protection on GET routes.

Before the fix, GET /api/invocations and GET /api/sessions returned agent-
produced run data without authentication, even when LIONAGI_STUDIO_AUTH_TOKEN
was set.  Any unauthenticated caller could enumerate all sessions and
invocations.  These tests assert that the guard fires on those routes and that
a valid token still allows access.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")

from fastapi.testclient import TestClient  # noqa: E402


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
        assert resp.status_code != 401

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
        assert resp.status_code != 401

    def test_open_when_no_token_configured(self, monkeypatch, tmp_path):
        """When LIONAGI_STUDIO_AUTH_TOKEN is absent, /api/sessions/ is open."""
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        client = _make_client(monkeypatch, tmp_path)

        resp = client.get("/api/sessions/")
        assert resp.status_code != 401
