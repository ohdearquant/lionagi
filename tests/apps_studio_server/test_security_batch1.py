# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for Batch 1 security fixes: #986, #987, #988."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")

from fastapi.testclient import TestClient  # noqa: E402

from tests.apps_studio_server._helpers import run_async as _run  # noqa: E402


def _make_fake_home(tmp_path: Path) -> Path:
    fake_home = tmp_path / "lionagi_home"
    fake_home.mkdir()
    (fake_home / "agents").mkdir()
    (fake_home / "playbooks").mkdir()
    (fake_home / "skills").mkdir()
    return fake_home


# ---------------------------------------------------------------------------
# #986 — public_path() never returns absolute paths
# ---------------------------------------------------------------------------


class TestPublicPath:
    def test_repo_relative_path(self, tmp_path):
        """public_path() with a path under the repo root returns relative posix."""
        # Simulate a path under the repo root (parents[4] from _path_safety.py)
        import lionagi.studio.services._path_safety as mod
        from lionagi.studio.services._path_safety import public_path

        repo_root = Path(mod.__file__).resolve().parents[3]
        target = repo_root / "lionagi" / "studio" / "dummy.md"
        result = public_path(target)
        assert not result.startswith("/"), f"Expected relative, got: {result!r}"
        assert "lionagi/studio/dummy.md" in result

    def test_home_relative_path(self, tmp_path):
        """public_path() with a path under home returns home-relative posix."""
        from lionagi.studio.services._path_safety import public_path

        target = Path.home() / ".lionagi" / "agents" / "test.md"
        result = public_path(target)
        assert not result.startswith("/"), f"Expected relative, got: {result!r}"
        assert ".lionagi/agents/test.md" in result

    def test_unknown_path_returns_filename(self, tmp_path):
        """public_path() with an unrelated path returns just the filename."""
        from lionagi.studio.services._path_safety import public_path

        # Use a path that is definitely outside repo and home
        target = Path("/var/log/system.log")
        result = public_path(target)
        assert "/" not in result or result == "system.log"
        assert result == "system.log"


# ---------------------------------------------------------------------------
# #986 — definitions disk_path is not absolute
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestDefinitionsDiskPath:
    def test_list_definitions_disk_path_not_absolute(self, tmp_path, monkeypatch):
        """list_definitions() must not expose absolute disk_path in response."""
        import lionagi.cli._runs as cli_runs_mod
        import lionagi.state.db as state_db_mod
        import lionagi.studio.services.definitions as defs_mod

        fake_home = _make_fake_home(tmp_path)
        agents_dir = fake_home / "agents"
        (agents_dir / "myagent.md").write_text("# Agent\ncontent")

        fake_db = tmp_path / "state.db"

        monkeypatch.setattr(cli_runs_mod, "LIONAGI_HOME", fake_home)
        monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
        monkeypatch.setattr(defs_mod, "DEFAULT_DB_PATH", fake_db)
        monkeypatch.setattr(defs_mod, "_DB", str(fake_db))
        monkeypatch.setattr(defs_mod, "LIONAGI_HOME", fake_home)
        monkeypatch.setattr(defs_mod, "AGENTS_DIR", agents_dir)
        monkeypatch.setattr(defs_mod, "PLAYBOOKS_DIR", fake_home / "playbooks")
        monkeypatch.setattr(
            defs_mod, "KIND_DIRS", {"agent": agents_dir, "playbook": fake_home / "playbooks"}
        )

        result = _run(defs_mod.list_definitions("agent"))
        assert len(result) == 1
        disk_path = result[0]["disk_path"]
        # disk_path must be relative to LIONAGI_HOME (not an absolute path)
        assert not disk_path.startswith("/"), f"disk_path must not be absolute: {disk_path!r}"
        assert "myagent" in disk_path


# ---------------------------------------------------------------------------
# #986 — plugins path field is not absolute
# ---------------------------------------------------------------------------


