# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Section 1 tests: _graph_from_metadata() and get_session() DAG graph paths.

Coverage targets:
  - lionagi.studio.services.sessions._graph_from_metadata  (all branches)
  - lionagi.studio.services.sessions.get_session           (node_metadata → graph)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")

from lionagi.state.db import StateDB  # noqa: E402

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------


def dag_metadata() -> dict:
    return {
        "agents": [
            {"id": "analyst", "name": "Analyst", "model": "openai/gpt-5.4"},
            {"id": "critic", "name": "Critic", "model": "anthropic/claude-sonnet-4-6"},
        ],
        "operations": [
            {"id": "collect", "agent_id": "analyst", "depends_on": []},
            {"id": "validate", "agent_id": "critic", "depends_on": ["collect"]},
        ],
    }


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_sessions_db(tmp_path, monkeypatch):
    import lionagi.studio.services.sessions as svc

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(svc, "_DB", str(db_path))
    monkeypatch.setattr(svc, "DEFAULT_DB_PATH", db_path)
    return svc, db_path


async def seed_session(
    db_path: Path,
    *,
    session_id: str = "sess-1",
    node_metadata=None,
    status: str = "running",
    started_at=None,
    ended_at=None,
) -> str:
    prog_id = f"{session_id}-prog"
    async with StateDB(db_path) as db:
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": session_id,
                "created_at": 100.0,
                "updated_at": 100.0,
                "progression_id": prog_id,
                "name": "Test Session",
                "status": status,
                "started_at": started_at,
                "ended_at": ended_at,
                "node_metadata": node_metadata,
                "invocation_kind": "flow",
                "source_kind": "live",
            }
        )
    return prog_id


