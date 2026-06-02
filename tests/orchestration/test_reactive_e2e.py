# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""End-to-end reactive flow through the real operate path + reactive bus.

Workers are ``TestBranch`` scripted endpoints (CLI), so ``operate`` runs the
production streaming/run + capability-extraction path with no network. A
worker granted spawn rights emits a ``spawn_request`` capability; the
ReactiveExecutor must catch it off ``session.observe`` and grow the live DAG.
"""

from __future__ import annotations

import json

import pytest

from lionagi.casts.emission import SpawnRequest
from lionagi.operations.node import create_operation
from lionagi.orchestration import grant_spawn
from lionagi.protocols.graph.graph import Graph
from lionagi.session.session import Session
from lionagi.testing import TestBranch


def _capability_chunk(**spawn_fields) -> dict:
    """A scripted CLI response whose assistant text is a spawn capability."""
    payload = json.dumps({"spawn_request": spawn_fields})
    return {"type": "stream", "chunks": [{"type": "text", "content": payload}]}


@pytest.mark.asyncio
async def test_worker_spawns_via_real_operate_bus():
    """A scripted worker emits a spawn capability -> node injected off the bus."""
    spawner = TestBranch.from_responses(
        [_capability_chunk(instruction="do the follow-up", independent=True)],
        name="spawner",
    )
    follower = TestBranch.from_text("follow-up complete", name="follower")

    session = Session()
    session.include_branches(spawner)
    session.include_branches(follower)
    session.default_branch = spawner
    grant_spawn(spawner, prompt=False)

    graph = Graph()
    root = create_operation("operate", parameters={"instruction": "start"})
    root.branch_id = spawner.id
    graph.add_node(root)

    def node_builder(req: SpawnRequest, emitter):
        node = create_operation("operate", parameters={"instruction": req.instruction})
        node.branch_id = follower.id
        return node

    result = await session.flow(graph, reactive=True, node_builder=node_builder)

    assert result["spawned_operations"] == 1
    assert len(result["completed_operations"]) == 2
    assert "follow-up complete" in str(result["operation_results"].values())


@pytest.mark.asyncio
async def test_no_spawn_when_worker_returns_plain_text():
    """A plain-text worker (no capability) leaves the DAG unchanged."""
    worker = TestBranch.from_text("just text", name="worker")
    session = Session()
    session.include_branches(worker)
    session.default_branch = worker

    graph = Graph()
    node = create_operation("operate", parameters={"instruction": "go"})
    node.branch_id = worker.id
    graph.add_node(node)

    result = await session.flow(graph, reactive=True)

    assert result["spawned_operations"] == 0
    assert len(result["completed_operations"]) == 1


@pytest.mark.asyncio
async def test_spawn_attributed_to_emitting_node():
    """A dependent (non-independent) spawn runs after its emitter."""
    spawner = TestBranch.from_responses(
        [_capability_chunk(instruction="downstream work", independent=False)],
        name="spawner",
    )
    follower = TestBranch.from_text("downstream done", name="follower")

    session = Session()
    session.include_branches(spawner)
    session.include_branches(follower)
    session.default_branch = spawner
    grant_spawn(spawner, prompt=False)

    graph = Graph()
    root = create_operation("operate", parameters={"instruction": "lead"})
    root.branch_id = spawner.id
    graph.add_node(root)

    def node_builder(req, emitter):
        # the emitter is attributed via the contextvar, not None
        assert emitter is not None
        assert emitter.id == root.id
        node = create_operation("operate", parameters={"instruction": req.instruction})
        node.branch_id = follower.id
        return node

    result = await session.flow(graph, reactive=True, node_builder=node_builder)

    assert result["spawned_operations"] == 1
    # the spawn edge makes the follower depend on the emitter
    assert len(graph.internal_edges) == 1
