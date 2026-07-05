# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the Leo (Studio operator agent) endpoints.

No network or real LLM calls — the Branch is monkey-patched with a fake ReAct().
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from lionagi.protocols.messages.action_response import ActionResponse

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402


def _make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Patch studio service roots and return a TestClient."""
    fake_db = tmp_path / "state.db"

    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.definitions as defs_mod
    import lionagi.studio.services.sessions as sessions_mod
    import lionagi.studio.services.shows as shows_mod
    import lionagi.studio.services.stats as stats_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(sessions_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(sessions_mod, "_DB", str(fake_db))
    monkeypatch.setattr(shows_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(shows_mod, "_DB", str(fake_db))
    monkeypatch.setattr(defs_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(defs_mod, "_DB", str(fake_db))
    monkeypatch.setattr(stats_mod, "DEFAULT_DB_PATH", fake_db)
    monkeypatch.setattr(stats_mod, "_DB", str(fake_db))

    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


@pytest.fixture(autouse=True)
def _clear_leo_sessions():
    """Reset the in-memory Leo session registry between tests."""
    from lionagi.studio.services import leo as leo_svc

    leo_svc._SESSIONS.clear()
    yield
    leo_svc._SESSIONS.clear()


# ---------------------------------------------------------------------------
# Session create
# ---------------------------------------------------------------------------


def test_leo_create_session(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    r = client.post("/api/leo/sessions")
    assert r.status_code == 200
    data = r.json()
    assert "id" in data
    assert isinstance(data["id"], str)
    assert len(data["id"]) == 36  # UUID


def test_leo_create_session_unique_ids(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    ids = {client.post("/api/leo/sessions").json()["id"] for _ in range(3)}
    assert len(ids) == 3


# ---------------------------------------------------------------------------
# 404 on unknown session
# ---------------------------------------------------------------------------


def test_leo_message_unknown_session(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    r = client.post(
        "/api/leo/sessions/does-not-exist/messages",
        json={"content": "hello"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Auth gate — the studio bearer-token middleware guards /api/leo like any
# other /api/* route; no per-route auth code, so this proves the mount point
# lands under the guarded prefix.
# ---------------------------------------------------------------------------


def test_leo_requires_bearer_when_token_set(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-leo-secret")
    from importlib import reload

    import lionagi.studio.app as app_mod

    reload(app_mod)
    client = TestClient(
        app_mod.app, raise_server_exceptions=False, base_url="http://127.0.0.1:8765"
    )
    try:
        r = client.post("/api/leo/sessions")
        assert r.status_code == 401
    finally:
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        reload(app_mod)


def test_leo_correct_token_not_401(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-leo-secret")
    from importlib import reload

    import lionagi.studio.app as app_mod

    reload(app_mod)
    client = TestClient(
        app_mod.app, raise_server_exceptions=False, base_url="http://127.0.0.1:8765"
    )
    try:
        r = client.post(
            "/api/leo/sessions",
            headers={"Authorization": "Bearer test-leo-secret"},
        )
        assert r.status_code == 200
    finally:
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        reload(app_mod)


# ---------------------------------------------------------------------------
# Tool registry shape
# ---------------------------------------------------------------------------


def test_leo_tool_registry_shape():
    from lionagi.studio.services.leo import _all_tools

    tools = _all_tools()
    names = [t.__name__ for t in tools]
    # Read-only tools
    assert "tool_list_runs" in names
    assert "tool_list_invocations" in names
    assert "tool_list_sessions" in names
    assert "tool_list_playbooks" in names
    assert "tool_get_playbook" in names
    assert "tool_list_schedules" in names
    assert "tool_studio_doctor" in names
    # UI-drive tools
    assert "tool_show_in_ui" in names
    assert "tool_prefill_schedule" in names
    # Mutating tools
    assert "tool_launch_playbook" in names
    assert "tool_create_playbook" in names
    assert "tool_run_maintenance" in names


# ---------------------------------------------------------------------------
# Proposed-action gating: mutating tools return proposals, never execute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_launch_playbook_returns_proposal():
    from lionagi.studio.services.leo import tool_launch_playbook

    result = await tool_launch_playbook("my-playbook")
    assert "proposed_action" in result
    pa = result["proposed_action"]
    assert pa["kind"] == "launch_playbook"
    assert pa["params"]["name"] == "my-playbook"
    assert "endpoint" in pa
    # Must not have triggered any network call or service mutation


@pytest.mark.asyncio
async def test_tool_create_playbook_returns_proposal():
    from lionagi.studio.services.leo import tool_create_playbook

    result = await tool_create_playbook("new-pb", description="A test playbook")
    assert "proposed_action" in result
    pa = result["proposed_action"]
    assert pa["kind"] == "create_playbook"
    assert pa["params"]["name"] == "new-pb"


@pytest.mark.asyncio
async def test_tool_run_maintenance_returns_proposal():
    from lionagi.studio.services.leo import tool_run_maintenance

    result = await tool_run_maintenance("vacuum")
    assert "proposed_action" in result
    pa = result["proposed_action"]
    assert pa["kind"] == "run_maintenance"
    assert pa["params"]["action"] == "vacuum"


@pytest.mark.asyncio
async def test_tool_run_maintenance_invalid_action():
    from lionagi.studio.services.leo import tool_run_maintenance

    result = await tool_run_maintenance("drop_tables")
    assert "error" in result
    assert "proposed_action" not in result


# ---------------------------------------------------------------------------
# UI-drive tools: declarative commands, no server-side effect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_show_in_ui_navigate_with_filter():
    from lionagi.studio.services.leo import tool_show_in_ui

    result = await tool_show_in_ui("history", status="failed")
    assert "ui_command" in result
    cmd = result["ui_command"]
    assert cmd["kind"] == "navigate"
    assert cmd["space"] == "history"
    assert cmd["params"] == {"status": "failed"}


@pytest.mark.asyncio
async def test_tool_show_in_ui_rejects_unknown_space():
    from lionagi.studio.services.leo import tool_show_in_ui

    result = await tool_show_in_ui("admin-console")
    assert "error" in result
    assert "ui_command" not in result


@pytest.mark.asyncio
async def test_tool_show_in_ui_rejects_unknown_status():
    from lionagi.studio.services.leo import tool_show_in_ui

    result = await tool_show_in_ui("history", status="exploded")
    assert "error" in result
    assert "ui_command" not in result


@pytest.mark.asyncio
async def test_tool_prefill_schedule_returns_command():
    from lionagi.studio.services.leo import tool_prefill_schedule

    result = await tool_prefill_schedule(
        "release-check",
        "0 9 * * *",
        "Check whether lionagi has a new release",
        description="Daily release watch",
    )
    assert "ui_command" in result
    cmd = result["ui_command"]
    assert cmd["kind"] == "prefill_schedule"
    assert cmd["space"] == "schedules"
    assert cmd["params"]["name"] == "release-check"
    assert cmd["params"]["cron"] == "0 9 * * *"
    assert "prompt" in cmd["params"]


# ---------------------------------------------------------------------------
# Message turn with a fake Branch (no LLM network)
# ---------------------------------------------------------------------------


def _fake_branch_with_response(text: str) -> MagicMock:
    """Build a mock Branch whose ReAct() returns `text`."""
    branch = MagicMock()
    branch.ReAct = AsyncMock(return_value=text)
    branch.messages = []  # no ActionResponse messages
    return branch


def test_leo_message_turn_text_response(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)

    # Create a session
    sid = client.post("/api/leo/sessions").json()["id"]

    # Inject a fake branch into the session registry
    from lionagi.studio.services import leo as leo_svc

    sess = leo_svc.get_session(sid)
    assert sess is not None
    sess.branch = _fake_branch_with_response("There are 3 running playbooks.")

    # Send a message and collect SSE
    r = client.post(
        f"/api/leo/sessions/{sid}/messages",
        json={"content": "How many runs are running?"},
    )
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]

    # Parse SSE frames
    events = _parse_sse(r.text)
    types = [e.get("type") for e in events]
    assert "text" in types
    assert "done" in types

    text_event = next(e for e in events if e.get("type") == "text")
    assert "3 running playbooks" in text_event["content"]


def test_leo_message_turn_proposed_action_surfaced(tmp_path, monkeypatch):
    """A mock branch that returns a proposed_action in an ActionResponse-like message."""
    client = _make_client(tmp_path, monkeypatch)
    sid = client.post("/api/leo/sessions").json()["id"]

    from lionagi.studio.services import leo as leo_svc

    sess = leo_svc.get_session(sid)
    assert sess is not None

    proposed = {
        "kind": "launch_playbook",
        "params": {"name": "ci-sweep"},
        "description": "Launch playbook 'ci-sweep'",
        "endpoint": "POST /api/launches/",
    }

    fake_msg = ActionResponse(
        content={"function": "tool_launch_playbook", "output": {"proposed_action": proposed}}
    )

    # A real Branch appends messages during the turn; the mock must do the
    # same because the router only scans messages added by the current turn.
    branch = MagicMock()
    branch.messages = []

    async def fake_turn(**_kwargs):
        branch.messages.append(fake_msg)
        return "I've surfaced a proposed action."

    branch.ReAct = AsyncMock(side_effect=fake_turn)
    sess.branch = branch

    r = client.post(
        f"/api/leo/sessions/{sid}/messages",
        json={"content": "Launch the ci-sweep playbook"},
    )
    assert r.status_code == 200
    events = _parse_sse(r.text)
    types = [e.get("type") for e in events]
    assert "proposed_action" in types
    assert "text" in types
    assert "done" in types

    pa_event = next(e for e in events if e.get("type") == "proposed_action")
    assert pa_event["action"]["kind"] == "launch_playbook"


def test_leo_message_turn_ui_command_surfaced(tmp_path, monkeypatch):
    """ui_command tool outputs stream as ui_command events before the text."""
    client = _make_client(tmp_path, monkeypatch)
    sid = client.post("/api/leo/sessions").json()["id"]

    from lionagi.studio.services import leo as leo_svc

    sess = leo_svc.get_session(sid)
    assert sess is not None

    command = {"kind": "navigate", "space": "history", "params": {"status": "failed"}}

    fake_msg = ActionResponse(
        content={"function": "tool_show_in_ui", "output": {"ui_command": command}}
    )

    branch = MagicMock()
    branch.messages = []

    async def fake_turn(**_kwargs):
        branch.messages.append(fake_msg)
        return "Here are the failed runs."

    branch.ReAct = AsyncMock(side_effect=fake_turn)
    sess.branch = branch

    r = client.post(
        f"/api/leo/sessions/{sid}/messages",
        json={"content": "what are some failed jobs recently"},
    )
    assert r.status_code == 200
    events = _parse_sse(r.text)
    types = [e.get("type") for e in events]
    assert "ui_command" in types
    assert "text" in types
    assert types.index("ui_command") < types.index("text")

    cmd_event = next(e for e in events if e.get("type") == "ui_command")
    assert cmd_event["command"] == command


def test_leo_prior_turn_proposals_not_reemitted(tmp_path, monkeypatch):
    """Proposals from earlier turns must not resurface on later turns."""
    client = _make_client(tmp_path, monkeypatch)
    sid = client.post("/api/leo/sessions").json()["id"]

    from lionagi.studio.services import leo as leo_svc

    sess = leo_svc.get_session(sid)
    assert sess is not None

    stale_msg = ActionResponse(
        content={
            "function": "tool_launch_playbook",
            "output": {
                "proposed_action": {"kind": "launch_playbook", "params": {}},
                "ui_command": {"kind": "navigate", "space": "history", "params": {}},
            },
        }
    )

    branch = MagicMock()
    branch.messages = [stale_msg]  # left over from a previous turn
    branch.ReAct = AsyncMock(return_value="Nothing new to propose.")
    sess.branch = branch

    r = client.post(
        f"/api/leo/sessions/{sid}/messages",
        json={"content": "Anything running?"},
    )
    assert r.status_code == 200
    events = _parse_sse(r.text)
    types = [e.get("type") for e in events]
    assert "proposed_action" not in types
    assert "ui_command" not in types
    assert "text" in types
    assert "done" in types


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_sse(body: str) -> list[dict[str, Any]]:
    """Parse a raw SSE body into a list of decoded JSON event dicts."""
    import json

    events = []
    for chunk in body.split("\n\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        for line in chunk.splitlines():
            if line.startswith("data:"):
                data = line[5:].strip()
                try:
                    events.append(json.loads(data))
                except json.JSONDecodeError:
                    pass
    return events
