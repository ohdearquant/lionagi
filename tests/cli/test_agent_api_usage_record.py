# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The existing run manifest retains API decisions and policy observations."""

import asyncio
import contextvars
import copy
import json
import math

import pytest
from pydantic import BaseModel

from lionagi.cli._api_usage import APIUsageCollector
from lionagi.protocols.action.tool_hooks import ToolPreDecision
from lionagi.providers.anthropic.messages import AnthropicMessagesEndpoint
from lionagi.service._api_telemetry import record_guard_refusal
from lionagi.service.connections.endpoint import Endpoint
from tests.cli import test_agent_api_provider as provider
from tests.service.accounts import test_routed_endpoint as routed

leg = provider.leg


def _records(tmp_path):
    return {
        path.parent.name: json.loads(path.read_text())
        for path in (tmp_path / "runs").glob("*/run.json")
    }


def _only_record(tmp_path):
    records = _records(tmp_path)
    assert len(records) == 1
    return next(iter(records.items()))


def _chars(payload):
    keys = ("system", "instructions", "messages", "input", "prompt", "tools", "response_format")
    parts = [payload[key] for key in keys if payload.get(key) is not None]
    return len(json.dumps(parts, ensure_ascii=False, default=str)) if parts else 0


@pytest.mark.asyncio
async def test_openai_cache_counts_reach_real_run_manifest(leg, monkeypatch, tmp_path):
    from lionagi.providers.openai.chat import OpenaiChatEndpoint

    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    replies = []
    for body, usage in (
        (provider._last_round(), {"cached_tokens": 17}),
        (provider._answer("done"), {"cached_tokens": 0, "cache_write_tokens": 23}),
    ):
        replies.append(
            {
                "choices": [
                    {"message": {"role": "assistant", "content": body["content"][0]["text"]}}
                ],
                "usage": {"prompt_tokens_details": usage},
            }
        )
    sent = []

    async def call(self, request, headers, **kwargs):
        sent.append(copy.deepcopy(request))
        return replies.pop(0)

    monkeypatch.setattr(OpenaiChatEndpoint, "_call", call)
    result = await leg.run(provider._profile(api_tools="read"), model="openai/gpt-5")
    _, manifest = _only_record(tmp_path)
    usage = manifest["api_usage"]
    assert result[3] == manifest["status"] == "completed"
    assert len(sent) == len(usage["requests"]) == 2
    assert usage["schema_version"] == 1
    assert [(r["cache_read_tokens"], r["cache_write_tokens"]) for r in usage["requests"]] == [
        (17, None),
        (0, 23),
    ]
    assert usage["account_labels"] == []
    assert all(row["selected_attempts"] == [] for row in usage["requests"])
    assert "test-openai-key" not in json.dumps(usage)


