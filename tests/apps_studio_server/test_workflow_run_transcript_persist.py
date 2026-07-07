# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Transcript-persistence slice: chat-node turns + engine sub-agent branches.

Two holes closed together (both surfaced by the same live demo run):

1. Engine sub-agent branches (``Engine.make_agent``) were included into the
   session but never got ``register_branch_hook`` wired for them, unlike
   flow-cloned branches (which already get it via ``session.flow(...,
   on_branch_created=...)``). Fixed by threading an ``on_branch_created``
   callback through ``EngineRun``/``Engine.run``/``make_engine_operation``.
2. The Studio "chat" node compiled to the native ``chat`` Branch operation,
   which by design never calls ``a_add_message`` -- so even though the
   branch it ran on already had a persistence hook, there was nothing for
   the hook to observe. Fixed with ``Branch.chat_and_record`` (records the
   turn the same way ``communicate()`` does) and compiling chat nodes to
   that operation name instead of the native ``chat``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")
pytest.importorskip("fastapi", reason="studio extra not installed")


def _spec(engine_def_id: str = "PLACEHOLDER") -> dict[str, Any]:
    return {
        "version": 1,
        "nodes": [
            {"id": "in", "kind": "input", "label": "Input", "pos": {"x": 0, "y": 0}},
            {
                "id": "chat1",
                "kind": "chat",
                "label": "Draft",
                "pos": {"x": 150, "y": 0},
                "config": {"prompt": "Draft a summary."},
            },
            {
                "id": "eng1",
                "kind": "engine",
                "label": "Research",
                "pos": {"x": 300, "y": 0},
                "config": {"engine_def_id": engine_def_id},
            },
        ],
        "edges": [
            {"id": "e1", "from": "in", "to": "chat1"},
            {"id": "e2", "from": "chat1", "to": "eng1", "condition": "result != None"},
        ],
        "inputs": ["topic"],
        "outputs": ["summary"],
    }


