# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Conversation metadata survives independently of the provider's result stream."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_fails", [False, True], ids=["result", "provider_failure"])
async def test_metadata_tool_has_only_the_provider_result_in_stream_and_history(
    tmp_path, monkeypatch, provider_fails
):
    import lionagi._paths as paths_module
    import lionagi.cli._runs as runs_module
    import lionagi.state.db as state_db_module
    import lionagi.studio.services.operator as operator_service
    import lionhub.providers.anthropic.claude_code as claude_module
    from lionagi.session.branch import Branch
    from lionagi.studio.operator.application_mcp import _dispatch
    from lionagi.studio.operator.coordinator import OperatorCoordinator
    from lionagi.studio.operator.engine import BranchOperatorEngine, _normalize_complete_turn
    from lionagi.studio.operator.store import OperatorStore

    path = tmp_path / "state.db"
    monkeypatch.setattr(paths_module, "LIONAGI_HOME", tmp_path / "lionagi-home")
    monkeypatch.setattr(state_db_module, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(runs_module, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setenv("LIONAGI_STUDIO_OPERATOR_CWD", str(tmp_path))
    monkeypatch.setenv("LIONAGI_STUDIO_OPERATOR_PROVIDER", "claude_code")
    monkeypatch.setenv("LIONAGI_STUDIO_OPERATOR_MODEL", "sonnet")
    monkeypatch.setattr(claude_module, "resolve_claude_cli", lambda: "/unused/claude")
    applied = asyncio.Event()
    release_provider = asyncio.Event()
    returned = []
    call_id = "provider-tool-use-1"
    arguments = {"title": "Provider title", "description": "Durable scope"}

    async def provider_run(branch, _prompt):
        # Replace only provider work: the real engine callback, MCP dispatcher,
        # coordinator persistence, SSE reader, and history compiler still run.
        callback = branch.chat_model.streaming_process_func
        await callback(
            SimpleNamespace(
                type="tool_use",
                tool_name="mcp__studio_operator__update_conversation",
                tool_id=call_id,
                tool_input=arguments,
            )
        )
        bridge_env = branch.chat_model.endpoint.config.kwargs["mcp_servers"]["studio_operator"][
            "env"
        ]
        for name, value in bridge_env.items():
            monkeypatch.setenv(name, value)
        response = await _dispatch(
            {
                "jsonrpc": "2.0",
                "id": "mcp-request-distinct-from-provider-call",
                "method": "tools/call",
                "params": {"name": "update_conversation", "arguments": arguments},
            }
        )
        assert not response["result"].get("isError"), response
        returned.append(json.loads(response["result"]["content"][0]["text"]))
        applied.set()
        await release_provider.wait()
        if provider_fails:
            raise RuntimeError("Provider stopped after applying metadata")
        await callback(
            SimpleNamespace(
                type="tool_result",
                tool_id=call_id,
                tool_output=response["result"]["content"],
                is_error=False,
            )
        )
        yield SimpleNamespace(content="Metadata updated")

    monkeypatch.setattr(Branch, "run", provider_run)
    store = OperatorStore(path)
    coordinator = OperatorCoordinator(store=store, engine_factory=BranchOperatorEngine)
    monkeypatch.setattr(operator_service, "get_operator_coordinator", lambda: coordinator)

    class Connected:
        scope: dict = {}
        headers: dict = {}

        async def is_disconnected(self):
            return False

    iterator = None
    live_frames = []

    async def read_until(frame_type):
        while True:
            raw = await anext(iterator)
            if isinstance(raw, bytes):
                raw = raw.decode()
            if not raw.startswith("data:"):
                continue
            frame = json.loads(raw.removeprefix("data:").strip())
            live_frames.append(frame)
            if frame["type"] == frame_type:
                return

    try:
        await coordinator.startup()
        cid = (await coordinator.create_conversation(title="Original"))["conversation"]["id"]
        accepted = await coordinator.submit(
            cid,
            instruction="Rename this conversation",
            context={"space": "mission", "route": "/", "filters": {}},
            expected_last_sequence=0,
        )
        rid = accepted["requestId"]
        await asyncio.wait_for(applied.wait(), 10)
        assert len(returned) == 1 and returned[0]["updated"] is True
        effect = returned[0]["effect"]
        reopened = OperatorStore(path)
        conversation = await reopened.get_conversation(cid)
        assert (conversation["title"], conversation["description"]) == (
            "Provider title",
            "Durable scope",
        )
        with sqlite3.connect(path) as db:
            row = db.execute(
                "SELECT conversation_id, request_id, effect_json, status "
                "FROM studio_operator_effects WHERE id=?",
                (effect["id"],),
            ).fetchone()
        assert row[:2] == (cid, rid)
        assert json.loads(row[2]) == effect and row[3] == "applied"
        response = await operator_service.stream_operator_conversation(
            cid, Connected(), after_sequence=0
        )
        iterator = response.body_iterator
        await asyncio.wait_for(read_until("tool_call"), 10)
        pending_frames = await store.list_frames(cid)
        pending_results = [
            frame
            for frame in pending_frames
            if frame["type"] == "tool_result" and not frame["payload"]["callId"].startswith("run:")
        ]
        assert pending_results == [], "The MCP mutation must not emit a synthetic result"

        release_provider.set()
        await asyncio.wait_for(read_until("done"), 10)
        durable_frames = await reopened.list_frames(cid)
        assert live_frames == durable_frames
        results = [
            frame
            for frame in live_frames
            if frame["type"] == "tool_result" and not frame["payload"]["callId"].startswith("run:")
        ]
        normalized = _normalize_complete_turn(durable_frames)
        tool_history = [
            frame for frame in normalized if frame["type"] in {"tool_call", "tool_result"}
        ]
        if provider_fails:
            assert results == []
            assert tool_history == []
            assert live_frames[-1]["payload"]["outcome"] == "failed"
            assert any(frame["type"] == "error" for frame in live_frames)
        else:
            assert len(results) == 1
            assert results[0]["payload"] == {
                "callId": call_id,
                "ok": True,
                "result": {"nativeToolCompleted": True},
            }
            assert [frame["type"] for frame in tool_history] == ["tool_call", "tool_result"]
            assert {frame["payload"]["callId"] for frame in tool_history} == {call_id}
            assert tool_history[-1]["payload"] == results[0]["payload"]
            assert live_frames[-1]["payload"]["outcome"] == "completed"
        conversation = await reopened.get_conversation(cid)
        assert (conversation["title"], conversation["description"]) == (
            "Provider title",
            "Durable scope",
        )
        with sqlite3.connect(path) as db:
            persisted = db.execute(
                "SELECT effect_json, status FROM studio_operator_effects WHERE id=?",
                (effect["id"],),
            ).fetchone()
        assert json.loads(persisted[0]) == effect and persisted[1] == "applied"
    finally:
        release_provider.set()
        if iterator is not None:
            await iterator.aclose()
        await coordinator.shutdown()
