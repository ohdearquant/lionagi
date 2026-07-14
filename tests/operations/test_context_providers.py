# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ContextProvider injection seam (ADR-0008): registry
budget/failure semantics, and the pre-turn fold that renders provider
blocks into the first message without ever touching the durable record."""

import asyncio

import pytest

from lionagi.operations.chat._prepare import _prepare_run_kwargs
from lionagi.operations.types import ChatParam
from lionagi.protocols.context_providers import ContextProviderRegistry, ProviderReport
from lionagi.session.branch import Branch

# ---------------------------------------------------------------------------
# Stub providers
# ---------------------------------------------------------------------------


class _StubProvider:
    def __init__(self, text, name="stub"):
        self.text = text
        self.name = name

    async def provide(self, branch, instruction):
        return self.text


class _NoneProvider:
    name = "silent"

    async def provide(self, branch, instruction):
        return None


class _RaisingProvider:
    name = "raiser"

    async def provide(self, branch, instruction):
        raise RuntimeError("boom")


class _BadOutput:
    def __bool__(self):
        raise TypeError("provider output cannot be inspected")


class _BadOutputProvider:
    name = "bad-output"

    async def provide(self, branch, instruction):
        return _BadOutput()


class _WritebackProvider:
    def __init__(self, name="writer"):
        self.name = name
        self.calls = []

    async def provide(self, branch, instruction):
        return None

    async def writeback(self, branch, action_responses):
        self.calls.append((branch, action_responses))


class _RaisingWritebackProvider:
    name = "raising-writer"

    async def provide(self, branch, instruction):
        return None

    async def writeback(self, branch, action_responses):
        raise RuntimeError("writeback failed")


def _chat_param(branch, **overrides):
    kw = dict(
        guidance=None,
        context=None,
        sender="user",
        recipient=branch.id,
        response_format=None,
        progression=None,
        tool_schemas=[],
        images=[],
        image_detail="auto",
        plain_content="",
        include_token_usage_to_model=False,
        imodel=branch.chat_model,
        imodel_kw={},
    )
    kw.update(overrides)
    return ChatParam(**kw)


# ---------------------------------------------------------------------------
# ContextProviderRegistry.gather — unit level
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gather_reports_fired_names_and_token_counts():
    registry = ContextProviderRegistry()
    registry.register(_StubProvider("hello world"), priority=1, name="p1")

    report = await registry.gather(branch=None, instruction=None)

    assert isinstance(report, ProviderReport)
    assert report.blocks == ["hello world"]
    assert len(report.fired) == 1
    assert report.fired[0]["provider_name"] == "p1"
    assert report.fired[0]["tokens"] > 0


@pytest.mark.asyncio
async def test_gather_empty_registry_returns_empty_report():
    registry = ContextProviderRegistry()
    report = await registry.gather(branch=None, instruction=None)
    assert report.blocks == []
    assert report.fired == []
    assert report.skipped == []
    assert report.failed == []


@pytest.mark.asyncio
async def test_gather_drops_lowest_priority_first_over_budget():
    registry = ContextProviderRegistry(budget=1)
    registry.register(_StubProvider("aaaa", name="low"), priority=0)
    registry.register(_StubProvider("b", name="high"), priority=10)

    report = await registry.gather(branch=None, instruction=None)

    fired_names = {f["provider_name"] for f in report.fired}
    assert "high" in fired_names
    assert "low" not in fired_names
    assert "low" in report.skipped


@pytest.mark.asyncio
async def test_gather_skips_none_returning_provider():
    registry = ContextProviderRegistry()
    registry.register(_NoneProvider())

    report = await registry.gather(branch=None, instruction=None)

    assert report.blocks == []
    assert report.fired == []


@pytest.mark.asyncio
async def test_gather_contains_raising_provider_and_still_renders_others():
    registry = ContextProviderRegistry()
    registry.register(_RaisingProvider(), priority=5)
    registry.register(_StubProvider("survivor", name="survivor"), priority=1)

    report = await registry.gather(branch=None, instruction=None)

    assert "raiser" in report.failed
    assert report.blocks == ["survivor"]
    assert report.fired[0]["provider_name"] == "survivor"


@pytest.mark.asyncio
async def test_gather_contains_bad_provider_output_and_still_renders_others():
    registry = ContextProviderRegistry()
    registry.register(_BadOutputProvider())
    registry.register(_StubProvider("survivor", name="survivor"))

    report = await registry.gather(branch=None, instruction=None)

    assert report.failed == ["bad-output"]
    assert report.blocks == ["survivor"]


@pytest.mark.asyncio
async def test_gather_writeback_calls_registered_hook():
    registry = ContextProviderRegistry()
    writer = _WritebackProvider()
    registry.register(writer)
    branch = object()
    action_responses = [object()]

    await registry.gather_writeback(branch, action_responses)

    assert writer.calls == [(branch, action_responses)]


@pytest.mark.asyncio
async def test_gather_writeback_skips_provider_without_hook():
    registry = ContextProviderRegistry()
    registry.register(_StubProvider("context", name="reader"))
    writer = _WritebackProvider()
    registry.register(writer)

    await registry.gather_writeback(None, ["response"])

    assert writer.calls == [(None, ["response"])]


@pytest.mark.asyncio
async def test_gather_writeback_warns_and_continues_to_sibling(caplog):
    registry = ContextProviderRegistry()
    registry.register(_RaisingWritebackProvider())
    writer = _WritebackProvider(name="survivor")
    registry.register(writer)

    await registry.gather_writeback(None, ["response"])

    assert writer.calls == [(None, ["response"])]
    assert "context provider 'raising-writer' writeback raised; skipping" in caplog.text


def test_registry_is_falsy_when_empty():
    registry = ContextProviderRegistry()
    assert not registry
    registry.register(_StubProvider("x"))
    assert registry


# ---------------------------------------------------------------------------
# Branch integration — registry lives on Branch (gate ruling Q1)
# ---------------------------------------------------------------------------


def test_branch_providers_lazily_created_and_zero_cost_when_unused():
    branch = Branch()
    assert branch._context_providers is None
    registry = branch.providers
    assert isinstance(registry, ContextProviderRegistry)
    assert branch._context_providers is registry


@pytest.mark.asyncio
async def test_zero_providers_path_leaves_fold_unchanged(make_mocked_branch):
    branch = make_mocked_branch(system="You are helpful", response="ok")
    chat_param = _chat_param(branch)

    ins, kw = _prepare_run_kwargs(branch, "hello", chat_param)

    first = kw["messages"][0]["content"]
    assert branch.msgs.system.rendered in first


def test_prepare_run_kwargs_renders_explicit_context_blocks(make_mocked_branch):
    branch = make_mocked_branch(system="You are helpful", response="ok")
    chat_param = _chat_param(branch)

    _, kw = _prepare_run_kwargs(
        branch,
        "hello",
        chat_param,
        context_blocks=["call-local-knowledge"],
    )

    assert "call-local-knowledge" in kw["messages"][0]["content"]


# ---------------------------------------------------------------------------
# End-to-end: provider text in rendered first message, absent from record
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_text_rendered_but_never_persisted(make_mocked_branch):
    branch = make_mocked_branch(system="You are helpful", response="ok")
    branch.providers.register(_StubProvider("INJECTED-KNOWLEDGE-BLOCK"), priority=10)

    await branch.communicate(instruction="hello", skip_validation=True)

    sent_messages = branch.chat_model.invoke.call_args.kwargs["messages"]
    first_content = sent_messages[0]["content"]
    assert "INJECTED-KNOWLEDGE-BLOCK" in first_content

    for msg in branch.msgs.messages:
        assert "INJECTED-KNOWLEDGE-BLOCK" not in str(msg.content)


@pytest.mark.asyncio
async def test_provider_budget_enforced_end_to_end(make_mocked_branch):
    branch = make_mocked_branch(system="You are helpful", response="ok")
    branch.providers.budget = 2
    branch.providers.register(_StubProvider("low priority filler text"), priority=0, name="low")
    branch.providers.register(_StubProvider("hi"), priority=10, name="high")

    await branch.communicate(instruction="hello", skip_validation=True)

    sent_messages = branch.chat_model.invoke.call_args.kwargs["messages"]
    first_content = sent_messages[0]["content"]
    assert "hi" in first_content
    assert "low priority filler text" not in first_content


@pytest.mark.asyncio
async def test_raising_provider_skipped_others_still_render(make_mocked_branch):
    branch = make_mocked_branch(system="You are helpful", response="ok")
    branch.providers.register(_RaisingProvider(), priority=5)
    branch.providers.register(_StubProvider("still here"), priority=1)

    result = await branch.communicate(instruction="hello", skip_validation=True)

    sent_messages = branch.chat_model.invoke.call_args.kwargs["messages"]
    first_content = sent_messages[0]["content"]
    assert "still here" in first_content
    assert result == "ok"


@pytest.mark.asyncio
async def test_non_string_provider_output_fails_open_end_to_end(make_mocked_branch):
    branch = make_mocked_branch(system="You are helpful", response="ok")
    invalid_output = object()
    branch.providers.register(_StubProvider(invalid_output, name="invalid"))
    branch.providers.register(_StubProvider("valid context", name="valid"))

    result = await branch.communicate(instruction="hello", skip_validation=True)

    sent_messages = branch.chat_model.invoke.call_args.kwargs["messages"]
    first_content = sent_messages[0]["content"]
    assert result == "ok"
    assert "valid context" in first_content
    assert str(invalid_output) not in first_content
    assert branch.last_context_report.blocks == ["valid context"]
    assert branch.last_context_report.failed == ["invalid"]


@pytest.mark.asyncio
async def test_chat_only_branch_no_tools_works_with_providers(make_mocked_branch):
    """Knowledge injection must work for chat-only branches with zero tools (gate Q1)."""
    branch = make_mocked_branch(system="You are helpful", response="ok")
    assert branch.tools == {}
    branch.providers.register(_StubProvider("floor-knowledge"))

    result = await branch.communicate(instruction="hello", skip_validation=True)

    assert result == "ok"
    sent_messages = branch.chat_model.invoke.call_args.kwargs["messages"]
    assert any("floor-knowledge" in m["content"] for m in sent_messages)


# ---------------------------------------------------------------------------
# Systemless branches: no render target — providers skipped, observably
# ---------------------------------------------------------------------------


class _CountingProvider:
    name = "counter"

    def __init__(self):
        self.calls = 0

    async def provide(self, branch, instruction):
        self.calls += 1
        return "should never render"


@pytest.mark.asyncio
async def test_systemless_branch_skips_providers_with_observable_report(
    make_mocked_branch,
):
    branch = make_mocked_branch(response="ok")
    assert branch.msgs.system is None
    counting = _CountingProvider()
    branch.providers.register(counting, name="counter")

    result = await branch.communicate(instruction="hello", skip_validation=True)

    assert result == "ok"
    assert counting.calls == 0
    report = branch.last_context_report
    assert isinstance(report, ProviderReport)
    assert report.skipped == ["counter"]
    assert report.blocks == [] and report.fired == []
    sent_messages = branch.chat_model.invoke.call_args.kwargs["messages"]
    assert all("should never render" not in m["content"] for m in sent_messages)


@pytest.mark.asyncio
async def test_last_context_report_populated_on_systemful_turn(make_mocked_branch):
    branch = make_mocked_branch(system="You are helpful", response="ok")
    branch.providers.register(_StubProvider("knowledge"), name="kp")

    await branch.communicate(instruction="hello", skip_validation=True)

    report = branch.last_context_report
    assert isinstance(report, ProviderReport)
    assert [f["provider_name"] for f in report.fired] == ["kp"]
    assert report.fired[0]["tokens"] > 0


@pytest.mark.asyncio
async def test_last_context_report_visible_after_child_task_turn(make_mocked_branch):
    branch = make_mocked_branch(system="You are helpful", response="ok")
    branch.providers.register(_StubProvider("child context"), name="child")
    assert branch.last_context_report is None

    turn = asyncio.create_task(branch.communicate(instruction="hello", skip_validation=True))
    result = await turn

    report = branch.last_context_report
    assert result == "ok"
    assert isinstance(report, ProviderReport)
    assert report.blocks == ["child context"]
    assert [entry["provider_name"] for entry in report.fired] == ["child"]


@pytest.mark.asyncio
async def test_concurrent_turn_reports_are_task_scoped_and_prompts_are_isolated(
    make_mocked_branch,
):
    branch = make_mocked_branch(system="You are helpful", response="ok")

    class _InstructionProvider:
        name = "instruction-context"

        async def provide(self, branch, instruction):
            return f"CTX-{instruction.content.instruction}"

    branch.providers.register(_InstructionProvider())

    first_invoke_started = asyncio.Event()
    release_first_invoke = asyncio.Event()
    original_invoke = branch.chat_model.invoke.side_effect
    rendered_prompts = {}

    async def invoke(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        turn = "first" if "CTX-first" in prompt else "second"
        rendered_prompts[turn] = prompt
        if turn == "first":
            first_invoke_started.set()
            await release_first_invoke.wait()
        return await original_invoke(**kwargs)

    branch.chat_model.invoke.side_effect = invoke

    async def communicate_and_read_report(turn):
        result = await branch.communicate(instruction=turn, skip_validation=True)
        return result, branch.last_context_report

    first_task = asyncio.create_task(communicate_and_read_report("first"))
    await first_invoke_started.wait()
    second_result, second_report = await communicate_and_read_report("second")
    release_first_invoke.set()
    first_result, first_report = await first_task

    assert first_result == second_result == "ok"
    assert first_report.blocks == ["CTX-first"]
    assert second_report.blocks == ["CTX-second"]
    assert "CTX-second" not in rendered_prompts["first"]
    assert "CTX-first" not in rendered_prompts["second"]
