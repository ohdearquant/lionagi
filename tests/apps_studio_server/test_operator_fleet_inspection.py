# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Bounded, redacted projections for Studio inspection tools."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")


SECRET = "sk-test-secret-value-1234567890"
PRIVATE_PATH = "/Users/example/private/report.txt"
STORE_URL = "postgresql+asyncpg://reader:password@db.example/data"


@pytest.fixture(autouse=True)
def readable_store(monkeypatch):
    """Declare the premise these tests stand on: a store that could be opened.

    They replace the carriers with fakes, which says what the store contains but
    not that it was reachable. The list handlers refuse to answer at all when it
    was not, so without this the fakes are never consulted and every read here
    would report `known: False`. Tests for that path set it False explicitly.
    """
    from lionagi.studio.services import _db

    monkeypatch.setattr(_db, "store_exists", lambda: True)


def _assert_private_input_is_removed(result, source):
    source_text = json.dumps(source)
    result_text = json.dumps(result)
    assert SECRET in source_text
    assert PRIVATE_PATH in source_text
    assert STORE_URL in source_text
    assert SECRET not in result_text
    assert PRIVATE_PATH not in result_text
    assert STORE_URL not in result_text


@pytest.mark.asyncio
async def test_list_sessions_caps_rows_and_projects_public_fields(monkeypatch):
    from lionagi.studio.operator.application_mcp import list_sessions
    from lionagi.studio.services import runs, sessions

    rows = [
        {
            "id": f"session-{index}",
            "name": f"run {index} " + "x" * 9_000,
            "project": PRIVATE_PATH,
            "status_reason_summary": f"token={SECRET} {STORE_URL}",
            "node_metadata": {"token": SECRET},
            "artifacts_path": PRIVATE_PATH,
            "artifact_contract_json": {"body": "x" * 2_000_001},
            "artifact_verification_json": {"token": SECRET},
            "project_source": "runtime",
        }
        for index in range(3)
    ]

    async def fake_list_runs(**_kwargs):
        assert _kwargs["status"] == "pending"
        return rows[:2]

    async def fake_count_sessions(_where):
        assert _where.statuses == {"pending", "prepared"}
        return 3

    monkeypatch.setattr(runs, "list_runs", fake_list_runs)
    monkeypatch.setattr(sessions, "count_sessions", fake_count_sessions)
    result = await list_sessions({"limit": 2, "status": "pending"})

    assert result["truncated"] is True
    assert result["total"] == 3
    assert result["sessions"][0]["project"] == "report.txt"
    assert result["content_truncated"] is True
    assert len(json.dumps(result["sessions"][0]["name"]).encode()) <= 8_000
    assert "node_metadata" not in result["sessions"][0]
    assert "artifact_contract_json" not in result["sessions"][0]
    assert "artifact_verification_json" not in result["sessions"][0]
    _assert_private_input_is_removed(result, rows)


@pytest.mark.asyncio
async def test_session_detail_preserves_window_flags_and_redacts_messages(monkeypatch):
    from lionagi.studio.operator.application_mcp import session_detail
    from lionagi.studio.services import sessions

    next_cursor = sessions._encode_message_cursor(
        "session-1",
        1,
        {f"branch-{index}": f"message-{index}" for index in range(500)},
    )
    source = {
        "id": "session-1",
        "project": PRIVATE_PATH,
        "node_metadata": {"token": SECRET},
        "artifacts_path": PRIVATE_PATH,
        "artifact_contract_json": {"body": "x" * 2_000_001},
        "artifact_verification_json": {"token": SECRET},
        "graph": {"path": PRIVATE_PATH},
        "segments": [{"token": SECRET}],
        "status_evidence_refs": [PRIVATE_PATH],
        "project_source": "runtime",
        "source_show": {"topic": SECRET},
        "message_next_cursor": next_cursor,
        "message_stats": {
            "message_count": 2,
            "errors": [{"output": "x" * 3_000_000}],
            "files": [PRIVATE_PATH],
            "branches": {"branch-1": {"message_count": 2}},
        },
        "branches": [
            {
                "id": "branch-1",
                "messages_truncated": True,
                "message_has_older": True,
                "messages": [
                    {
                        "role": "user",
                        "content": f"token={SECRET} {PRIVATE_PATH} {STORE_URL}",
                    },
                    {"role": "assistant", "content": "x" * (2 * 1024 * 1024 + 1)},
                ],
            }
        ],
    }

    async def fake_get_session(*_args, **_kwargs):
        return source

    monkeypatch.setattr(sessions, "get_session", fake_get_session)
    result = await session_detail({"session_id": "session-1", "message_limit": 1})

    assert result["known"] is True
    assert result["message_next_cursor"] == next_cursor
    assert sessions._decode_message_cursor(
        result["message_next_cursor"], session_id="session-1", limit=1
    )
    assert result["messages_bytes_truncated"] is True
    assert result["message_stats_truncated"] is True
    assert "errors" not in result["message_stats"]
    assert "files" not in result["message_stats"]
    assert result["branches"][0]["messages_truncated"] is True
    assert result["branches"][0]["message_has_older"] is True
    for field in (
        "node_metadata",
        "artifacts_path",
        "artifact_contract_json",
        "artifact_verification_json",
        "graph",
        "segments",
        "status_evidence_refs",
    ):
        assert field not in result
    _assert_private_input_is_removed(result, source)


