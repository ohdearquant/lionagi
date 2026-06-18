"""Shared fixtures for the apps_studio_server test suite."""

from __future__ import annotations

import pytest


@pytest.fixture()
def studio_client():
    """Return a TestClient wired to the studio app with no additional patching."""
    fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
    from fastapi.testclient import TestClient

    from lionagi.studio.app import app

    return TestClient(app)
