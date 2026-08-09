# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Focused tests for the invocation-detail and artifact Operator read tools."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")

pytestmark = pytest.mark.asyncio

PLANTED_SECRET = "sk-live-9f8e7d6c5b4a3210"
PLANTED_PATH = "/Users/example-user/private/workspace/notes.txt"
PLANTED_STORE_URL = "postgresql+asyncpg://reader:hunter2@internal-db.example/data"


def _assert_planted_values_are_scrubbed(raw_source: object, tool_result: object) -> None:
    """Confirm every planted value is absent from the result, with a positive
    control proving each assertion would fail if the value leaked."""
    raw_text = json.dumps(raw_source)
    result_text = json.dumps(tool_result)

    # Positive control: the values really are present in the unredacted input,
    # so the absence checks below are not vacuously true.
    assert PLANTED_SECRET in raw_text
    assert PLANTED_PATH in raw_text
    assert PLANTED_STORE_URL in raw_text

    assert PLANTED_SECRET not in result_text
    assert PLANTED_PATH not in result_text
    assert PLANTED_STORE_URL not in result_text


# ---------------------------------------------------------------------------
# get_invocation
# ---------------------------------------------------------------------------


async def test_get_invocation_returns_projected_fields_for_known_id(monkeypatch):
    from lionagi.studio.operator.application_mcp import get_invocation
    from lionagi.studio.services import invocations

    source = {
        "id": "inv-happy",
        "skill": "tester",
        "prompt": "summarize the run",
        "sessions": [{"id": "s-1", "name": "child session"}],
        "artifacts": [{"id": "a-1", "kind": "result", "name": "result", "content": {"ok": True}}],
    }

    async def fake_get_invocation(_invocation_id):
        return source

    monkeypatch.setattr(invocations, "get_invocation", fake_get_invocation)
    result = await get_invocation({"invocation_id": "inv-happy"})

    assert result["known"] is True
    assert result["source"] == "store"
    assert result["skill"] == "tester"
    assert result["sessions_truncated"] is False
    assert result["artifacts_truncated"] is False
    assert len(result["sessions"]) == 1
    assert len(result["artifacts"]) == 1


async def test_get_invocation_caps_oversized_artifact_content_and_flags_truncation(monkeypatch):
    from lionagi.studio.operator.application_mcp import get_invocation
    from lionagi.studio.services import invocations

    oversized_body = "y" * 2_000_050
    source = {
        "id": "inv-oversized",
        "sessions": [],
        "artifacts": [
            {"id": "a-1", "kind": "result", "name": "result", "content": {"body": oversized_body}}
        ],
    }

    async def fake_get_invocation(_invocation_id):
        return source

    monkeypatch.setattr(invocations, "get_invocation", fake_get_invocation)
    result = await get_invocation({"invocation_id": "inv-oversized"})

    artifact = result["artifacts"][0]
    assert artifact["content_truncated"] is True
    assert len(json.dumps(artifact["content"]).encode()) <= 2_000_000


async def test_get_invocation_redacts_secret_url_and_path_from_all_content(monkeypatch):
    from lionagi.studio.operator.application_mcp import get_invocation
    from lionagi.studio.services import invocations

    source = {
        "id": "inv-secret",
        "prompt": f"Authorization: Bearer {PLANTED_SECRET} see {PLANTED_PATH} at {PLANTED_STORE_URL}",
        "sessions": [{"id": "s-1", "name": f"child of {PLANTED_PATH}"}],
        "artifacts": [
            {
                "id": "a-1",
                "kind": "result",
                "name": "result",
                "content": {
                    "token": PLANTED_SECRET,
                    "path": PLANTED_PATH,
                    "url": PLANTED_STORE_URL,
                },
            }
        ],
    }

    async def fake_get_invocation(_invocation_id):
        return source

    monkeypatch.setattr(invocations, "get_invocation", fake_get_invocation)
    result = await get_invocation({"invocation_id": "inv-secret"})

    _assert_planted_values_are_scrubbed(source, result)


async def test_get_invocation_reports_unknown_for_missing_invocation_id(monkeypatch):
    from lionagi.studio.operator.application_mcp import get_invocation
    from lionagi.studio.services import invocations

    async def fake_get_invocation(_invocation_id):
        return None

    monkeypatch.setattr(invocations, "get_invocation", fake_get_invocation)
    result = await get_invocation({"invocation_id": "does-not-exist"})

    assert result == {"known": False}


# ---------------------------------------------------------------------------
# list_artifacts
# ---------------------------------------------------------------------------


async def test_list_artifacts_returns_metadata_for_known_owner(monkeypatch):
    import lionagi.studio.operator.application_mcp as app_mcp

    source = [
        {"id": "a-1", "kind": "result", "name": "first"},
        {"id": "a-2", "kind": "result", "name": "second"},
    ]

    async def fake_rows(*_args, **_kwargs):
        return source

    monkeypatch.setattr(app_mcp, "_artifact_rows", fake_rows)
    result = await app_mcp.list_artifacts({"session_id": "session-happy", "limit": 50})

    assert result["source"] == "store"
    assert result["truncated"] is False
    assert [row["id"] for row in result["artifacts"]] == ["a-1", "a-2"]