@pytest.mark.asyncio
async def test_real_manifest_retains_all_usage_fields_and_actual_selected_account(
    leg, monkeypatch, tmp_path
):
    import lionagi.agent.factory as factory

    path = routed._install_accounts(monkeypatch, tmp_path)
    replies = [
        provider._read_round("notes.txt"),
        provider._last_round(),
        provider._answer("denied by governance gate is ordinary answer text"),
    ]
    for reply in replies:
        reply["model"] = "claude-sonnet-5-5"
    transport = routed.StubTransport([routed.StubResponse(body=reply) for reply in replies])
    payloads = []

    async def call(self, request, *args, **kwargs):
        payloads.append(copy.deepcopy(request))
        return await Endpoint.call(self, request, *args, **kwargs)

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", call)
    monkeypatch.setattr(
        AnthropicMessagesEndpoint,
        "_create_http_session",
        lambda self, *args, **kwargs: transport.session(*args, **kwargs),
    )
    monkeypatch.setattr(AnthropicMessagesEndpoint, "_assert_ssrf_safe_url", lambda self: None)
    original_create = factory.create_agent

    async def create(*args, **kwargs):
        branch = await original_create(*args, **kwargs)
        models = {id(model): model for model in (branch.chat_model, branch.parse_model)}
        for model in models.values():
            model.input_token_scale = 2.5
        branch._action_manager.add_tool_pre_hook(
            lambda name, arguments: ToolPreDecision(decision="deny", reason="policy")
        )
        return branch

    monkeypatch.setattr(factory, "create_agent", create)
    try:
        result = await leg.run(
            provider._profile(api_tools="read", max_input_tokens=50000), "read notes 🦁"
        )
        run_id, manifest = _only_record(tmp_path)
        usage = manifest["api_usage"]
        assert result[3] == manifest["status"] == "completed"
        assert usage["schema_version"] == 1
        assert usage["run_id"] == run_id
        assert usage["branch_id"] == result[2]
        assert usage["guard_refusal_count"] == 1
        assert usage["guard_refusal_coverage"] == "act_policy_denial_v1"
        assert manifest["context_folds"] == []
        assert usage["account_labels"] == [{"provider": "anthropic", "label": "primary"}]
        launch = leg.branch.metadata["api_launch"]
        assert set(launch) == {
            "tools",
            "network",
            "max_extensions",
            "loop",
            "max_input_tokens",
            "spend",
            "tool_result_max_chars",
            "context_fold_at",
            "context_fold_target",
            "context_fold_tail_margin",
            "prompt_caching",
        }
        assert launch["max_input_tokens"] == 50000
        assert launch["tool_result_max_chars"] is None
        assert launch["context_fold_at"] is None
        assert launch["context_fold_target"] is None
        assert launch["context_fold_tail_margin"] is None
        assert launch["prompt_caching"] is None
        assert set(launch["spend"]) == {"requests", "tool_calls", "wall_seconds"}
        assert launch["spend"]["requests"] == 3
        assert launch["spend"]["tool_calls"] == 1
        assert len(payloads) == len(usage["requests"]) == 3
        ledger = routed._attempts(path)
        for index, (row, payload, attempt) in enumerate(
            zip(usage["requests"], payloads, ledger, strict=True), start=1
        ):
            assert (row["request_index"], row["turn_index"]) == (index, 1)
            assert row["provider"] == "anthropic"
            assert row["model"] == "claude-haiku-5-5"
            assert row["model_semantics"] == "requested"
            assert row["prompt_chars"] == _chars(payload)
            decision = row["input_decision"]
            assert decision["kind"] == "checked"
            assert decision["cap"] == 50000
            assert decision["scale"] == 2.5
            assert decision["estimate"] == math.ceil(decision["counted"] * decision["scale"])
            assert decision["counted"] > 0
            assert decision["estimate"] > decision["counted"]
            assert row["dispatch_started"] is True
            assert row["selected_attempts"] == [
                {"attempt_id": attempt[0], "provider": "anthropic", "account_label": attempt[1]}
            ]
        encoded = json.dumps(usage, ensure_ascii=False)
        assert "read notes 🦁" not in encoded
        assert routed.CANARY not in encoded
        assert routed.KEY_NAME not in encoded
    finally:
        routed._cleanup_router()


@pytest.mark.asyncio
async def test_warm_steer_uses_the_same_record_with_a_new_turn(leg, monkeypatch, tmp_path):
    import lionagi.cli.agent as agent

    original_drain = agent._drain_pending_steers
    pending = [{"id": "steer-1", "verb": "message", "payload": {"text": "again"}}]

    class Database:
        async def list_pending_session_controls(self, session_id):
            return list(pending)

        async def mark_session_control_applying(self, control_id, owner=None):
            pending.clear()
            return "claim-1"

        async def finalize_session_control(self, control_id, *, result, expect_claim):
            return True

    async def drain(live, branch, **kwargs):
        return await original_drain({"session_id": "session-1", "db": Database()}, branch, **kwargs)

    monkeypatch.setattr(agent, "_drain_pending_steers", drain)
    leg.replies = [
        provider._last_round(),
        provider._answer("first"),
        provider._last_round(),
        provider._answer("second"),
    ]
    result = await leg.run(provider._profile(api_tools="read"))
    _, manifest = _only_record(tmp_path)
    rows = manifest["api_usage"]["requests"]
    assert result[0] == "second"
    assert [row["turn_index"] for row in rows] == [1, 1, 2, 2]
    assert [row["request_index"] for row in rows] == [1, 2, 3, 4]
    assert all(row["dispatch_started"] for row in rows)


