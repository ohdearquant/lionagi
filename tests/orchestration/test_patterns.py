# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.orchestration.patterns — the thin glue over flow.

Graph construction and role wiring are exercised directly; execution itself
is covered by tests/operations/test_reactive_flow.py (no LLM needed).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lionagi.casts.emission import SpawnRequest, TaskAssignment
from lionagi.orchestration import (
    build_dag_graph,
    build_fanout_graph,
    grant_spawn,
    plan,
    role_node_builder,
    spawn_roles,
)
from lionagi.session.branch import Branch
from lionagi.session.session import Session


def _roles(*names: str) -> tuple[Session, dict[str, Branch]]:
    session = Session()
    roles: dict[str, Branch] = {}
    for n in names:
        b = Branch(name=n)
        session.include_branches(b)
        roles[n] = b
    return session, roles


class TestRoleNodeBuilder:
    def test_maps_assignee_to_branch(self):
        session, roles = _roles("researcher")
        nb = role_node_builder(roles)
        node = nb(SpawnRequest(instruction="dig", assignee="researcher"), None)
        assert node.operation == "operate"
        assert node.branch_id == roles["researcher"].id

    def test_no_assignee_leaves_branch_unset(self):
        session, roles = _roles("researcher")
        nb = role_node_builder(roles)
        node = nb(SpawnRequest(instruction="x"), None)
        assert node.branch_id is None

    def test_maps_assignee_stamps_metadata(self):
        """The spawned node's role must be recoverable after branch_id is later
        overwritten by the executor's per-spawn clone — flow.py's post-run
        artifact-contract and DAG-metadata surfaces read this back."""
        session, roles = _roles("researcher")
        nb = role_node_builder(roles)
        node = nb(SpawnRequest(instruction="dig", assignee="researcher"), None)
        assert node.metadata.get("assignee") == "researcher"

    def test_no_assignee_leaves_metadata_unset(self):
        session, roles = _roles("researcher")
        nb = role_node_builder(roles)
        node = nb(SpawnRequest(instruction="x"), None)
        assert node.metadata.get("assignee") is None

    def test_unknown_assignee_raises(self):
        session, roles = _roles("researcher")
        nb = role_node_builder(roles)
        with pytest.raises(ValueError, match="not a recognized role"):
            nb(SpawnRequest(instruction="x", assignee="ghost"), None)

    def test_custom_operation_preserved(self):
        session, roles = _roles("researcher")
        nb = role_node_builder(roles)
        node = nb(
            SpawnRequest(instruction="x", operation="ReAct", assignee="researcher"),
            None,
        )
        assert node.operation == "ReAct"


class TestBuildFanoutGraph:
    def test_parallel_workers_are_independent(self):
        session, roles = _roles("researcher", "architect")
        assignments = [
            TaskAssignment(task="survey", assignee="researcher"),
            TaskAssignment(task="design", assignee="architect"),
        ]
        graph, worker_ids = build_fanout_graph(session, assignments, roles)

        assert len(worker_ids) == 2
        # two nodes, NO edges (independent parallel workers)
        assert len(graph.internal_nodes) == 2
        assert len(graph.internal_edges) == 0
        assert graph.is_acyclic()

    def test_synthesis_aggregates_workers(self):
        session, roles = _roles("researcher", "architect", "synthesizer")
        assignments = [
            TaskAssignment(task="survey", assignee="researcher"),
            TaskAssignment(task="design", assignee="architect"),
        ]
        graph, worker_ids = build_fanout_graph(
            session, assignments, roles, synthesis_role="synthesizer"
        )
        # 2 workers + 1 synthesis node
        assert len(graph.internal_nodes) == 3
        # 2 aggregate edges (each worker -> synthesis)
        assert len(graph.internal_edges) == 2
        assert graph.is_acyclic()

    def test_workers_run_on_clones_not_templates(self):
        session, roles = _roles("researcher")
        assignments = [TaskAssignment(task="t", assignee="researcher")]
        graph, worker_ids = build_fanout_graph(session, assignments, roles)
        node = graph.internal_nodes[worker_ids[0]]
        # the worker's branch is a clone, distinct from the role template
        assert node.branch_id != roles["researcher"].id
        assert node.branch_id in session.branches

    def test_unknown_assignee_skipped(self):
        session, roles = _roles("researcher")
        assignments = [
            TaskAssignment(task="ok", assignee="researcher"),
            TaskAssignment(task="orphan", assignee="nobody"),
        ]
        graph, worker_ids = build_fanout_graph(session, assignments, roles)
        assert len(worker_ids) == 1

    def test_no_valid_assignments_raises(self):
        session, roles = _roles("researcher")
        assignments = [TaskAssignment(task="x", assignee="ghost")]
        with pytest.raises(ValueError, match="no assignments"):
            build_fanout_graph(session, assignments, roles)


