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
