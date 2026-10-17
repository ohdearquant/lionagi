"""Conversation metadata belongs to one active turn and commits with its outcome."""

from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

pytestmark = pytest.mark.asyncio


async def _active(tmp_path, monkeypatch):
    from lionagi.studio.operator.store import OperatorStore

    path = tmp_path / "state.db"
    store = OperatorStore(path)
    cid = (await store.create_conversation(title="Original"))["id"]
    turn = await store.submit_turn(
        cid,
        instruction="Organize this conversation",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    rid = turn["requestId"]
    assert await store.mark_running(rid)
    monkeypatch.setenv("LIONAGI_OPERATOR_DB_PATH", str(path))
    monkeypatch.setenv("LIONAGI_OPERATOR_CONVERSATION_ID", cid)
    monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", rid)
    return path, store, cid, rid


async def _call(arguments):
    from lionagi.studio.operator.application_mcp import _dispatch

    response = await _dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "update_conversation", "arguments": arguments},
        }
    )
    result = response["result"]
    return result, json.loads(result["content"][0]["text"])


async def _update(**arguments):
    result, value = await _call(arguments)
    assert not result.get("isError"), value
    assert value["updated"] is True
    return value


def _snapshot(path):
    with sqlite3.connect(path) as db:
        return tuple(
            db.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
            for table in (
                "studio_operator_conversations",
                "studio_operator_views",
                "studio_operator_turns",
                "studio_operator_proposals",
                "studio_operator_effects",
                "studio_operator_frames",
            )
        )


async def test_tool_commits_metadata_effect_and_result_frame_before_reporting_success(
    tmp_path, monkeypatch
):
    from lionagi.studio.operator.store import OperatorStore

    path, store, cid, rid = await _active(tmp_path, monkeypatch)
    other = await store.create_conversation(title="Other conversation")
    value = await _update(
        conversation_id=cid, title="  Café planning  ", description="  Notes\n\tfor tomorrow  "
    )
    effect = value["effect"]
    assert str(uuid.UUID(effect["id"])) == effect["id"]
    assert effect == {
        "id": effect["id"],
        "kind": "conversation_updated",
        "conversationId": cid,
        "title": "Café planning",
        "description": "Notes\n\tfor tomorrow",
        "status": "applied",
        "appliedAt": effect["appliedAt"],
    }
    assert isinstance(effect["appliedAt"], (int, float)) and effect["appliedAt"] > 0
    reopened = OperatorStore(path)
    row = await reopened.get_conversation(cid)
    assert row == value["conversation"]
    assert (row["title"], row["description"]) == (effect["title"], effect["description"])
    assert await reopened.get_conversation(other["id"]) == other
    with sqlite3.connect(path) as db:
        persisted = db.execute(
            "SELECT conversation_id, request_id, effect_type, status, effect_json "
            "FROM studio_operator_effects WHERE id=?",
            (effect["id"],),
        ).fetchone()
    assert persisted[:4] == (cid, rid, "conversation_updated", "applied")
    assert json.loads(persisted[4]) == effect
    frames = await reopened.list_frames(cid)
    outcomes = [frame for frame in frames if frame["type"] == "tool_result"]
    assert len(outcomes) == 1
    assert outcomes[0]["payload"] == {
        "callId": effect["id"],
        "ok": True,
        "result": {"updated": True, "effect": effect},
    }
    assert outcomes[0]["requestId"] == rid
    assert not any(frame["type"] == "ui_command" for frame in frames)


async def test_tool_partial_updates_preserve_omitted_fields_and_clear_description(
    tmp_path, monkeypatch
):
    _, store, cid, _ = await _active(tmp_path, monkeypatch)
    await _update(title="Kept title", description="First description")
    value = await _update(description="Second description")
    assert value["conversation"]["title"] == "Kept title"
    assert value["effect"]["description"] == "Second description"
    value = await _update(title="Second title")
    assert value["conversation"]["description"] == "Second description"
    for clear in (None, " \n\t "):
        await _update(description="Description to clear")
        value = await _update(description=clear)
        assert value["conversation"]["description"] is None
        assert value["effect"]["description"] is None
        assert (await store.get_conversation(cid))["title"] == "Second title"


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"title": None},
        {"title": ""},
        {"title": "   "},
        {"title": "x" * 513},
        {"title": "bad\nname"},
        {"title": "bad\tname"},
        {"title": "bad\x00name"},
        {"title": "bad\u202ename"},
        {"title": "bad\u2028name"},
        {"title": "bad\u2029name"},
        {"description": "x" * 2001},
        {"description": "bad\rdescription"},
        {"description": "bad\x00description"},
        {"description": "bad\u202edescription"},
        {"description": "bad\u2028description"},
        {"description": "bad\u2029description"},
    ],
)
async def test_invalid_tool_metadata_is_refused_without_writes(tmp_path, monkeypatch, bad):
    path, _, _, _ = await _active(tmp_path, monkeypatch)
    await _update(title="  " + "é" * 512 + "  ", description="  " + "é" * 2000 + "  ")
    before = _snapshot(path)
    response, _ = await _call(bad)
    assert response.get("isError") is True
    assert _snapshot(path) == before


