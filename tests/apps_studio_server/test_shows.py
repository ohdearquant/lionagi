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
# /api/shows/gated-plays — real gate signal for the Mission Control queue
# ---------------------------------------------------------------------------


@pytest.fixture()
def show_with_gated_play(shows_root: Path) -> str:
    """A show with one play sitting in the `gated` lifecycle status."""
    topic = "gated-show"
    show_dir = shows_root / topic
    play_dir = show_dir / "play-001"
    play_dir.mkdir(parents=True)

    (show_dir / "_show.md").write_text("# Show: gated-show\n\nA gated test show.")
    meta = {"status": "gated", "started_at": "2024-01-01T00:00:00Z"}
    (play_dir / "_meta.json").write_text(json.dumps(meta))
    verdict = {"gate_passed": False, "feedback": "needs another pass"}
    (play_dir / "_verdict.json").write_text(json.dumps(verdict))

    return topic


def test_gated_plays_route_not_swallowed_by_topic_route(patched_app):
    """/shows/gated-plays must resolve to the dedicated route, not be parsed
    as a topic named 'gated-plays' by /shows/{topic} — route registration
    order matters here."""
    r = patched_app.get("/api/shows/gated-plays")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_gated_plays_surfaces_a_real_gated_play(patched_app, show_with_gated_play):
    r = patched_app.get("/api/shows/gated-plays")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    item = items[0]
    assert item["topic"] == show_with_gated_play
    assert item["play_name"] == "play-001"
    assert item["id"] == f"play:{show_with_gated_play}:play-001"
    assert item["feedback"] == "needs another pass"


def test_gated_plays_excludes_non_gated_plays(patched_app, show_with_play):
    """show_with_play's play has status 'success', not 'gated'."""
    r = patched_app.get("/api/shows/gated-plays")
    assert r.status_code == 200
    assert r.json() == []


def test_gated_plays_surfaces_a_gate_created_after_import(shows_root: Path, patched_app):
    """import_shows() populates the plays table once; a play directory
    created afterward has no DB row. The gated queue must still see it
    rather than reading only the stale DB mirror.

    Regression for: a show imported while play-0 is running, then the
    external director creates play-1/_meta.json with status 'gated' — the
    queue must not silently omit it.
    """
    import lionagi.studio.services.shows as shows_mod

    from ._helpers import run_async

    topic = "imported-show"
    show_dir = shows_root / topic
    play0 = show_dir / "play-0"
    play0.mkdir(parents=True)
    (show_dir / "_show.md").write_text(f"# Show: {topic}\n")
    (play0 / "_meta.json").write_text(
        json.dumps({"status": "running", "started_at": "2024-01-01T00:00:00Z"})
    )

    run_async(shows_mod.import_shows())

    play1 = show_dir / "play-1"
    play1.mkdir()
    (play1 / "_meta.json").write_text(
        json.dumps({"status": "gated", "started_at": "2024-01-02T00:00:00Z"})
    )

    r = patched_app.get("/api/shows/gated-plays")
    assert r.status_code == 200
    items = r.json()
    play_names = {item["play_name"] for item in items}
    assert "play-1" in play_names, f"gated play created after import missing from queue: {items!r}"


def test_gated_plays_disk_status_wins_over_stale_db_row(shows_root: Path, patched_app):
    """A play imported with one status, then rewritten on disk to `gated`,
    must surface in the queue with the disk status — the DB row is a
    one-time import mirror with no live status writer, so a stale DB status
    must not hide a live gate.

    Regression for: import play-0 with disk status `running`, then rewrite
    play-0/_meta.json to `gated`. get_show() must report `gated` (not the
    stale `running`), and list_gated_plays() must not be `[]` — the two must
    never disagree about the same play.
    """
    import lionagi.studio.services.shows as shows_mod

    from ._helpers import run_async

    topic = "stale-db-show"
    show_dir = shows_root / topic
    play0 = show_dir / "play-0"
    play0.mkdir(parents=True)
    (show_dir / "_show.md").write_text(f"# Show: {topic}\n")
    (play0 / "_meta.json").write_text(
        json.dumps({"status": "running", "started_at": "2024-01-01T00:00:00Z"})
    )

    run_async(shows_mod.import_shows())

    # The play is rewritten in place after import — no new play, so the DB
    # row for play-0 stays "running" forever unless something re-imports.
    (play0 / "_meta.json").write_text(
        json.dumps({"status": "gated", "started_at": "2024-01-01T00:00:00Z"})
    )

    detail_r = patched_app.get(f"/api/shows/{topic}")
    assert detail_r.status_code == 200
    play0_status = next(
        p["meta"]["status"] for p in detail_r.json()["plays"] if p["name"] == "play-0"
    )
    assert play0_status == "gated", (
        f"get_show() must report the live disk status, got {play0_status!r}"
    )

    gated_r = patched_app.get("/api/shows/gated-plays")
    assert gated_r.status_code == 200
    items = gated_r.json()
    assert any(item["topic"] == topic and item["play_name"] == "play-0" for item in items), (
        f"disk gate hidden by stale DB row: {items!r}"
    )