class TestBuildDagGraph:
    def test_linear_chain_wires_edges(self):
        session, roles = _roles("researcher", "implementer")
        assignments = [
            TaskAssignment(task="research", assignee="researcher"),
            TaskAssignment(task="build on 1", assignee="implementer", depends_on=["1"]),
            TaskAssignment(task="review 2", assignee="researcher", depends_on=["2"]),
        ]
        graph, ids = build_dag_graph(session, assignments, roles)
        assert len(ids) == 3 and all(ids)
        assert len(graph.internal_nodes) == 3
        assert len(graph.internal_edges) == 2  # 1->2, 2->3
        assert graph.is_acyclic()

    def test_independent_assignments_have_no_edges(self):
        session, roles = _roles("researcher")
        assignments = [
            TaskAssignment(task="a", assignee="researcher"),
            TaskAssignment(task="b", assignee="researcher"),
        ]
        graph, ids = build_dag_graph(session, assignments, roles)
        assert len(graph.internal_edges) == 0

    def test_diamond_dependencies(self):
        session, roles = _roles("researcher")
        assignments = [
            TaskAssignment(task="root", assignee="researcher"),
            TaskAssignment(task="left", assignee="researcher", depends_on=["1"]),
            TaskAssignment(task="right", assignee="researcher", depends_on=["1"]),
            TaskAssignment(task="join", assignee="researcher", depends_on=["2", "3"]),
        ]
        graph, ids = build_dag_graph(session, assignments, roles)
        assert len(graph.internal_edges) == 4
        assert graph.is_acyclic()

    def test_out_of_range_and_self_deps_dropped(self):
        session, roles = _roles("researcher")
        assignments = [
            TaskAssignment(task="a", assignee="researcher", depends_on=["1"]),  # self
            TaskAssignment(task="b", assignee="researcher", depends_on=["9", "x"]),  # bad
        ]
        graph, ids = build_dag_graph(session, assignments, roles)
        assert len(graph.internal_edges) == 0  # all dropped, no crash

    def test_unknown_assignee_becomes_none_and_skips_edges(self):
        session, roles = _roles("researcher")
        assignments = [
            TaskAssignment(task="ok", assignee="researcher"),
            TaskAssignment(task="ghost", assignee="nobody", depends_on=["1"]),
        ]
        graph, ids = build_dag_graph(session, assignments, roles)
        assert ids[0] is not None and ids[1] is None
        assert len(graph.internal_nodes) == 1
        assert len(graph.internal_edges) == 0

    def test_workers_run_on_clones(self):
        session, roles = _roles("researcher")
        assignments = [TaskAssignment(task="t", assignee="researcher")]
        graph, ids = build_dag_graph(session, assignments, roles)
        node = graph.internal_nodes[ids[0]]
        assert node.branch_id != roles["researcher"].id
        assert node.branch_id in session.branches

    def test_return_annotation_allows_none_in_node_ids(self):
        """Return annotation must be list[str | None] to match runtime."""
        import types
        import typing

        import lionagi.orchestration.patterns as _mod

        # Session/Branch are TYPE_CHECKING-only; inject them so
        # get_type_hints can resolve the full signature.
        ns = dict(vars(_mod))
        ns.setdefault("Session", Session)
        ns.setdefault("Branch", Branch)
        hints = typing.get_type_hints(build_dag_graph, globalns=ns, include_extras=True)
        ret = hints["return"]
        # tuple[Graph, list[str | None]]
        assert typing.get_origin(ret) is tuple
        _, list_type = typing.get_args(ret)
        assert typing.get_origin(list_type) is list
        (elem_type,) = typing.get_args(list_type)
        assert typing.get_origin(elem_type) is types.UnionType
        assert set(typing.get_args(elem_type)) == {str, type(None)}

        # Verify runtime: a dropped assignee produces None in the list
        session, roles = _roles("researcher")
        assignments = [
            TaskAssignment(task="ok", assignee="researcher"),
            TaskAssignment(task="ghost", assignee="nobody"),
        ]
        _, ids = build_dag_graph(session, assignments, roles)
        assert ids[0] is not None
        assert ids[1] is None


