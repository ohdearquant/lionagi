# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Transactional, existing-identity-only consumer operator mutation."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import uuid

import pytest

from lionagi.state.db import StateDB
from lionagi.state.lifecycle.deliveries import (
    ConsumerAlreadyActiveError,
    ConsumerAlreadyRetiredError,
    ConsumerNotRegisteredError,
    change_consumer,
    is_acknowledged,
    register_consumer,
)


def _forbid(*_args, **_kwargs):
    raise AssertionError("offline test forbids processes, signals and network")


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, _forbid)
    for name in ("create_subprocess_exec", "create_subprocess_shell"):
        monkeypatch.setattr(asyncio, name, _forbid)
    for name in ("kill", "killpg", "system"):
        monkeypatch.setattr(os, name, _forbid, raising=False)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, _forbid)
    monkeypatch.setattr(socket, "create_connection", _forbid)


async def _seed(db):
    sid, progression = str(uuid.uuid4()), str(uuid.uuid4())
    await db.create_progression(progression)
    await db.create_session({"id": sid, "progression_id": progression, "status": "running"})
    assert await db.update_status(
        "session",
        sid,
        new_status="failed",
        reason_code="session.stale.no_heartbeat",
        source="system",
        actor="test",
    )
    row = await db.fetch_one(
        "SELECT id FROM status_transitions WHERE entity_type = ? AND entity_id = ? AND status = ?",
        ("session", sid, "failed"),
    )
    return row["id"]


@pytest.mark.asyncio
async def test_absent_and_wrong_state_mutations_refuse_without_changes(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        await _seed(db)
        with pytest.raises(ConsumerNotRegisteredError):
            await change_consumer(db, "missing", action="reactivate", start="all")
        with pytest.raises(ConsumerNotRegisteredError):
            await change_consumer(db, "missing", action="retire")
        assert not await db.fetch_all("SELECT * FROM terminal_consumers")
        await register_consumer(db, "known", start="all")
        with pytest.raises(ConsumerAlreadyActiveError):
            await change_consumer(db, "known", action="reactivate", start="now")
        assert not await db.fetch_all("SELECT * FROM terminal_deliveries")
        result = await change_consumer(db, "known", action="retire")
        assert result.changed and result.owed_before == 1 and result.owed_after == 0
        with pytest.raises(ConsumerAlreadyRetiredError):
            await change_consumer(db, "known", action="retire")
        rows = await db.fetch_all("SELECT * FROM terminal_consumers")
        assert len(rows) == 1 and rows[0]["retired_at"] == result.retired_at_after


@pytest.mark.asyncio
@pytest.mark.parametrize("start", ["now", "all"])
async def test_reactivation_snapshot_counts_and_baselines(tmp_path, start):
    async with StateDB(tmp_path / "state.db") as db:
        first, second = await _seed(db), await _seed(db)
        await register_consumer(db, "known", start="all")
        retired = await change_consumer(db, "known", action="retire")
        assert retired.owed_before == retired.candidate_before == 2
        assert retired.owed_after == 0 and retired.baselined == 0
        result = await change_consumer(db, "known", action="reactivate", start=start)
        assert result.changed and result.retired_at_before is not None
        assert result.retired_at_after is None and result.owed_before == 0
        assert result.candidate_before == 2
        assert result.baselined == (2 if start == "now" else 0)
        assert result.owed_after == (0 if start == "now" else 2)
        for event_id in (first, second):
            assert await is_acknowledged(db, event_id, "known") is (start == "now")


@pytest.mark.asyncio
async def test_invalid_action_or_start_does_not_modify_existing_registration(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        await register_consumer(db, "known", start="all")
        for kwargs in (
            {"action": "reactivate"},
            {"action": "reactivate", "start": "unexpected"},
            {"action": "retire", "start": "now"},
            {"action": "unexpected"},
        ):
            with pytest.raises(ValueError):
                await change_consumer(db, "known", **kwargs)
        row = await db.fetch_one(
            "SELECT retired_at FROM terminal_consumers WHERE name = ?", ("known",)
        )
        assert row["retired_at"] is None


@pytest.mark.asyncio
async def test_concurrent_reactivation_has_one_winner_and_no_rebaseline(tmp_path):
    path = tmp_path / "state.db"
    async with StateDB(path) as one, StateDB(path) as two:
        event_id = await _seed(one)
        await register_consumer(one, "known", start="all")
        await change_consumer(one, "known", action="retire")
        results = await asyncio.gather(
            change_consumer(one, "known", action="reactivate", start="now"),
            change_consumer(two, "known", action="reactivate", start="now"),
            return_exceptions=True,
        )
        winners = [item for item in results if not isinstance(item, BaseException)]
        losers = [item for item in results if isinstance(item, ConsumerAlreadyActiveError)]
        assert len(winners) == len(losers) == 1
        assert winners[0].baselined == 1 and winners[0].owed_after == 0
        assert await is_acknowledged(one, event_id, "known")
        rows = await one.fetch_all(
            "SELECT * FROM terminal_deliveries WHERE consumer = ?", ("known",)
        )
        assert len(rows) == 1