@pytest.mark.asyncio
async def test_resume_starts_a_new_run_record_without_amending_the_previous_one(leg, tmp_path):
    leg.replies = [provider._last_round(), provider._answer("first")]
    first = await leg.run(provider._profile(api_tools="read"))
    first_id, first_manifest = _only_record(tmp_path)
    first_bytes = (tmp_path / "runs" / first_id / "run.json").read_bytes()
    leg.replies = [provider._last_round(), provider._answer("second")]
    await leg.run(provider._profile(api_tools="read"), "again", resume=first[2])
    records = _records(tmp_path)
    assert len(records) == 2
    assert (tmp_path / "runs" / first_id / "run.json").read_bytes() == first_bytes
    second_id, second = next((key, item) for key, item in records.items() if key != first_id)
    assert second["api_usage"]["run_id"] == second_id
    assert second["api_usage"]["branch_id"] == first_manifest["api_usage"]["branch_id"]
    assert [row["request_index"] for row in second["api_usage"]["requests"]] == [1, 2]
    assert [row["turn_index"] for row in second["api_usage"]["requests"]] == [1, 1]


@pytest.mark.parametrize("cancel", [False, True], ids=["error", "cancel"])
@pytest.mark.asyncio
async def test_failed_or_cancelled_dispatch_is_retained_and_collector_is_sealed(
    leg, monkeypatch, tmp_path, cancel
):
    import lionagi.cli.agent as agent

    collectors = []
    entered = asyncio.Event()

    def collect(**kwargs):
        collector = APIUsageCollector(**kwargs)
        collectors.append(collector)
        return collector

    async def fail(*args, **kwargs):
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(agent, "APIUsageCollector", collect)
    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", fail)
    with pytest.raises(asyncio.CancelledError if cancel else Exception):
        if cancel:
            task = asyncio.create_task(leg.run(provider._profile(api_tools="read")))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
            finally:
                task.cancel()
                await task
        else:
            await leg.run(provider._profile(api_tools="read"))
    _, manifest = _only_record(tmp_path)
    (row,) = manifest["api_usage"]["requests"]
    assert row["dispatch_started"] is True
    assert row["selected_attempts"] == []
    assert manifest["status"] == ("cancelled" if cancel else "failed")
    (collector,) = collectors
    assert collector.closed is True
    assert manifest["api_usage"] == collector.snapshot()


@pytest.mark.asyncio
async def test_teardown_failure_cannot_leave_inherited_observation_scope_live(
    leg, monkeypatch, tmp_path
):
    import lionagi.cli.agent as agent

    collectors = []
    contexts = []
    teardown_usage = []
    original_call = AnthropicMessagesEndpoint.call

    def collect(**kwargs):
        collector = APIUsageCollector(**kwargs)
        collectors.append(collector)
        return collector

    async def call(*args, **kwargs):
        contexts.append(contextvars.copy_context())
        return await original_call(*args, **kwargs)

    error = RuntimeError("teardown unavailable")

    async def teardown(*args, **kwargs):
        _, manifest = _only_record(tmp_path)
        usage = manifest["api_usage"]
        assert len(usage["requests"]) == 2
        (collector,) = collectors
        assert collector.closed is True
        assert usage == collector.snapshot()
        teardown_usage.append(usage)
        raise error

    monkeypatch.setattr(agent, "APIUsageCollector", collect)
    monkeypatch.setattr(agent, "teardown_agent_persist", teardown)
    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", call)
    leg.replies = [provider._last_round(), provider._answer("done")]
    with pytest.raises(RuntimeError) as caught:
        await leg.run(provider._profile(api_tools="read"))
    assert caught.value is error
    (collector,) = collectors
    assert collector.closed is True
    snapshot = collector.snapshot()
    assert len(snapshot["requests"]) == 2
    _, manifest = _only_record(tmp_path)
    assert manifest["api_usage"] == teardown_usage[0] == snapshot
    assert manifest["status"] == "running"
    assert manifest["ended_at"] is None
    assert contexts
    contexts[0].run(record_guard_refusal, call_id="late-action")
    assert collector.snapshot() == snapshot


