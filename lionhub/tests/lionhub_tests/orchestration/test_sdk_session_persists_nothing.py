"""A Session used through the SDK writes nothing to the state store by default.

Persistence belongs to the hub run path, which installs its handlers on the run's
own session bus (``lionhub.orchestration.run_persistence``).
"""

from __future__ import annotations

import lionhub.state.db as state_db
from lionagi.hooks import HookPoint, build_session_bus
from lionagi.session.session import Session
from lionagi.testing import TestBranch


def _record_store_opens(monkeypatch) -> list[tuple]:
    opened: list[tuple] = []

    async def _get_shared_db(*args, **kwargs):
        opened.append((args, kwargs))
        raise RuntimeError("state store opened")

    monkeypatch.setattr(state_db, "get_shared_db", _get_shared_db)
    return opened


async def _emit_lifecycle(bus, session_id: str, branch_id: str) -> None:
    await bus.emit(HookPoint.SESSION_START, session_id=session_id, model="m", provider="p")
    await bus.emit(HookPoint.BRANCH_CREATE, session_id=session_id, branch_id=branch_id)
    await bus.emit(
        HookPoint.BRANCH_END, session_id=session_id, branch_id=branch_id, status="completed"
    )
    await bus.emit(HookPoint.SESSION_END, session_id=session_id, status="completed")


async def test_sdk_session_lifecycle_never_opens_the_state_store(monkeypatch):
    opened = _record_store_opens(monkeypatch)
    # A scripted branch keeps the default chat model from needing a provider lookup.
    session = Session(default_branch=TestBranch.from_text("unused"))

    await _emit_lifecycle(session.hooks, str(session.id), "branch-sdk-1")

    assert opened == []


async def test_a_bus_wired_for_persistence_does_open_the_state_store(monkeypatch):
    """Control: the recorder sees a store open when a persistence handler is wired."""
    opened = _record_store_opens(monkeypatch)
    bus = build_session_bus({"session.start": ["persist_session_start"]})

    try:
        await bus.emit(HookPoint.SESSION_START, session_id="session-sdk-2", model="m", provider="p")
    except RuntimeError:
        pass

    assert len(opened) == 1