async def test_tool_refuses_other_conversation_with_no_row_or_effect_changes(tmp_path, monkeypatch):
    path, store, _, _ = await _active(tmp_path, monkeypatch)
    await _update(title="Own title")
    other = await store.create_conversation(title="Untouched")
    before = _snapshot(path)
    response, value = await _call({"conversation_id": other["id"], "title": "Wrong target"})
    assert not response.get("isError")
    assert value == {"updated": False, "reason": "conversation_mismatch"}
    assert _snapshot(path) == before


@pytest.mark.parametrize("state", ["mismatched", "finished", "missing"])
async def test_tool_refuses_a_turn_that_does_not_own_the_active_conversation(
    tmp_path, monkeypatch, state
):
    path, store, cid, rid = await _active(tmp_path, monkeypatch)
    await _update(title="Positive control")
    if state == "finished":
        await store.finish_turn(rid, outcome="completed")
    elif state == "mismatched":
        other = await store.create_conversation(title="Other")
        turn = await store.submit_turn(
            other["id"],
            instruction="Other turn",
            context={"space": "mission", "route": "/", "filters": {}},
            expected_last_sequence=0,
        )
        assert await store.mark_running(turn["requestId"])
        monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", turn["requestId"])
    else:
        monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", str(uuid.uuid4()))
    before = _snapshot(path)
    response, value = await _call({"conversation_id": cid, "description": "Must not land"})
    assert not response.get("isError")
    assert value == {"updated": False, "reason": "turn_not_active"}
    assert _snapshot(path) == before


@pytest.mark.parametrize(
    "guard",
    ["inactive", "active_request", "missing_turn", "turn_conversation", "turn_status", "done"],
)
async def test_tool_checks_each_active_turn_guard_independently(tmp_path, monkeypatch, guard):
    path, store, cid, rid = await _active(tmp_path, monkeypatch)
    await _update(title="Positive control", description="Kept description")
    if guard == "done":
        assert await store.append_frame(cid, rid, "done", {"outcome": "completed"})
    else:
        other = await store.create_conversation(title="Other")
        # Model one stale persisted fact at a time. finish_turn changes the
        # status, active pointer, and done frame together, hiding guard gaps.
        with sqlite3.connect(path) as db:
            if guard == "inactive":
                db.execute(
                    "UPDATE studio_operator_conversations SET status='archived' WHERE id=?",
                    (cid,),
                )
            elif guard == "active_request":
                db.execute(
                    "UPDATE studio_operator_conversations SET active_request_id=NULL WHERE id=?",
                    (cid,),
                )
            elif guard == "missing_turn":
                missing = str(uuid.uuid4())
                db.execute(
                    "UPDATE studio_operator_conversations SET active_request_id=? WHERE id=?",
                    (missing, cid),
                )
                monkeypatch.setenv("LIONAGI_OPERATOR_REQUEST_ID", missing)
            elif guard == "turn_conversation":
                db.execute(
                    "UPDATE studio_operator_turns SET conversation_id=? WHERE request_id=?",
                    (other["id"], rid),
                )
            else:
                db.execute(
                    "UPDATE studio_operator_turns SET status='queued' WHERE request_id=?",
                    (rid,),
                )
    before = _snapshot(path)
    response, value = await _call({"title": "Must not land", "description": "Must not land"})
    assert not response.get("isError")
    assert value == {"updated": False, "reason": "turn_not_active"}
    assert _snapshot(path) == before


@pytest.mark.parametrize("budget", ["frame_size", "frame_count", "turn_bytes"])
async def test_metadata_outcome_budget_refusal_rolls_back_all_tables(tmp_path, monkeypatch, budget):
    import lionagi.studio.operator.store as store_module

    path, _, _, rid = await _active(tmp_path, monkeypatch)
    await _update(title="Before", description="Before description")
    with sqlite3.connect(path) as db:
        frame_count, payload_bytes = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(CAST(payload_json AS BLOB))), 0) "
            "FROM studio_operator_frames WHERE request_id=?",
            (rid,),
        ).fetchone()
    if budget == "frame_size":
        monkeypatch.setattr(store_module, "MAX_FRAME_PAYLOAD_BYTES", 1)
    elif budget == "frame_count":
        monkeypatch.setattr(store_module, "MAX_FRAMES_PER_TURN", frame_count)
    else:
        monkeypatch.setattr(store_module, "MAX_TURN_PAYLOAD_BYTES", payload_bytes)
    before = _snapshot(path)
    response, value = await _call({"title": "After", "description": "Must roll back"})
    assert not response.get("isError")
    assert value == {"updated": False, "reason": "turn_budget_exceeded"}
    assert _snapshot(path) == before


