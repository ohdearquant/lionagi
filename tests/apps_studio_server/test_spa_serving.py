# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for SPA static serving (Vite dist/ mount in lionagi/studio/app.py).

Covers:
- SPA fallback serves index.html for /, /runs, /runs/abc, /agents
- /api/* unknown paths return 404, not index.html
- Existing API routes are unaffected
- No-dist case: app works API-only (no crash, / returns 404 or JSON)
- index.html response carries no-cache headers
- CORS methods still include HEAD after the SPA mount (registration-order guard)

Test strategy: set LIONAGI_STUDIO_FRONTEND_DIST to a tmp_path dir we populate
with a real index.html and an assets/ subdirectory.  We reload the app module so
the module-level _resolve_frontend_dist() and _mount_spa() calls re-run with the
new env.  We do NOT mock _resolve_frontend_dist itself — we test through the real
construction path per spec.
"""

from __future__ import annotations

import os
from importlib import reload
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")

from fastapi.testclient import TestClient  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def dist_dir(tmp_path: Path) -> Path:
    """A minimal Vite-style dist/ directory."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        "<!doctype html><html><head></head><body><div id='root'></div></body></html>"
    )
    assets = dist / "assets"
    assets.mkdir()
    (assets / "index-abc123.js").write_text("// bundled js")
    (assets / "index-def456.css").write_text("/* bundled css */")
    return dist