@pytest.mark.asyncio
async def test_session_detail_labels_cursor_fallback(monkeypatch):
    from lionagi.studio.operator.application_mcp import session_detail
    from lionagi.studio.services import sessions

    calls = []

    async def fake_get_session(*_args, message_cursor, **_kwargs):
        calls.append(message_cursor)
        if message_cursor is not None:
            raise sessions.MessageCursorError("stale")
        return {"id": "session-1", "branches": []}

    monkeypatch.setattr(sessions, "get_session", fake_get_session)
    result = await session_detail({"session_id": "session-1", "message_cursor": "old"})

    assert calls == ["old", None]
    assert result["source"] == "fallback"


@pytest.mark.asyncio
async def test_session_signals_caps_rows_and_each_payload(monkeypatch):
    from lionagi.studio.operator.application_mcp import session_signals
    from lionagi.studio.services import signals

    source = [
        {
            "id": "a",
            "session_id": "s",
            "seq": 1,
            "kind": "note",
            "op_id": "",
            "ts": 1,
            "payload": {"token": SECRET, "path": PRIVATE_PATH, "url": STORE_URL},
        },
        {
            "id": "b",
            "session_id": "s",
            "seq": 2,
            "kind": "note",
            "op_id": "",
            "ts": 2,
            "payload": 'é"\\' * 40_000,
        },
        {
            "id": "c",
            "session_id": "s",
            "seq": 3,
            "kind": "note",
            "op_id": "",
            "ts": 3,
            "payload": {},
        },
    ]

    async def fake_get_signals_after(*_args, **_kwargs):
        return source

    monkeypatch.setattr(signals, "get_signals_after", fake_get_signals_after)
    result = await session_signals({"session_id": "s", "after_seq": None, "limit": 2})

    assert result["truncated"] is True
    assert result["signals"][1]["payload_truncated"] is True
    assert len(json.dumps(result["signals"][1]["payload"]).encode()) <= 100_000
    _assert_private_input_is_removed(result, source)


@pytest.mark.asyncio
async def test_get_invocation_caps_children_and_artifact_content(monkeypatch):
    from lionagi.studio.operator.application_mcp import get_invocation
    from lionagi.studio.services import invocations

    source = {
        "id": "invocation-1",
        "prompt": f"Bearer {SECRET} {PRIVATE_PATH} {STORE_URL} " + "x" * 9_000,
        "node_metadata": {"token": SECRET},
        "status_evidence_refs": [PRIVATE_PATH],
        "schedule_run_error_detail": f"token={SECRET} {PRIVATE_PATH}",
        "sessions": [{"id": f"s-{i}", "name": f"session {i}"} for i in range(51)],
        "artifacts": [
            {
                "id": f"a-{i}",
                "kind": "result",
                "name": "result",
                "file_path": PRIVATE_PATH,
                "content": (
                    {"token": SECRET, "path": PRIVATE_PATH, "body": "x" * 2_000_001}
                    if i == 0
                    else {"ok": True}
                ),
            }
            for i in range(51)
        ],
    }

    async def fake_get_invocation(_invocation_id, **_kwargs):
        return source

    monkeypatch.setattr(invocations, "get_invocation", fake_get_invocation)
    result = await get_invocation({"invocation_id": "invocation-1"})

    assert result["sessions_truncated"] is True
    assert result["artifacts_truncated"] is True
    assert result["content_truncated"] is True
    assert len(json.dumps(result["prompt"]).encode()) <= 8_000
    assert len(result["sessions"]) == 50
    assert result["artifacts"][0]["content_truncated"] is True
    assert len(json.dumps(result["artifacts"][0]["content"]).encode()) <= 2_000_000
    assert "file_path" not in result["artifacts"][0]
    _assert_private_input_is_removed(result, source)


@pytest.mark.asyncio
async def test_list_artifacts_caps_rows_and_drops_storage_fields(monkeypatch):
    import lionagi.studio.operator.application_mcp as app

    source = [
        {
            "id": f"a-{i}",
            "kind": "result",
            "name": f"token={SECRET} {PRIVATE_PATH} {STORE_URL}",
            "content": {"token": SECRET, "path": PRIVATE_PATH},
            "file_path": PRIVATE_PATH,
        }
        for i in range(3)
    ]

    async def fake_rows(*_args, **_kwargs):
        return source

    monkeypatch.setattr(app, "_artifact_rows", fake_rows)
    result = await app.list_artifacts({"session_id": "s", "limit": 2})

    assert result["truncated"] is True
    assert len(result["artifacts"]) == 2
    assert all("content" not in item for item in result["artifacts"])
    assert all("file_path" not in item for item in result["artifacts"])
    _assert_private_input_is_removed(result, source)


