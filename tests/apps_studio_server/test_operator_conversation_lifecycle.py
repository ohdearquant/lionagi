# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Rename, organize (pin/archive), and fork for durable Operator conversations."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from lionagi.studio.operator.coordinator import OperatorCoordinator
from lionagi.studio.operator.store import (
    OperatorConflictError,
    OperatorNotFoundError,
    OperatorStore,
)
from lionagi.studio.operator.types import OperatorEngineEvent


class _ScriptedEngine:
    async def _stream(self, _turn):
        yield OperatorEngineEvent(
            "text", {"content": "reply", "format": "plain", "role": "assistant"}
        )

    def stream(self, turn):
        return self._stream(turn)


class _BlockingEngine:
    async def _stream(self, _turn):
        await asyncio.Event().wait()
        yield  # pragma: no cover

    def stream(self, turn):
        return self._stream(turn)


async def _wait_done(store: OperatorStore, conversation_id: str, *, timeout: float = 5) -> list:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frames = await store.list_frames(conversation_id)
        if any(frame["type"] == "done" for frame in frames):
            return frames
        await asyncio.sleep(0.01)
    raise TimeoutError("Operator turn did not finish")


def _patch_state_db(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    import lionagi.cli._runs as runs_mod
    import lionagi.state.db as state_db_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", path.parent / "runs")


# Rename


@pytest.mark.asyncio
async def test_update_conversation_renames_without_disturbing_other_fields(tmp_path):
    store = OperatorStore(tmp_path / "state.db")
    conversation = await store.create_conversation(title="Original")
    updated = await store.update_conversation(conversation["id"], title="Renamed")
    assert updated["title"] == "Renamed"
    assert updated["status"] == "active"
    assert updated["pinned"] is False


@pytest.mark.asyncio
async def test_rename_a_conversation_that_does_not_exist_raises_not_found(tmp_path):
    store = OperatorStore(tmp_path / "state.db")
    await store.ensure_schema()
    with pytest.raises(OperatorNotFoundError):
        await store.update_conversation("does-not-exist", title="New title")


@pytest.mark.asyncio
async def test_rename_over_long_title_is_rejected_at_the_wire_boundary(tmp_path, monkeypatch):
    httpx = pytest.importorskip("httpx")
    from lionagi.studio.app import create_app
    from lionagi.studio.operator.coordinator import reset_operator_coordinator_for_testing

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
    app = create_app()
    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=_ScriptedEngine)
    await reset_operator_coordinator_for_testing(coordinator)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 54321))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
        created = await client.post("/api/operator/conversations", json={"title": "ok"})
        cid = created.json()["conversation"]["id"]
        response = await client.patch(
            f"/api/operator/conversations/{cid}",
            json={"title": "x" * 513},
        )
        assert response.status_code == 422
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_a_null_pinned_or_status_is_rejected_rather_than_acted_on(tmp_path, monkeypatch):
    """Only ``title`` is nullable. A null pin has no meaning, and accepting one
    would silently unpin, because a falsey value reaches the store's
    ``1 if pinned else 0``. A null status reaches a store that has to refuse it
    as a conflict rather than as the malformed request it is.
    """
    httpx = pytest.importorskip("httpx")
    from lionagi.studio.app import create_app
    from lionagi.studio.operator.coordinator import reset_operator_coordinator_for_testing

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
    app = create_app()
    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=_ScriptedEngine)
    await reset_operator_coordinator_for_testing(coordinator)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 54321))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
        created = await client.post("/api/operator/conversations", json={"title": "pinned one"})
        cid = created.json()["conversation"]["id"]
        assert (
            await client.patch(f"/api/operator/conversations/{cid}", json={"pinned": True})
        ).status_code == 200

        assert (
            await client.patch(f"/api/operator/conversations/{cid}", json={"pinned": None})
        ).status_code == 422
        assert (
            await client.patch(f"/api/operator/conversations/{cid}", json={"status": None})
        ).status_code == 422

        # The rejection did not act on the conversation on its way out.
        listed = await client.get("/api/operator/conversations")
        row = next(r for r in listed.json()["conversations"] if r["id"] == cid)
        assert row["pinned"] is True
        assert row["status"] == "active"

        # Controls: the shapes that DO mean something are still accepted, so
        # this is not a rule that rejects everything.
        assert (
            await client.patch(f"/api/operator/conversations/{cid}", json={"pinned": False})
        ).status_code == 200
        assert (
            await client.patch(f"/api/operator/conversations/{cid}", json={"title": None})
        ).status_code == 200
        listed = await client.get("/api/operator/conversations")
        row = next(r for r in listed.json()["conversations"] if r["id"] == cid)
        assert row["pinned"] is False
        assert row["title"] is None
    await coordinator.shutdown()


