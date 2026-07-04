"""Hermetic tests for the /api/shows routes.

All filesystem roots are redirected to tmp_path via monkeypatching so these
tests run on any machine without pre-existing show directories.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402 — must follow importorskip

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def shows_root(tmp_path: Path) -> Path:
    return tmp_path / "shows"


@pytest.fixture()
def patched_app(shows_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Return a TestClient whose SHOWS_ROOT is redirected to a tmp directory.

    Also redirects DEFAULT_DB_PATH to a non-existent fake DB so that
    _list_shows_db() returns no rows (forcing the filesystem fallback that
    reads from the patched SHOWS_ROOT, not the real state.db).
    """
    import lionagi.state.db as state_db_mod
    import lionagi.studio.config as config_mod
    import lionagi.studio.services.shows as shows_mod

    shows_root.mkdir(parents=True, exist_ok=True)
    fake_db = tmp_path / "state.db"  # does not exist → _db_available() returns False
    monkeypatch.setattr(config_mod, "SHOWS_ROOT", shows_root)
    monkeypatch.setattr(shows_mod, "SHOWS_ROOT", shows_root)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(shows_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(shows_mod, "_DB", str(fake_db))

    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


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


@pytest.mark.integration
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


def test_show_detail_status_source_is_filesystem_without_db(patched_app, show_with_play):
    """status_source must be 'filesystem' when no DB is available (fake DB path → _db_available() False)."""
    r = patched_app.get(f"/api/shows/{show_with_play}")
    assert r.status_code == 200
    data = r.json()
    assert "status_source" in data, "status_source field missing from GET /api/shows/{topic}"
    assert data["status_source"] == "filesystem", (
        f"status_source must be 'filesystem' (no DB), got {data['status_source']!r}"
    )


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


async def test_watch_show_invalid_topic_yields_done(tmp_path, monkeypatch):
    """An invalid topic must yield a single `done` event, not raise.

    watch_show() runs inside an SSE stream, so a rejected topic must follow
    the same yield-done contract as a missing directory rather than raise.
    """
    import lionagi.studio.services.shows as shows_mod

    shows_root = tmp_path / "shows"
    shows_root.mkdir()
    monkeypatch.setattr(shows_mod, "SHOWS_ROOT", shows_root)

    events = [event async for event in shows_mod.watch_show("../etc")]

    assert len(events) == 1
    assert json.loads(events[0].removeprefix("data: ").strip()) == {"type": "done"}


# ---------------------------------------------------------------------------
# strict status_source provenance
# ---------------------------------------------------------------------------


@pytest.fixture()
def sqlite_patched_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """TestClient backed by a real SQLite DB with one show row (status_source == 'sqlite' path)."""
    import lionagi.state.db as state_db_mod
    import lionagi.studio.config as config_mod
    import lionagi.studio.services.shows as shows_mod

    shows_root = tmp_path / "shows"
    shows_root.mkdir(parents=True)
    fake_db = tmp_path / "state.db"

    monkeypatch.setattr(config_mod, "SHOWS_ROOT", shows_root)
    monkeypatch.setattr(shows_mod, "SHOWS_ROOT", shows_root)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(shows_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(shows_mod, "_DB", str(fake_db))

    # Create the show directory on disk so safe_path_join() passes and
    # show_dir.is_dir() is True inside get_show().
    topic = "sqlite-show"
    show_dir = shows_root / topic
    show_dir.mkdir(parents=True)
    (show_dir / "_show.md").write_text(f"# Show: {topic}\n\nA SQLite-backed test show.")

    # Populate the DB schema and insert one show row so _db_available() returns True.
    async def _seed_db():
        async with state_db_mod.StateDB() as db:
            await db.execute(
                """INSERT INTO shows (id, topic, goal, status, show_dir, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    topic,
                    "test goal",
                    "active",
                    str(show_dir),
                    0.0,
                    0.0,
                ),
            )

    # Python 3.10+: asyncio.get_event_loop() raises in a fresh thread.
    # CI xdist workers start without a loop. Use a fresh loop per fixture.
    _loop = asyncio.new_event_loop()
    try:
        _loop.run_until_complete(_seed_db())
    finally:
        _loop.close()

    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765"), topic


def test_show_detail_status_source_is_sqlite_with_db(sqlite_patched_app):
    """status_source must be 'sqlite' when the show row is found in the DB."""
    client, topic = sqlite_patched_app
    r = client.get(f"/api/shows/{topic}")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    data = r.json()
    assert "status_source" in data, "status_source field missing from response"
    assert data["status_source"] == "sqlite", (
        f"status_source must be 'sqlite' when show row exists in DB, got {data['status_source']!r}"
    )


# ---------------------------------------------------------------------------
# Docker regression test: get_show works from DB even when show dir is absent
# ---------------------------------------------------------------------------


@pytest.fixture()
def docker_patched_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Simulate the Docker scenario: state.db is mounted but show dirs are NOT.

    list_shows() returns topics from the DB; get_show(topic) must also return
    the show (not 404) even though the show directory does not exist on disk.
    This is the exact regression that caused every topic from list_shows() to
    return 404 in Docker.
    """
    import lionagi.state.db as state_db_mod
    import lionagi.studio.config as config_mod
    import lionagi.studio.services.shows as shows_mod

    # shows_root is created but show subdirectories are intentionally absent.
    shows_root = tmp_path / "shows"
    shows_root.mkdir(parents=True)
    fake_db = tmp_path / "state.db"

    monkeypatch.setattr(config_mod, "SHOWS_ROOT", shows_root)
    monkeypatch.setattr(shows_mod, "SHOWS_ROOT", shows_root)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(shows_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(shows_mod, "_DB", str(fake_db))

    topic = "overnight-sweep"
    # Deliberately do NOT create shows_root / topic on disk.

    async def _seed_db():
        async with state_db_mod.StateDB() as db:
            await db.execute(
                """INSERT INTO shows (id, topic, goal, status, show_dir, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    topic,
                    "overnight sweep goal",
                    "active",
                    str(shows_root / topic),  # path that does NOT exist on disk
                    0.0,
                    0.0,
                ),
            )

    _loop = asyncio.new_event_loop()
    try:
        _loop.run_until_complete(_seed_db())
    finally:
        _loop.close()

    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765"), topic


def test_get_show_returns_200_when_dir_absent_but_db_has_row(docker_patched_app):
    """get_show must not 404 when the show directory is absent but the DB row exists.

    Regression test for the Docker bug: list_shows() reads the DB (returns 27
    shows), but get_show(topic) checked show_dir.is_dir() first and returned
    None (→ 404) for every topic because the host show dirs are not mounted in
    the container.

    The fix: only return None when BOTH the dir is absent AND no DB row exists.
    """
    client, topic = docker_patched_app

    # list_shows should include the topic (from DB)
    list_r = client.get("/api/shows")
    assert list_r.status_code == 200
    listed_topics = {item["topic"] for item in list_r.json()}
    assert topic in listed_topics, f"{topic!r} missing from list_shows() response"

    # get_show must also succeed (not 404) — this is the regression case
    detail_r = client.get(f"/api/shows/{topic}")
    assert detail_r.status_code == 200, (
        f"get_show returned {detail_r.status_code} for topic {topic!r} "
        f"that list_shows() listed — Docker 404 regression"
    )
    data = detail_r.json()
    assert data["topic"] == topic
    assert data["status_source"] == "sqlite"
    assert data["status"] == "active"
    assert isinstance(data["plays"], list)


def test_get_show_returns_404_when_dir_absent_and_no_db_row(docker_patched_app):
    """get_show must still 404 for topics not in DB and not on filesystem."""
    client, _topic = docker_patched_app
    r = client.get("/api/shows/nonexistent-topic-xyz")
    assert r.status_code == 404
