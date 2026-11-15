"""Reactive workers must inherit profile-origin MCP containment."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest


async def _spawn(
    tmp_path, monkeypatch, *, source, explicit=False, descendant=False, checkpoint=False
):
    import lionhub.orchestration.engines as engines
    from lionagi import Branch, iModel
    from lionhub.agent.casts.emission import SpawnRequest
    from lionhub.serve.cli.orchestrate import flow

    class Captured(Exception):
        pass

    callbacks = {}

    class Run:
        async def run_dag(self, *args, **kwargs):
            callbacks.update(kwargs)
            raise Captured

    class Engine:
        def new_run(self, session=None):
            return Run()

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(engines, "PlanningEngine", Engine)
    monkeypatch.setattr(flow, "_persist_session_phase", noop)
    monkeypatch.setattr(flow, "_persist_control_poller_state_bounded", noop)
    handed = []
    monkeypatch.setattr(
        flow,
        "_hand_mcp_servers",
        lambda _model, servers, **kwargs: handed.append((servers, kwargs.get("exclusive"))),
    )

    def branch(name):
        return Branch(
            name=name, system="work", chat_model=iModel(provider="claude_code", model="sonnet")
        )

    orchestrator = branch("orchestrator")
    emitter = branch("emitter")
    role = branch("reviewer")
    disabled = {"no_mcp_config": True, "no_mcp_config_source": "profile", "mcp_config": None}
    if source != "unrestricted":
        (role if source == "role" else emitter).metadata["mcp_launch"] = disabled
    handlers = {}
    nodes = {}
    env = SimpleNamespace(
        orc_branch=orchestrator,
        run=SimpleNamespace(
            agent_artifact_dir=lambda aid: tmp_path / "artifacts" / aid,
            checkpoint_path=tmp_path / "checkpoint.json",
        ),
        session=SimpleNamespace(
            branches=[], observe=lambda typ, handler: handlers.update({typ: handler})
        ),
        builder=SimpleNamespace(get_graph=lambda: SimpleNamespace(internal_nodes=nodes)),
        worker_artifact_dirs={},
        cwd=str(tmp_path),
        verbose=False,
        effort=None,
        _live_persist=None,
        team_data=None,
        exchange=None,
        expect_worker=lambda _id: None,
        mcp_servers={"example": {"command": "example"}},
        mcp_config_explicit=explicit,
    )
    dag = SimpleNamespace(
        reactive=True,
        spawn_roles=None,
        node_ids={},
        known_nodes=set(),
        deps_by_node={},
        worker_models={},
        role_base={"reviewer": role},
        role_artifact_defaults={},
        op_segments=[],
        max_spawn=5,
    )
    plan = SimpleNamespace(assignments=[], agent_ids=[])
    with pytest.raises(Captured):
        await flow._execute_dag(
            env,
            plan,
            dag,
            max_concurrent=1,
            max_ops=5,
            checkpoint_config={} if checkpoint else None,
        )
    emitter_op = SimpleNamespace(id="emitter-id")
    callbacks["executor_ref"]["executor"] = SimpleNamespace(
        operation_branches={emitter_op.id: emitter}
    )
    req = SpawnRequest(
        instruction="work", independent=True, assignee="reviewer" if source == "role" else None
    )
    operation = callbacks["node_builder"](req, emitter_op)
    child = (role if source == "role" else orchestrator).clone()
    callbacks["spawn_branch_setup"](operation, child)
    if checkpoint:
        from lionagi.session.signal import NodeFailed

        nodes[operation.id] = operation
        handlers[NodeFailed](
            SimpleNamespace(
                op_id=str(operation.id),
                name="spawn",
                parent_id=None,
                elapsed=0,
            ),
            None,
        )
        # The signal handler schedules an atomic checkpoint write.
        for _ in range(100):
            await asyncio.sleep(0.01)
            saved = json.loads(env.run.checkpoint_path.read_text())
            if saved["spawned"]:
                break
        assert saved["spawned"][0]["profile_mcp_disabled"] is True
    if descendant:
        child_op = SimpleNamespace(id="child-id")
        callbacks["executor_ref"]["executor"].operation_branches[child_op.id] = child
        next_op = callbacks["node_builder"](
            SpawnRequest(instruction="continue", independent=True), child_op
        )
        next_child = orchestrator.clone()
        callbacks["spawn_branch_setup"](next_op, next_child)
        assert next_child.metadata["mcp_launch"]["no_mcp_config_source"] == "profile"
    return handed, child


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["role", "emitter"])
async def test_spawn_keeps_profile_mcp_disabled(tmp_path, monkeypatch, source):
    handed, child = await _spawn(tmp_path, monkeypatch, source=source)
    assert handed == [({}, True)]
    assert child.metadata["mcp_launch"]["no_mcp_config_source"] == "profile"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["role", "emitter"])
async def test_spawn_refuses_explicit_mcp_config_with_profile(tmp_path, monkeypatch, source):
    from lionagi._errors import CLIInputRefusedError

    with pytest.raises(CLIInputRefusedError, match="mcp: none.*--mcp-config"):
        await _spawn(tmp_path, monkeypatch, source=source, explicit=True)


@pytest.mark.asyncio
async def test_unrestricted_spawn_keeps_worker_servers(tmp_path, monkeypatch):
    handed, child = await _spawn(tmp_path, monkeypatch, source="unrestricted")
    assert handed == [({"example": {"command": "example"}}, True)]
    assert "mcp_launch" not in child.metadata


@pytest.mark.asyncio
async def test_contained_spawn_keeps_its_descendant_contained(tmp_path, monkeypatch):
    handed, _ = await _spawn(tmp_path, monkeypatch, source="role", descendant=True)
    assert handed == [({}, True), ({}, True)]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["role", "emitter"])
async def test_failed_spawn_signal_persists_profile_containment(tmp_path, monkeypatch, source):
    await _spawn(tmp_path, monkeypatch, source=source, checkpoint=True)