@pytest.mark.asyncio
async def test_get_artifact_caps_and_redacts_content(monkeypatch):
    from lionagi.studio.operator.application_mcp import get_artifact
    from lionagi.studio.services import invocations

    source = {
        "id": "a-1",
        "kind": "result",
        "name": f"token={SECRET} {PRIVATE_PATH} {STORE_URL} " + "x" * 9_000,
        "file_path": PRIVATE_PATH,
        "content": {"token": SECRET, "path": PRIVATE_PATH, "body": "x" * 2_000_001},
    }

    async def fake_get_artifact(_artifact_id, **_kwargs):
        return source

    monkeypatch.setattr(invocations, "get_artifact", fake_get_artifact)
    result = await get_artifact({"artifact_id": "a-1"})

    assert result["known"] is True
    assert result["content_truncated"] is True
    assert result["metadata_truncated"] is True
    assert len(json.dumps(result["name"]).encode()) <= 8_000
    assert len(json.dumps(result["content"]).encode()) <= 2_000_000
    assert "file_path" not in result
    _assert_private_input_is_removed(result, source)


# An unreadable store must not answer like an empty one
#
# Each guard gets both arms. The unknown-arm alone would pass against a `known`
# hardcoded to False, and the known-arm alone would pass against one hardcoded
# True, so neither on its own shows the flag carries information.


async def test_list_sessions_reports_unknown_when_the_store_cannot_be_opened(monkeypatch):
    from lionagi.studio.operator.application_mcp import list_sessions
    from lionagi.studio.services import _db, runs

    consulted = False

    async def fake_list_runs(**_kwargs):
        nonlocal consulted
        consulted = True
        return []

    monkeypatch.setattr(runs, "list_runs", fake_list_runs)
    monkeypatch.setattr(_db, "store_exists", lambda: False)

    result = await list_sessions({"limit": 10})

    assert result["known"] is False
    assert "sessions" not in result
    # Declining to answer is the behaviour under test: a carrier consulted here
    # would return [], which is the empty answer this guard exists to withhold.
    assert consulted is False


async def test_list_sessions_reports_known_for_a_readable_but_empty_store(monkeypatch):
    from lionagi.studio.operator.application_mcp import list_sessions
    from lionagi.studio.services import runs, sessions

    async def fake_list_runs(**_kwargs):
        return []

    async def fake_count_sessions(_where):
        return 0

    monkeypatch.setattr(runs, "list_runs", fake_list_runs)
    monkeypatch.setattr(sessions, "count_sessions", fake_count_sessions)

    result = await list_sessions({"limit": 10})

    assert result["known"] is True
    assert result["sessions"] == []


async def test_session_signals_reports_unknown_when_the_store_cannot_be_opened(monkeypatch):
    from lionagi.studio.operator.application_mcp import session_signals
    from lionagi.studio.services import _db, signals

    consulted = False

    async def fake_get_signals_after(*_args, **_kwargs):
        nonlocal consulted
        consulted = True
        return []

    monkeypatch.setattr(signals, "get_signals_after", fake_get_signals_after)
    monkeypatch.setattr(_db, "store_exists", lambda: False)

    result = await session_signals({"session_id": "s", "after_seq": None, "limit": 10})

    assert result["known"] is False
    assert "signals" not in result
    assert consulted is False


async def test_session_signals_reports_known_for_a_session_with_no_signals(monkeypatch):
    from lionagi.studio.operator.application_mcp import session_signals
    from lionagi.studio.services import signals

    async def fake_get_signals_after(*_args, **_kwargs):
        return []

    monkeypatch.setattr(signals, "get_signals_after", fake_get_signals_after)

    result = await session_signals({"session_id": "s", "after_seq": None, "limit": 10})

    assert result["known"] is True
    assert result["signals"] == []


async def test_list_artifacts_reports_unknown_when_read_only_open_is_unavailable(monkeypatch):
    """A read-only open the store cannot provide is reported as an unavailable
    read, never widened into an ordinary writable one -- which is exactly what
    the availability helper hands back, and what a read-only tool must refuse."""
    from lionagi.state import db as db_module
    from lionagi.studio.operator.application_mcp import list_artifacts

    def _must_not_open(*_args, **_kwargs):
        raise AssertionError("the store must not be opened when read-only is unavailable")

    monkeypatch.setattr(db_module, "state_db_known_absent", lambda: False)
    monkeypatch.setattr(db_module, "read_only_open_supported", lambda: False)
    monkeypatch.setattr(db_module, "StateDB", _must_not_open)

    result = await list_artifacts({"session_id": "sess-1"})

    assert result["known"] is False
    assert "artifacts" not in result


async def test_list_artifacts_reports_known_when_read_only_open_is_available(monkeypatch):
    from lionagi.studio.operator import application_mcp as app
    from lionagi.studio.operator.application_mcp import list_artifacts

    async def fake_rows(**_kwargs):
        return []

    monkeypatch.setattr(app, "_artifact_rows", fake_rows)

    result = await list_artifacts({"session_id": "sess-1"})

    assert result["known"] is True
    assert result["artifacts"] == []