def test_gated_plays_surfaces_a_show_never_imported(shows_root: Path, patched_app):
    """A show directory that was never imported must still be scanned for
    gated plays. Once any show has been imported, list_shows() returns DB
    rows only, so deriving the gated queue's show set from list_shows()
    silently drops every show that import_shows() never touched.

    Regression for: import one show so the DB is non-empty, then create a
    second, never-imported show with a gated play — the queue must not
    silently omit it.
    """
    import lionagi.studio.services.shows as shows_mod

    from ._helpers import run_async

    imported_topic = "imported-show-r6"
    imported_dir = shows_root / imported_topic
    imported_play = imported_dir / "play-0"
    imported_play.mkdir(parents=True)
    (imported_dir / "_show.md").write_text(f"# Show: {imported_topic}\n")
    (imported_play / "_meta.json").write_text(json.dumps({"status": "success"}))

    run_async(shows_mod.import_shows())

    never_topic = "never-imported-show-r6"
    never_dir = shows_root / never_topic
    never_play = never_dir / "play-1"
    never_play.mkdir(parents=True)
    (never_dir / "_show.md").write_text(f"# Show: {never_topic}\n")
    (never_play / "_meta.json").write_text(json.dumps({"status": "gated"}))

    list_r = patched_app.get("/api/shows")
    assert list_r.status_code == 200
    listed_topics = {item["topic"] for item in list_r.json()}
    assert never_topic not in listed_topics, (
        "test assumption broken: /api/shows already surfaces the unimported "
        "show, so this no longer exercises the gated-queue's own scan"
    )

    gated_r = patched_app.get("/api/shows/gated-plays")
    assert gated_r.status_code == 200
    items = gated_r.json()
    assert any(item["topic"] == never_topic and item["play_name"] == "play-1" for item in items), (
        f"gated play in a never-imported show omitted from queue: {items!r}"
    )


def test_gated_plays_deleted_show_directory_is_reported_unavailable_not_stale(
    shows_root: Path, patched_app
):
    """A play imported as `running`, rewritten on disk to `gated`, whose show
    directory then disappears entirely (deleted or moved), must not have the
    stale imported `running` status presented as current — the live read
    failed, and the response must say so.

    Regression for: get_show() silently falling back to the one-time-import
    DB row once the disk directory is gone, reporting `running` as if it
    were still true and dropping the play from list_gated_plays() as if it
    had never been gated.
    """
    import shutil

    import lionagi.studio.services.shows as shows_mod

    from ._helpers import run_async

    topic = "deleted-dir-show"
    show_dir = shows_root / topic
    play0 = show_dir / "play-0"
    play0.mkdir(parents=True)
    (show_dir / "_show.md").write_text(f"# Show: {topic}\n")
    (play0 / "_meta.json").write_text(json.dumps({"status": "running"}))

    run_async(shows_mod.import_shows())

    (play0 / "_meta.json").write_text(json.dumps({"status": "gated"}))
    shutil.rmtree(show_dir)

    detail_r = patched_app.get(f"/api/shows/{topic}")
    assert detail_r.status_code == 200
    play0_entry = next(p for p in detail_r.json()["plays"] if p["name"] == "play-0")
    assert play0_entry["live_state"] == "unavailable", (
        f"a deleted show directory must not be reported as a live 'running' status: {play0_entry!r}"
    )

    gated_r = patched_app.get("/api/shows/gated-plays")
    assert gated_r.status_code == 200
    items = gated_r.json()
    entry = next((i for i in items if i["topic"] == topic and i["play_name"] == "play-0"), None)
    assert entry is not None, (
        f"a play whose live state is unreadable must still surface in the "
        f"gate queue instead of silently vanishing: {items!r}"
    )
    assert entry["live_state"] == "unavailable", (
        f"an unreadable live state must not be presented as a confirmed 'gated' row: {entry!r}"
    )


