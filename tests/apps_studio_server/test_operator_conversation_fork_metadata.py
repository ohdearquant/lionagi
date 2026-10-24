# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Forked conversations retain their descriptive metadata independently."""

from __future__ import annotations

import pytest

from lionagi.studio.operator.store import OperatorStore


@pytest.mark.asyncio
@pytest.mark.parametrize("fork_title", [None, "Separate investigation"])
async def test_fork_copies_description_and_preserves_source(tmp_path, fork_title):
    path = tmp_path / "state.db"
    store = OperatorStore(path)
    source = await store.create_conversation(title="Original investigation")
    cid = source["id"]
    await store.update_conversation(cid, description="Original scope\nFollow-up questions")
    turn = await store.submit_turn(
        cid,
        instruction="Investigate the original scope",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    assert await store.mark_running(turn["requestId"])
    await store.finish_turn(turn["requestId"], outcome="completed")
    source = await store.get_conversation(cid)
    source_frames = await store.list_frames(cid)

    forked = await store.fork_conversation(cid, title=fork_title)
    reopened = OperatorStore(path)
    persisted = await reopened.get_conversation(forked["id"])
    assert persisted == forked
    assert persisted["description"] == "Original scope\nFollow-up questions"
    assert persisted["title"] == (fork_title or "Original investigation (fork)")
    assert persisted["id"] != cid
    assert any(frame["type"] == "done" for frame in await reopened.list_frames(forked["id"]))
    assert await reopened.get_conversation(cid) == source
    assert await reopened.list_frames(cid) == source_frames

    await reopened.update_conversation(forked["id"], description="Fork-specific scope")
    assert (await reopened.get_conversation(cid))["description"] == source["description"]
    assert (await reopened.get_conversation(forked["id"]))["description"] == "Fork-specific scope"
