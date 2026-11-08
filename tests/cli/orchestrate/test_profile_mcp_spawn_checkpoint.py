"""Profile MCP containment survives checkpointing a failed reactive spawn."""

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from lionagi import Branch, iModel
from lionagi._errors import CLIInputRefusedError
from lionagi.casts.emission import TaskAssignment
from lionagi.cli.orchestrate import flow
from lionagi.operations.builder import OperationGraphBuilder
from lionagi.protocols.types import EventStatus
from lionhub.orchestration.flow.checkpoint import CheckpointWriter


@pytest.mark.asyncio
async def test_checkpoint_records_profile_mcp_disable_only_when_set(tmp_path):
    writer = CheckpointWriter(tmp_path / "checkpoint.json", "session", "prompt", [], {})
    await writer.record_spawned(
        "restricted", status="failed", response=None, profile_mcp_disabled=True
    )
    await writer.record_spawned("legacy", status="failed", response=None)
    assert writer.spawned[0]["profile_mcp_disabled"] is True
    assert "profile_mcp_disabled" not in writer.spawned[1]
    assert json.loads(writer.path.read_text())["spawned"] == writer.spawned


@pytest.mark.parametrize("restricted", [True, False])
@pytest.mark.parametrize("assigned", [True, False])
def test_failed_spawn_resume_restores_profile_mcp_containment(monkeypatch, restricted, assigned):
    builder = OperationGraphBuilder()
    role = Branch(name="worker", chat_model=iModel(provider="claude_code", model="sonnet"))
    parent_id = builder.add_operation("operate", branch=role, instruction="do it")
    branches = []
    env = SimpleNamespace(
        builder=builder,
        orc_branch=role,
        session=SimpleNamespace(include_branches=branches.append),
        mcp_config_explicit=False,
    )
    dag = flow._DagState(
        node_ids=[parent_id],
        known_nodes={parent_id},
        deps_by_node={},
        reactive=True,
        spawn_roles=None,
        role_base={"worker": role},
        worker_models=[],
    )
    plan = flow._PlanResult(
        assignments=[TaskAssignment(task="do it", assignee="worker")],
        agent_ids=["worker"],
        dep_indices=[[]],
        pool=[],
        budget_preambles={},
    )
    child_id = str(uuid4())
    entry = {
        "node_id": child_id,
        "status": "failed",
        "operation": "operate",
        "assignee": "worker" if assigned else None,
        "instruction": "follow up",
        "parent_id": str(parent_id),
        "spawn_id": "spawn-1",
    }
    if restricted:
        entry["profile_mcp_disabled"] = True
    handed = []
    real_hand_off = flow._hand_mcp_servers

    def record_hand_off(model, servers, **kw):
        handed.append((servers, kw["exclusive"]))
        real_hand_off(model, servers, **kw)

    monkeypatch.setattr(flow, "_hand_mcp_servers", record_hand_off)
    if restricted:
        env.mcp_config_explicit = True
        with pytest.raises(CLIInputRefusedError, match="mcp: none.*--mcp-config"):
            flow._reconstruct_spawned_nodes(
                env, plan, dag, {"worker": {"status": "completed"}}, [entry], retry_failed=True
            )
        assert not branches
        assert not handed
        env.mcp_config_explicit = False
    flow._reconstruct_spawned_nodes(
        env, plan, dag, {"worker": {"status": "completed"}}, [entry], retry_failed=True
    )
    node = builder.get_graph().internal_nodes[UUID(child_id)]
    assert node.execution.status == EventStatus.PENDING
    if restricted:
        assert handed == [({}, True)]
        assert len(branches) == 1
        assert node.branch_id == branches[0].id
        assert branches[0].metadata["mcp_launch"]["no_mcp_config_source"] == "profile"
        assert node.metadata["profile_mcp_disabled"] is True
        assert branches[0].chat_model.endpoint.config.kwargs["mcp_servers"] == {}
        assert branches[0].chat_model.endpoint.config.kwargs["strict_mcp_config"] is True
    else:
        assert handed == []
        assert branches == []
        assert node.branch_id == (role.id if assigned else None)
        assert "profile_mcp_disabled" not in node.metadata