# Organize: pin + archive


@pytest.mark.asyncio
async def test_pin_sorts_a_conversation_first_regardless_of_recency(tmp_path):
    store = OperatorStore(tmp_path / "state.db")
    older = await store.create_conversation(title="older")
    newer = await store.create_conversation(title="newer")
    await store.update_conversation(older["id"], pinned=True)
    rows = await store.list_conversations()
    assert [row["id"] for row in rows] == [older["id"], newer["id"]]
    assert rows[0]["pinned"] is True
    assert rows[1]["pinned"] is False


@pytest.mark.asyncio
async def test_archive_removes_a_conversation_from_the_default_list_but_keeps_it_reachable(
    tmp_path,
):
    store = OperatorStore(tmp_path / "state.db")
    conversation = await store.create_conversation(title="stash me")
    await store.update_conversation(conversation["id"], status="archived")

    active_only = await store.list_conversations()
    assert conversation["id"] not in [row["id"] for row in active_only]

    archived_only = await store.list_conversations(status="archived")
    assert [row["id"] for row in archived_only] == [conversation["id"]]

    everything = await store.list_conversations(status="all")
    assert conversation["id"] in [row["id"] for row in everything]

    restored = await store.update_conversation(conversation["id"], status="active")
    assert restored["status"] == "active"
    assert conversation["id"] in [row["id"] for row in await store.list_conversations()]


