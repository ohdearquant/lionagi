"""The studio services open the store the daemon serves.

Every service in this layer used to name ``DEFAULT_DB_PATH`` at import.
That is the right file until ``LIONAGI_STATE_DB_URL`` moves the store, and
then it is a different database from the one the daemon writes: the routes
report on rows nobody is serving, and SQLite creates the unrelated file on
connect if it is not already there.

Two things are pinned: a deployment that has not moved anything sees no
change, and a health answer and a data answer come from the same store
(two services resolving separately was the original defect).

The default path is present and wrong in these tests, not absent, so a
service still reading it answers from an empty database instead of
raising -- that silent-wrong-answer failure is what must stay visible.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")

import lionagi.state.db as state_db_mod  # noqa: E402
from lionagi.state.db import StateDB  # noqa: E402
from lionagi.studio.services._db import store_exists, store_path  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _seed(db_path: Path, name: str, count: int = 1) -> list[str]:
    ids = []
    async with StateDB(db_path) as db:
        for i in range(count):
            session_id = str(uuid.uuid4())
            pid = str(uuid.uuid4())
            await db.create_progression(pid)
            await db.create_session(
                {
                    "id": session_id,
                    "progression_id": pid,
                    "name": name if count == 1 else f"{name}-{i}",
                    "status": "completed",
                    "started_at": time.time(),
                }
            )
            ids.append(session_id)
    return ids


def _configure(monkeypatch, *, default: Path, url: str | None) -> None:
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", default)
    monkeypatch.setattr(
        state_db_mod,
        "settings",
        state_db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": url}),
    )


def test_unconfigured_deployment_resolves_the_default_path(tmp_path, monkeypatch):
    """With nothing configured, this is the path the services always used."""
    default = tmp_path / "state.db"
    _configure(monkeypatch, default=default, url=None)

    assert store_path() == str(default)
    assert store_exists() is False

    _run(_seed(default, "s"))
    assert store_path() == str(default)
    assert store_exists() is True


def test_configured_store_moves_the_path_the_services_open(tmp_path, monkeypatch):
    default = tmp_path / "default_state.db"
    configured = tmp_path / "configured_state.db"
    _run(_seed(default, "in-the-default-store"))
    _run(_seed(configured, "in-the-configured-store"))

    _configure(monkeypatch, default=default, url=str(configured))

    assert store_path() == str(configured)


def test_health_and_data_answers_come_from_the_same_store(tmp_path, monkeypatch):
    """A configured store is one store: the size reported and the rows listed
    are both about it, and neither is about the default path.

    The configured store is seeded heavily enough that the two files differ in
    size. Two stores holding one row each come out byte-identical, so a size
    assertion between them passes whichever file was measured, which is worth
    less than no assertion at all.
    """
    import lionagi.studio.services.admin as admin_mod
    import lionagi.studio.services.sessions as sessions_mod

    default = tmp_path / "default_state.db"
    configured = tmp_path / "configured_state.db"
    _run(_seed(default, "in-the-default-store"))
    configured_ids = _run(_seed(configured, "in-the-configured-store", count=120))
    assert configured.stat().st_size != default.stat().st_size

    _configure(monkeypatch, default=default, url=str(configured))

    health = admin_mod.db_health()
    assert health["size_bytes"] == configured.stat().st_size

    rows = _run(sessions_mod.list_sessions(limit=200))
    assert sorted(r["id"] for r in rows) == sorted(configured_ids)


def test_a_configured_store_is_never_created_by_reading_it(tmp_path, monkeypatch):
    """Resolution alone must not bring a store into existence."""
    default = tmp_path / "default_state.db"
    configured = tmp_path / "not_there_yet.db"
    _run(_seed(default, "in-the-default-store"))

    _configure(monkeypatch, default=default, url=str(configured))

    assert store_exists() is False
    assert not configured.exists()


# ── A store with no file is not a store that is merely empty ────────────────


def _seed_definition(db_path: Path, *, content: str, message: str) -> None:
    async def _go():
        async with StateDB(db_path) as db:
            await db.save_definition(
                kind="agent",
                name="demo",
                path="agents/demo.md",
                content=content,
                message=message,
            )

    _run(_go())


def test_history_enrichment_is_dropped_rather_than_read_from_a_stale_file(tmp_path, monkeypatch):
    """The definition routes read current content from disk and enrich it with
    version history from the store. Against a server-backed store there is no
    file for that history, and resolution falls back to the default path — so a
    deployment that once ran locally still has that database, and the route
    reported its versions and audit messages over content read live from disk.
    Nothing about the payload looks wrong; the two halves simply came from
    different stores, and only one of them is the store in use.

    Writes are not affected, which is what makes it plausible rather than
    obviously broken: `save_definition` goes through StateDB and lands in the
    configured store, so the local file only ever looks out of date, never
    empty.
    """
    import lionagi.studio.services.definitions as definitions_mod

    fallback = tmp_path / "state.db"
    _seed_definition(fallback, content="what the old local database holds", message="old")
    disk = tmp_path / "agents"
    disk.mkdir()
    (disk / "demo.md").write_text("what is on disk now")

    _configure(monkeypatch, default=fallback, url="postgresql://user:secret@host/prod")
    monkeypatch.setitem(definitions_mod.KIND_DIRS, "agent", disk)

    got = _run(definitions_mod.get_definition("agent", "demo"))
    listed = _run(definitions_mod.list_definitions("agent"))

    assert got["content"] == "what is on disk now", (
        "the disk half is correct whatever the store is, and must still be answered"
    )
    # The stale local file must not be the source, and "unreadable" must not be
    # dressed up as "empty": an empty history is a claim about the definition,
    # and the true statement here is about the store. A caller told there are no
    # versions concludes nothing was ever saved, which is the opposite of true.
    assert "what the old local database holds" not in str(got)
    assert got["history_available"] is False
    assert got["versions"] is None, "an unreadable history is null, never an empty list"
    assert got["version"] is None
    assert listed[0]["history_available"] is False
    assert listed[0]["has_versions"] is None, "unknown, not False"


def test_the_routes_that_are_only_history_refuse_rather_than_report_absence(tmp_path, monkeypatch):
    """A version read and a rollback have no disk half to fall back on, so an
    unreadable store leaves them nothing true to say. Answering 404 would say
    the store was read and does not have this version.

    503 rather than 501: every store this deployment can be configured for is
    one StateDB reads, so an unreadable one is an operational condition a retry
    can outlive. The Operator routes answer 501 for the opposite reason, their
    store being SQLite-only, and the two must not be conflated.

    The port is one nothing listens on, so the connection is refused rather
    than hanging.
    """
    from fastapi.testclient import TestClient

    default = tmp_path / "state.db"
    disk = tmp_path / "agents"
    disk.mkdir()
    (disk / "demo.md").write_text("what is on disk now")
    _configure(monkeypatch, default=default, url="postgresql://user:secret@127.0.0.1:1/db")

    import lionagi.studio.services.definitions as definitions_mod

    monkeypatch.setitem(definitions_mod.KIND_DIRS, "agent", disk)

    from lionagi.studio.app import app

    with TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False) as client:
        version_read = client.get("/api/definitions/agent/demo/versions/1")
        rollback = client.post("/api/definitions/agent/demo/rollback?version=1")

    for label, response in (("version read", version_read), ("rollback", rollback)):
        assert response.status_code == 503, f"{label}: {response.text}"
        # The body is the fixed refusal and carries nothing the driver said.
        # Asserting that directly, rather than searching the body for the
        # password: the five store failures reachable from here name a socket
        # or a plugin and never quote the URL, so a search for the credential
        # passes whether or not anything guards it. What is worth pinning is
        # that the driver's message does not reach the caller at all, which is
        # the property that keeps this true for a driver that does quote it.
        assert response.json()["detail"] == definitions_mod._HISTORY_UNAVAILABLE_DETAIL, (
            f"{label} passed the driver's own message through"
        )


def test_a_readable_store_still_answers_those_routes(tmp_path, monkeypatch):
    """The over-refusal arm for the pair above. Without it, a version route
    that refused unconditionally would pass that test."""
    from fastapi.testclient import TestClient

    store = tmp_path / "state.db"
    _seed_definition(store, content="what the database holds", message="recorded")
    disk = tmp_path / "agents"
    disk.mkdir()
    (disk / "demo.md").write_text("what is on disk now")
    _configure(monkeypatch, default=store, url=None)

    import lionagi.studio.services.definitions as definitions_mod

    monkeypatch.setitem(definitions_mod.KIND_DIRS, "agent", disk)

    from lionagi.studio.app import app

    with TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False) as client:
        response = client.get("/api/definitions/agent/demo/versions/1")

    assert response.status_code == 200, response.text
    assert response.json()["content"] == "what the database holds"


def test_history_enrichment_still_happens_against_the_configured_file(tmp_path, monkeypatch):
    """The over-refusal arm, on the same seeded database: configure it as the
    store and the identical call must enrich. Without this, dropping enrichment
    unconditionally would pass the test above."""
    import lionagi.studio.services.definitions as definitions_mod

    store = tmp_path / "state.db"
    _seed_definition(store, content="what the database holds", message="recorded")
    disk = tmp_path / "agents"
    disk.mkdir()
    (disk / "demo.md").write_text("what is on disk now")

    _configure(monkeypatch, default=store, url=None)
    monkeypatch.setitem(definitions_mod.KIND_DIRS, "agent", disk)

    got = _run(definitions_mod.get_definition("agent", "demo"))

    assert got["version"] == 1
    assert [v["message"] for v in got["versions"]] == ["recorded"]