class _FakeSubAgentEngine:
    """Stand-in Engine: spawns one sub-agent branch through the SAME seam a
    real Engine (research/review/coding/...) uses -- make_agent's
    include_branches + on_branch_created -- then adds messages to it via the
    hooked async add-path, exactly like a real ``branch.operate()`` call
    inside ``operate_with_repair`` would."""

    calls: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def run(
        self,
        spec_input: str,
        *,
        session: Any = None,
        on_branch_created: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        from lionagi.session.branch import Branch

        sub = Branch(name="researcher-1")
        session.include_branches(sub)
        if on_branch_created is not None:
            on_branch_created(sub)
        # Messages added AFTER the hook is wired -- proving the hook (fired at
        # include-time, before any operate()) catches the whole transcript.
        await sub.msgs.a_add_message(instruction="investigate the topic")
        await sub.msgs.a_add_message(assistant_response="findings: grouped-query attention")

        _FakeSubAgentEngine.calls.append({"spec_input": spec_input, "sub_branch_id": str(sub.id)})
        return {"echo": spec_input}


def _mock_chat_branch(name: str = "workflow-default"):
    """Same convention as test_workflow_run.py / test_workflow_run_persist.py."""
    from lionagi.protocols.generic.event import EventStatus
    from lionagi.session.branch import Branch
    from lionagi.testing import LionAGIMockFactory

    branch = Branch(user="test_user", name=name)

    async def _fake_invoke(**kwargs):
        return LionAGIMockFactory.create_api_calling_mock(
            response_data="a one-paragraph summary",
            status=EventStatus.COMPLETED,
            model="gpt-4-mini",
        )

    mock_chat_model = LionAGIMockFactory.create_mocked_imodel(
        provider="openai", model="gpt-4-mini", response="overridden-below"
    )
    mock_chat_model.invoke = AsyncMock(side_effect=_fake_invoke)
    branch.chat_model = mock_chat_model
    return branch


@pytest.fixture
def patched_env(tmp_path: Path, monkeypatch):
    import lionagi.state.db as db_mod
    import lionagi.studio.services.engine_defs as engine_defs_svc
    import lionagi.studio.services.sessions as sessions_svc
    import lionagi.studio.services.workflow_defs as wf_svc

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(wf_svc, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(engine_defs_svc, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_svc, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_svc, "_DB", str(db_path))

    import lionagi.cli.engine as cli_engine

    monkeypatch.setitem(
        cli_engine._KIND_META,
        "research",
        {
            **cli_engine._KIND_META["research"],
            "cls_path": (
                "tests.apps_studio_server.test_workflow_run_transcript_persist",
                "_FakeSubAgentEngine",
            ),
        },
    )
    return wf_svc, engine_defs_svc, db_path


async def test_chat_turn_and_engine_subagent_branch_both_persist(patched_env):
    """The demo-run regression this slice closes: before the fix,
    ``list_branches(run_id)`` came back empty even after a real 4-agent run.
    After the fix: the chat branch has a persisted user+assistant turn, and
    the engine's sub-agent branch has its own persisted transcript -- both
    under the SAME run/session id.
    """
    wf_svc, engine_defs_svc, db_path = patched_env
    _FakeSubAgentEngine.calls = []

    engine_def = await engine_defs_svc.create_engine_def(
        {"name": "transcript-eng", "kind": "research"}
    )
    spec = _spec(engine_def["id"])
    created = await wf_svc.create_workflow_def({"name": "transcript-flow", "spec_json": spec})

    from lionagi.session.session import Session
    from lionagi.studio.services.workflow_run import run_workflow_def

    mock_branch = _mock_chat_branch()
    session = Session(default_branch=mock_branch)

    result = await run_workflow_def(created["id"], {"topic": "GQA"}, _session=session)
    assert result["status"] == "completed"
    run_id = result["run_id"]
    assert run_id == str(session.id)

    assert len(_FakeSubAgentEngine.calls) == 1
    sub_branch_id = _FakeSubAgentEngine.calls[0]["sub_branch_id"]
    assert sub_branch_id != str(mock_branch.id)

    from lionagi.state.db import StateDB

    db = StateDB(db_path)
    await db.open()
    try:
        branches = await db.list_branches(run_id)
        branch_ids = {b["id"] for b in branches}
        assert str(mock_branch.id) in branch_ids, (
            "the chat node's branch row is missing -- BEFORE this fix "
            "list_branches(run_id) came back empty for a run with a real "
            "chat + engine transcript"
        )
        assert sub_branch_id in branch_ids, (
            "the engine sub-agent branch row is missing -- include_branches() "
            "does not fire on_branch_created on its own, so without the "
            "EngineRun/Engine.run/make_engine_operation wiring this branch "
            "is never registered for persistence"
        )

        chat_messages = await db.get_branch_messages(str(mock_branch.id))
        chat_roles = {m.get("role") for m in chat_messages}
        assert {"user", "assistant"} <= chat_roles, (
            f"chat node turn must persist both sides; got roles {chat_roles!r} "
            f"from {chat_messages!r}"
        )

        sub_messages = await db.get_branch_messages(sub_branch_id)
        assert len(sub_messages) == 2, (
            "engine sub-agent branch must persist its full transcript, "
            f"including messages added before make_agent returns; got {sub_messages!r}"
        )
        sub_roles = {m.get("role") for m in sub_messages}
        assert {"user", "assistant"} <= sub_roles
    finally:
        await db.close()


async def test_chat_and_record_returns_same_text_shape_as_chat(patched_env):
    """The chat node's compiled output must be unchanged (plain text via
    operation_results) -- only the transcript is new. Exercise
    Branch.chat_and_record directly against the mocked model."""
    branch = _mock_chat_branch()

    text = await branch.chat_and_record(instruction="say hi")
    assert isinstance(text, str)
    assert text == "a one-paragraph summary"

    # The turn is now on the branch (chat() alone would leave this empty).
    assert len(branch.messages) == 2
    roles = {m.role.value if hasattr(m.role, "value") else m.role for m in branch.messages}
    assert {"user", "assistant"} <= roles