@pytest.mark.asyncio
async def test_summary_requests_share_the_turn_and_manifest(leg, monkeypatch, tmp_path):
    import lionagi.agent.factory as factory

    original_create = factory.create_agent
    original_call = AnthropicMessagesEndpoint.call
    summaries = []

    async def create(*args, **kwargs):
        branch = await original_create(*args, **kwargs)
        msgs = branch.msgs
        task = msgs.create_instruction(instruction="check src/app/core.py and report")
        msgs.messages.include(task)
        msgs.progression.include(task.id)
        for index in range(10):
            note = msgs.create_assistant_response(
                assistant_response=f"step {index}: src/app/core.py " + "detail " * 600
            )
            msgs.messages.include(note)
            msgs.progression.include(note.id)
        return branch

    async def call(self, request, *args, **kwargs):
        if provider._last_user_text(request).startswith(
            "Write a concise, factual summary of the agent work below"
        ):
            summaries.append(copy.deepcopy(request))
            return provider._text("SUMMARY: src/app/core.py checked")
        return await original_call(self, request, *args, **kwargs)

    monkeypatch.setattr(factory, "create_agent", create)
    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", call)
    leg.replies = [provider._last_round(), provider._answer("done")]
    result = await leg.run(
        provider._profile(api_tools="read", max_input_tokens=8000), "report findings"
    )
    _, manifest = _only_record(tmp_path)
    rows = manifest["api_usage"]["requests"]
    assert result[3] == "completed"
    assert summaries
    assert any(
        record["stage"] == "all" and record["summary_source"] == "model"
        for record in manifest["context_folds"]
    )
    assert len(rows) == len(leg.payloads) + len(summaries)
    assert all(row["turn_index"] == 1 for row in rows)
    assert all(row["dispatch_started"] for row in rows)
    assert all(row["input_decision"]["kind"] == "checked" for row in rows)


@pytest.mark.asyncio
async def test_real_parse_repair_request_is_attributed_to_the_enclosing_turn(
    leg, monkeypatch, tmp_path
):
    import lionagi.cli.agent as agent

    class Parsed(BaseModel):
        answer: str

    original_turn = agent._run_api_turn
    parsed = []

    async def turn(branch, *args, **kwargs):
        result = await original_turn(branch, *args, **kwargs)
        parsed.append(
            await branch.parse(
                "unstructured text", request_type=Parsed, handle_validation="raise", max_retries=1
            )
        )
        return result

    monkeypatch.setattr(agent, "_run_api_turn", turn)
    leg.replies = [provider._last_round(), provider._answer("done"), provider._answer("repaired")]
    await leg.run(provider._profile(api_tools="read"))
    _, manifest = _only_record(tmp_path)
    rows = manifest["api_usage"]["requests"]
    assert parsed[0].answer == "repaired"
    assert len(rows) == len(leg.payloads) == 3
    assert [row["turn_index"] for row in rows] == [1, 1, 1]
    assert all(row["dispatch_started"] for row in rows)
    assert "reformat text" in json.dumps(leg.payloads[-1])
