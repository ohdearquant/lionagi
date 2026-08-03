"""The studio services open the store the daemon serves.

Every service in this layer used to name ``DEFAULT_DB_PATH`` at import. That is
the right file until ``LIONAGI_STATE_DB_URL`` moves the store, and then it is a
different database from the one the daemon writes: the routes report on rows
nobody is serving, and SQLite creates the unrelated file on connect if it is
not already there.

Two things are worth pinning. The first is that nothing changes for a
deployment that has not moved anything, since that is what makes changing every
service at once safe. The second is that a health answer and a data answer come
from the same store, because two services resolving separately is the defect
this replaces rather than a smaller version of it.

The default path is present and wrong in these tests, not absent. A service
still reading it would then answer from an empty database instead of raising,
which is the failure that has to be visible here.
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
