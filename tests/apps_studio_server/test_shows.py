"""Hermetic tests for the /api/shows routes.

All filesystem roots are redirected to tmp_path via monkeypatching so these
tests run on any machine without pre-existing show directories.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def shows_root(tmp_path: Path) -> Path:
    return tmp_path / "shows"


@pytest.fixture()
def patched_app(shows_root: Path, monkeypatch: pytest.MonkeyPatch):
    """Return a TestClient whose SHOWS_ROOT is redirected to a tmp directory."""
    import apps.studio.server.config as config_mod
    import apps.studio.server.services.shows as shows_mod

    shows_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config_mod, "SHOWS_ROOT", shows_root)
    monkeypatch.setattr(shows_mod, "SHOWS_ROOT", shows_root)

    from apps.studio.server.app import app

    return TestClient(app)


@pytest.fixture()
def show_with_play(shows_root: Path) -> str:
    """Create a minimal show directory with one play and return its topic."""
    topic = "test-show"
    show_dir = shows_root / topic
    play_dir = show_dir / "play-001"
    play_dir.mkdir(parents=True)

    (show_dir / "_show.md").write_text("# Show: test-show\n\nA test show.")
    meta = {
        "status": "success",
        "started_at": "2024-01-01T00:00:00Z",
        "branch": "show/test-show/play-001",
    }
    (play_dir / "_meta.json").write_text(json.dumps(meta))
    verdict = {"gate_passed": True}
    (play_dir / "_verdict.json").write_text(json.dumps(verdict))

    return topic


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_shows_list_returns_array(patched_app):
    r = patched_app.get("/api/shows")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_shows_list_contains_fixture(patched_app, show_with_play):
    r = patched_app.get("/api/shows")
    assert r.status_code == 200
    topics = {item["topic"] for item in r.json()}
    assert show_with_play in topics


def test_show_detail_has_meta(patched_app, show_with_play):
    r = patched_app.get(f"/api/shows/{show_with_play}")
    assert r.status_code == 200
    data = r.json()
    assert data["topic"] == show_with_play
    assert isinstance(data["show_md"], str)
    plays = data["plays"]
    assert isinstance(plays, list)
    assert len(plays) > 0


def test_show_detail_not_found(patched_app):
    r = patched_app.get("/api/shows/nonexistent-topic")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Path traversal tests (Fix 1)
# ---------------------------------------------------------------------------


def test_path_traversal_encoded_dotdot_shows(patched_app):
    """URL-encoded %2e%2e must not escape SHOWS_ROOT."""
    r = patched_app.get("/api/shows/%2e%2e")
    assert r.status_code == 404


def test_path_traversal_encoded_slash_shows(patched_app):
    """Encoded slash in topic must be rejected."""
    r = patched_app.get("/api/shows/aaa%2Fbbb")
    assert r.status_code == 404


def test_path_traversal_double_dotdot_shows(patched_app):
    """Double dotdot segment must be rejected."""
    r = patched_app.get("/api/shows/../../../etc")
    # FastAPI normalises raw /.. to 404 before it reaches our code.
    # Either way, must not be 200.
    assert r.status_code == 404