class TestPluginsPathSanitization:
    def test_plugin_summary_path_not_absolute(self, tmp_path, monkeypatch):
        """_plugin_summary() must return a relative path for the 'path' field."""
        import lionagi.studio.services.plugins as plugins_mod

        # Build a minimal marketplace plugin under tmp_path
        repo_root = tmp_path / "repo"
        marketplace = repo_root / "marketplace" / "test_plugin"
        marketplace.mkdir(parents=True)
        plugin_json = marketplace / ".claude-plugin"
        plugin_json.mkdir()
        (plugin_json / "plugin.json").write_text(
            json.dumps({"name": "test_plugin", "description": "A test plugin", "version": "1.0.0"})
        )

        monkeypatch.setattr(plugins_mod, "_REPO_ROOT", repo_root)
        monkeypatch.setattr(plugins_mod, "MARKETPLACE_DIR", repo_root / "marketplace")

        result = plugins_mod._plugin_summary(marketplace, "test_plugin", "desc", "marketplace")
        path_val = result["path"]
        assert not path_val.startswith("/"), f"path must not be absolute: {path_val!r}"

    def test_get_plugin_skill_uses_real_dir(self, tmp_path, monkeypatch):
        """get_plugin_skill() must not reconstruct path from sanitized response."""
        import lionagi.studio.services.plugins as plugins_mod

        repo_root = tmp_path / "repo"
        plugin_dir = repo_root / "marketplace" / "myplugin"
        skills_dir = plugin_dir / "skills" / "myskill"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(
            "---\nname: myskill\ndescription: test\n---\ncontent here"
        )

        plugin_json_dir = plugin_dir / ".claude-plugin"
        plugin_json_dir.mkdir()
        (plugin_json_dir / "plugin.json").write_text(
            json.dumps({"name": "myplugin", "description": "desc", "version": "1.0"})
        )

        manifest_dir = repo_root / ".claude-plugin"
        manifest_dir.mkdir(parents=True)
        manifest = manifest_dir / "marketplace.json"
        manifest.write_text(
            json.dumps(
                {
                    "plugins": [
                        {"name": "myplugin", "source": "marketplace/myplugin", "description": ""}
                    ]
                }
            )
        )

        monkeypatch.setattr(plugins_mod, "_REPO_ROOT", repo_root)
        monkeypatch.setattr(plugins_mod, "MARKETPLACE_DIR", repo_root / "marketplace")
        monkeypatch.setattr(plugins_mod, "MARKETPLACE_MANIFEST", manifest)

        result = plugins_mod.get_plugin_skill("myplugin", "myskill")
        assert result is not None
        assert result["content"] == "content here"
        assert not result["path"].startswith("/"), (
            f"skill path must not be absolute: {result['path']!r}"
        )


# ---------------------------------------------------------------------------
# #987 — marketplace plugin source paths bounded to repo root
# ---------------------------------------------------------------------------