@pytest.mark.parametrize("table", ["studio_operator_effects", "studio_operator_frames"])
async def test_effect_or_result_frame_failure_rolls_back_metadata(tmp_path, monkeypatch, table):
    path, _, _, _ = await _active(tmp_path, monkeypatch)
    await _update(title="Before", description="Before description")
    before = _snapshot(path)
    with sqlite3.connect(path) as db:
        db.execute(
            f"CREATE TRIGGER refuse_metadata_outcome BEFORE INSERT ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'outcome write failed'); END"
        )
    response, _ = await _call({"title": "After", "description": "Must roll back"})
    assert response.get("isError") is True
    assert _snapshot(path) == before


async def test_old_conversation_store_migrates_description_and_keeps_existing_writes(tmp_path):
    from lionagi.studio.operator.store import OperatorStore

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE studio_operator_conversations (
                id TEXT PRIMARY KEY, project TEXT, title TEXT, status TEXT NOT NULL,
                next_sequence INTEGER NOT NULL DEFAULT 1, active_request_id TEXT,
                provider_session_id TEXT, provider_model TEXT, provider TEXT,
                resolved_provider TEXT, resolved_model TEXT, branch_id TEXT,
                pinned INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
                updated_at REAL NOT NULL, archived_at REAL, deleted_at REAL
            );
            INSERT INTO studio_operator_conversations
                (id, title, status, created_at, updated_at)
                VALUES ('legacy', 'Legacy title', 'active', 1, 2);
        """)
    store = OperatorStore(path)
    old = await store.get_conversation("legacy")
    assert old["description"] is None
    assert (old["title"], old["createdAt"], old["updatedAt"]) == ("Legacy title", 1, 2)
    with sqlite3.connect(path) as db:
        assert "description" in {
            row[1] for row in db.execute("PRAGMA table_info(studio_operator_conversations)")
        }
    updated = await store.update_conversation("legacy", description="Persisted", pinned=True)
    assert (updated["title"], updated["description"], updated["pinned"]) == (
        "Legacy title",
        "Persisted",
        True,
    )
    assert (await OperatorStore(path).list_conversations())[0]["description"] == "Persisted"


async def test_http_description_round_trips_and_preserves_existing_title_policy(
    tmp_path, monkeypatch
):
    import httpx

    import lionagi.state.db as state_db_mod
    from lionagi.studio.app import create_app
    from lionagi.studio.operator.coordinator import (
        OperatorCoordinator,
        reset_operator_coordinator_for_testing,
    )

    path, store, cid, rid = await _active(tmp_path, monkeypatch)
    await _update(title="Tool title", description="Tool description")
    await store.finish_turn(rid, outcome="completed")
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
    coordinator = OperatorCoordinator(store=store)
    await reset_operator_coordinator_for_testing(coordinator)
    transport = httpx.ASGITransport(app=create_app(), client=("127.0.0.1", 54321))
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8765"
        ) as client:
            target = f"/api/operator/conversations/{cid}"
            response = await client.patch(target, json={"description": "  REST\n\tnotes  "})
            assert response.status_code == 200
            assert response.json()["conversation"]["title"] == "Tool title"
            listed = await client.get("/api/operator/conversations")
            row = next(row for row in listed.json()["conversations"] if row["id"] == cid)
            assert row["description"] == "REST\n\tnotes"
            for title in ("  whitespace preserved  ", "", "bad\nlegacy\x00title", None):
                response = await client.patch(target, json={"title": title})
                assert response.status_code == 200
                assert response.json()["conversation"]["title"] == title
                assert response.json()["conversation"]["description"] == "REST\n\tnotes"
            assert (await client.patch(target, json={"title": "x" * 513})).status_code == 422
            for clear in (None, " \n\t "):
                assert (
                    await client.patch(target, json={"description": "Set again"})
                ).status_code == 200
                response = await client.patch(target, json={"description": clear})
                assert response.status_code == 200
                assert response.json()["conversation"]["description"] is None
    finally:
        await reset_operator_coordinator_for_testing(None)
