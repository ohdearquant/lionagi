"""Shared fixtures for the Studio application server tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_claude_mirror(monkeypatch):
    """Keep studio's in-process Claude mirror tail off in tests.

    Enabled by default in production, it would read the real ~/.claude/projects
    and spin a background task that leaks writes into the temp DB and slows tests.
    """
    import lionagi_studio.config as config_mod
    import lionhub.serve.daemon.settings as daemon_settings

    monkeypatch.setattr(daemon_settings, "MIRROR_CLAUDE_ENABLED", False)


@pytest.fixture()
def studio_client():
    """Return a TestClient wired to the studio app with no additional patching."""
    fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
    from fastapi.testclient import TestClient

    from lionagi_studio.app import app

    return TestClient(
        app,
        base_url="http://127.0.0.1:8765",
        headers={"Content-Type": "application/json"},
    )
