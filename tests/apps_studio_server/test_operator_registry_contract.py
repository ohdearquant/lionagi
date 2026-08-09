# Copyright (c) 2023 - 2026, HaiyangLi <quantocean.li@gmail.com>
#
# SPDX-License-Identifier: Apache-2.0
"""Registry contract for the Studio Operator MCP tool surface.

Covers two things that the individual handler test files do not:
one, that `_TOOL_HANDLERS` still contains every tool it held before this
change plus every tool this change adds; two, that each newly added
handler never reaches a write-shaped method on the store it is given,
proving the "read-only" contract by exercising the handler rather than
by reading its source.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("fastapi")

from lionagi.studio.operator import application_mcp


@pytest.fixture(autouse=True)
def readable_store(monkeypatch):
    """The exercisers fake the carriers; this states the other half of the
    premise, that the store was reachable at all. The list handlers decline to
    answer when it was not, so without this they would short-circuit and the
    read-only exercise would never reach the handler body it is testing."""
    from lionagi.studio.services import _db

    monkeypatch.setattr(_db, "store_exists", lambda: True)


PRE_EXISTING_TOOL_NAMES = frozenset(
    {
        "list_recent_runs",
        "run_stats",
        "get_current_view",
        "list_schedules",
        "list_agents",
        "list_playbooks",
        "navigate",
        "prefill_schedule",
        "launch_playbook",
        "run_progress",
        "run_findings",
        "cancel_run",
        "resume_run",
        "rename_session",
    }
)

NEWLY_ADDED_TOOL_NAMES = frozenset(
    {
        "list_sessions",
        "session_detail",
        "session_signals",
        "get_invocation",
        "list_artifacts",
        "get_artifact",
    }
)


def test_pre_existing_tools_are_all_still_registered():
    missing = PRE_EXISTING_TOOL_NAMES - application_mcp._TOOL_HANDLERS.keys()
    assert missing == set()


def test_newly_added_tools_are_all_registered():
    missing = NEWLY_ADDED_TOOL_NAMES - application_mcp._TOOL_HANDLERS.keys()
    assert missing == set()


def test_registry_holds_no_unexpected_tool_names():
    expected = PRE_EXISTING_TOOL_NAMES | NEWLY_ADDED_TOOL_NAMES
    assert set(application_mcp._TOOL_HANDLERS.keys()) == expected


class _WriteMethodTrap:
    """A store/db double whose write-shaped attributes are `Mock`s.

    A handler that is truly read-only never touches any attribute in
    `write_mocks()`. Handing this double to a handler and then asserting
    zero calls on every write-shaped attribute is the exercised, not
    just-read, evidence that the handler makes no state-changing call.
    """

    def __init__(self, **read_returns: Any) -> None:
        self.insert = Mock(name="insert")
        self.update = Mock(name="update")
        self.upsert = Mock(name="upsert")
        self.delete = Mock(name="delete")
        self.save = Mock(name="save")
        self.commit = Mock(name="commit")
        self.write = Mock(name="write")
        self.execute = Mock(name="execute")
        self.execute_write = Mock(name="execute_write")
        self.create_session = Mock(name="create_session")
        self.create_branch = Mock(name="create_branch")
        self.mutate = Mock(name="mutate")
        self._read_returns = read_returns

    def write_mocks(self) -> list[Mock]:
        return [
            self.insert,
            self.update,
            self.upsert,
            self.delete,
            self.save,
            self.commit,
            self.write,
            self.execute,
            self.execute_write,
            self.create_session,
            self.create_branch,
            self.mutate,
        ]

    def assert_no_write_method_called(self) -> None:
        for mock in self.write_mocks():
            assert mock.call_count == 0, f"read-only handler called {mock._mock_name}"

    async def __aenter__(self) -> _WriteMethodTrap:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


SAMPLE_SESSION_ROW = {"id": "sess-1", "agent_name": "demo", "status": "completed"}
SAMPLE_INVOCATION_ROW = {
    "id": "inv-1",
    "skill": "demo",
    "sessions": [],
    "artifacts": [],
}
SAMPLE_ARTIFACT_ROW = {"id": "art-1", "name": "demo.txt", "content": "hello"}


async def _exercise_list_sessions(monkeypatch: pytest.MonkeyPatch, store: _WriteMethodTrap) -> None:
    from lionagi.studio.services import runs as runs_service
    from lionagi.studio.services import sessions as sessions_service

    async def list_runs(**_kwargs: Any) -> list[dict[str, Any]]:
        return [dict(SAMPLE_SESSION_ROW)]

    async def count_sessions(*_args: Any, **_kwargs: Any) -> int:
        return 1

    monkeypatch.setattr(runs_service, "list_runs", list_runs)
    monkeypatch.setattr(sessions_service, "count_sessions", count_sessions)
    result = await application_mcp.list_sessions({"limit": 10})
    assert result["sessions"]


async def _exercise_session_detail(
    monkeypatch: pytest.MonkeyPatch, store: _WriteMethodTrap
) -> None:
    from lionagi.studio.services import sessions as sessions_service

    async def get_session(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return dict(SAMPLE_SESSION_ROW)

    monkeypatch.setattr(sessions_service, "get_session", get_session)
    result = await application_mcp.session_detail({"session_id": "sess-1"})
    assert result["known"] is True


async def _exercise_session_signals(
    monkeypatch: pytest.MonkeyPatch, store: _WriteMethodTrap
) -> None:
    from lionagi.studio.services import signals as signals_service

    async def get_signals_after(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(signals_service, "get_signals_after", get_signals_after)
    result = await application_mcp.session_signals({"session_id": "sess-1"})
    assert result["known"] is True


async def _exercise_get_invocation(
    monkeypatch: pytest.MonkeyPatch, store: _WriteMethodTrap
) -> None:
    from lionagi.studio.services import invocations as invocations_service

    async def get_invocation(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return dict(SAMPLE_INVOCATION_ROW)

    monkeypatch.setattr(invocations_service, "get_invocation", get_invocation)
    result = await application_mcp.get_invocation({"invocation_id": "inv-1"})
    assert result["known"] is True


async def _exercise_list_artifacts(
    monkeypatch: pytest.MonkeyPatch, store: _WriteMethodTrap
) -> None:
    from lionagi.state import db as db_module
    from lionagi.studio.services import invocations as invocations_service

    store.list_artifacts_for_session = AsyncMock(return_value=[dict(SAMPLE_ARTIFACT_ROW)])
    store.list_artifacts_for_invocation = AsyncMock(return_value=[dict(SAMPLE_ARTIFACT_ROW)])
    monkeypatch.setattr(db_module, "StateDB", lambda *_a, **_k: store)
    monkeypatch.setattr(db_module, "read_only_open_supported", lambda: True)
    monkeypatch.setattr(db_module, "state_db_known_absent", lambda: False)
    monkeypatch.setattr(invocations_service, "_serialize_artifact", lambda row: dict(row))
    result = await application_mcp.list_artifacts({"session_id": "sess-1"})
    assert result["artifacts"]


async def _exercise_get_artifact(monkeypatch: pytest.MonkeyPatch, store: _WriteMethodTrap) -> None:
    from lionagi.studio.services import invocations as invocations_service

    async def get_artifact(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return dict(SAMPLE_ARTIFACT_ROW)

    monkeypatch.setattr(invocations_service, "get_artifact", get_artifact)
    result = await application_mcp.get_artifact({"artifact_id": "art-1"})
    assert result["known"] is True


_EXERCISERS = {
    "list_sessions": _exercise_list_sessions,
    "session_detail": _exercise_session_detail,
    "session_signals": _exercise_session_signals,
    "get_invocation": _exercise_get_invocation,
    "list_artifacts": _exercise_list_artifacts,
    "get_artifact": _exercise_get_artifact,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", sorted(NEWLY_ADDED_TOOL_NAMES))
async def test_newly_added_handler_calls_no_write_method(
    tool_name: str, monkeypatch: pytest.MonkeyPatch
):
    assert tool_name in application_mcp._TOOL_HANDLERS, "exerciser/registry drift"
    store = _WriteMethodTrap()
    await _EXERCISERS[tool_name](monkeypatch, store)
    store.assert_no_write_method_called()


@pytest.mark.asyncio
async def test_every_new_handler_exerciser_is_registered_exactly_once():
    assert set(_EXERCISERS.keys()) == NEWLY_ADDED_TOOL_NAMES


def test_fleet_attention_tool_has_no_registered_handler():
    """No Fleet-attention tool exists to test.

    The implementation record for this change states the capability was not
    built because no existing server-side read serves it (recorded as "no
    existing carrier"). This test pins that fact against the registry rather
    than silently skipping it: if a handler named for this capability is ever
    registered, this assertion starts failing and the happy-path/bound/
    redaction/empty-case tests this capability still needs can be written
    against a real handler instead of an assumed one.
    """
    attention_like = {
        name for name in application_mcp._TOOL_HANDLERS if "attention" in name.lower()
    }
    assert attention_like == set()
