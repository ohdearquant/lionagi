"""Shared fixtures for the apps_studio_server test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_claude_mirror(monkeypatch):
    """Keep studio's in-process Claude mirror tail off in tests.

    Enabled by default in production, it would read the real ~/.claude/projects
    and spin a background task that leaks writes into the temp DB and slows tests.
    """
    import lionhub.serve.daemon.settings as daemon_settings

    monkeypatch.setattr(daemon_settings, "MIRROR_CLAUDE_ENABLED", False)
