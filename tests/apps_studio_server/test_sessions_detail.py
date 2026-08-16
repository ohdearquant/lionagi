# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for _graph_from_metadata() and get_session() DAG graph paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import lionagi.state.db as state_db_mod

aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")

from lionagi.state.claude_mirror import session_db_id  # noqa: E402
from lionagi.state.db import SESSION_TERMINAL_STATUSES, StateDB  # noqa: E402

# Shared test data


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


# Fixtures and helpers


@pytest.fixture
def patched_sessions_db(tmp_path, monkeypatch):
    import lionagi.studio.services.sessions as svc

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    return svc, db_path


async def seed_session(
    db_path: Path,
    *,
    session_id: str = "sess-1",
    node_metadata=None,
    status: str = "running",
    started_at=None,
    ended_at=None,
    artifacts_path: str | None = None,
    artifact_contract_json: dict | None = None,
    artifact_verification_json: dict | None = None,
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
                "artifacts_path": artifacts_path,
                "artifact_contract_json": artifact_contract_json,
                "artifact_verification_json": artifact_verification_json,
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


# Test 1.1 — falsy / unparseable inputs return None


def test_graph_from_metadata_none_empty_and_invalid_json_return_none():
    from lionagi.studio.services.sessions import _graph_from_metadata

    assert _graph_from_metadata(None) is None
    assert _graph_from_metadata("") is None
    assert _graph_from_metadata("{not-json") is None


# Test 1.2 — non-dict root and empty operations list return None


def test_graph_from_metadata_rejects_non_dict_and_missing_operations():
    from lionagi.studio.services.sessions import _graph_from_metadata

    assert _graph_from_metadata(json.dumps(["not", "a", "dict"])) is None
    assert _graph_from_metadata(json.dumps({"agents": [{"id": "a1", "name": "Analyst"}]})) is None
    assert _graph_from_metadata(json.dumps({"agents": [], "operations": []})) is None


# Test 1.3 — valid DAG: correct node fields and dependency edge


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


# Test 1.4 — malformed agents/operations entries are silently filtered


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


# Test 1.5 — unknown agent_id yields blank role and assignment


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


# Test 1.6 — string depends_on must not produce character-level edges


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


# Test 1.7 — get_session: valid DAG metadata → full graph in response


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


# Test 1.8 — get_session: null metadata → graph is None, duration is None


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


# Artifact verification display state


ARTIFACT_CONTRACT = {"expected": [{"id": "report", "path": "REPORT.md", "required": True}]}


async def test_get_session_returns_live_provisional_artifact_progress(
    patched_sessions_db, tmp_path
):
    svc, db_path = patched_sessions_db
    (tmp_path / "REPORT.md").write_text("ready")
    await seed_session(
        db_path,
        session_id="sess-live-artifacts",
        status="running",
        artifacts_path=str(tmp_path),
        artifact_contract_json=ARTIFACT_CONTRACT,
    )

    result = await svc.get_session("sess-live-artifacts")

    assert result is not None
    verification = result["artifact_verification_json"]
    assert verification["provisional"] is True
    assert [item["id"] for item in verification["produced"]] == ["report"]


@pytest.mark.parametrize("status", sorted(SESSION_TERMINAL_STATUSES))
async def test_get_session_reports_terminal_verdict_was_not_recorded_without_artifact_path(
    patched_sessions_db, status
):
    svc, db_path = patched_sessions_db
    await seed_session(
        db_path,
        session_id=f"sess-{status}",
        status=status,
        artifacts_path=None,
        artifact_contract_json=ARTIFACT_CONTRACT,
        artifact_verification_json=None,
    )

    result = await svc.get_session(f"sess-{status}")

    assert result is not None
    assert result["artifact_verification_json"] == {"status": "not_recorded"}


async def test_get_session_does_not_synthesize_a_terminal_verdict_from_disk(
    patched_sessions_db, tmp_path
):
    svc, db_path = patched_sessions_db
    (tmp_path / "REPORT.md").write_text("ready")
    await seed_session(
        db_path,
        session_id="sess-terminal-artifacts",
        status="completed",
        artifacts_path=str(tmp_path),
        artifact_contract_json=ARTIFACT_CONTRACT,
        artifact_verification_json=None,
    )

    result = await svc.get_session("sess-terminal-artifacts")

    assert result is not None
    assert result["artifact_verification_json"] == {"status": "not_recorded"}


async def test_get_session_keeps_live_null_verification_pending_without_artifact_path(
    patched_sessions_db,
):
    svc, db_path = patched_sessions_db
    await seed_session(
        db_path,
        session_id="sess-live-no-root",
        status="running",
        artifacts_path=None,
        artifact_contract_json=ARTIFACT_CONTRACT,
        artifact_verification_json=None,
    )

    result = await svc.get_session("sess-live-no-root")

    assert result is not None
    assert result["artifact_verification_json"] is None


async def test_get_session_preserves_a_stored_terminal_verdict(patched_sessions_db):
    svc, db_path = patched_sessions_db
    verdict = {
        "status": "passed",
        "checked_at": 42.0,
        "missing_required": [],
        "missing_optional": [],
        "produced": [{"id": "report", "path": "REPORT.md", "size": 5, "present": True}],
    }
    await seed_session(
        db_path,
        session_id="sess-recorded-verdict",
        status="completed",
        artifact_contract_json=ARTIFACT_CONTRACT,
        artifact_verification_json=verdict,
    )

    result = await svc.get_session("sess-recorded-verdict")

    assert result is not None
    resolved = result["artifact_verification_json"]
    assert {k: v for k, v in resolved.items() if k != "staleness_check"} == verdict
    # No artifacts_path was seeded, so staleness cannot be checked against disk.
    assert resolved["staleness_check"] == "unknown"


async def test_get_session_keeps_verification_null_when_no_contract_exists(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_session(
        db_path,
        session_id="sess-no-contract",
        status="completed",
        artifact_contract_json=None,
        artifact_verification_json=None,
    )

    result = await svc.get_session("sess-no-contract")

    assert result is not None
    assert result["artifact_verification_json"] is None


# Test 1.8a — get_session_by_cc_id: legacy rows fall back to deterministic id


async def test_get_session_by_cc_id_falls_back_for_legacy_row(patched_sessions_db):
    svc, db_path = patched_sessions_db
    cc_uid = "11111111-2222-3333-4444-555555555555"
    legacy_session_id = session_db_id(cc_uid)
    await seed_session(db_path, session_id=legacy_session_id)

    result = await svc.get_session_by_cc_id(cc_uid)

    assert result is not None
    assert result["id"] == legacy_session_id
    assert result["name"] == "Test Session"


# Test 1.9 — get_session: corrupt raw metadata → graph is None, no exception


async def test_get_session_returns_none_graph_for_raw_invalid_node_metadata(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-bad-dag", node_metadata=None)
    await overwrite_session_node_metadata(db_path, "sess-bad-dag", "{bad-json")

    result = await svc.get_session("sess-bad-dag")

    assert result is not None
    assert result["id"] == "sess-bad-dag"
    assert result["graph"] is None


# Test 1.10 — get_session: branch + ordered messages + DAG graph together


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


# Tests 3.1–3.6 — list_sessions


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


async def test_list_sessions_surfaces_status_reason(patched_sessions_db):
    """ADR-0057: list_sessions must carry the reason fields the detail path does."""
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-failed", status="running")
    from lionagi.state.db import StateDB
    from lionagi.state.reasons import RunReasons

    async with StateDB(db_path) as db:
        await db.update_status(
            "session",
            "sess-failed",
            new_status="failed",
            reason_code=RunReasons.FAILED_EXIT_NONZERO,
            reason_summary="worker exited with code 1",
        )

    rows = await svc.list_sessions()

    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "failed"
    assert row["status_reason_code"] == RunReasons.FAILED_EXIT_NONZERO
    assert row["status_reason_summary"] == "worker exited with code 1"


async def test_list_sessions_agrees_with_the_detail_route_on_terminal_absence(
    patched_sessions_db,
):
    """The same session must not report absence one way and null the other."""
    svc, db_path = patched_sessions_db
    await seed_session(
        db_path,
        session_id="sess-terminal-absent",
        status="completed",
        artifacts_path=None,
        artifact_contract_json=ARTIFACT_CONTRACT,
        artifact_verification_json=None,
    )

    rows = await svc.list_sessions()
    detail = await svc.get_session("sess-terminal-absent")

    assert len(rows) == 1
    assert rows[0]["artifact_verification_json"] == {"status": "not_recorded"}
    # Asserted as equality between the two routes rather than against the literal
    # twice: the defect being closed is a disagreement, so the test fails if
    # either side moves, not only if the list side does.
    assert detail is not None
    assert rows[0]["artifact_verification_json"] == detail["artifact_verification_json"]


async def test_list_sessions_preserves_a_stored_verdict(patched_sessions_db):
    """A recorded verdict is returned as recorded, never re-derived."""
    svc, db_path = patched_sessions_db
    stored = {"status": "verified", "produced": [{"id": "report"}]}
    await seed_session(
        db_path,
        session_id="sess-stored",
        status="completed",
        artifacts_path=None,
        artifact_contract_json=ARTIFACT_CONTRACT,
        artifact_verification_json=stored,
    )

    rows = await svc.list_sessions()

    resolved = rows[0]["artifact_verification_json"]
    assert {k: v for k, v in resolved.items() if k != "staleness_check"} == stored
    # `stored` has no checked_at/produced, so staleness cannot be derived.
    assert resolved["staleness_check"] == "unknown"


async def test_list_sessions_does_not_read_the_artifacts_directory(patched_sessions_db, tmp_path):
    """The list route declines the live-progress read, and that is deliberate.

    The session is running, holds a contract, names a real artifacts directory,
    and that directory contains the file the contract requires -- everything the
    provisional arm needs to report progress. The list route still returns None,
    because computing it means a filesystem walk per row on a paginated read.
    Progress belongs to the single-session view, which this same fixture shape is
    covered for elsewhere.

    This is the test that fails if someone closes the remaining difference by
    handing the list route its artifacts_path, so the decision has to be made
    again rather than drifted into.
    """
    svc, db_path = patched_sessions_db
    (tmp_path / "REPORT.md").write_text("ready")
    await seed_session(
        db_path,
        session_id="sess-running-on-disk",
        status="running",
        artifacts_path=str(tmp_path),
        artifact_contract_json=ARTIFACT_CONTRACT,
        artifact_verification_json=None,
    )

    rows = await svc.list_sessions()

    assert rows[0]["artifact_verification_json"] is None
    # The detail route, given the same row, does report the progress -- which is
    # what makes the None above a scoping decision rather than a lost capability.
    detail = await svc.get_session("sess-running-on-disk")
    assert detail is not None
    assert detail["artifact_verification_json"]["provisional"] is True


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


# Tests 4.1–4.5 — get_session_messages_after


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


async def test_get_session_messages_after_orders_by_created_at(patched_sessions_db):
    """get_session_messages_after is a cursor-driven SSE tail read — it orders by
    created_at (not raw progression order) so after_ts can advance monotonically
    even when a branch's progression collection is not itself chronological."""
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
    assert result[0]["id"] == "m-first"
    assert result[1]["id"] == "m-second"


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


async def test_get_session_messages_after_handles_branch_over_sqlite_variable_limit(
    patched_sessions_db,
):
    """Regression: a branch whose progression collection holds more message ids than
    SQLite's bound-variable limit used to blow up get_session_messages_after with
    sqlite3.OperationalError("too many SQL variables") on every 0.5s SSE poll, killing
    the stream for any long-lived session (the classic SQLite default is 999; this
    build's default, per PRAGMA compile_options MAX_VARIABLE_NUMBER, is 32766 — 33000
    exceeds both so the test reproduces the failure regardless of build). The
    json_each-joined query has no per-message bind variable, so it must return every
    id without error. Only the progression collection needs to be this large — the
    corresponding message rows are irrelevant to the bind-limit failure itself, so
    this seeds ids without materializing 33000 message rows (keeps the test fast)."""
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-huge")
    count = 33000
    msg_ids = [f"huge-{i}" for i in range(count)]
    await seed_branch(db_path, branch_id="br-huge", session_id="sess-huge", msg_ids=msg_ids)
    # A handful of real message rows (including one outside the msg_ids progression,
    # and one before after_ts) prove the join+filter still behave correctly at scale.
    async with StateDB(db_path) as db:
        await db.insert_message(
            {
                "id": "huge-0",
                "created_at": 50.0,
                "content": {"text": "too old"},
                "sender": "worker",
                "recipient": "user",
                "role": "assistant",
                "node_metadata": {},
            }
        )
        await db.insert_message(
            {
                "id": "huge-1",
                "created_at": 150.0,
                "content": {"text": "in range"},
                "sender": "worker",
                "recipient": "user",
                "role": "assistant",
                "node_metadata": {},
            }
        )

    result = await svc.get_session_messages_after("sess-huge", 100.0)

    assert result == [
        {
            "id": "huge-1",
            "role": "assistant",
            "content": {"text": "in range"},
            "content_withheld": False,
            "sender": "worker",
            "timestamp": 150.0,
            "lion_class": "__unknown__",
            "branch_id": "br-huge",
        }
    ]


async def test_get_session_messages_after_message_shape_matches_expected_fields(
    patched_sessions_db,
):
    """Message shape parity: id/created_at/content/sender/role/lion_class/branch_id
    must be present and match the pre-fix _format_message() output exactly.

    `content_withheld` joins them: this endpoint returns no per-session bounds
    alongside the rows, so it is the only place a caller can learn that a payload
    was refused rather than absent."""
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-shape")
    await seed_branch(db_path, branch_id="br-shape", session_id="sess-shape", msg_ids=["shape-1"])
    async with StateDB(db_path) as db:
        await db.insert_message(
            {
                "id": "shape-1",
                "created_at": 111.0,
                "content": {"text": "hello shape"},
                "sender": "worker",
                "recipient": "user",
                "role": "assistant",
                "node_metadata": {
                    "lion_class": "lionagi.protocols.messages.assistant_response.AssistantResponse"
                },
            }
        )

    result = await svc.get_session_messages_after("sess-shape", 0.0)

    assert result == [
        {
            "id": "shape-1",
            "role": "assistant",
            "content": {"text": "hello shape"},
            "content_withheld": False,
            "sender": "worker",
            "timestamp": 111.0,
            "lion_class": "lionagi.protocols.messages.assistant_response.AssistantResponse",
            "branch_id": "br-shape",
        }
    ]


# Tests 5.1–5.3 — session_exists


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


# Message pagination — detail responses window from the progression tail


async def seed_paginated_session(db_path: Path, *, count: int = 10) -> list[str]:
    """Session with one branch holding `count` messages; returns message ids in order."""
    await seed_session(db_path, session_id="sess-paged")
    msg_ids = [f"pmsg-{i}" for i in range(count)]
    await seed_branch(db_path, branch_id="br-paged", session_id="sess-paged", msg_ids=msg_ids)
    async with StateDB(db_path) as db:
        for i, mid in enumerate(msg_ids):
            await db.insert_message(
                {
                    "id": mid,
                    "created_at": 100.0 + i,
                    "content": {"text": f"m{i}"},
                    "sender": "worker",
                    "recipient": "user",
                    "role": "assistant",
                    "node_metadata": {},
                }
            )
    return msg_ids


async def test_get_session_windows_newest_messages_by_default(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_paginated_session(db_path, count=10)

    result = await svc.get_session("sess-paged", message_limit=3)

    branch = result["branches"][0]
    assert [m["id"] for m in branch["messages"]] == ["pmsg-7", "pmsg-8", "pmsg-9"]
    assert branch["message_total"] == 10
    assert branch["message_offset"] == 0


async def test_get_session_branch_bounds_cover_full_progression_when_messages_are_windowed(
    patched_sessions_db,
):
    svc, db_path = patched_sessions_db
    await seed_paginated_session(db_path, count=10)

    result = await svc.get_session("sess-paged", message_limit=3)

    branch = result["branches"][0]
    assert [m["timestamp"] for m in branch["messages"]] == [107.0, 108.0, 109.0]
    assert branch["first_message_at"] == 100.0
    assert branch["last_message_at"] == 109.0


async def test_get_session_offset_pages_older_history(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_paginated_session(db_path, count=10)

    result = await svc.get_session("sess-paged", message_limit=3, message_offset=3)

    branch = result["branches"][0]
    assert [m["id"] for m in branch["messages"]] == ["pmsg-4", "pmsg-5", "pmsg-6"]
    assert branch["message_offset"] == 3


async def test_get_session_offset_clamps_at_oldest_message(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_paginated_session(db_path, count=10)

    result = await svc.get_session("sess-paged", message_limit=3, message_offset=9)

    branch = result["branches"][0]
    assert [m["id"] for m in branch["messages"]] == ["pmsg-0"]


async def test_get_session_offset_past_total_returns_empty_page(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_paginated_session(db_path, count=10)

    result = await svc.get_session("sess-paged", message_limit=3, message_offset=50)

    branch = result["branches"][0]
    assert branch["messages"] == []
    assert branch["message_total"] == 10


async def test_get_session_limit_clamped_to_max(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_paginated_session(db_path, count=5)

    result = await svc.get_session("sess-paged", message_limit=10_000)

    branch = result["branches"][0]
    assert len(branch["messages"]) == 5
    assert branch["message_total"] == 5


# message_cursor — stable pagination under concurrent progression appends


async def test_get_session_cursor_pages_are_stable_under_concurrent_appends(patched_sessions_db):
    svc, db_path = patched_sessions_db
    msg_ids = await seed_paginated_session(db_path, count=10)

    page1 = await svc.get_session("sess-paged", message_limit=3)
    branch1 = page1["branches"][0]
    assert [m["id"] for m in branch1["messages"]] == ["pmsg-7", "pmsg-8", "pmsg-9"]
    assert branch1["messages_truncated"] is True
    cursor = page1["message_next_cursor"]
    assert cursor

    # Concurrent writer appends two more messages to the live tail while the
    # cursor from page 1 is still in flight.
    new_ids = ["pmsg-10", "pmsg-11"]
    async with StateDB(db_path) as db:
        for i, mid in enumerate(new_ids, start=len(msg_ids)):
            await db.insert_message(
                {
                    "id": mid,
                    "created_at": 100.0 + i,
                    "content": {"text": f"m{i}"},
                    "sender": "worker",
                    "recipient": "user",
                    "role": "assistant",
                    "node_metadata": {},
                }
            )
            await db.append_to_progression("br-paged-prog", mid)

    page2 = await svc.get_session("sess-paged", message_limit=3, message_cursor=cursor)
    branch2 = page2["branches"][0]
    assert [m["id"] for m in branch2["messages"]] == ["pmsg-4", "pmsg-5", "pmsg-6"]

    ids1 = {m["id"] for m in branch1["messages"]}
    ids2 = {m["id"] for m in branch2["messages"]}
    assert ids1.isdisjoint(ids2), "cursor page must not duplicate rows from the tail page"
    combined = ids1 | ids2
    assert combined == {f"pmsg-{i}" for i in range(4, 10)}, (
        "combined two-page slice must not skip any expected id"
    )


async def test_get_session_rejects_invalid_message_cursor(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_paginated_session(db_path, count=10)

    with pytest.raises(svc.MessageCursorError):
        await svc.get_session("sess-paged", message_limit=3, message_cursor="not-a-valid-cursor")


async def test_get_session_full_aggregates_do_not_hydrate_every_message_row(
    patched_sessions_db, monkeypatch
):
    """Regression: computing full-session aggregates must not force-hydrate the entire
    progression on every detail read — only the display window is fetched in full."""
    svc, db_path = patched_sessions_db
    await seed_paginated_session(db_path, count=50)

    calls: list[list[str]] = []
    original = svc._fetch_messages_by_ids

    async def spy(db, ids):
        calls.append(list(ids))
        return await original(db, ids)

    monkeypatch.setattr(svc, "_fetch_messages_by_ids", spy)

    result = await svc.get_session("sess-paged", message_limit=3)

    assert len(calls) == 1
    assert calls[0] == ["pmsg-47", "pmsg-48", "pmsg-49"]
    assert result["message_stats"]["message_count"] == 50


async def test_get_session_rejects_cursor_from_a_different_session(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_paginated_session(db_path, count=10)
    await seed_session(db_path, session_id="sess-other")
    await seed_branch(
        db_path, branch_id="br-other", session_id="sess-other", msg_ids=["om-0", "om-1"]
    )
    async with StateDB(db_path) as db:
        for i, mid in enumerate(["om-0", "om-1"]):
            await db.insert_message(
                {
                    "id": mid,
                    "created_at": 50.0 + i,
                    "content": {"text": f"m{i}"},
                    "sender": "worker",
                    "recipient": "user",
                    "role": "user",
                    "node_metadata": {},
                }
            )

    other_page = await svc.get_session("sess-other", message_limit=1)
    foreign_cursor = other_page["message_next_cursor"]
    assert foreign_cursor

    with pytest.raises(svc.MessageCursorError):
        await svc.get_session("sess-paged", message_limit=1, message_cursor=foreign_cursor)


# Action-stat aggregation must match the canonical persisted lion_class values


async def test_get_session_action_stats_match_canonical_fully_qualified_lion_class(
    patched_sessions_db,
):
    """The runtime persists lion_class as the fully-qualified dotted path (see the
    message_types seed rows in state/schema.sql), not the bare class name. Tool/error/
    file aggregation must recognize that shape, not just a legacy short name.
    The absolute path has no artifact root proving it is safe to disclose, so
    it is counted as redacted instead of copied into the response."""
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-canonical", status="completed")
    msg_ids = ["req-0", "resp-0"]
    await seed_branch(
        db_path, branch_id="br-canonical", session_id="sess-canonical", msg_ids=msg_ids
    )
    async with StateDB(db_path) as db:
        await db.insert_message(
            {
                "id": "req-0",
                "created_at": 100.0,
                "content": {
                    "function": "Write",
                    "arguments": {"file_path": "/tmp/canonical.txt"},
                    "action_response_id": "resp-0",
                },
                "sender": "worker",
                "recipient": "user",
                "role": "action",
                "node_metadata": {
                    "lion_class": "lionagi.protocols.messages.action_request.ActionRequest"
                },
            }
        )
        await db.insert_message(
            {
                "id": "resp-0",
                "created_at": 101.0,
                "content": {"function": "Write", "output": "process exited with code 1."},
                "sender": "worker",
                "recipient": "user",
                "role": "action",
                "node_metadata": {
                    "lion_class": "lionagi.protocols.messages.action_response.ActionResponse"
                },
            }
        )

    result = await svc.get_session("sess-canonical")

    stats = result["message_stats"]
    assert stats["tool_call_count"] == 1
    assert stats["error_count"] == 1
    assert stats["files"] == []
    assert result["run_files"]["redacted_count"] == 1


def test_per_branch_stats_do_not_carry_a_second_file_surface():
    """Branch stats report counts. The file surface has exactly one producer.

    These stats used to aggregate their own set of file paths, straight out of
    raw tool arguments, which meant two independent answers to "what did this
    touch" with different path policies: this one disclosed absolute host paths
    with no containment check at all. The session-level run_files summary is
    the one that decides, so there is nothing to report here.
    """
    from lionagi.studio.services.sessions import _branch_message_stats

    action_messages = [
        {
            "id": "read",
            "lion_class": "ActionRequest",
            "content": {"function": "Read", "arguments": {"file_path": "/repo/src/main.py"}},
        },
        {
            "id": "edit",
            "lion_class": "ActionRequest",
            "content": {"function": "Edit", "arguments": {"path": "/repo/Makefile"}},
        },
    ]

    stats = _branch_message_stats(2, {"action": 2}, action_messages)

    assert stats["tool_call_count"] == 2
    assert "files" not in stats
    assert not [key for key, value in stats.items() if value == "/repo/Makefile"]


async def test_get_session_message_count_is_db_aggregate_not_progression_length(
    patched_sessions_db,
):
    """A progression can reference an id whose message row was never persisted (or was
    pruned). message_count must reflect the DB role aggregate, not len(progression)."""
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-stale-prog", status="completed")
    # Two ids in the progression, only one has a persisted message row.
    await seed_branch(
        db_path,
        branch_id="br-stale-prog",
        session_id="sess-stale-prog",
        msg_ids=["m0", "m1-never-persisted"],
    )
    async with StateDB(db_path) as db:
        await db.insert_message(
            {
                "id": "m0",
                "created_at": 100.0,
                "content": {"text": "hello"},
                "sender": "worker",
                "recipient": "user",
                "role": "assistant",
                "node_metadata": {},
            }
        )

    result = await svc.get_session("sess-stale-prog")

    branch = result["branches"][0]
    assert branch["message_total"] == 2  # progression length, kept as a separate field
    assert result["message_stats"]["message_count"] == 1  # DB aggregate, not progression length
    assert branch["message_stats"]["message_count"] == 1


# An approximate end must not be turned back into a measured duration


async def test_get_session_does_not_reconstruct_a_duration_from_an_approximate_end(
    patched_sessions_db,
):
    """Nulling the stored duration is not enough on its own.

    The flag makes the read discard duration_ms, and the very next branch
    recomputes one from ended_at minus started_at. The row then reports a
    measured length derived from a timestamp explicitly marked as a guess,
    which is what the flag exists to prevent.
    """
    import sqlite3

    svc, db_path = patched_sessions_db
    await seed_session(
        db_path,
        session_id="sess-approx",
        status="completed",
        started_at=10.0,
        ended_at=13.5,
    )
    await seed_session(
        db_path,
        session_id="sess-measured",
        status="completed",
        started_at=10.0,
        ended_at=13.5,
    )
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE sessions SET ended_at_is_approximate = 1, duration_ms = NULL WHERE id = ?",
            ("sess-approx",),
        )
        conn.execute(
            "UPDATE sessions SET ended_at_is_approximate = 0, duration_ms = NULL WHERE id = ?",
            ("sess-measured",),
        )
        conn.commit()
    finally:
        conn.close()

    approximate = await svc.get_session("sess-approx")
    measured = await svc.get_session("sess-measured")

    assert approximate is not None
    assert approximate["duration_ms"] is None
    # Control: the same shape with a measured end still reconstructs, so the
    # assertion above is about the flag and not about a reconstruction that
    # stopped working.
    assert measured is not None
    assert measured["duration_ms"] == 3500.0


async def _drop_column(db_path: Path, table: str, column: str) -> None:
    """Reshape a store to the schema version that predates a column."""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        conn.commit()
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        assert column not in present, "the column survived the drop"
    finally:
        conn.close()


async def test_session_reads_work_against_a_store_from_the_previous_schema_version(
    patched_sessions_db,
):
    """Reads must not require a column that this schema version introduced.

    The daemon reads stores through its own connection and never migrates
    them, so a store last written by the previous version keeps that version's
    columns for as long as nothing opens it for writing. That is the state of
    every store immediately after an upgrade, and of any store the daemon can
    only read. Selecting the new column by name makes those reads fail with a
    missing-column error rather than degrade.
    """
    svc, db_path = patched_sessions_db
    await seed_session(
        db_path,
        session_id="sess-prev-schema",
        status="completed",
        started_at=10.0,
        ended_at=13.5,
    )

    # Control: both reads work while the column is present, so a failure after
    # the drop is about the column and not about the fixture.
    assert await svc.get_session("sess-prev-schema") is not None
    assert [row["id"] for row in await svc.list_sessions(limit=10)] == ["sess-prev-schema"]

    await _drop_column(db_path, "sessions", "ended_at_is_approximate")

    detail = await svc.get_session("sess-prev-schema")
    assert detail is not None
    # A store that never had the column recorded no approximate ends, which is
    # what the previous version reported for every row.
    assert detail["ended_at_is_approximate"] is False

    listed = await svc.list_sessions(limit=10)
    assert [row["id"] for row in listed] == ["sess-prev-schema"]
    assert listed[0]["ended_at_is_approximate"] is False


async def _seed_action_requests(
    db_path: Path, *, branch_id: str, session_id: str, count: int, start: int = 0
) -> None:
    """One ActionRequest per file, in progression order, oldest first."""
    ids = [f"{branch_id}-act-{i}" for i in range(start, start + count)]
    await seed_branch(db_path, branch_id=branch_id, session_id=session_id, msg_ids=ids)
    async with StateDB(db_path) as db:
        for i, msg_id in enumerate(ids, start=start):
            await db.insert_message(
                {
                    "id": msg_id,
                    "created_at": 100.0 + i,
                    "content": {"function": "Read", "arguments": {"file_path": f"/run/f{i}.py"}},
                    "sender": "worker",
                    "recipient": "tool",
                    "role": "action",
                    "node_metadata": {
                        "lion_class": "lionagi.protocols.messages.action_request.ActionRequest"
                    },
                }
            )


async def test_action_hydration_stops_at_its_bound_and_keeps_the_newest(
    patched_sessions_db, monkeypatch
):
    """A session accumulates action rows for as long as it runs, so the detail
    read has to stop somewhere. It stops at the newest end, because that is the
    part every field derived from these rows is describing, and it says that it
    stopped rather than reporting a short list as a complete one."""
    svc, db_path = patched_sessions_db
    monkeypatch.setattr(svc, "MAX_HYDRATED_ACTION_MESSAGES", 3)
    await seed_session(db_path, session_id="sess-hydration", artifacts_path="/run")
    await _seed_action_requests(db_path, branch_id="b1", session_id="sess-hydration", count=8)

    detail = await svc.get_session("sess-hydration")

    assert detail is not None
    stats = detail["message_stats"]
    assert stats["bounded"] is True
    assert stats["tool_call_count"] == 3
    assert detail["run_files"]["bounded"] is True
    assert detail["run_files"]["truncated"] is True
    assert {item["path"] for item in detail["run_files"]["items"]} == {
        "f5.py",
        "f6.py",
        "f7.py",
    }


async def test_an_unbounded_session_reports_the_whole_action_surface(patched_sessions_db):
    """Control: the flags above have to be able to read false, or a caller
    cannot tell a bounded read from a complete one."""
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-hydration-small", artifacts_path="/run")
    await _seed_action_requests(db_path, branch_id="b1", session_id="sess-hydration-small", count=3)

    detail = await svc.get_session("sess-hydration-small")

    assert detail is not None
    assert detail["message_stats"]["bounded"] is False
    assert detail["message_stats"]["tool_call_count"] == 3
    assert detail["run_files"]["bounded"] is False


async def test_the_hydration_budget_is_spent_on_the_newest_branch(patched_sessions_db, monkeypatch):
    """The budget covers the session, not each branch, so where it is spent is
    a real choice. Spending it in branch order would hand back the oldest
    branch's files under a heading about this run."""
    svc, db_path = patched_sessions_db
    monkeypatch.setattr(svc, "MAX_HYDRATED_ACTION_MESSAGES", 2)
    await seed_session(db_path, session_id="sess-two-branches", artifacts_path="/run")
    await _seed_action_requests(db_path, branch_id="older", session_id="sess-two-branches", count=3)
    await _seed_action_requests(
        db_path, branch_id="newer", session_id="sess-two-branches", count=3, start=10
    )

    detail = await svc.get_session("sess-two-branches")

    assert detail is not None
    assert detail["message_stats"]["bounded"] is True
    # Both branches together hold six requests and the budget is two, so the
    # two that survive must both come from the branch created last.
    assert {item["path"] for item in detail["run_files"]["items"]} == {"f11.py", "f12.py"}


@pytest.fixture
def sessions_svc():
    import lionagi.studio.services.sessions as svc

    return svc


class _CountingChanges(list):
    """A real list (so the isinstance check still passes) that records how far
    something iterated it. Nothing else can tell whether the walk stopped early
    or read the whole payload and threw the rest away."""

    def __init__(self, items):
        super().__init__(items)
        self.yielded = 0

    def __iter__(self):
        for item in super().__iter__():
            self.yielded += 1
            yield item


def _multiedit_message(changes, *, sequence=0):
    return {
        "lion_class": "lionagi.protocols.messages.action_request.ActionRequest",
        "timestamp": float(sequence),
        "content": {"function": "multi_edit", "arguments": {"changes": changes}},
    }


def test_one_oversized_change_list_cannot_outrun_the_scan_budget(sessions_svc, monkeypatch):
    """The request budget counts rows, and a row is not a bounded amount of
    work: one call carries as many structured changes as its caller wrote. The
    file-preview path reaches this with a single message, where a bound on the
    number of messages is not in play at all."""
    monkeypatch.setattr(sessions_svc, "MAX_RUN_FILE_SCANNED_PATHS", 3)
    changes = [{"file_path": f"f{i}.py"} for i in range(50)]

    summary = sessions_svc._derive_run_files([_multiedit_message(changes)], artifact_root=None)

    assert summary["bounded"] is True
    assert summary["truncated"] is True
    # Three resolves bought at most three paths; the other 47 were never spent.
    assert summary["total"] <= 3


def test_the_change_walk_stops_at_the_budget_instead_of_materializing_the_list(
    sessions_svc, monkeypatch
):
    """Bounding what is kept is not the same as bounding what is read. The cost
    this finding is about is the resolve per entry, which is paid during the
    walk, so the walk itself has to stop."""
    monkeypatch.setattr(sessions_svc, "MAX_RUN_FILE_SCANNED_PATHS", 4)
    changes = _CountingChanges({"file_path": f"f{i}.py"} for i in range(1000))

    sessions_svc._derive_run_files([_multiedit_message(changes)], artifact_root=None)

    # The generator is abandoned once the budget is spent, so iteration stops a
    # bounded distance past it rather than running to the end of the payload.
    assert changes.yielded <= 5
    assert changes.yielded < 1000


def test_a_change_list_inside_the_budget_is_reported_whole(sessions_svc, monkeypatch):
    """Control: both flags above must be able to read false and every path must
    survive, or the test cannot tell a working bound from a broken walk."""
    monkeypatch.setattr(sessions_svc, "MAX_RUN_FILE_SCANNED_PATHS", 20)
    changes = _CountingChanges({"file_path": f"f{i}.py"} for i in range(5))

    summary = sessions_svc._derive_run_files([_multiedit_message(changes)], artifact_root=None)

    assert summary["bounded"] is False
    assert summary["truncated"] is False
    assert summary["total"] == 5
    assert changes.yielded == 5


async def _seed_one_action(db_path: Path, *, session_id: str, changes: int) -> None:
    """A single ActionRequest whose payload size is set by its change count."""
    msg_id = f"{session_id}-big"
    await seed_branch(db_path, branch_id="b1", session_id=session_id, msg_ids=[msg_id])
    async with StateDB(db_path) as db:
        await db.insert_message(
            {
                "id": msg_id,
                "created_at": 100.0,
                "content": {
                    "function": "MultiEdit",
                    "arguments": {
                        "changes": [{"file_path": f"/run/f{i}.py"} for i in range(changes)]
                    },
                },
                "sender": "worker",
                "recipient": "tool",
                "role": "action",
                "node_metadata": {
                    "lion_class": "lionagi.protocols.messages.action_request.ActionRequest"
                },
            }
        )


async def test_an_oversized_action_payload_never_reaches_the_parser(
    patched_sessions_db, monkeypatch
):
    """The row and resolve budgets both count things, which bounds the work only
    while one thing costs a bounded amount to handle. A stored payload has no
    ceiling and decoding it is what builds the object graph, so the size test has
    to happen before the parser sees the value rather than inside the walk."""
    svc, db_path = patched_sessions_db
    monkeypatch.setattr(svc, "MAX_ACTION_CONTENT_CHARS", 400)

    seen_lengths: list[int] = []
    real_parse = svc._parse_json_col

    def spy(value):
        if isinstance(value, str):
            seen_lengths.append(len(value))
        return real_parse(value)

    monkeypatch.setattr(svc, "_parse_json_col", spy)
    await seed_session(db_path, session_id="sess-oversized", artifacts_path="/run")
    await _seed_one_action(db_path, session_id="sess-oversized", changes=500)

    detail = await svc.get_session("sess-oversized")

    assert detail is not None
    # The whole point: no string above the ceiling was ever decoded.
    assert seen_lengths, "parser was never called at all, so this proves nothing"
    assert max(seen_lengths) <= 400, seen_lengths
    # And the caller is told the surface it got back is not the whole one.
    assert detail["message_stats"]["bounded"] is True
    assert detail["run_files"]["bounded"] is True


async def test_a_payload_inside_the_ceiling_is_parsed_and_reported_whole(
    patched_sessions_db, monkeypatch
):
    """Control: the assertions above pass trivially if nothing is ever parsed or
    if `bounded` is stuck on. Same shape of row, small enough to keep."""
    svc, db_path = patched_sessions_db
    monkeypatch.setattr(svc, "MAX_ACTION_CONTENT_CHARS", 1_048_576)
    await seed_session(db_path, session_id="sess-normal", artifacts_path="/run")
    await _seed_one_action(db_path, session_id="sess-normal", changes=3)

    detail = await svc.get_session("sess-normal")

    assert detail is not None
    assert detail["message_stats"]["bounded"] is False
    assert detail["run_files"]["bounded"] is False
    assert {item["path"] for item in detail["run_files"]["items"]} == {
        "f0.py",
        "f1.py",
        "f2.py",
    }


async def test_the_decoded_total_is_bounded_not_just_the_row_count_and_the_row_size(
    patched_sessions_db, monkeypatch
):
    """A cap on rows and a cap on each row's payload are bounds on different
    things, and two such bounds multiply. Twenty thousand rows of a megabyte
    each is the product, and the product is what has to fit in memory, so the
    total needs a bound of its own."""
    svc, db_path = patched_sessions_db
    monkeypatch.setattr(svc, "MAX_ACTION_CONTENT_CHARS", 100_000)
    monkeypatch.setattr(svc, "MAX_HYDRATED_ACTION_MESSAGES", 50)
    monkeypatch.setattr(svc, "MAX_HYDRATED_CONTENT_CHARS", 2_000)
    await seed_session(db_path, session_id="sess-total", artifacts_path="/run")
    await _seed_action_requests(db_path, branch_id="b1", session_id="sess-total", count=40)

    detail = await svc.get_session("sess-total")

    assert detail is not None
    assert detail["message_stats"]["bounded"] is True
    # Neither of the other two bounds was reached: 40 rows is under the row cap
    # of 50, and each row is far under the per-payload ceiling. Only the total
    # can have stopped this.
    kept = detail["message_stats"]["tool_call_count"]
    assert 0 < kept < 40, kept


async def test_a_session_inside_every_bound_reports_itself_complete(
    patched_sessions_db, monkeypatch
):
    """Control: the test above passes if `bounded` is stuck on, or if the new
    budget binds on ordinary sessions. Same rows, a total large enough to hold
    them."""
    svc, db_path = patched_sessions_db
    monkeypatch.setattr(svc, "MAX_ACTION_CONTENT_CHARS", 100_000)
    monkeypatch.setattr(svc, "MAX_HYDRATED_ACTION_MESSAGES", 50)
    monkeypatch.setattr(svc, "MAX_HYDRATED_CONTENT_CHARS", 10_000_000)
    await seed_session(db_path, session_id="sess-total-ok", artifacts_path="/run")
    await _seed_action_requests(db_path, branch_id="b1", session_id="sess-total-ok", count=40)

    detail = await svc.get_session("sess-total-ok")

    assert detail is not None
    assert detail["message_stats"]["bounded"] is False
    assert detail["message_stats"]["tool_call_count"] == 40
