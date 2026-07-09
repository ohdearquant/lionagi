# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Backend end-to-end test for the Studio workflow execution bridge (slice 1).

Builds a small WorkflowDef (input -> chat -> engine, with one conditioned
edge), runs it through the real compile -> Session.flow -> persist vertical,
and asserts it produces a real run whose node_metadata.early_graph carries
the authored node ids and edges (the data get_session()["graph"] reads).
"""

from __future__ import annotations

import asyncio
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


class _AllAgentsFailedFakeEngine:
    """Stand-in for an Engine whose every sub-agent terminally errored (e.g.
    missing API key) — sets the same diagnostics EngineRun/Engine.run() would
    after a real all-agent-failed run, without any network calls."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def run(self, spec_input: str, *, session: Any = None, **kwargs: Any) -> str:
        self._agent_errors = ["worker-1: API key is required", "worker-2: API key is required"]
        self._total_agent_failure = True
        return ""


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
    monkeypatch.setitem(
        cli_engine._KIND_META,
        "coding",
        {
            **cli_engine._KIND_META["coding"],
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


async def test_workflow_run_all_agents_failed_reports_failed_not_completed(
    patched_env, monkeypatch
):
    """An engine node whose every sub-agent terminally errored (all-auth-failed)
    must surface as an {"error": ...} operation result and a run status of
    'failed', not silently report 'completed' with an empty result."""
    wf_svc, engine_defs_svc = patched_env

    import lionagi.cli.engine as cli_engine

    monkeypatch.setitem(
        cli_engine._KIND_META,
        "research",
        {
            **cli_engine._KIND_META["research"],
            "cls_path": (
                "tests.apps_studio_server.test_workflow_run",
                "_AllAgentsFailedFakeEngine",
            ),
        },
    )

    engine_def = await engine_defs_svc.create_engine_def(
        {"name": "all-fail-eng", "kind": "research"}
    )

    spec = _spec()
    spec["nodes"][2]["config"]["engine_def_id"] = engine_def["id"]
    created = await wf_svc.create_workflow_def({"name": "all-fail-flow", "spec_json": spec})
    def_id = created["id"]

    from lionagi.session.session import Session
    from lionagi.studio.services.workflow_run import run_workflow_def

    mock_branch = _mock_chat_branch()
    session = Session(default_branch=mock_branch)

    result = await run_workflow_def(def_id, {"topic": "GQA"}, _session=session)

    assert result["status"] == "failed"

    from lionagi.studio.services.sessions import get_session

    session_row = await get_session(result["run_id"])
    assert session_row is not None
    assert session_row["status"] == "failed"


async def test_workflow_run_persists_node_lifecycle_signals(patched_env, tmp_path):
    """The authored workflow DAG must emit per-node lifecycle signals.

    run_workflow_def drives session.flow directly (bypassing the engine, the
    usual source of these signals). Without wiring on_progress the run persists
    structure + results but no node-progress rows, so RunDetail/SSE cannot show
    the DAG nodes moving to running/completed. This asserts NodeStarted and
    NodeCompleted rows land in session_signals for every executable node, and
    that their queued signals carry the AUTHORED node ids (reference_id) so the
    animated run-DAG boxes correlate to what the human drew in the designer.
    """
    wf_svc, engine_defs_svc = patched_env
    _FakeEngine.calls = []

    engine_def = await engine_defs_svc.create_engine_def({"name": "sig-eng", "kind": "research"})
    spec = _spec()
    spec["nodes"][2]["config"]["engine_def_id"] = engine_def["id"]
    created = await wf_svc.create_workflow_def({"name": "sig-flow", "spec_json": spec})

    from lionagi.session.session import Session
    from lionagi.studio.services.workflow_run import run_workflow_def

    session = Session(default_branch=_mock_chat_branch())
    result = await run_workflow_def(created["id"], {"topic": "GQA"}, _session=session)
    assert result["status"] == "completed"

    # Read signals back from a FRESH StateDB connection (the run's own db is
    # closed by teardown) — the same path RunDetail/SSE reads.
    from lionagi.state.db import StateDB

    db = StateDB(tmp_path / "state.db")
    await db.open()
    try:
        signals = await db.get_session_signals_after(str(session.id), 0)
    finally:
        await db.close()

    by_kind: dict[str, set[str]] = {}
    for s in signals:
        by_kind.setdefault(s["kind"], set()).add(s["op_id"])

    # The two executable nodes (chat1, eng1 — the "input" node compiles to no
    # Operation) must each report started AND completed.
    assert "NodeStarted" in by_kind and "NodeCompleted" in by_kind, (
        f"missing lifecycle signals; got kinds {sorted(by_kind)}"
    )
    assert len(by_kind["NodeStarted"]) == 2
    assert by_kind["NodeStarted"] == by_kind["NodeCompleted"], (
        "every started node must also complete"
    )

    # EVERY lifecycle signal — not just queued — must name the authored ids, so
    # a live/SSE consumer can map a started/completed row back to the box the
    # human drew without waiting for the queued row. (The executor names
    # started/completed after the branch; flow_progress_signals overrides it
    # with the node's reference_id.)
    for kind in ("NodeQueued", "NodeStarted", "NodeCompleted"):
        names = {s["payload"].get("name") for s in signals if s["kind"] == kind}
        assert {"chat1", "eng1"} <= names, (
            f"{kind} signals must carry authored node ids; got {names}"
        )


async def test_cancelled_run_is_recorded_as_cancelled(patched_env, monkeypatch):
    """If session.flow is cancelled mid-run (Studio request/task cancelled),
    CancelledError (a BaseException) bypasses the `except Exception` handler.
    The run must be recorded as 'cancelled', not left at the optimistic
    'completed' default, and the cancellation must re-propagate.
    """
    wf_svc, engine_defs_svc = patched_env

    engine_def = await engine_defs_svc.create_engine_def({"name": "cancel-eng", "kind": "research"})
    spec = _spec()
    spec["nodes"][2]["config"]["engine_def_id"] = engine_def["id"]
    created = await wf_svc.create_workflow_def({"name": "cancel-flow", "spec_json": spec})
    def_id = created["id"]

    from lionagi.session.session import Session
    from lionagi.studio.services.workflow_run import run_workflow_def

    session = Session(default_branch=_mock_chat_branch())

    async def _cancelled_flow(self, *args, **kwargs):
        raise asyncio.CancelledError

    # Session is a pydantic model (no instance-attr assignment); patch the
    # bound method on the class for this test only.
    monkeypatch.setattr(Session, "flow", _cancelled_flow)

    with pytest.raises(asyncio.CancelledError):
        await run_workflow_def(def_id, {"topic": "GQA"}, _session=session)

    from lionagi.studio.services.sessions import get_session

    row = await get_session(str(session.id))
    assert row is not None
    assert row["status"] == "cancelled"


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


async def test_workflow_run_bare_chat_model_rejected_at_compile_defense_in_depth(patched_env):
    """A row saved with a bare config.model BEFORE the write-path validation
    existed (or written directly, bypassing it) must still be caught at
    compile time — the write-path check alone is not sufficient defense."""
    wf_svc, engine_defs_svc = patched_env
    from lionagi.studio.services.workflow_compile import WorkflowCompileError
    from lionagi.studio.services.workflow_run import run_workflow_def

    engine_def = await engine_defs_svc.create_engine_def({"name": "eng-c", "kind": "research"})
    spec = _spec()
    spec["nodes"][2]["config"]["engine_def_id"] = engine_def["id"]
    spec["nodes"][1]["config"]["model"] = "gpt-4.1-mini"  # bare — no provider prefix

    import time
    import uuid

    from lionagi.state.db import StateDB

    def_id = uuid.uuid4().hex[:12]
    now = time.time()
    async with StateDB() as db:
        await db.create_workflow_def(
            {
                "id": def_id,
                "name": "legacy-bare-model",
                "description": None,
                "spec_json": spec,
                "created_at": now,
                "updated_at": now,
            }
        )

    with pytest.raises(WorkflowCompileError) as exc_info:
        await run_workflow_def(def_id)
    assert exc_info.value.node_id == "chat1"
    assert "chat1" in str(exc_info.value)


# ─── Per-node cwd (D-F1) end-to-end ─────────────────────────────────────────


async def test_workflow_run_node_cwd_reaches_engine_invocation(patched_env, tmp_path):
    """A contained, relative node cwd must resolve to an absolute path under
    base_dir and reach the engine's run(workspace=...) call — the actual
    provider seam (make_engine_operation._engine_op forwards it as
    run_kwargs['workspace'] for coding engine nodes)."""
    wf_svc, engine_defs_svc = patched_env
    _FakeEngine.calls = []

    sub = tmp_path / "worktree"
    sub.mkdir()

    engine_def = await engine_defs_svc.create_engine_def(
        {"name": "coding-eng", "kind": "coding", "options": {"test_cmd": "pytest"}}
    )
    spec = _spec()
    spec["nodes"][2]["config"]["engine_def_id"] = engine_def["id"]
    spec["nodes"][2]["config"]["cwd"] = "worktree"
    created = await wf_svc.create_workflow_def({"name": "cwd-flow", "spec_json": spec})

    from lionagi.session.session import Session
    from lionagi.studio.services.workflow_run import run_workflow_def

    session = Session(default_branch=_mock_chat_branch())
    result = await run_workflow_def(
        created["id"], {"topic": "GQA"}, base_dir=str(tmp_path), _session=session
    )

    assert result["status"] == "completed"
    assert len(_FakeEngine.calls) == 1
    assert _FakeEngine.calls[0]["kwargs"]["workspace"] == str(sub.resolve())


async def test_workflow_run_node_cwd_without_base_dir_rejected(patched_env, tmp_path):
    wf_svc, engine_defs_svc = patched_env

    engine_def = await engine_defs_svc.create_engine_def(
        {"name": "coding-eng-2", "kind": "coding", "options": {"test_cmd": "pytest"}}
    )
    spec = _spec()
    spec["nodes"][2]["config"]["engine_def_id"] = engine_def["id"]
    spec["nodes"][2]["config"]["cwd"] = "worktree"
    created = await wf_svc.create_workflow_def({"name": "cwd-no-basedir-flow", "spec_json": spec})

    from lionagi.studio.services.workflow_compile import WorkflowCompileError
    from lionagi.studio.services.workflow_run import run_workflow_def

    with pytest.raises(WorkflowCompileError) as exc_info:
        await run_workflow_def(created["id"])
    assert exc_info.value.node_id == "eng1"
    assert "base_dir" in str(exc_info.value)


async def test_workflow_run_node_absolute_cwd_outside_base_dir_rejected(patched_env, tmp_path):
    wf_svc, engine_defs_svc = patched_env

    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    engine_def = await engine_defs_svc.create_engine_def(
        {"name": "coding-eng-3", "kind": "coding", "options": {"test_cmd": "pytest"}}
    )
    spec = _spec()
    spec["nodes"][2]["config"]["engine_def_id"] = engine_def["id"]
    spec["nodes"][2]["config"]["cwd"] = str(outside)
    created = await wf_svc.create_workflow_def({"name": "cwd-outside-flow", "spec_json": spec})

    from lionagi.studio.services.workflow_compile import WorkflowCompileError
    from lionagi.studio.services.workflow_run import run_workflow_def

    with pytest.raises(WorkflowCompileError) as exc_info:
        await run_workflow_def(created["id"], base_dir=str(base))
    assert exc_info.value.node_id == "eng1"
    assert "escapes" in str(exc_info.value)


async def test_workflow_run_node_absolute_cwd_inside_base_dir_accepted(patched_env, tmp_path):
    wf_svc, engine_defs_svc = patched_env
    _FakeEngine.calls = []

    sub = tmp_path / "worktree"
    sub.mkdir()

    engine_def = await engine_defs_svc.create_engine_def(
        {"name": "coding-eng-4", "kind": "coding", "options": {"test_cmd": "pytest"}}
    )
    spec = _spec()
    spec["nodes"][2]["config"]["engine_def_id"] = engine_def["id"]
    spec["nodes"][2]["config"]["cwd"] = str(sub)
    created = await wf_svc.create_workflow_def({"name": "cwd-abs-inside-flow", "spec_json": spec})

    from lionagi.session.session import Session
    from lionagi.studio.services.workflow_run import run_workflow_def

    session = Session(default_branch=_mock_chat_branch())
    result = await run_workflow_def(created["id"], base_dir=str(tmp_path), _session=session)

    assert result["status"] == "completed"
    assert _FakeEngine.calls[0]["kwargs"]["workspace"] == str(sub.resolve())


async def test_workflow_run_spec_level_base_dir_rejected(patched_env):
    """Defense in depth: a def row that somehow carries a top-level base_dir
    field (bypassing the write-path check, or written directly) must still be
    caught at compile/run time — see test_workflow_run_bare_chat_model_rejected_at_compile_defense_in_depth
    for the identical precedent with config.model."""
    wf_svc, engine_defs_svc = patched_env

    engine_def = await engine_defs_svc.create_engine_def(
        {"name": "eng-basedir", "kind": "research"}
    )
    spec = _spec()
    spec["nodes"][2]["config"]["engine_def_id"] = engine_def["id"]
    spec["base_dir"] = "/tmp/hostile"

    import time
    import uuid

    from lionagi.state.db import StateDB

    def_id = uuid.uuid4().hex[:12]
    now = time.time()
    async with StateDB() as db:
        await db.create_workflow_def(
            {
                "id": def_id,
                "name": "legacy-spec-base-dir",
                "description": None,
                "spec_json": spec,
                "created_at": now,
                "updated_at": now,
            }
        )

    from lionagi.studio.services.workflow_compile import WorkflowCompileError
    from lionagi.studio.services.workflow_run import run_workflow_def

    with pytest.raises(WorkflowCompileError, match="base_dir"):
        await run_workflow_def(def_id)


async def test_workflow_run_no_cwd_unaffected_with_and_without_base_dir(patched_env, tmp_path):
    """A def with no node cwd anywhere runs exactly as today, whether or not
    the run supplies a base_dir."""
    wf_svc, engine_defs_svc = patched_env
    _FakeEngine.calls = []

    engine_def = await engine_defs_svc.create_engine_def({"name": "no-cwd-eng", "kind": "research"})
    spec = _spec()
    spec["nodes"][2]["config"]["engine_def_id"] = engine_def["id"]
    created = await wf_svc.create_workflow_def({"name": "no-cwd-flow", "spec_json": spec})

    from lionagi.session.session import Session
    from lionagi.studio.services.workflow_run import run_workflow_def

    session_a = Session(default_branch=_mock_chat_branch())
    result_a = await run_workflow_def(created["id"], {"topic": "GQA"}, _session=session_a)
    assert result_a["status"] == "completed"

    session_b = Session(default_branch=_mock_chat_branch())
    result_b = await run_workflow_def(
        created["id"], {"topic": "GQA"}, base_dir=str(tmp_path), _session=session_b
    )
    assert result_b["status"] == "completed"


async def test_run_route_returns_404_for_missing_def(patched_env):
    from fastapi import HTTPException

    from lionagi.studio.services.workflow_defs import RunWorkflowDefRequest, run_workflow_def_route

    with pytest.raises(HTTPException) as exc_info:
        await run_workflow_def_route("nonexistent", RunWorkflowDefRequest(inputs=None))
    assert exc_info.value.status_code == 404
