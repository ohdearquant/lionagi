# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Backend end-to-end test for the Studio workflow execution bridge (slice 1).

Builds a small WorkflowDef (input -> chat -> engine, with one conditioned
edge), runs it through the real compile -> Session.flow -> persist vertical,
and asserts it produces a real run whose node_metadata.early_graph carries
the authored node ids and edges (the data get_session()["graph"] reads).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")
pytest.importorskip("fastapi", reason="studio extra not installed")


def _spec() -> dict[str, Any]:
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
                "config": {"engine_def_id": "PLACEHOLDER"},
            },
        ],
        "edges": [
            {"id": "e1", "from": "in", "to": "chat1"},
            {"id": "e2", "from": "chat1", "to": "eng1", "condition": "result != None"},
        ],
        "inputs": ["topic"],
        "outputs": ["summary"],
    }


class _FakeEngine:
    """Stand-in for a real Engine (research/review/coding/...) — no network calls."""

    calls: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def run(self, spec_input: str, *, session: Any = None, **kwargs: Any) -> dict[str, Any]:
        _FakeEngine.calls.append({"spec_input": spec_input, "kwargs": kwargs})
        return {"echo": spec_input}


def _mock_chat_branch(name: str = "workflow-default"):
    """Branch whose chat_model is mocked — copies the convention already
    established in tests/operations/test_edge_conditions_tdd.py."""
    from lionagi.protocols.generic.event import EventStatus
    from lionagi.session.branch import Branch
    from lionagi.testing import LionAGIMockFactory

    branch = Branch(user="test_user", name=name)

    async def _fake_invoke(**kwargs):
        return LionAGIMockFactory.create_api_calling_mock(
            response_data="go",
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
    # sessions.py freezes `_DB = str(DEFAULT_DB_PATH)` at import time (module
    # constant, not re-read per call) — both attrs need patching, matching the
    # convention already established in test_admin.py.
    monkeypatch.setattr(sessions_svc, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_svc, "_DB", str(db_path))

    import lionagi.cli.engine as cli_engine

    monkeypatch.setitem(
        cli_engine._KIND_META,
        "research",
        {
            **cli_engine._KIND_META["research"],
            "cls_path": ("tests.apps_studio_server.test_workflow_run", "_FakeEngine"),
        },
    )
    return wf_svc, engine_defs_svc


async def test_workflow_run_end_to_end(patched_env):
    wf_svc, engine_defs_svc = patched_env
    _FakeEngine.calls = []

    engine_def = await engine_defs_svc.create_engine_def(
        {"name": "research-eng", "kind": "research"}
    )

    spec = _spec()
    spec["nodes"][2]["config"]["engine_def_id"] = engine_def["id"]
    created = await wf_svc.create_workflow_def({"name": "e2e-flow", "spec_json": spec})
    def_id = created["id"]

    from lionagi.session.session import Session
    from lionagi.studio.services.workflow_run import run_workflow_def

    mock_branch = _mock_chat_branch()
    session = Session(default_branch=mock_branch)

    result = await run_workflow_def(def_id, {"topic": "GQA"}, _session=session)

    assert result["status"] == "completed"
    assert len(_FakeEngine.calls) == 1
    assert _FakeEngine.calls[0]["spec_input"]  # the chat node's result flowed into the engine

    run_id = result["run_id"]
    assert run_id == str(session.id)

    from lionagi.studio.services.sessions import get_session

    session_row = await get_session(run_id)
    assert session_row is not None
    assert session_row["status"] == "completed"
    assert session_row["invocation_kind"] == "flow"

    graph = session_row["graph"]
    assert graph is not None, "node_metadata.early_graph must render through get_session()['graph']"
    node_ids = {n["id"] for n in graph["nodes"]}
    assert node_ids == {"in", "chat1", "eng1"}  # the AUTHORED ids, not internal Operation UUIDs

    edges_by_id = {e["id"]: e for e in graph["edges"]}
    assert set(edges_by_id) == {"e1", "e2"}
    assert edges_by_id["e2"]["condition"] == "result != None"
    assert edges_by_id["e2"]["mode"] == "code"


async def test_workflow_run_not_found_raises(patched_env):
    from lionagi.studio.services.workflow_run import WorkflowNotFoundError, run_workflow_def

    with pytest.raises(WorkflowNotFoundError):
        await run_workflow_def("does-not-exist")


async def test_workflow_run_compile_error_surfaces_node_id(patched_env):
    wf_svc, engine_defs_svc = patched_env
    from lionagi.studio.services.workflow_compile import WorkflowCompileError
    from lionagi.studio.services.workflow_run import run_workflow_def

    engine_def = await engine_defs_svc.create_engine_def({"name": "eng-a", "kind": "research"})
    spec = _spec()
    spec["nodes"][2]["config"]["engine_def_id"] = engine_def["id"]
    spec["edges"][1]["condition"] = "__import__('os')"
    created = await wf_svc.create_workflow_def({"name": "bad-cond-flow", "spec_json": spec})

    with pytest.raises(WorkflowCompileError) as exc_info:
        await run_workflow_def(created["id"])
    assert exc_info.value.edge_id == "e2"


async def test_run_route_returns_structured_422_on_compile_error(patched_env):
    from fastapi import HTTPException

    wf_svc, engine_defs_svc = patched_env
    from lionagi.studio.services.workflow_defs import RunWorkflowDefRequest, run_workflow_def_route

    engine_def = await engine_defs_svc.create_engine_def({"name": "eng-b", "kind": "research"})
    spec = _spec()
    spec["nodes"][2]["config"]["engine_def_id"] = engine_def["id"]
    spec["edges"][1]["condition"] = "__import__('os')"
    created = await wf_svc.create_workflow_def({"name": "bad-cond-route", "spec_json": spec})

    with pytest.raises(HTTPException) as exc_info:
        await run_workflow_def_route(created["id"], RunWorkflowDefRequest(inputs=None))
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["edge_id"] == "e2"


async def test_run_route_returns_404_for_missing_def(patched_env):
    from fastapi import HTTPException

    from lionagi.studio.services.workflow_defs import RunWorkflowDefRequest, run_workflow_def_route

    with pytest.raises(HTTPException) as exc_info:
        await run_workflow_def_route("nonexistent", RunWorkflowDefRequest(inputs=None))
    assert exc_info.value.status_code == 404