class _FakeOrc:
    """Stand-in orchestrator branch: returns scripted assignments from operate()."""

    def __init__(self, assignments: list[TaskAssignment]):
        self._assignments = assignments
        self.calls: list[dict] = []

    async def operate(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(assignments=self._assignments)


class TestPlan:
    @pytest.mark.asyncio
    async def test_returns_valid_assignments(self):
        orc = _FakeOrc(
            [
                TaskAssignment(task="a", assignee="researcher"),
                TaskAssignment(task="b", assignee="implementer"),
            ]
        )
        out = await plan(orc, "task", roles={"researcher", "implementer"})
        assert [a.task for a in out] == ["a", "b"]
        # planning asks for the assignments structured field
        assert orc.calls[0]["field_models"]

    @pytest.mark.asyncio
    async def test_drops_unknown_assignees(self):
        orc = _FakeOrc(
            [
                TaskAssignment(task="ok", assignee="researcher"),
                TaskAssignment(task="bad", assignee="ghost"),
            ]
        )
        out = await plan(orc, "task", roles={"researcher"})
        assert [a.assignee for a in out] == ["researcher"]

    @pytest.mark.asyncio
    async def test_truncates_to_max_tasks(self):
        orc = _FakeOrc([TaskAssignment(task=str(i), assignee="researcher") for i in range(5)])
        out = await plan(orc, "task", roles={"researcher"}, max_tasks=2)
        assert len(out) == 2

    @pytest.mark.asyncio
    async def test_dag_vs_fanout_instruction(self):
        orc = _FakeOrc([])
        await plan(orc, "t", roles=set(), dag=True)
        await plan(orc, "t", roles=set(), dag=False)
        assert "DAG" in orc.calls[0]["instruction"]
        assert "parallel" in orc.calls[1]["instruction"]


class TestGrantSpawn:
    def test_grant_spawn_sets_capabilities(self):
        session, roles = _roles("architect")
        branch = roles["architect"]
        assert branch.capabilities is None
        grant_spawn(branch, prompt=False)
        assert branch.capabilities is not None
        names = [s.name for s in branch.capabilities.__op_fields__]
        assert "spawn_request" in names


class TestSpawnRoles:
    @pytest.mark.asyncio
    async def test_spawn_roles_builds_branches(self):
        session = Session()
        roles = await spawn_roles(
            session,
            {"researcher": "researcher", "architect": "architect"},
        )
        assert set(roles) == {"researcher", "architect"}
        # role bodies are composed into the system message
        msg = roles["researcher"].system.content.system_message
        assert "hypothesis" in msg.lower() or "research" in msg.lower()

    @pytest.mark.asyncio
    async def test_spawners_get_spawn_capability(self):
        session = Session()
        roles = await spawn_roles(
            session,
            {"researcher": "researcher", "architect": "architect"},
            spawners=["architect"],
        )
        assert roles["architect"].capabilities is not None
        arch_caps = [s.name for s in roles["architect"].capabilities.__op_fields__]
        assert "spawn_request" in arch_caps