@pytest.mark.asyncio
async def test_archiving_a_conversation_with_an_active_turn_conflicts(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=_BlockingEngine)
    await coordinator.startup()
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    await coordinator.submit(
        cid,
        instruction="hang",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    with pytest.raises(OperatorConflictError):
        await coordinator.store.update_conversation(cid, status="archived")
    await coordinator.shutdown()


# Fork


@pytest.mark.asyncio
async def test_fork_copies_completed_turns_into_a_new_independent_conversation(
    tmp_path, monkeypatch
):
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=_ScriptedEngine)
    await coordinator.startup()
    store = coordinator.store
    cid = (await coordinator.create_conversation(title="source"))["conversation"]["id"]
    accepted = await coordinator.submit(
        cid,
        instruction="hello",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    await _wait_done(store, cid)

    forked = await store.fork_conversation(cid)
    assert forked["id"] != cid
    assert forked["title"] == "source (fork)"
    assert forked["providerSessionId"] is None

    original_frames = await store.list_frames(cid)
    fork_frames = await store.list_frames(forked["id"])
    assert [f["type"] for f in fork_frames] == [f["type"] for f in original_frames]
    assert [f["sequence"] for f in fork_frames] == list(range(1, len(fork_frames) + 1))
    # Forked turns are copies with their own request ids, not shared rows.
    assert {f["requestId"] for f in fork_frames}.isdisjoint(
        {f["requestId"] for f in original_frames}
    )
    assert accepted["requestId"] not in {f["requestId"] for f in fork_frames}
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_fork_carries_the_whole_pin_not_just_the_model(tmp_path, monkeypatch):
    """A fork of a pinned conversation resolves to the same provider and model.

    The provider and the model are read as an independent pair when a turn is
    built, and an absent provider falls back to the environment. A fork that
    carried only the model would therefore run the source's model name against
    a different provider rather than refusing.
    """
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    store = OperatorStore(path)
    cid = (await store.create_conversation(title="pinned"))["id"]
    await store.select_provider_model(cid, provider="codex", model="gpt-5.4")

    forked = await store.fork_conversation(cid)

    assert forked["provider"] == "codex"
    assert forked["providerModel"] == "gpt-5.4"
    # The session belongs to the pair that opened it, so it is not inherited.
    assert forked["providerSessionId"] is None


@pytest.mark.asyncio
async def test_fork_up_to_sequence_excludes_later_turns(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=_ScriptedEngine)
    await coordinator.startup()
    store = coordinator.store
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    await coordinator.submit(
        cid,
        instruction="first",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    await _wait_done(store, cid)
    first_cut = (await store.get_conversation(cid))["nextSequence"] - 1

    await coordinator.submit(
        cid,
        instruction="second",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=first_cut,
    )
    await _wait_done(store, cid)

    forked = await store.fork_conversation(cid, up_to_sequence=first_cut)
    fork_frames = await store.list_frames(forked["id"])
    instructions = {
        frame["payload"].get("content") for frame in fork_frames if frame["type"] == "text"
    }
    assert "first" in instructions
    assert "second" not in instructions
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_fork_mid_stream_excludes_the_in_flight_turn_and_leaves_original_streaming(
    tmp_path, monkeypatch
):
    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=_ScriptedEngine)
    await coordinator.startup()
    store = coordinator.store
    cid = (await coordinator.create_conversation())["conversation"]["id"]
    await coordinator.submit(
        cid,
        instruction="done already",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    await _wait_done(store, cid)
    completed_cut = (await store.get_conversation(cid))["nextSequence"] - 1

    # Swap in a blocking engine for the second, still-streaming turn.
    coordinator.engine_factory = _BlockingEngine
    accepted = await coordinator.submit(
        cid,
        instruction="still streaming",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=completed_cut,
    )
    assert (await store.get_conversation(cid))["activeRequestId"] == accepted["requestId"]

    forked = await store.fork_conversation(cid)
    fork_frames = await store.list_frames(forked["id"])
    instructions = {
        frame["payload"].get("content") for frame in fork_frames if frame["type"] == "text"
    }
    assert "done already" in instructions
    assert "still streaming" not in instructions

    # The source conversation's in-flight turn is untouched by the fork.
    assert (await store.get_conversation(cid))["activeRequestId"] == accepted["requestId"]
    await coordinator.cancel(cid, accepted["requestId"])
    await _wait_done(store, cid)
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_fork_of_a_conversation_with_no_completed_turns_yields_an_empty_conversation(
    tmp_path,
):
    store = OperatorStore(tmp_path / "state.db")
    conversation = await store.create_conversation()
    forked = await store.fork_conversation(conversation["id"])
    assert forked["title"] == f"Fork of {conversation['id'][:8]}"
    assert await store.list_frames(forked["id"]) == []


@pytest.mark.asyncio
async def test_forking_a_conversation_that_does_not_exist_raises_not_found(tmp_path):
    store = OperatorStore(tmp_path / "state.db")
    await store.ensure_schema()
    with pytest.raises(OperatorNotFoundError):
        await store.fork_conversation("does-not-exist")


# HTTP contract


@pytest.mark.asyncio
async def test_http_patch_and_fork_routes(tmp_path, monkeypatch):
    httpx = pytest.importorskip("httpx")
    from lionagi.studio.app import create_app
    from lionagi.studio.operator.coordinator import reset_operator_coordinator_for_testing

    path = tmp_path / "state.db"
    _patch_state_db(monkeypatch, path)
    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
    app = create_app()
    coordinator = OperatorCoordinator(store=OperatorStore(path), engine_factory=_ScriptedEngine)
    await reset_operator_coordinator_for_testing(coordinator)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 54321))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
        created = await client.post("/api/operator/conversations", json={"title": "http source"})
        cid = created.json()["conversation"]["id"]

        renamed = await client.patch(
            f"/api/operator/conversations/{cid}", json={"title": "renamed via http"}
        )
        assert renamed.status_code == 200
        assert renamed.json()["conversation"]["title"] == "renamed via http"

        pinned = await client.patch(f"/api/operator/conversations/{cid}", json={"pinned": True})
        assert pinned.json()["conversation"]["pinned"] is True

        archived = await client.patch(
            f"/api/operator/conversations/{cid}", json={"status": "archived"}
        )
        assert archived.json()["conversation"]["status"] == "archived"

        listed_active = await client.get("/api/operator/conversations")
        assert cid not in [row["id"] for row in listed_active.json()["conversations"]]
        listed_archived = await client.get(
            "/api/operator/conversations", params={"status": "archived"}
        )
        assert cid in [row["id"] for row in listed_archived.json()["conversations"]]

        missing = await client.patch(
            "/api/operator/conversations/does-not-exist", json={"title": "x"}
        )
        assert missing.status_code == 404

        await client.patch(f"/api/operator/conversations/{cid}", json={"status": "active"})
        submitted = await client.post(
            f"/api/operator/conversations/{cid}/turns",
            json={
                "instruction": "hello over http",
                "context": {"space": "mission", "route": "/", "filters": {}},
                "expectedLastSequence": 0,
            },
        )
        assert submitted.status_code == 202
        await _wait_done(coordinator.store, cid)

        forked = await client.post(f"/api/operator/conversations/{cid}/fork", json={})
        assert forked.status_code == 201
        body = forked.json()
        assert body["conversation"]["id"] != cid
        assert body["conversation"]["title"] == "renamed via http (fork)"
        assert len(body["frames"]) > 0

        fork_missing = await client.post("/api/operator/conversations/does-not-exist/fork", json={})
        assert fork_missing.status_code == 404
    await coordinator.shutdown()
