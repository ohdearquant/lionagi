# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A null in a surviving progression must not pin unrelated orphan messages."""

from __future__ import annotations

import json
import time
import uuid

import pytest

from lionagi.cli.state import _prune
from lionagi.state import db as db_mod
from lionagi.state.db import StateDB


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["empty", "shared", "null_and_shared"])
async def test_prune_reclaims_orphan_with_empty_or_null_surviving_collection(
    tmp_path, monkeypatch, case
):
    path = tmp_path / "state.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    victim_sid, survivor_sid, victim_pid, survivor_pid, orphan, shared = (
        str(uuid.uuid4()) for _ in range(6)
    )
    now = time.time()
    survivor_collection = {"empty": [], "shared": [shared], "null_and_shared": [None, shared]}[case]

    async with StateDB(path) as db:
        await db.create_progression(victim_pid)
        # A legacy store may already contain a null even though new writes
        # reject it. Keep the prune regression against that historical data.
        if case == "null_and_shared":
            await db.create_progression(survivor_pid)
            await db.execute(
                "UPDATE progressions SET collection = ? WHERE id = ?",
                (json.dumps(survivor_collection), survivor_pid),
            )
        else:
            await db.create_progression(survivor_pid, survivor_collection)
        assert await db.get_progression(survivor_pid) == survivor_collection
        for sid, pid in ((victim_sid, victim_pid), (survivor_sid, survivor_pid)):
            await db.create_session({"id": sid, "progression_id": pid, "status": "completed"})
        for mid in (orphan, shared):
            await db.insert_message(
                {
                    "id": mid,
                    "created_at": now,
                    "node_metadata": {},
                    "content": {"text": mid},
                    "role": "user",
                    "sender": "u",
                    "recipient": "x",
                    "channel": "test",
                }
            )
            await db.append_to_progression(victim_pid, mid)
        await db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (now - 60 * 86400, victim_sid)
        )
        await db.execute(
            "UPDATE sessions SET updated_at = ?, first_msg_id = ? WHERE id = ?",
            (now, shared if case == "empty" else None, survivor_sid),
        )

    preview = await _prune(keep_days=30, keep_n=1, dry_run=True)
    assert preview == {"sessions": 1, "branches": 0, "messages": 1}
    async with StateDB(path) as db:
        assert await db.get_session(victim_sid) is not None
        assert await db.get_session(survivor_sid) is not None
        assert await db.fetch_one("SELECT id FROM messages WHERE id = ?", (orphan,))
        assert await db.fetch_one("SELECT id FROM messages WHERE id = ?", (shared,))
        assert await db.get_progression(victim_pid) == [orphan, shared]
        assert await db.get_progression(survivor_pid) == survivor_collection

    actual = await _prune(keep_days=30, keep_n=1, dry_run=False)
    assert actual == preview
    async with StateDB(path) as db:
        assert await db.get_session(victim_sid) is None
        survivor = await db.get_session(survivor_sid)
        assert survivor is not None
        assert survivor["first_msg_id"] == (shared if case == "empty" else None)
        assert not await db.fetch_one("SELECT id FROM messages WHERE id = ?", (orphan,))
        assert await db.fetch_one("SELECT id FROM messages WHERE id = ?", (shared,))
        assert not await db.fetch_one("SELECT id FROM progressions WHERE id = ?", (victim_pid,))
        assert await db.fetch_one("SELECT id FROM progressions WHERE id = ?", (survivor_pid,))
        assert await db.get_progression(survivor_pid) == survivor_collection