def test_gated_plays_truncated_meta_json_is_reported_unavailable(shows_root: Path, patched_app):
    """A `_meta.json` left truncated by a crashed writer must not be treated
    the same as a play that simply has no metadata yet — the parse failure
    must be surfaced, not silently swallowed into the stale DB status.

    Regression for: `_io.read_json_file` converting a JSONDecodeError into
    `None`, which is indistinguishable from a missing file, so a corrupt
    live write reads as "not gated" instead of "could not be read".
    """
    import lionagi.studio.services.shows as shows_mod

    from ._helpers import run_async

    topic = "truncated-meta-show"
    show_dir = shows_root / topic
    play0 = show_dir / "play-0"
    play0.mkdir(parents=True)
    (show_dir / "_show.md").write_text(f"# Show: {topic}\n")
    (play0 / "_meta.json").write_text(json.dumps({"status": "running"}))

    run_async(shows_mod.import_shows())

    # Simulate a writer crashing mid-write: valid JSON prefix, no closing brace.
    (play0 / "_meta.json").write_text('{"status": "gated", "started_a')

    detail_r = patched_app.get(f"/api/shows/{topic}")
    assert detail_r.status_code == 200
    play0_entry = next(p for p in detail_r.json()["plays"] if p["name"] == "play-0")
    assert play0_entry["live_state"] == "unavailable", (
        f"a truncated _meta.json must be surfaced as an unreadable live "
        f"read, not silently treated as absent metadata: {play0_entry!r}"
    )
    assert play0_entry["live_error"], "an unavailable live read must carry a diagnostic"

    gated_r = patched_app.get("/api/shows/gated-plays")
    assert gated_r.status_code == 200
    items = gated_r.json()
    entry = next((i for i in items if i["topic"] == topic and i["play_name"] == "play-0"), None)
    assert entry is not None, (
        f"a play with a corrupt live metadata file must still surface in the gate queue: {items!r}"
    )
    assert entry["live_state"] == "unavailable"


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


# ---------------------------------------------------------------------------
# import_shows() must not write an undeclared plays.status value (issue #2619)
# ---------------------------------------------------------------------------


def test_import_shows_refuses_undeclared_play_status(tmp_path, monkeypatch):
    """A play whose on-disk _meta.json carries a status outside the ADR-0011
    vocabulary (e.g. "success", a near-miss of "completed"/"merged" that was
    never declared) must not land in plays.status verbatim -- create_play()
    already refuses this for every other writer; import_shows() wrote around
    it via a raw INSERT. The bad play is skipped (loud log, not a crash);
    every write that does land is a member of the declared vocabulary.
    """
    import lionagi.state.db as state_db_mod
    import lionagi.studio.config as config_mod
    import lionagi.studio.services.shows as shows_mod
    from lionagi.state.db import VALID_STATUSES_BY_ENTITY_TYPE, StateDB

    shows_root = tmp_path / "shows"
    topic = "undeclared-status-show"
    show_dir = shows_root / topic
    play_dir = show_dir / "play-001"
    play_dir.mkdir(parents=True)
    (show_dir / "_show.md").write_text("# Show: undeclared-status-show\n")
    (play_dir / "_meta.json").write_text(json.dumps({"status": "success"}))

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(config_mod, "SHOWS_ROOT", shows_root)
    monkeypatch.setattr(shows_mod, "SHOWS_ROOT", shows_root)
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)

    async def _run() -> tuple[dict, dict | None]:
        result = await shows_mod.import_shows()
        async with StateDB(db_path) as db:
            row = await db.fetch_one(
                "SELECT status FROM plays WHERE show_id IN (SELECT id FROM shows WHERE topic = ?)",
                (topic,),
            )
        return result, row

    loop = asyncio.new_event_loop()
    try:
        result, row = loop.run_until_complete(_run())
    finally:
        loop.close()

    if row is not None:
        assert row["status"] in VALID_STATUSES_BY_ENTITY_TYPE["play"], (
            f"import_shows() wrote undeclared play status {row['status']!r} "
            "directly from _meta.json"
        )
    else:
        # The raw INSERT OR IGNORE silently drops a CHECK-violating row today
        # -- no exception, no row. plays_imported must not claim a play that
        # never actually landed; a caller trusting this count over-reports.
        assert result["plays_imported"] == 0, (
            f"import_shows() reported plays_imported={result['plays_imported']} "
            "but the play with an undeclared status was silently dropped -- "
            "the count is lying about what actually landed in the DB"
        )
