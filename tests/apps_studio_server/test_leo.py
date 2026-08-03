# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the retired Leo compatibility helpers.

The legacy HTTP routes must stay unregistered. No network or real LLM calls —
the compatibility Branch is monkey-patched with a fake ReAct().
"""

from __future__ import annotations

import asyncio
import time
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

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)

    from lionagi.studio.app import create_app

    return TestClient(create_app(), base_url="http://127.0.0.1:8765")


@pytest.fixture(autouse=True)
def _clear_leo_sessions():
    """Reset the in-memory Leo session registry between tests."""
    from lionagi.studio.services import leo as leo_svc

    leo_svc._SESSIONS.clear()
    yield
    leo_svc._SESSIONS.clear()


# ---------------------------------------------------------------------------
# Retired HTTP surface and direct session compatibility
# ---------------------------------------------------------------------------


def test_legacy_leo_routes_are_retired(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch) as client:
        create = client.post("/api/leo/sessions")
        send = client.post(
            "/api/leo/sessions/does-not-exist/messages",
            json={"content": "hello"},
        )
    assert create.status_code == 404
    assert send.status_code == 404


def test_leo_compatibility_session_unique_ids():
    from lionagi.studio.services import leo as leo_svc

    ids = {leo_svc.create_session().id for _ in range(3)}
    assert len(ids) == 3


# ---------------------------------------------------------------------------
# Session registry bounds: capacity eviction (LRU) and idle expiry
# ---------------------------------------------------------------------------


def test_session_registry_evicts_lru_over_cap(monkeypatch):
    from lionagi.studio.services import leo as leo_svc

    monkeypatch.setattr(leo_svc, "_MAX_SESSIONS", 3)

    ids = [leo_svc.create_session().id for _ in range(3)]
    now = time.time()
    leo_svc._SESSIONS[ids[0]].last_used_at = now + 10
    leo_svc._SESSIONS[ids[1]].last_used_at = now  # oldest -> evicted
    leo_svc._SESSIONS[ids[2]].last_used_at = now + 20

    new_sess = leo_svc.create_session()

    assert ids[1] not in leo_svc._SESSIONS
    assert ids[0] in leo_svc._SESSIONS
    assert ids[2] in leo_svc._SESSIONS
    assert new_sess.id in leo_svc._SESSIONS
    assert len(leo_svc._SESSIONS) == 3


def test_session_registry_skips_busy_session_when_evicting_lru(monkeypatch):
    """A session whose lock is held (mid-turn) must never be the eviction victim."""
    import asyncio

    from lionagi.studio.services import leo as leo_svc

    monkeypatch.setattr(leo_svc, "_MAX_SESSIONS", 3)

    ids = [leo_svc.create_session().id for _ in range(3)]
    now = time.time()
    leo_svc._SESSIONS[ids[0]].last_used_at = now + 10
    leo_svc._SESSIONS[ids[1]].last_used_at = now  # oldest, but busy
    leo_svc._SESSIONS[ids[2]].last_used_at = now + 20

    asyncio.run(leo_svc._SESSIONS[ids[1]].lock.acquire())

    new_sess = leo_svc.create_session()

    assert ids[1] in leo_svc._SESSIONS  # busy session survives even though it's LRU
    assert leo_svc._SESSIONS[ids[1]].lock.locked()
    assert ids[0] not in leo_svc._SESSIONS  # next-oldest idle session evicted instead
    assert ids[2] in leo_svc._SESSIONS
    assert new_sess.id in leo_svc._SESSIONS
    assert len(leo_svc._SESSIONS) == 3


def test_session_registry_all_busy_exceeds_cap_rather_than_evict(monkeypatch):
    """When every session is mid-turn, capacity is temporarily exceeded rather than evicting one."""
    import asyncio

    from lionagi.studio.services import leo as leo_svc

    monkeypatch.setattr(leo_svc, "_MAX_SESSIONS", 3)

    ids = [leo_svc.create_session().id for _ in range(3)]
    for sid in ids:
        asyncio.run(leo_svc._SESSIONS[sid].lock.acquire())

    new_sess = leo_svc.create_session()

    for sid in ids:
        assert sid in leo_svc._SESSIONS
        assert leo_svc._SESSIONS[sid].lock.locked()
    assert new_sess.id in leo_svc._SESSIONS
    assert len(leo_svc._SESSIONS) == 4


def test_session_registry_expires_idle_sessions():
    from lionagi.studio.services import leo as leo_svc

    sess = leo_svc.create_session()
    sess.last_used_at = time.time() - leo_svc._IDLE_EXPIRY_SECONDS - 1

    assert leo_svc.get_session(sess.id) is None
    assert sess.id not in leo_svc._SESSIONS


def test_session_registry_idle_eviction_runs_on_create():
    from lionagi.studio.services import leo as leo_svc

    stale = leo_svc.create_session()
    stale.last_used_at = time.time() - leo_svc._IDLE_EXPIRY_SECONDS - 1

    leo_svc.create_session()

    assert stale.id not in leo_svc._SESSIONS


# ---------------------------------------------------------------------------
# Direct compatibility handler rejects an unknown session
# ---------------------------------------------------------------------------


def test_leo_compatibility_message_unknown_session():
    from lionagi._errors import NotFoundError
    from lionagi.studio.services import leo as leo_svc

    with pytest.raises(NotFoundError):
        asyncio.run(
            leo_svc.send_leo_message_route(
                "does-not-exist",
                leo_svc._MessageBody(content="hello"),
            )
        )


# ---------------------------------------------------------------------------
# Auth gate — retired paths remain under the global /api/* bearer boundary,
# even though an authenticated caller receives a route-level 404.
# ---------------------------------------------------------------------------


def test_leo_requires_bearer_when_token_set(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-leo-secret")

    import lionagi.studio.app as app_mod

    # create_app() bakes the monkeypatched token into a fresh app instance;
    # unlike importlib.reload(app_mod), there is no shared singleton to
    # restore afterwards.
    app = app_mod.create_app()
    with TestClient(
        app,
        raise_server_exceptions=False,
        base_url="http://127.0.0.1:8765",
    ) as client:
        r = client.post("/api/leo/sessions")
    assert r.status_code == 401


def test_leo_correct_token_reaches_retired_route_404(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-leo-secret")

    import lionagi.studio.app as app_mod

    app = app_mod.create_app()
    with TestClient(
        app,
        raise_server_exceptions=False,
        base_url="http://127.0.0.1:8765",
    ) as client:
        r = client.post(
            "/api/leo/sessions",
            headers={"Authorization": "Bearer test-leo-secret"},
        )
    assert r.status_code == 404


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
async def test_tool_launch_playbook_returns_proposal(monkeypatch):
    from lionagi.studio.services import launches as launches_svc
    from lionagi.studio.services.leo import tool_launch_playbook

    mock_launch = AsyncMock()
    monkeypatch.setattr(launches_svc, "launch", mock_launch)

    result = await tool_launch_playbook("my-playbook")
    assert "proposed_action" in result
    pa = result["proposed_action"]
    assert pa["kind"] == "launch_playbook"
    assert pa["params"]["name"] == "my-playbook"
    assert "endpoint" in pa
    # Must not have triggered any network call or service mutation
    mock_launch.assert_not_called()


@pytest.mark.asyncio
async def test_tool_create_playbook_returns_proposal(monkeypatch):
    from lionagi.studio.services import playbooks as playbooks_svc
    from lionagi.studio.services.leo import tool_create_playbook

    mock_create = AsyncMock()
    mock_update = MagicMock()
    monkeypatch.setattr(playbooks_svc, "create_playbook", mock_create)
    monkeypatch.setattr(playbooks_svc, "update_playbook", mock_update)

    result = await tool_create_playbook("new-pb", description="A test playbook")
    assert "proposed_action" in result
    pa = result["proposed_action"]
    assert pa["kind"] == "create_playbook"
    assert pa["params"]["name"] == "new-pb"
    mock_create.assert_not_called()
    mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_tool_run_maintenance_returns_proposal(monkeypatch):
    from lionagi.studio.services import db_maintenance as db_maint_svc
    from lionagi.studio.services.leo import tool_run_maintenance

    mock_vacuum = AsyncMock()
    mock_checkpoint = AsyncMock()
    mock_prune = AsyncMock()
    monkeypatch.setattr(db_maint_svc, "vacuum_state_db", mock_vacuum)
    monkeypatch.setattr(db_maint_svc, "checkpoint_state_db", mock_checkpoint)
    monkeypatch.setattr(db_maint_svc, "prune_old_data", mock_prune)

    result = await tool_run_maintenance("vacuum")
    assert "proposed_action" in result
    pa = result["proposed_action"]
    assert pa["kind"] == "run_maintenance"
    assert pa["params"]["action"] == "vacuum"
    mock_vacuum.assert_not_called()
    mock_checkpoint.assert_not_called()
    mock_prune.assert_not_called()


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

    result = await tool_show_in_ui("fleet", status="failed")
    assert "ui_command" in result
    cmd = result["ui_command"]
    assert cmd["kind"] == "navigate"
    assert cmd["space"] == "fleet"
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

    result = await tool_show_in_ui("fleet", status="exploded")
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


def _run_compatibility_turn(sess: Any, content: str) -> list[dict[str, Any]]:
    from lionagi.studio.services import leo as leo_svc

    async def collect() -> str:
        await sess.lock.acquire()
        return "".join([chunk async for chunk in leo_svc._run_turn_locked(sess, content)])

    return _parse_sse(asyncio.run(collect()))


def test_leo_message_turn_text_response():
    from lionagi.studio.services import leo as leo_svc

    sess = leo_svc.create_session()
    sess.branch = _fake_branch_with_response("There are 3 running playbooks.")

    events = _run_compatibility_turn(sess, "How many runs are running?")
    types = [e.get("type") for e in events]
    assert "text" in types
    assert "done" in types

    text_event = next(e for e in events if e.get("type") == "text")
    assert "3 running playbooks" in text_event["content"]


def test_leo_message_turn_proposed_action_surfaced():
    """A mock branch that returns a proposed_action in an ActionResponse-like message."""
    from lionagi.studio.services import leo as leo_svc

    sess = leo_svc.create_session()

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

    events = _run_compatibility_turn(sess, "Launch the ci-sweep playbook")
    types = [e.get("type") for e in events]
    assert "proposed_action" in types
    assert "text" in types
    assert "done" in types

    pa_event = next(e for e in events if e.get("type") == "proposed_action")
    assert pa_event["action"]["kind"] == "launch_playbook"


def test_leo_message_turn_ui_command_surfaced():
    """ui_command tool outputs stream as ui_command events before the text."""
    from lionagi.studio.services import leo as leo_svc

    sess = leo_svc.create_session()

    command = {"kind": "navigate", "space": "fleet", "params": {"status": "failed"}}

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

    events = _run_compatibility_turn(sess, "what are some failed jobs recently")
    types = [e.get("type") for e in events]
    assert "ui_command" in types
    assert "text" in types
    assert types.index("ui_command") < types.index("text")

    cmd_event = next(e for e in events if e.get("type") == "ui_command")
    assert cmd_event["command"] == command


def test_leo_prior_turn_proposals_not_reemitted():
    """Proposals from earlier turns must not resurface on later turns."""
    from lionagi.studio.services import leo as leo_svc

    sess = leo_svc.create_session()

    stale_msg = ActionResponse(
        content={
            "function": "tool_launch_playbook",
            "output": {
                "proposed_action": {"kind": "launch_playbook", "params": {}},
                "ui_command": {"kind": "navigate", "space": "fleet", "params": {}},
            },
        }
    )

    branch = MagicMock()
    branch.messages = [stale_msg]  # left over from a previous turn
    branch.ReAct = AsyncMock(return_value="Nothing new to propose.")
    sess.branch = branch

    events = _run_compatibility_turn(sess, "Anything running?")
    types = [e.get("type") for e in events]
    assert "proposed_action" not in types
    assert "ui_command" not in types
    assert "text" in types
    assert "done" in types


# ---------------------------------------------------------------------------
# Compatibility handler rejects a second turn while the session is busy
# ---------------------------------------------------------------------------


def test_leo_concurrent_turn_second_gets_409():
    from fastapi import HTTPException

    from lionagi.studio.services import leo as leo_svc

    async def scenario() -> None:
        sess = leo_svc.create_session()
        await sess.lock.acquire()
        try:
            with pytest.raises(HTTPException) as error:
                await leo_svc.send_leo_message_route(
                    sess.id,
                    leo_svc._MessageBody(content="hello"),
                )
            assert error.value.status_code == 409
        finally:
            sess.lock.release()

    asyncio.run(scenario())


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
