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
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_cost_usd: float | None = None,
    num_turns: int | None = None,
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
        usage_fields = {
            k: v
            for k, v in {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_cost_usd": total_cost_usd,
                "num_turns": num_turns,
            }.items()
            if v is not None
        }
        if usage_fields:
            await db.update_session(session_id, **usage_fields)
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
# Test 1.7a: get_session usage fields land beside duration_ms
# ---------------------------------------------------------------------------


async def test_get_session_surfaces_usage_fields_beside_duration(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_session(
        db_path,
        session_id="sess-usage-detail",
        status="completed",
        started_at=10.0,
        ended_at=13.5,
        input_tokens=1500,
        output_tokens=2500,
        total_cost_usd=0.0007,
        num_turns=4,
    )

    result = await svc.get_session("sess-usage-detail")

    assert result is not None
    assert result["duration_ms"] == 3500.0
    assert result["input_tokens"] == 1500
    assert result["output_tokens"] == 2500
    assert result["total_cost_usd"] == 0.0007
    assert result["num_turns"] == 4


async def test_get_session_distinguishes_unreported_cost_from_zero_cost(patched_sessions_db):
    """None (unreported) and 0.0 (genuinely free) must round-trip distinctly."""
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-detail-no-cost", status="completed")
    await seed_session(
        db_path, session_id="sess-detail-free", status="completed", total_cost_usd=0.0
    )

    no_cost = await svc.get_session("sess-detail-no-cost")
    free = await svc.get_session("sess-detail-free")

    assert no_cost["total_cost_usd"] is None
    assert free["total_cost_usd"] == 0.0


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
# Artifact verification display state
# ---------------------------------------------------------------------------


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
    assert result["artifact_verification_json"] == verdict


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


# ---------------------------------------------------------------------------
# Test 1.8a — get_session_by_cc_id: legacy rows fall back to deterministic id
# ---------------------------------------------------------------------------


async def test_get_session_by_cc_id_falls_back_for_legacy_row(patched_sessions_db):
    svc, db_path = patched_sessions_db
    cc_uid = "11111111-2222-3333-4444-555555555555"
    legacy_session_id = session_db_id(cc_uid)
    await seed_session(db_path, session_id=legacy_session_id)

    result = await svc.get_session_by_cc_id(cc_uid)

    assert result is not None
    assert result["id"] == legacy_session_id
    assert result["name"] == "Test Session"


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
    status: str | None = None,
    started_at: float | None = None,
    ended_at: float | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_cost_usd: float | None = None,
    num_turns: int | None = None,
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
        lifecycle_fields = {
            k: v
            for k, v in {
                "status": status,
                "started_at": started_at,
                "ended_at": ended_at,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_cost_usd": total_cost_usd,
                "num_turns": num_turns,
            }.items()
            if v is not None
        }
        if lifecycle_fields:
            await db.update_branch(branch_id, **lifecycle_fields)
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


async def test_list_sessions_surfaces_usage_fields(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_session(
        db_path,
        session_id="sess-usage",
        status="completed",
        input_tokens=120,
        output_tokens=340,
        total_cost_usd=0.0042,
        num_turns=3,
    )

    rows = await svc.list_sessions()

    assert len(rows) == 1
    row = rows[0]
    assert row["input_tokens"] == 120
    assert row["output_tokens"] == 340
    assert row["total_cost_usd"] == 0.0042
    assert row["num_turns"] == 3


async def test_list_sessions_distinguishes_unreported_cost_from_zero_cost(patched_sessions_db):
    """None (unreported) and 0.0 (genuinely free) must round-trip distinctly."""
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-no-cost", status="completed")
    await seed_session(db_path, session_id="sess-free", status="completed", total_cost_usd=0.0)

    rows = {row["id"]: row for row in await svc.list_sessions()}

    assert rows["sess-no-cost"]["total_cost_usd"] is None
    assert rows["sess-free"]["total_cost_usd"] == 0.0


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

    assert rows[0]["artifact_verification_json"] == stored


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


async def test_get_session_branch_surfaces_usage_and_derived_duration(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-branch-usage")
    await seed_branch(
        db_path,
        branch_id="br-usage",
        session_id="sess-branch-usage",
        status="completed",
        started_at=200.0,
        ended_at=205.25,
        input_tokens=800,
        output_tokens=1600,
        total_cost_usd=0.0031,
        num_turns=2,
    )

    result = await svc.get_session("sess-branch-usage")

    assert result is not None
    branch = result["branches"][0]
    assert branch["input_tokens"] == 800
    assert branch["output_tokens"] == 1600
    assert branch["total_cost_usd"] == 0.0031
    assert branch["num_turns"] == 2
    assert branch["duration_ms"] == 5250.0


async def test_get_session_branch_duration_is_none_without_both_timestamps(patched_sessions_db):
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-branch-partial")
    await seed_branch(
        db_path,
        branch_id="br-partial",
        session_id="sess-branch-partial",
        status="running",
        started_at=200.0,
        # ended_at intentionally omitted
    )

    result = await svc.get_session("sess-branch-partial")

    branch = result["branches"][0]
    assert branch["started_at"] == 200.0
    assert branch["ended_at"] is None
    assert branch["duration_ms"] is None


async def test_get_session_branch_distinguishes_unreported_cost_from_zero_cost(
    patched_sessions_db,
):
    """None (unreported) and 0.0 (genuinely free) must round-trip distinctly."""
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-branch-cost")
    await seed_branch(
        db_path, branch_id="br-no-cost", session_id="sess-branch-cost", status="completed"
    )
    await seed_branch(
        db_path,
        branch_id="br-free",
        session_id="sess-branch-cost",
        status="completed",
        total_cost_usd=0.0,
    )

    result = await svc.get_session("sess-branch-cost")

    branches = {b["id"]: b for b in result["branches"]}
    assert branches["br-no-cost"]["total_cost_usd"] is None
    assert branches["br-free"]["total_cost_usd"] == 0.0


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
    must be present and match the pre-fix _format_message() output exactly."""
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
            "sender": "worker",
            "timestamp": 111.0,
            "lion_class": "lionagi.protocols.messages.assistant_response.AssistantResponse",
            "branch_id": "br-shape",
        }
    ]


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
# Message pagination — detail responses window from the progression tail
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# message_cursor — stable pagination under concurrent progression appends
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Action-stat aggregation must match the canonical persisted lion_class values
# ---------------------------------------------------------------------------


async def test_get_session_action_stats_match_canonical_fully_qualified_lion_class(
    patched_sessions_db,
):
    """The runtime persists lion_class as the fully-qualified dotted path (see the
    message_types seed rows in state/schema.sql), not the bare class name. Tool/error/
    file aggregation must recognize that shape, not just a legacy short name."""
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
    assert "/tmp/canonical.txt" in stats["files"]


def test_branch_file_stats_only_accept_structured_file_tool_paths():
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
        {
            "id": "glob",
            "lion_class": "ActionRequest",
            "content": {"function": "Glob", "arguments": {"path": "/repo/src"}},
        },
        {
            "id": "bash",
            "lion_class": "ActionRequest",
            "content": {"function": "Bash", "arguments": {"path": "//"}},
        },
    ]

    stats = _branch_message_stats(4, {"action": 4}, action_messages)

    assert stats["files"] == ["/repo/Makefile", "/repo/src/main.py"]


def test_branch_file_stats_capture_empty_or_missing_function_name():
    from lionagi.studio.services.sessions import _branch_message_stats

    action_messages = [
        {
            "id": "empty-fn",
            "lion_class": "ActionRequest",
            "content": {"function": "", "arguments": {"file_path": "/repo/src/empty.py"}},
        },
        {
            "id": "missing-fn",
            "lion_class": "ActionRequest",
            "content": {"arguments": {"path": "/repo/src/missing.py"}},
        },
        {
            "id": "bash",
            "lion_class": "ActionRequest",
            "content": {"function": "Bash", "arguments": {"path": "//"}},
        },
    ]

    stats = _branch_message_stats(3, {"action": 3}, action_messages)

    assert stats["files"] == ["/repo/src/empty.py", "/repo/src/missing.py"]


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


# ---------------------------------------------------------------------------
# resolve_session_display_name() served through list_sessions() / get_session()
# ---------------------------------------------------------------------------


async def test_unrenamed_session_display_name_is_the_legacy_fallback(patched_sessions_db):
    """A session nobody has renamed (user_label NULL) must resolve to exactly
    the same name list_sessions()/get_session() already returned before
    user_label existed -- no visual change for the common case."""
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-unrenamed", status="completed")

    rows = await svc.list_sessions()
    detail = await svc.get_session("sess-unrenamed")

    assert rows[0]["user_label"] is None
    assert rows[0]["display_name"] == "Test Session"  # seed_session's default `name`
    assert detail["user_label"] is None
    assert detail["display_name"] == "Test Session"


async def test_renamed_session_display_name_agrees_across_list_and_detail(patched_sessions_db):
    """The rename must be visible, and identical, everywhere a session name is
    served -- a second, independently-computed fallback is exactly the defect
    resolve_session_display_name() exists to remove."""
    svc, db_path = patched_sessions_db
    await seed_session(db_path, session_id="sess-renamed", status="completed")

    async with StateDB(db_path) as db:
        await db.update_session("sess-renamed", user_label="Ocean's Debug Run")

    rows = await svc.list_sessions()
    detail = await svc.get_session("sess-renamed")

    assert rows[0]["user_label"] == "Ocean's Debug Run"
    assert rows[0]["display_name"] == "Ocean's Debug Run"
    assert detail["user_label"] == "Ocean's Debug Run"
    assert detail["display_name"] == "Ocean's Debug Run"


async def test_renamed_session_display_name_wins_over_agent_and_playbook(patched_sessions_db):
    """user_label outranks every existing fallback candidate, not just `name`."""
    svc, db_path = patched_sessions_db
    async with StateDB(db_path) as db:
        prog_id = "sess-priority-prog"
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": "sess-priority",
                "created_at": 100.0,
                "updated_at": 100.0,
                "progression_id": prog_id,
                "name": None,
                "agent_name": "worker-agent",
                "playbook_name": "worker-playbook",
                "status": "completed",
            }
        )
        await db.update_session("sess-priority", user_label="Priority Label")

    detail = await svc.get_session("sess-priority")
    assert detail["display_name"] == "Priority Label"
