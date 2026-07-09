# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Flow-level regression: a leg whose CLI subprocess never
produces a first chunk must fail loud (WorkerLivenessError) instead of
hanging as a zombie "running" operation forever — and its dependents must
be released, not deadlocked behind it."""

from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock

import anyio
import pytest

from lionagi.operations.builder import OperationGraphBuilder
from lionagi.protocols.generic.event import EventStatus
from lionagi.service.imodel import iModel
from lionagi.service.types.stream_chunk import StreamChunk
from lionagi.session.branch import Branch
from lionagi.session.session import Session


def _make_hanging_cli_model() -> iModel:
    """A CLI iModel whose stream() never yields anything — simulates a
    worker subprocess that dies at/near spawn and never produces output."""
    m = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    m.endpoint = types.SimpleNamespace(
        is_cli=True,
        session_id=None,
        to_dict=lambda: {"type": "fake_cli", "session_id": None},
    )
    m.streaming_process_func = None

    async def create_event(**kw):
        return object()

    m.create_event = create_event
    m.executor = types.SimpleNamespace(append=AsyncMock(), config={})

    async def stream(api_call=None):
        await anyio.sleep(999)
        yield StreamChunk(type="text", content="unreachable")  # pragma: no cover

    m.stream = stream
    return m


def _make_fast_chat_branch(name: str) -> Branch:
    """A plain (non-CLI) branch whose chat_model.invoke() completes quickly —
    stands in for a downstream leg that must still complete after an
    unrelated upstream leg fails."""
    from lionagi.providers.openai.chat import OpenAIChatCompletionsRequest
    from lionagi.service.connections.api_calling import APICalling
    from lionagi.service.connections.endpoint import Endpoint
    from lionagi.service.connections.endpoint_config import EndpointConfig

    branch = Branch(user="test_user", name=name)

    async def _fake_invoke(**kwargs):
        config = EndpointConfig(
            name="oai_chat",
            provider="openai",
            base_url="https://api.openai.com/v1",
            endpoint="chat/completions",
            api_key="dummy-key-for-testing",
            request_options=OpenAIChatCompletionsRequest,
            auth_type="bearer",
            content_type="application/json",
            method="POST",
            requires_tokens=True,
            kwargs={"model": "gpt-4.1-mini"},
        )
        endpoint = Endpoint(config=config)
        fake_call = APICalling(
            payload={"model": "gpt-4.1-mini", "messages": []},
            headers={"Authorization": "Bearer test"},
            endpoint=endpoint,
        )
        fake_call.execution.response = "downstream ok"
        fake_call.execution.status = EventStatus.COMPLETED
        return fake_call

    mock_chat_model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    mock_chat_model.invoke = AsyncMock(side_effect=_fake_invoke)
    branch.chat_model = mock_chat_model
    return branch


async def test_flow_worker_liveness_failure_releases_dependents():
    """A -> B where A's worker never produces a first chunk: A must
    transition to FAILED (WorkerLivenessError) and B must still run to
    completion — the flow overall must terminate, not hang."""
    session = Session()

    branch_a = Branch(user="test_user", name="HangingWorker")
    branch_a.chat_model = _make_hanging_cli_model()

    branch_b = _make_fast_chat_branch("Downstream")

    session.include_branches(branch_a)
    session.include_branches(branch_b)
    session.default_branch = branch_a

    builder = OperationGraphBuilder("LivenessRegression")
    op_a = builder.add_operation(
        "operate",
        branch=branch_a,
        instruction="hang please",
        chat_model=branch_a.chat_model,
        liveness_timeout=0.05,
    )
    builder.add_operation(
        "operate",
        branch=branch_b,
        depends_on=[op_a],
        instruction="proceed regardless",
    )
    graph = builder.get_graph()

    # The whole point of the fix: this must NOT hang. Bound it generously —
    # 2 liveness attempts * 0.05s is ~0.1s; 10s is a large CI-tolerant margin
    # that still proves termination rather than a multi-minute/forever hang.
    result = await asyncio.wait_for(
        session.flow(graph, parallel=True, verbose=False),
        timeout=10.0,
    )

    op_a_node = graph.internal_nodes[op_a]
    assert op_a_node.execution.status == EventStatus.FAILED
    assert op_a_node.execution.error is not None
    assert "worker" in str(op_a_node.execution.error).lower()

    # Downstream operation was released and ran to completion instead of
    # waiting forever behind the dead leg.
    completed_ops = result["completed_operations"]
    assert len(completed_ops) >= 1, (
        "no operation completed — the flow deadlocked behind the dead worker leg"
    )