async def test_list_artifacts_caps_row_count_and_flags_truncation(monkeypatch):
    import lionagi.studio.operator.application_mcp as app_mcp

    source = [{"id": f"a-{i}", "kind": "result", "name": f"item {i}"} for i in range(5)]

    async def fake_rows(*_args, **_kwargs):
        return source

    monkeypatch.setattr(app_mcp, "_artifact_rows", fake_rows)
    result = await app_mcp.list_artifacts({"invocation_id": "inv-many", "limit": 2})

    assert result["truncated"] is True
    assert len(result["artifacts"]) == 2


async def test_list_artifacts_redacts_secret_url_and_path_from_metadata(monkeypatch):
    import lionagi.studio.operator.application_mcp as app_mcp

    source = [
        {
            "id": "a-1",
            "kind": "result",
            "name": f"token={PLANTED_SECRET} path={PLANTED_PATH} url={PLANTED_STORE_URL}",
            "content": {"token": PLANTED_SECRET},
            "file_path": PLANTED_PATH,
        }
    ]

    async def fake_rows(*_args, **_kwargs):
        return source

    monkeypatch.setattr(app_mcp, "_artifact_rows", fake_rows)
    result = await app_mcp.list_artifacts({"session_id": "session-secret", "limit": 10})

    assert "content" not in result["artifacts"][0]
    assert "file_path" not in result["artifacts"][0]
    _assert_planted_values_are_scrubbed(source, result)


async def test_list_artifacts_returns_empty_for_unknown_owner_id(monkeypatch):
    import lionagi.studio.operator.application_mcp as app_mcp

    async def fake_rows(*_args, **_kwargs):
        return []

    monkeypatch.setattr(app_mcp, "_artifact_rows", fake_rows)
    result = await app_mcp.list_artifacts({"session_id": "no-such-session", "limit": 10})

    assert result["artifacts"] == []
    assert result["truncated"] is False


# ---------------------------------------------------------------------------
# get_artifact
# ---------------------------------------------------------------------------


async def test_get_artifact_returns_full_projection_for_known_id(monkeypatch):
    from lionagi.studio.operator.application_mcp import get_artifact
    from lionagi.studio.services import invocations

    source = {"id": "a-happy", "kind": "result", "name": "result", "content": {"ok": True}}

    async def fake_get_artifact(_artifact_id):
        return source

    monkeypatch.setattr(invocations, "get_artifact", fake_get_artifact)
    result = await get_artifact({"artifact_id": "a-happy"})

    assert result["known"] is True
    assert result["source"] == "store"
    assert result["content_truncated"] is False
    assert result["content"] == {"ok": True}


async def test_get_artifact_caps_oversized_content_and_flags_truncation(monkeypatch):
    from lionagi.studio.operator.application_mcp import get_artifact
    from lionagi.studio.services import invocations

    oversized_body = "z" * 2_000_050
    source = {
        "id": "a-oversized",
        "kind": "result",
        "name": "result",
        "content": {"body": oversized_body},
    }

    async def fake_get_artifact(_artifact_id):
        return source

    monkeypatch.setattr(invocations, "get_artifact", fake_get_artifact)
    result = await get_artifact({"artifact_id": "a-oversized"})

    assert result["content_truncated"] is True
    assert len(json.dumps(result["content"]).encode()) <= 2_000_000


async def test_get_artifact_redacts_secret_url_and_path_from_content(monkeypatch):
    from lionagi.studio.operator.application_mcp import get_artifact
    from lionagi.studio.services import invocations

    source = {
        "id": "a-secret",
        "kind": "result",
        "name": f"token={PLANTED_SECRET} path={PLANTED_PATH} url={PLANTED_STORE_URL}",
        "file_path": PLANTED_PATH,
        "content": {"token": PLANTED_SECRET, "path": PLANTED_PATH, "url": PLANTED_STORE_URL},
    }

    async def fake_get_artifact(_artifact_id):
        return source

    monkeypatch.setattr(invocations, "get_artifact", fake_get_artifact)
    result = await get_artifact({"artifact_id": "a-secret"})

    assert "file_path" not in result
    _assert_planted_values_are_scrubbed(source, result)


async def test_get_artifact_reports_unknown_for_missing_artifact_id(monkeypatch):
    from lionagi.studio.operator.application_mcp import get_artifact
    from lionagi.studio.services import invocations

    async def fake_get_artifact(_artifact_id):
        return None

    monkeypatch.setattr(invocations, "get_artifact", fake_get_artifact)
    result = await get_artifact({"artifact_id": "does-not-exist"})

    assert result == {"known": False}