async def overwrite_session_node_metadata(db_path: Path, session_id: str, raw: str) -> None:
    """Write raw (possibly invalid) JSON directly into the sessions.node_metadata column."""
    import aiosqlite as aio

    async with aio.connect(str(db_path)) as db:
        await db.execute(
            "UPDATE sessions SET node_metadata = ? WHERE id = ?",
            (raw, session_id),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Test 1.1 — falsy / unparseable inputs return None
# ---------------------------------------------------------------------------


def test_graph_from_metadata_none_empty_and_invalid_json_return_none():
    from lionagi.studio.services.sessions import _graph_from_metadata

    assert _graph_from_metadata(None) is None
    assert _graph_from_metadata("") is None
    assert _graph_from_metadata("{not-json") is None


# ---------------------------------------------------------------------------
# Test 1.2 — non-dict root and empty operations list return None
# ---------------------------------------------------------------------------


def test_graph_from_metadata_rejects_non_dict_and_missing_operations():
    from lionagi.studio.services.sessions import _graph_from_metadata

    assert _graph_from_metadata(json.dumps(["not", "a", "dict"])) is None
    assert _graph_from_metadata(json.dumps({"agents": [{"id": "a1", "name": "Analyst"}]})) is None
    assert _graph_from_metadata(json.dumps({"agents": [], "operations": []})) is None


# ---------------------------------------------------------------------------
# Test 1.3 — valid DAG: correct node fields and dependency edge
# ---------------------------------------------------------------------------


def test_graph_from_metadata_builds_nodes_and_dependency_edges():
    from lionagi.studio.services.sessions import _graph_from_metadata

    graph = _graph_from_metadata(json.dumps(dag_metadata()))

    assert graph is not None
    nodes = graph["nodes"]
    edges = graph["edges"]

    assert len(nodes) == 2

    first = nodes[0]
    assert first["id"] == "collect"
    assert first["label"] == "collect"
    assert first["role"] == "Analyst"
    assert first["assignment"] == "openai/gpt-5.4"
    assert first["prompt"] == ""
    assert first["capacity"] == 1
    assert first["timeout"] is None
    assert first["inputs"] == []
    assert first["outputs"] == []

    second = nodes[1]
    assert second["id"] == "validate"
    assert second["role"] == "Critic"
    assert second["assignment"] == "anthropic/claude-sonnet-4-6"
    assert second["inputs"] == ["collect"]

    assert edges == [
        {"id": "e-collect-validate", "source": "collect", "target": "validate", "mode": "simple"}
    ]


# ---------------------------------------------------------------------------
# Test 1.4 — malformed agents/operations entries are silently filtered
# ---------------------------------------------------------------------------


def test_graph_from_metadata_filters_malformed_agents_and_operations():
    from lionagi.studio.services.sessions import _graph_from_metadata

    meta = {
        "agents": [
            None,
            {},
            {"name": "No Id"},
            {"id": "a1", "name": "Analyst", "model": "gpt-5"},
        ],
        "operations": [
            None,
            {},
            {"agent_id": "a1"},
            {"id": "ok", "agent_id": "a1", "depends_on": []},
        ],
    }
    graph = _graph_from_metadata(json.dumps(meta))

    assert graph is not None
    assert len(graph["nodes"]) == 1
    node = graph["nodes"][0]
    assert node["id"] == "ok"
    assert node["role"] == "Analyst"
    assert node["assignment"] == "gpt-5"
    assert graph["edges"] == []


# ---------------------------------------------------------------------------
# Test 1.5 — unknown agent_id yields blank role and assignment
# ---------------------------------------------------------------------------


def test_graph_from_metadata_unknown_agent_uses_blank_role_and_assignment():
    from lionagi.studio.services.sessions import _graph_from_metadata

    meta = {
        "agents": [],
        "operations": [{"id": "solo", "agent_id": "missing", "depends_on": []}],
    }
    graph = _graph_from_metadata(json.dumps(meta))

    assert graph is not None
    assert len(graph["nodes"]) == 1
    node = graph["nodes"][0]
    assert node["id"] == "solo"
    assert node["role"] == ""
    assert node["assignment"] == ""
    assert graph["edges"] == []


# ---------------------------------------------------------------------------
# Test 1.6 — string depends_on must not produce character-level edges
# ---------------------------------------------------------------------------


def test_graph_from_metadata_malformed_depends_on_does_not_create_character_edges():
    from lionagi.studio.services.sessions import _graph_from_metadata

    meta = {
        "agents": [{"id": "a1", "name": "Analyst", "model": "gpt-5"}],
        "operations": [{"id": "child", "agent_id": "a1", "depends_on": "root"}],
    }
    graph = _graph_from_metadata(json.dumps(meta))

    assert graph is not None
    assert len(graph["nodes"]) == 1
    node = graph["nodes"][0]
    assert node["inputs"] == []
    assert graph["edges"] == []


# ---------------------------------------------------------------------------
# Test 1.7 — get_session: valid DAG metadata → full graph in response
# ---------------------------------------------------------------------------


async def test_get_session_returns_graph_from_session_node_metadata(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_session(
        db_path,
        session_id="sess-dag",
        node_metadata=dag_metadata(),
        status="completed",
        started_at=10.0,
        ended_at=13.5,
    )

    result = await svc.get_session("sess-dag")

    assert result is not None
    assert result["id"] == "sess-dag"
    assert result["status"] == "completed"
    assert result["duration_ms"] == 3500.0

    graph = result["graph"]
    assert graph is not None
    assert graph["nodes"][0]["id"] == "collect"
    assert graph["nodes"][1]["inputs"] == ["collect"]
    assert graph["edges"] == [
        {"id": "e-collect-validate", "source": "collect", "target": "validate", "mode": "simple"}
    ]


# ---------------------------------------------------------------------------
# Test 1.8 — get_session: null metadata → graph is None, duration is None
# ---------------------------------------------------------------------------


async def test_get_session_returns_none_graph_for_null_node_metadata(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_session(
        db_path,
        session_id="sess-no-dag",
        node_metadata=None,
        status="running",
        started_at=20.0,
        ended_at=None,
    )

    result = await svc.get_session("sess-no-dag")

    assert result is not None
    assert result["graph"] is None
    assert result["branches"] == []
    assert result["duration_ms"] is None
    assert result["source_kind"] == "live"


# ---------------------------------------------------------------------------
# Test 1.9 — get_session: corrupt raw metadata → graph is None, no exception
# ---------------------------------------------------------------------------


async def test_get_session_returns_none_graph_for_raw_invalid_node_metadata(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-bad-dag", node_metadata=None)
    await overwrite_session_node_metadata(db_path, "sess-bad-dag", "{bad-json")

    result = await svc.get_session("sess-bad-dag")

    assert result is not None
    assert result["id"] == "sess-bad-dag"
    assert result["graph"] is None


# ---------------------------------------------------------------------------
# Test 1.10 — get_session: branch + ordered messages + DAG graph together
# ---------------------------------------------------------------------------


async def test_get_session_orders_branch_messages_and_keeps_dag_graph(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-branch-dag", node_metadata=dag_metadata())

    async with StateDB(db_path) as db:
        # Progression lists msg-2 first, then msg-1 — order must follow progression
        await db.create_progression("branch-prog", ["msg-2", "msg-1"])
        await db.insert_message(
            {
                "id": "msg-1",
                "created_at": 101.0,
                "content": {"text": "first-created"},
                "sender": "user",
                "recipient": "worker",
                "role": "user",
                "node_metadata": {
                    "lion_class": "lionagi.protocols.messages.instruction.Instruction"
                },
            }
        )
        await db.insert_message(
            {
                "id": "msg-2",
                "created_at": 102.0,
                "content": {"text": "first-in-progression"},
                "sender": "worker",
                "recipient": "user",
                "role": "assistant",
                "node_metadata": {
                    "lion_class": "lionagi.protocols.messages.assistant_response.AssistantResponse"
                },
            }
        )
        await db.create_branch(
            {
                "id": "branch-1",
                "created_at": 100.5,
                "name": "worker",
                "session_id": "sess-branch-dag",
                "progression_id": "branch-prog",
                "model": "openai/gpt-5.4",
                "provider": "openai",
                "agent_name": "worker",
            }
        )

    result = await svc.get_session("sess-branch-dag")

    assert result is not None
    assert result["graph"] is not None

    branches = result["branches"]
    assert len(branches) == 1

    branch = branches[0]
    assert branch["id"] == "branch-1"
    assert branch["name"] == "worker"
    assert branch["model"] == "openai/gpt-5.4"
    assert branch["provider"] == "openai"
    assert branch["agent_name"] == "worker"

    # Message order follows progression, not creation timestamp
    msg_ids = [m["id"] for m in branch["messages"]]
    assert msg_ids == ["msg-2", "msg-1"]

    first_msg = branch["messages"][0]
    assert first_msg["content"] == {"text": "first-in-progression"}
    assert first_msg["lion_class"] == (
        "lionagi.protocols.messages.assistant_response.AssistantResponse"
    )


# ===========================================================================
# Round 2 helpers
# ===========================================================================


async def seed_branch(
    db_path: Path,
    *,
    branch_id: str,
    session_id: str,
    msg_ids: list[str] | None = None,
    name: str = "worker",
) -> str:
    """Create a progression + branch row; returns the progression id."""
    prog_id = f"{branch_id}-prog"
    async with StateDB(db_path) as db:
        if msg_ids:
            await db.create_progression(prog_id, msg_ids)
        else:
            await db.create_progression(prog_id)
        await db.create_branch(
            {
                "id": branch_id,
                "created_at": 200.0,
                "name": name,
                "session_id": session_id,
                "progression_id": prog_id,
                "model": "gpt-5",
                "provider": "openai",
                "agent_name": name,
            }
        )
    return prog_id


# ---------------------------------------------------------------------------
# Tests 3.1–3.6 — list_sessions
# ---------------------------------------------------------------------------


async def test_list_sessions_returns_empty_when_db_absent(patched_sessions_db):
    svc, db_path = patched_sessions_db
    # db_path has not been created — DEFAULT_DB_PATH.exists() is False
    result = await svc.list_sessions()
    assert result == []


async def test_list_sessions_returns_empty_for_empty_db(patched_sessions_db):
    svc, db_path = patched_sessions_db
    async with StateDB(db_path) as db:
        await db.create_progression("init-prog")  # creates file + schema, no sessions
    result = await svc.list_sessions()
    assert result == []


async def test_list_sessions_single_session_correct_fields(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_session(
        db_path, session_id="sess-fields", status="completed", started_at=10.0, ended_at=20.0
    )

    rows = await svc.list_sessions()

    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "sess-fields"
    assert row["name"] == "Test Session"
    assert row["created_at"] == 100.0
    assert row["updated_at"] == 100.0
    assert row["status"] == "completed"
    assert row["source_kind"] == "live"
    assert row["started_at"] == 10.0
    assert row["ended_at"] == 20.0
    assert row["branch_count"] == 0
    assert row["message_count"] == 0
    assert row["invocation_kind"] == "flow"


async def test_list_sessions_orders_by_updated_at_desc(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-a")
    await seed_session(db_path, session_id="sess-b")
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("UPDATE sessions SET updated_at = 200.0 WHERE id = 'sess-a'")
        await conn.execute("UPDATE sessions SET updated_at = 100.0 WHERE id = 'sess-b'")
        await conn.commit()

    rows = await svc.list_sessions()

    assert len(rows) == 2
    assert rows[0]["id"] == "sess-a"
    assert rows[1]["id"] == "sess-b"


async def test_list_sessions_null_status_and_source_kind_fall_back_to_defaults(
    patched_sessions_db,
):
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-nulls")
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "UPDATE sessions SET status = NULL, source_kind = NULL WHERE id = 'sess-nulls'"
        )
        await conn.commit()

    rows = await svc.list_sessions()

    assert len(rows) == 1
    assert rows[0]["status"] == "completed"
    assert rows[0]["source_kind"] == "live"


async def test_list_sessions_branch_and_message_counts(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-cnt")
    await seed_branch(db_path, branch_id="br-1", session_id="sess-cnt", msg_ids=["m1", "m2"])

    rows = await svc.list_sessions()

    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "sess-cnt"
    assert row["branch_count"] == 1
    assert row["message_count"] == 2


# ---------------------------------------------------------------------------
# Tests 4.1–4.5 — get_session_messages_after
# ---------------------------------------------------------------------------


async def test_get_session_messages_after_returns_empty_when_db_absent(patched_sessions_db):
    svc, db_path = patched_sessions_db
    result = await svc.get_session_messages_after("sess-x", 0.0)
    assert result == []


async def test_get_session_messages_after_filters_by_timestamp(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-ts")
    await seed_branch(db_path, branch_id="br-ts", session_id="sess-ts", msg_ids=["m-old", "m-new"])
    async with StateDB(db_path) as db:
        await db.insert_message(
            {
                "id": "m-old",
                "created_at": 50.0,
                "content": {"text": "old"},
                "sender": "user",
                "recipient": "worker",
                "role": "user",
                "node_metadata": {},
            }
        )
        await db.insert_message(
            {
                "id": "m-new",
                "created_at": 150.0,
                "content": {"text": "new"},
                "sender": "user",
                "recipient": "worker",
                "role": "assistant",
                "node_metadata": {},
            }
        )

    result = await svc.get_session_messages_after("sess-ts", 100.0)

    assert len(result) == 1
    assert result[0]["id"] == "m-new"
    assert result[0]["content"] == {"text": "new"}
    assert result[0]["branch_id"] == "br-ts"


async def test_get_session_messages_after_preserves_progression_order(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-order")
    # progression lists m-second before m-first (reverse of creation timestamp)
    await seed_branch(
        db_path, branch_id="br-order", session_id="sess-order", msg_ids=["m-second", "m-first"]
    )
    async with StateDB(db_path) as db:
        await db.insert_message(
            {
                "id": "m-first",
                "created_at": 101.0,
                "content": {"text": "first by time"},
                "sender": "user",
                "recipient": "worker",
                "role": "user",
                "node_metadata": {},
            }
        )
        await db.insert_message(
            {
                "id": "m-second",
                "created_at": 102.0,
                "content": {"text": "second by time"},
                "sender": "assistant",
                "recipient": "worker",
                "role": "assistant",
                "node_metadata": {},
            }
        )

    result = await svc.get_session_messages_after("sess-order", 0.0)

    assert len(result) == 2
    assert result[0]["id"] == "m-second"
    assert result[1]["id"] == "m-first"


async def test_get_session_messages_after_aggregates_across_branches(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-multi")
    await seed_branch(
        db_path, branch_id="br-alpha", session_id="sess-multi", msg_ids=["ma-1"], name="alpha"
    )
    await seed_branch(
        db_path, branch_id="br-beta", session_id="sess-multi", msg_ids=["mb-1"], name="beta"
    )
    async with StateDB(db_path) as db:
        await db.insert_message(
            {
                "id": "ma-1",
                "created_at": 200.0,
                "content": {"text": "from alpha"},
                "sender": "alpha",
                "recipient": "system",
                "role": "assistant",
                "node_metadata": {},
            }
        )
        await db.insert_message(
            {
                "id": "mb-1",
                "created_at": 201.0,
                "content": {"text": "from beta"},
                "sender": "beta",
                "recipient": "system",
                "role": "assistant",
                "node_metadata": {},
            }
        )

    result = await svc.get_session_messages_after("sess-multi", 0.0)

    assert len(result) == 2
    by_branch = {m["branch_id"]: m for m in result}
    assert "br-alpha" in by_branch
    assert "br-beta" in by_branch
    assert by_branch["br-alpha"]["id"] == "ma-1"
    assert by_branch["br-beta"]["id"] == "mb-1"


async def test_get_session_messages_after_empty_progression_is_skipped(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-emptyprog")
    # Branch has a progression but with no message IDs (empty collection)
    await seed_branch(db_path, branch_id="br-empty", session_id="sess-emptyprog", msg_ids=[])

    result = await svc.get_session_messages_after("sess-emptyprog", 0.0)
    assert result == []


# ---------------------------------------------------------------------------
# Tests 5.1–5.3 — session_exists
# ---------------------------------------------------------------------------


async def test_session_exists_returns_true_for_existing_session(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-real")

    assert await svc.session_exists("sess-real") is True


async def test_session_exists_returns_false_for_missing_session(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-real")

    assert await svc.session_exists("nonexistent-id") is False


async def test_session_exists_returns_false_when_db_file_absent(patched_sessions_db):
    svc, db_path = patched_sessions_db
    # Do not create the DB file

    assert await svc.session_exists("any-id") is False


# ---------------------------------------------------------------------------
# Edge: get_session_messages_after with negative timestamp returns all messages
# ---------------------------------------------------------------------------


async def test_get_session_messages_after_negative_timestamp(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-neg")
    await seed_branch(db_path, branch_id="br-neg", session_id="sess-neg", msg_ids=["m-neg-1"])
    async with StateDB(db_path) as db:
        await db.insert_message(
            {
                "id": "m-neg-1",
                "created_at": 10.0,
                "content": {"text": "msg"},
                "sender": "user",
                "recipient": "worker",
                "role": "user",
                "node_metadata": {},
            }
        )

    result = await svc.get_session_messages_after("sess-neg", -1.0)
    assert len(result) == 1
    assert result[0]["id"] == "m-neg-1"


async def test_get_session_messages_after_zero_timestamp(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-zero")
    await seed_branch(db_path, branch_id="br-zero", session_id="sess-zero", msg_ids=["m-zero-1"])
    async with StateDB(db_path) as db:
        await db.insert_message(
            {
                "id": "m-zero-1",
                "created_at": 1.0,
                "content": {"text": "msg"},
                "sender": "user",
                "recipient": "worker",
                "role": "user",
                "node_metadata": {},
            }
        )

    result = await svc.get_session_messages_after("sess-zero", 0.0)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Edge: concurrent list_sessions and save_session (WAL-mode writer/reader)
# ---------------------------------------------------------------------------


async def test_concurrent_list_and_seed_sessions(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-concurrent-1")

    async def _seed_more():
        for i in range(3):
            await seed_session(db_path, session_id=f"sess-concurrent-extra-{i}")

    results, _ = await asyncio.gather(svc.list_sessions(), _seed_more())
    assert isinstance(results, list)
    assert len(results) >= 1


# ---------------------------------------------------------------------------
# Edge: get_session with very large node_metadata JSON
# ---------------------------------------------------------------------------


async def test_get_session_large_node_metadata_parses_correctly(patched_sessions_db):
    svc, db_path = patched_sessions_db
    large_meta = {
        "agents": [{"id": f"agent-{i}", "name": f"Agent {i}", "model": "gpt-4"} for i in range(50)],
        "operations": [
            {"id": f"op-{i}", "agent_id": f"agent-{i % 50}", "depends_on": []} for i in range(100)
        ],
    }
    await seed_session(db_path, session_id="sess-large-meta", node_metadata=large_meta)

    result = await svc.get_session("sess-large-meta")
    assert result is not None
    assert result["graph"] is not None
    assert len(result["graph"]["nodes"]) == 100