@pytest.fixture()
def spa_client(
    dist_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """TestClient with SPA serving enabled (real dist dir via env override).

    Yield-fixture: teardown clears the env var and reloads the app module so
    the API-only singleton is restored for later tests in the same xdist worker.
    """
    fake_db = tmp_path / "state.db"

    import lionagi.studio.services.sessions as sessions_mod
    import lionagi.studio.services.stats as stats_mod

    monkeypatch.setattr(stats_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(stats_mod, "_DB", str(fake_db))
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(sessions_mod, "_DB", str(fake_db))
    monkeypatch.setenv("LIONAGI_STUDIO_FRONTEND_DIST", str(dist_dir))

    import lionagi.studio.app as app_mod

    reload(app_mod)
    yield TestClient(app_mod.app, raise_server_exceptions=False)

    # Restore the API-only module singleton so it does not leak into later
    # tests in the same xdist worker.  Fixture finalizers run LIFO: this code
    # executes BEFORE monkeypatch undoes setenv, so the env var must be
    # removed explicitly here or the reload re-mounts the SPA.
    os.environ.pop("LIONAGI_STUDIO_FRONTEND_DIST", None)
    reload(app_mod)


@pytest.fixture()
def no_dist_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """TestClient with no dist dir — API-only mode.

    Yield-fixture: teardown reloads the app module so the module singleton is
    restored for later tests in the same xdist worker.
    """
    fake_db = tmp_path / "state.db"

    import lionagi.studio.services.sessions as sessions_mod
    import lionagi.studio.services.stats as stats_mod

    monkeypatch.setattr(stats_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(stats_mod, "_DB", str(fake_db))
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(sessions_mod, "_DB", str(fake_db))
    # Ensure no dist is resolved (env var must be absent so _resolve_frontend_dist
    # returns None and the 404 exception handler is not registered on reload).
    monkeypatch.delenv("LIONAGI_STUDIO_FRONTEND_DIST", raising=False)

    import lionagi.studio.app as app_mod

    reload(app_mod)
    yield TestClient(app_mod.app, raise_server_exceptions=False)

    # The env var is still absent here (finalizers run LIFO, before monkeypatch
    # restores anything), so this reload restores the API-only singleton.
    reload(app_mod)


# ---------------------------------------------------------------------------
# SPA fallback routes
# ---------------------------------------------------------------------------


class TestSPAFallback:
    def test_root_serves_index_html(self, spa_client: TestClient) -> None:
        """GET / returns the SPA index.html."""
        resp = spa_client.get("/")
        assert resp.status_code == 200
        assert "root" in resp.text

    def test_runs_route_serves_index_html(self, spa_client: TestClient) -> None:
        """GET /runs returns index.html (client-side route)."""
        resp = spa_client.get("/runs")
        assert resp.status_code == 200
        assert "root" in resp.text

    def test_runs_detail_route_serves_index_html(self, spa_client: TestClient) -> None:
        """GET /runs/abc123 returns index.html (deep client-side route)."""
        resp = spa_client.get("/runs/abc123")
        assert resp.status_code == 200
        assert "root" in resp.text

    def test_agents_route_serves_index_html(self, spa_client: TestClient) -> None:
        """GET /agents returns index.html."""
        resp = spa_client.get("/agents")
        assert resp.status_code == 200
        assert "root" in resp.text

    def test_arbitrary_deep_path_serves_index_html(self, spa_client: TestClient) -> None:
        """GET /some/deep/nested/path returns index.html."""
        resp = spa_client.get("/some/deep/nested/path")
        assert resp.status_code == 200
        assert "root" in resp.text


# ---------------------------------------------------------------------------
# /api/* paths must NOT be swallowed by the SPA fallback
# ---------------------------------------------------------------------------


class TestAPIPathsNotSwallowed:
    def test_api_unknown_path_returns_404(self, spa_client: TestClient) -> None:
        """/api/nonexistent returns 404, not index.html."""
        resp = spa_client.get("/api/nonexistent")
        assert resp.status_code == 404
        # Must be JSON (FastAPI 404), not HTML
        assert "root" not in resp.text

    def test_api_stats_still_returns_json(self, spa_client: TestClient) -> None:
        """/api/stats returns JSON even when SPA is mounted."""
        resp = spa_client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)

    def test_api_runs_still_returns_json(self, spa_client: TestClient) -> None:
        """/api/runs returns JSON, not HTML."""
        resp = spa_client.get("/api/runs")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert "runs" in data

    def test_health_endpoint_unaffected(self, spa_client: TestClient) -> None:
        """/health (non-/api prefix) still returns JSON from its registered route."""
        resp = spa_client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# No-cache headers on index.html
# ---------------------------------------------------------------------------


class TestNoCacheHeaders:
    def test_index_html_has_no_cache_header(self, spa_client: TestClient) -> None:
        """index.html must carry Cache-Control: no-store (prevents stale SPA shell)."""
        resp = spa_client.get("/")
        cc = resp.headers.get("cache-control", "")
        assert "no-store" in cc or "no-cache" in cc, (
            f"Expected no-cache/no-store on index.html; got Cache-Control: {cc!r}"
        )

    def test_runs_route_index_html_has_no_cache(self, spa_client: TestClient) -> None:
        """/runs fallback also serves index.html with no-cache headers."""
        resp = spa_client.get("/runs")
        cc = resp.headers.get("cache-control", "")
        assert "no-store" in cc or "no-cache" in cc, (
            f"Expected no-cache/no-store on /runs; got Cache-Control: {cc!r}"
        )


# ---------------------------------------------------------------------------
# No-dist (API-only) mode
# ---------------------------------------------------------------------------


class TestNoDist:
    def test_api_routes_work_without_dist(self, no_dist_client: TestClient) -> None:
        """/api/runs works even when no dist/ exists."""
        resp = no_dist_client.get("/api/runs")
        assert resp.status_code == 200

    def test_root_returns_non_200_without_dist(self, no_dist_client: TestClient) -> None:
        """Without a dist/, / returns 404 (no SPA fallback registered)."""
        resp = no_dist_client.get("/")
        # FastAPI returns 404 for unmatched paths when no fallback is mounted.
        assert resp.status_code == 404

    def test_no_crash_without_dist(self, no_dist_client: TestClient) -> None:
        """App starts cleanly without a dist/ directory."""
        resp = no_dist_client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# CORS HEAD method present after SPA mount (registration-order regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestCORSAfterSPAMount:
    def test_head_in_cors_allowlist_with_spa(
        self,
        spa_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """HEAD must still be in the CORS allowlist after the SPA fallback is mounted.

        The SPA fallback is a GET route, which FastAPI auto-generates a HEAD
        companion for.  _collect_cors_methods must be called AFTER the SPA
        mount so HEAD from the fallback route is included.
        """
        import lionagi.studio.app as app_mod

        allowlist = {m.upper() for m in app_mod._collect_cors_methods(app_mod.app)}
        assert "HEAD" in allowlist, (
            f"HEAD must be in CORS allowlist after SPA mount; got {sorted(allowlist)}"
        )

    def test_options_preflight_head_succeeds_with_spa(
        self,
        spa_client: TestClient,
    ) -> None:
        """CORS preflight requesting HEAD must return 200/204 (not 400)."""
        resp = spa_client.options(
            "/",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "HEAD",
            },
        )
        assert resp.status_code in (200, 204), (
            f"HEAD preflight must succeed; got {resp.status_code}"
        )
        raw = resp.headers.get("access-control-allow-methods", "")
        allowed = {m.strip().upper() for m in raw.split(",")}
        assert "HEAD" in allowed, f"HEAD must be in Access-Control-Allow-Methods; got {raw!r}"


# ---------------------------------------------------------------------------
# Auth-mode interplay: shell public, API guarded
# ---------------------------------------------------------------------------


class TestSPAWithAuthToken:
    """With LIONAGI_STUDIO_AUTH_TOKEN set, the static shell must stay loadable.

    Browsers navigate without an Authorization header; the SPA shell and
    hashed assets are the public surface, while every /api path remains
    bearer-guarded.  Regression guard for the single-origin migration: the
    old two-process layout served the shell from Node (auth-free) and only
    guarded the FastAPI origin.
    """

    def test_shell_and_assets_public_with_token(
        self,
        spa_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "sekrit")
        assert spa_client.get("/").status_code == 200
        assert spa_client.get("/runs/abc").status_code == 200
        assert spa_client.get("/assets/index-abc123.js").status_code == 200

    def test_api_requires_token(
        self,
        spa_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "sekrit")
        assert spa_client.get("/api/stats").status_code == 401
        ok = spa_client.get("/api/stats", headers={"Authorization": "Bearer sekrit"})
        assert ok.status_code == 200

    def test_non_get_non_api_still_guarded(
        self,
        spa_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "sekrit")
        resp = spa_client.post("/runs")
        assert resp.status_code == 401

    def test_openapi_json_requires_token(
        self,
        spa_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GET /openapi.json must be 401 without a token in token mode."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "sekrit")
        assert spa_client.get("/openapi.json").status_code == 401
        ok = spa_client.get("/openapi.json", headers={"Authorization": "Bearer sekrit"})
        assert ok.status_code == 200

    def test_docs_requires_token(
        self,
        spa_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GET /docs must be 401 without a token in token mode."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "sekrit")
        assert spa_client.get("/docs").status_code == 401

    def test_redoc_requires_token(
        self,
        spa_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GET /redoc must be 401 without a token in token mode."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "sekrit")
        assert spa_client.get("/redoc").status_code == 401

    def test_spa_deep_links_still_public_with_token(
        self,
        spa_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Arbitrary non-API GET paths (SPA deep links) must stay public in token mode."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "sekrit")
        assert spa_client.get("/projects/my-project").status_code == 200
        assert spa_client.get("/agents/my-agent").status_code == 200
