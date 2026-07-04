"""Shared fixtures for the apps_studio_server test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_claude_mirror(monkeypatch):
    """Keep studio's in-process Claude mirror tail off in tests.

    Enabled by default in production, it would read the real ~/.claude/projects
    and spin a background task that leaks writes into the temp DB and slows tests.
    """
    import lionagi.studio.config as config_mod

    monkeypatch.setattr(config_mod, "MIRROR_CLAUDE_ENABLED", False)


@pytest.fixture()
def studio_client():
    """Return a TestClient wired to the studio app with no additional patching."""
    fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
    from fastapi.testclient import TestClient

    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")
