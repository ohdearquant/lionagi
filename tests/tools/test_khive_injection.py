# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for KhiveInjectionProvider (ADR-0100 PR2): query construction, cadence
gating, the recall+auto_feedback round-trip, failure containment, writeback
extraction, and module-load purity. The khive MCP transport is fully mocked —
no live daemon required."""

import json
import sys
from unittest.mock import AsyncMock, patch

import pytest

from lionagi.operations.fields import ActionResponseModel
from lionagi.tools.khive_injection import (
    ComposePolicy,
    KhiveInjectionPolicy,
    KhiveInjectionProvider,
    RecallPolicy,
    WritebackPolicy,
    _extract_writeback_pairs,
)

# ---------------------------------------------------------------------------
# Policy validation
# ---------------------------------------------------------------------------


def test_profile_id_required():
    with pytest.raises(ValueError, match="profile_id is required"):
        KhiveInjectionPolicy(profile_id="")


def test_invalid_cadence_rejected():
    with pytest.raises(ValueError, match="cadence must be one of"):
        KhiveInjectionPolicy(profile_id="implementer-recall-v1", cadence="on_task_shift")


def test_defaults_mirror_design_yaml():
    policy = KhiveInjectionPolicy(profile_id="implementer-recall-v1")
    assert policy.enabled is True
    assert policy.snapshot_id is None
    assert policy.recall == RecallPolicy(limit=5, min_score=0.4, max_tokens=800)
    assert policy.compose == ComposePolicy(enabled=False, max_tokens=2000)
    assert policy.cadence == "first_turn"
    assert policy.writeback == WritebackPolicy(enabled=False, salience_cap=0.4, tags=())


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _khive_recall_response(result_id="abc123", content="a prior lesson"):
    return json.dumps(
        {
            "results": [
                {
                    "ok": True,
                    "tool": "memory.recall",
                    "result": [{"id": result_id, "content": content, "score": 0.7}],
                }
            ],
            "summary": {"total": 1, "succeeded": 1, "failed": 0},
        }
    )


def _mcp_result(text):
    class _Item:
        def __init__(self, text):
            self.text = text

    class _Result:
        def __init__(self, text):
            self.content = [_Item(text)]

    return _Result(text)


class _FakeInstruction:
    def __init__(self, rendered):
        self.rendered = rendered


class _FakeBranch:
    def __init__(self, name="tester", last_response=None):
        self.name = name
        self.msgs = _FakeMsgs(last_response)


class _FakeMsgs:
    def __init__(self, last_response):
        self.last_response = last_response


@pytest.fixture
def patched_transport():
    """Patch the MCP client at the module boundary; returns the mocked call_tool."""
    call_tool = AsyncMock()
    fake_client = AsyncMock()
    fake_client.call_tool = call_tool

    with patch(
        "lionagi.service.connections.mcp_wrapper.MCPConnectionPool.get_client",
        AsyncMock(return_value=fake_client),
    ):
        yield call_tool


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_construction_uses_role_and_truncated_task_text(patched_transport):
    patched_transport.return_value = _mcp_result(_khive_recall_response())
    policy = KhiveInjectionPolicy(profile_id="implementer-recall-v1")
    provider = KhiveInjectionProvider(policy)
    branch = _FakeBranch(name="implementer")
    long_task = "x" * 1000
    instruction = _FakeInstruction(rendered=long_task)

    await provider.provide(branch, instruction)

    recall_call_ops = patched_transport.call_args_list[0].args[1]["ops"]
    assert "role=implementer" in recall_call_ops
    assert "x" * 400 in recall_call_ops
    assert "x" * 401 not in recall_call_ops


# ---------------------------------------------------------------------------
# Cadence gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_turn_cadence_fires_when_no_prior_response(patched_transport):
    patched_transport.return_value = _mcp_result(_khive_recall_response())
    policy = KhiveInjectionPolicy(profile_id="implementer-recall-v1", cadence="first_turn")
    provider = KhiveInjectionProvider(policy)
    branch = _FakeBranch(last_response=None)

    text = await provider.provide(branch, _FakeInstruction("hello"))

    assert text is not None
    assert patched_transport.called


@pytest.mark.asyncio
async def test_first_turn_cadence_skips_when_prior_response_exists(patched_transport):
    policy = KhiveInjectionPolicy(profile_id="implementer-recall-v1", cadence="first_turn")
    provider = KhiveInjectionProvider(policy)
    branch = _FakeBranch(last_response=object())

    text = await provider.provide(branch, _FakeInstruction("hello"))

    assert text is None
    assert not patched_transport.called


@pytest.mark.asyncio
async def test_every_turn_cadence_always_fires(patched_transport):
    patched_transport.return_value = _mcp_result(_khive_recall_response())
    policy = KhiveInjectionPolicy(profile_id="implementer-recall-v1", cadence="every_turn")
    provider = KhiveInjectionProvider(policy)
    branch = _FakeBranch(last_response=object())

    text = await provider.provide(branch, _FakeInstruction("hello"))

    assert text is not None
    assert patched_transport.called


@pytest.mark.asyncio
async def test_disabled_policy_never_calls_transport(patched_transport):
    policy = KhiveInjectionPolicy(profile_id="implementer-recall-v1", enabled=False)
    provider = KhiveInjectionProvider(policy)
    branch = _FakeBranch(last_response=None)

    text = await provider.provide(branch, _FakeInstruction("hello"))

    assert text is None
    assert not patched_transport.called


# ---------------------------------------------------------------------------
# auto_feedback emitted in the same round-trip, explicit profile_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_feedback_emitted_with_explicit_profile_id(patched_transport):
    patched_transport.return_value = _mcp_result(_khive_recall_response(result_id="the-first-id"))
    policy = KhiveInjectionPolicy(profile_id="implementer-recall-v1")
    provider = KhiveInjectionProvider(policy)
    branch = _FakeBranch(last_response=None)

    await provider.provide(branch, _FakeInstruction("hello"))

    ops_calls = [c.args[1]["ops"] for c in patched_transport.call_args_list]
    assert any(op.startswith("memory.recall(") for op in ops_calls)
    feedback_calls = [op for op in ops_calls if op.startswith("brain.auto_feedback(")]
    assert len(feedback_calls) == 1
    assert '"the-first-id"' in feedback_calls[0]
    assert 'served_by_profile_id="implementer-recall-v1"' in feedback_calls[0]


@pytest.mark.asyncio
async def test_no_auto_feedback_when_recall_empty(patched_transport):
    empty_response = json.dumps(
        {"results": [{"ok": True, "tool": "memory.recall", "result": []}], "summary": {}}
    )
    patched_transport.return_value = _mcp_result(empty_response)
    policy = KhiveInjectionPolicy(profile_id="implementer-recall-v1")
    provider = KhiveInjectionProvider(policy)
    branch = _FakeBranch(last_response=None)

    text = await provider.provide(branch, _FakeInstruction("hello"))

    assert text is None
    ops_calls = [c.args[1]["ops"] for c in patched_transport.call_args_list]
    assert not any(op.startswith("brain.auto_feedback(") for op in ops_calls)


# ---------------------------------------------------------------------------
# Transport failure containment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_failure_returns_none_turn_proceeds():
    policy = KhiveInjectionPolicy(profile_id="implementer-recall-v1")
    provider = KhiveInjectionProvider(policy)
    branch = _FakeBranch(last_response=None)

    with patch(
        "lionagi.service.connections.mcp_wrapper.MCPConnectionPool.get_client",
        AsyncMock(side_effect=RuntimeError("khive daemon unreachable")),
    ):
        text = await provider.provide(branch, _FakeInstruction("hello"))

    assert text is None


@pytest.mark.asyncio
async def test_auto_feedback_failure_does_not_fail_the_turn(patched_transport):
    async def _side_effect(tool_name, kwargs):
        if kwargs["ops"].startswith("brain.auto_feedback("):
            raise RuntimeError("feedback endpoint down")
        return _mcp_result(_khive_recall_response())

    patched_transport.side_effect = _side_effect
    policy = KhiveInjectionPolicy(profile_id="implementer-recall-v1")
    provider = KhiveInjectionProvider(policy)
    branch = _FakeBranch(last_response=None)

    text = await provider.provide(branch, _FakeInstruction("hello"))

    assert text is not None
    assert "a prior lesson" in text


# ---------------------------------------------------------------------------
# Compose
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compose_included_when_enabled(patched_transport):
    async def _side_effect(tool_name, kwargs):
        if kwargs["ops"].startswith("knowledge.compose("):
            return _mcp_result(
                json.dumps(
                    {
                        "results": [
                            {"ok": True, "tool": "knowledge.compose", "result": "corpus frame"}
                        ]
                    }
                )
            )
        return _mcp_result(_khive_recall_response())

    patched_transport.side_effect = _side_effect
    policy = KhiveInjectionPolicy(
        profile_id="implementer-recall-v1", compose=ComposePolicy(enabled=True, max_tokens=2000)
    )
    provider = KhiveInjectionProvider(policy)
    branch = _FakeBranch(last_response=None)

    text = await provider.provide(branch, _FakeInstruction("hello"))

    assert "corpus frame" in text
    ops_calls = [c.args[1]["ops"] for c in patched_transport.call_args_list]
    assert any(op.startswith("knowledge.compose(") for op in ops_calls)


@pytest.mark.asyncio
async def test_compose_not_called_when_disabled(patched_transport):
    patched_transport.return_value = _mcp_result(_khive_recall_response())
    policy = KhiveInjectionPolicy(profile_id="implementer-recall-v1")
    provider = KhiveInjectionProvider(policy)
    branch = _FakeBranch(last_response=None)

    await provider.provide(branch, _FakeInstruction("hello"))

    ops_calls = [c.args[1]["ops"] for c in patched_transport.call_args_list]
    assert not any(op.startswith("knowledge.compose(") for op in ops_calls)


# ---------------------------------------------------------------------------
# Token cap truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_truncated_to_recall_max_tokens(patched_transport):
    huge_content = "lesson " * 5000
    patched_transport.return_value = _mcp_result(_khive_recall_response(content=huge_content))
    policy = KhiveInjectionPolicy(
        profile_id="implementer-recall-v1",
        recall=RecallPolicy(limit=5, min_score=0.4, max_tokens=50),
    )
    provider = KhiveInjectionProvider(policy)
    branch = _FakeBranch(last_response=None)

    text = await provider.provide(branch, _FakeInstruction("hello"))

    from lionagi.service.token_calculator import TokenCalculator

    assert text is not None
    assert TokenCalculator.tokenize(text) <= 50
    assert len(text) < len(huge_content)


# ---------------------------------------------------------------------------
# Writeback pair extraction + salience cap
# ---------------------------------------------------------------------------


def _resp(function, output):
    return ActionResponseModel(function=function, arguments={}, output=output)


def test_extract_writeback_pairs_matches_error_then_resolution():
    responses = [
        _resp("bash", {"error": "permission denied"}),
        _resp("bash", {"stdout": "ok now"}),
    ]

    pairs = _extract_writeback_pairs(responses)

    assert len(pairs) == 1
    assert pairs[0]["function"] == "bash"
    assert pairs[0]["error"] == "permission denied"
    assert pairs[0]["resolved_by"] == "bash"
    assert pairs[0]["resolution_output"] == {"stdout": "ok now"}


def test_extract_writeback_pairs_ignores_unresolved_error():
    responses = [_resp("bash", {"error": "permission denied"})]

    assert _extract_writeback_pairs(responses) == []


def test_extract_writeback_pairs_no_errors_no_pairs():
    responses = [_resp("bash", {"stdout": "fine"}), _resp("read", {"content": "x"})]

    assert _extract_writeback_pairs(responses) == []


@pytest.mark.asyncio
async def test_writeback_off_by_default_never_calls_transport(patched_transport):
    policy = KhiveInjectionPolicy(profile_id="implementer-recall-v1")
    provider = KhiveInjectionProvider(policy)
    branch = _FakeBranch()
    responses = [_resp("bash", {"error": "boom"}), _resp("bash", {"stdout": "fixed"})]

    await provider.writeback(branch, responses)

    assert not patched_transport.called


@pytest.mark.asyncio
async def test_writeback_writes_capped_salience_and_tags(patched_transport):
    patched_transport.return_value = _mcp_result(json.dumps({"results": [], "summary": {}}))
    policy = KhiveInjectionPolicy(
        profile_id="implementer-recall-v1",
        writeback=WritebackPolicy(enabled=True, salience_cap=0.3, tags=("agent:implementer",)),
    )
    provider = KhiveInjectionProvider(policy)
    branch = _FakeBranch(name="implementer")
    responses = [_resp("bash", {"error": "boom"}), _resp("bash", {"stdout": "fixed"})]

    await provider.writeback(branch, responses)

    assert patched_transport.called
    ops = patched_transport.call_args_list[0].args[1]["ops"]
    assert ops.startswith("memory.remember(")
    assert "salience=0.3" in ops
    assert '"agent:implementer"' in ops


@pytest.mark.asyncio
async def test_writeback_no_pairs_skips_transport(patched_transport):
    policy = KhiveInjectionPolicy(
        profile_id="implementer-recall-v1", writeback=WritebackPolicy(enabled=True)
    )
    provider = KhiveInjectionProvider(policy)
    branch = _FakeBranch()
    responses = [_resp("bash", {"stdout": "fine"})]

    await provider.writeback(branch, responses)

    assert not patched_transport.called


@pytest.mark.asyncio
async def test_writeback_transport_failure_contained(patched_transport):
    patched_transport.side_effect = RuntimeError("khive down")
    policy = KhiveInjectionPolicy(
        profile_id="implementer-recall-v1", writeback=WritebackPolicy(enabled=True)
    )
    provider = KhiveInjectionProvider(policy)
    branch = _FakeBranch()
    responses = [_resp("bash", {"error": "boom"}), _resp("bash", {"stdout": "fixed"})]

    await provider.writeback(branch, responses)  # must not raise


# ---------------------------------------------------------------------------
# Module-load purity: core lionagi import path stays clean without the mcp extra
# ---------------------------------------------------------------------------


def test_module_import_does_not_pull_in_mcp_transport():
    for mod in ("fastmcp", "mcp"):
        sys.modules.pop(mod, None)

    import importlib

    import lionagi.tools.khive_injection as mod_

    importlib.reload(mod_)

    assert "fastmcp" not in sys.modules
    assert "mcp" not in sys.modules


def test_core_lionagi_import_is_clean():
    """Mirrors the reference core-only-importability pattern: importing lionagi
    itself must never require fastmcp/mcp to be installed."""
    for mod in ("fastmcp", "mcp"):
        sys.modules.pop(mod, None)

    import importlib

    import lionagi

    importlib.reload(lionagi)

    assert "fastmcp" not in sys.modules