class TestMarketplaceSourcePaths:
    def test_valid_relative_source_accepted(self, tmp_path, monkeypatch):
        """A source path like './marketplace/X' that stays under repo root is accepted."""
        import lionagi.studio.services.plugins as plugins_mod

        repo_root = tmp_path / "repo"
        valid_dir = repo_root / "marketplace" / "valid"
        valid_dir.mkdir(parents=True)

        monkeypatch.setattr(plugins_mod, "_REPO_ROOT", repo_root)

        result = plugins_mod._resolve_marketplace_source("marketplace/valid")
        assert result is not None
        assert result.resolve() == valid_dir.resolve()

    def test_absolute_source_rejected(self, tmp_path, monkeypatch):
        """An absolute path in marketplace source must be rejected."""
        import lionagi.studio.services.plugins as plugins_mod

        monkeypatch.setattr(plugins_mod, "_REPO_ROOT", tmp_path / "repo")

        result = plugins_mod._resolve_marketplace_source("/etc/passwd")
        assert result is None

    def test_parent_traversal_rejected(self, tmp_path, monkeypatch):
        """A '../escape' source must be rejected."""
        import lionagi.studio.services.plugins as plugins_mod

        monkeypatch.setattr(plugins_mod, "_REPO_ROOT", tmp_path / "repo")

        result = plugins_mod._resolve_marketplace_source("../outside")
        assert result is None

    def test_symlink_escape_rejected(self, tmp_path, monkeypatch):
        """A source that resolves via symlink outside repo root must be rejected."""
        import lionagi.studio.services.plugins as plugins_mod

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        link = repo_root / "symlink_escape"
        link.symlink_to(outside)

        monkeypatch.setattr(plugins_mod, "_REPO_ROOT", repo_root)

        result = plugins_mod._resolve_marketplace_source("symlink_escape")
        assert result is None

    def test_marketplace_manifest_with_escape_ignored(self, tmp_path, monkeypatch):
        """_iter_marketplace_plugins() drops entries with escape paths."""
        import lionagi.studio.services.plugins as plugins_mod

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        valid_dir = repo_root / "marketplace" / "good"
        valid_dir.mkdir(parents=True)
        (valid_dir / ".claude-plugin").mkdir()
        (valid_dir / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "good", "description": "", "version": "1.0"})
        )

        manifest_dir = repo_root / ".claude-plugin"
        manifest_dir.mkdir()
        manifest = manifest_dir / "marketplace.json"
        manifest.write_text(
            json.dumps(
                {
                    "plugins": [
                        {"name": "good", "source": "marketplace/good", "description": ""},
                        {"name": "bad", "source": "../outside", "description": ""},
                        {"name": "abs", "source": "/etc/passwd", "description": ""},
                    ]
                }
            )
        )

        monkeypatch.setattr(plugins_mod, "_REPO_ROOT", repo_root)
        monkeypatch.setattr(plugins_mod, "MARKETPLACE_MANIFEST", manifest)

        results = plugins_mod._iter_marketplace_plugins()
        names = [name for _, name, _ in results]
        assert "good" in names
        assert "bad" not in names
        assert "abs" not in names


# ---------------------------------------------------------------------------
# #988 — optional bearer token auth
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestBearerTokenAuth:
    def _get_client(self, monkeypatch, fake_db: Path | None = None) -> TestClient:
        from importlib import reload

        import lionagi.studio.app as app_mod
        import lionagi.studio.services.stats as stats_mod

        if fake_db is not None:
            monkeypatch.setattr(stats_mod, "DEFAULT_DB_PATH", fake_db)
            monkeypatch.setattr(stats_mod, "_DB", str(fake_db))

        reload(app_mod)
        return TestClient(app_mod.app, raise_server_exceptions=False)

    def test_mutating_route_requires_bearer_when_token_set(self, monkeypatch):
        """POST to /api/* must return 401 when token is set and auth is missing/wrong."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "testsecret")
        client = self._get_client(monkeypatch)

        # No auth header
        resp = client.post("/api/shows/import")
        assert resp.status_code == 401

        # Wrong token
        resp = client.post("/api/shows/import", headers={"Authorization": "Bearer wrongtoken"})
        assert resp.status_code == 401

    def test_mutating_route_allowed_with_correct_bearer(self, monkeypatch):
        """POST with correct Bearer token must not return 401."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "testsecret")
        client = self._get_client(monkeypatch)

        resp = client.post("/api/shows/import", headers={"Authorization": "Bearer testsecret"})
        # Not 401 (may be 200, 422, 500 depending on service state, but not auth-rejected)
        assert resp.status_code != 401

    def test_health_endpoint_open_when_token_set(self, monkeypatch):
        """GET /health must remain accessible when auth token is set."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "testsecret")
        client = self._get_client(monkeypatch)

        resp = client.get("/health")
        assert resp.status_code == 200

    def test_readonly_api_open_when_token_set(self, monkeypatch, tmp_path):
        """GET /api/stats requires auth when a token is configured."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "testsecret")
        fake_db = tmp_path / "state.db"
        client = self._get_client(monkeypatch, fake_db=fake_db)

        resp = client.get("/api/stats")
        assert resp.status_code == 401

    def test_no_auth_when_token_unset(self, monkeypatch):
        """When LIONAGI_STUDIO_AUTH_TOKEN is not set, all routes are open."""
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        client = self._get_client(monkeypatch)

        resp = client.post("/api/shows/import")
        assert resp.status_code != 401
