# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Readers must decide "there is nothing recorded" against the configured
store, not against the default path.

``LIONAGI_STATE_DB_URL`` moves the state store — to another SQLite file, to a
server, or into memory. A guard that asks whether ``DEFAULT_DB_PATH`` exists
answers about a file that need not be involved, and every row in the store that
*is* involved is reported missing.

Both shapes are covered here: a SQLite store moved to a non-default path, and a
URL that is not a local file at all.
"""

from __future__ import annotations

import uuid

import pytest

import lionagi.state.db as db_mod
from lionagi.state.db import StateDB, state_db_file, state_db_known_absent


def _set_url(monkeypatch, url: str | None) -> None:
    """Settings are frozen, so redirect the whole object the module reads."""
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": url})
    )


@pytest.fixture
def absent_default(tmp_path, monkeypatch):
    """Point the default path at a file that does not exist.

    Without this the machine's own ``~/.lionagi/state.db`` may exist and mask
    the defect: the guard would pass for the wrong reason.
    """
    missing = tmp_path / "no-such-home" / "state.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", missing)
    return missing


@pytest.fixture
def moved_sqlite_url(tmp_path, absent_default, monkeypatch):
    """A real SQLite store at a non-default path, selected by the env URL."""
    moved = tmp_path / "moved" / "state.db"
    moved.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite+aiosqlite:///{moved}"
    _set_url(monkeypatch, url)
    return url


# A URL with no local file behind it. The host is unreachable by construction —
# nothing here connects to it; what matters is that the guard cannot answer
# "absent" from the filesystem for a store shaped like this.
_SERVER_URL = "postgresql+asyncpg://user:pw@127.0.0.1:1/lionagi_state"


async def _seed_engine_def(name: str) -> str:
    def_id = str(uuid.uuid4())
    async with StateDB() as db:
        await db.create_engine_def(
            {
                "id": def_id,
                "name": name,
                "kind": "fanout",
                "model": "openai/gpt-4.1-mini",
                "max_depth": 1,
                "max_agents": 1,
                "options": {},
                "description": "",
            }
        )
    return def_id


# ── the helpers themselves ────────────────────────────────────────────────


def test_state_db_file_follows_the_url(moved_sqlite_url, absent_default, tmp_path):
    assert state_db_file() == tmp_path / "moved" / "state.db"


def test_state_db_file_is_none_when_the_store_is_not_a_file(absent_default, monkeypatch):
    _set_url(monkeypatch, _SERVER_URL)
    assert state_db_file() is None


def test_known_absent_is_false_once_the_moved_store_exists(moved_sqlite_url, tmp_path):
    assert state_db_known_absent() is True
    (tmp_path / "moved" / "state.db").touch()
    assert state_db_known_absent() is False


def test_known_absent_is_false_for_a_server_url(absent_default, monkeypatch):
    """No filesystem answer exists, so the open attempt must give the real one."""
    _set_url(monkeypatch, _SERVER_URL)
    assert state_db_known_absent() is False


def test_known_absent_is_false_for_an_in_memory_url(absent_default, monkeypatch):
    _set_url(monkeypatch, "sqlite+aiosqlite:///:memory:")
    assert state_db_known_absent() is False


# ── shape 1: a SQLite store moved off the default path ────────────────────


async def test_engine_defs_are_found_in_a_moved_store(moved_sqlite_url):
    from lionagi.studio.services import engine_defs as svc

    await _seed_engine_def("moved-def")

    rows = await svc.list_engine_defs()
    assert [r["name"] for r in rows] == ["moved-def"]
    assert await svc.get_engine_def_by_name("moved-def") is not None


async def test_schedules_are_found_in_a_moved_store(moved_sqlite_url):
    from lionagi.studio.services import schedules as svc

    await svc.create_schedule(
        {
            "name": "moved-schedule",
            "trigger_type": "interval",
            "interval_sec": 3600,
            "action_kind": "agent",
            "action_model": "openai/gpt-4.1-mini",
            "action_prompt": "noop",
        }
    )

    assert [r["name"] for r in await svc.list_schedules()] == ["moved-schedule"]
    assert await svc.get_schedule_by_name("moved-schedule") is not None


async def test_li_status_consults_a_moved_store(moved_sqlite_url, monkeypatch):
    """`li status` must report on the id, not on a file it never opens."""
    from lionagi.cli import status as status_mod

    await _seed_engine_def("anything")  # materializes the moved store
    monkeypatch.setattr(status_mod, "detect_project", lambda _p: ("dburl-sweep", "test"))
    out, _code = await status_mod._run_status_inner(
        command="agent", entity_id="deadbeef", as_json=False
    )
    assert "state.db not found" not in out


async def test_li_ctl_consults_a_moved_store(moved_sqlite_url):
    from lionagi.cli.orchestrate import _control

    await _seed_engine_def("anything")  # materializes the moved store
    msg, _code = await _control._enqueue_control_inner(
        entity_id="deadbeef", verb="pause", payload=None
    )
    assert "state.db not found" not in msg


async def test_engine_def_absent_from_a_moved_store_still_reads_as_absent(
    moved_sqlite_url,
):
    """The other half of the contract: a genuine miss keeps its answer."""
    from lionagi.studio.services import engine_defs as svc

    await _seed_engine_def("present")

    assert await svc.get_engine_def_by_name("never-created") is None


# ── shape 2: a URL that is not a local file ───────────────────────────────


async def test_a_server_url_does_not_answer_from_the_filesystem(absent_default, monkeypatch):
    """The default path is absent and the store is a server, so a filesystem
    guard would return an empty list without ever trying to connect.

    The reader must instead attempt the store and let the attempt speak — here
    that surfaces as a raised error (the driver is absent, or the host refuses),
    which is a true answer where ``[]`` was a fabricated one.
    """
    _set_url(monkeypatch, _SERVER_URL)
    from lionagi.studio.services import engine_defs as svc

    with pytest.raises(Exception):  # noqa: B017 — any failure beats a silent []
        await svc.list_engine_defs()


# ── the shared readonly seam ──────────────────────────────────────────────────
# Several reporting commands stopped guarding for themselves and now go through
# one helper. That concentrates the same question in one place: if the helper
# asks the filesystem while opening the configured store, every reader that
# migrated to it inherits the defect at once.


@pytest.mark.asyncio
async def test_the_readonly_seam_opens_a_moved_store_instead_of_reporting_it_absent(
    moved_sqlite_url,
):
    from lionagi.cli.machine import readonly_state_db

    name = f"seam-{uuid.uuid4().hex[:8]}"
    await _seed_engine_def(name)

    async with readonly_state_db() as (db, why):
        assert why is None, why
        assert db is not None
        rows = await db.fetch_all("SELECT name FROM engine_defs", [])

    assert name in {row["name"] for row in rows}


@pytest.mark.asyncio
async def test_the_readonly_seam_will_not_call_a_server_url_absent(absent_default, monkeypatch):
    # Nothing here connects: the point is that "absent" must not be answerable
    # from the filesystem for a store the filesystem knows nothing about. A
    # failure to reach it is a different answer, and the reason says which.
    from lionagi.cli.machine import REASON_NOT_FOUND, readonly_state_db

    _set_url(monkeypatch, _SERVER_URL)

    async with readonly_state_db() as (db, why):
        if db is None:
            assert why["reason_code"] != REASON_NOT_FOUND, why


def test_the_absent_reason_names_the_store_that_was_consulted(moved_sqlite_url, absent_default):
    from lionagi.cli.machine import state_db_absent

    detail = state_db_absent()["detail"]
    assert str(absent_default) not in detail
    assert "moved" in detail


def test_reported_sizes_describe_the_configured_store(moved_sqlite_url, tmp_path):
    from lionagi.cli.state import _db_sizes

    sizes = _db_sizes()
    assert sizes["is_file"] is True
    assert sizes["path"] == str(tmp_path / "moved" / "state.db")


def test_a_store_with_no_file_reports_no_size_rather_than_zero(absent_default, monkeypatch):
    # Zero would be a claim about an empty file. There is no file, so the honest
    # answer is that the question does not apply -- and `is_file` is what lets a
    # reader tell those apart instead of guessing from a bare 0.
    from lionagi.cli.state import _db_sizes

    _set_url(monkeypatch, _SERVER_URL)

    sizes = _db_sizes()
    assert sizes["is_file"] is False
    assert sizes["size_bytes"] is None
    assert sizes["wal_size_bytes"] is None
