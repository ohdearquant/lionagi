# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for run lifecycle signal emission and ReAct double-wrap fix.

Covers:
  R0 — Branch.run() / run() emits RunStart, RunEnd, RunFailed via observer.
  R1 — Branch.ReAct() emits exactly ONE RunStart per call (no double-wrap).
  R2 — Bug #1347: empty / empty-dict "error" chunk does not raise RuntimeError
       (resume end-of-stream treated as clean completion).
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import pytest

from lionagi.operations.run.run import RunParam, run
from lionagi.service.imodel import iModel
from lionagi.service.types.stream_chunk import StreamChunk
from lionagi.session.branch import Branch
from lionagi.session.session import Session
from lionagi.session.signal import RunEnd, RunFailed, RunStart

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_fake_cli_model(chunks: list[StreamChunk], session_id: str | None = None):
    """Return an iModel patched to behave as a CLI endpoint yielding *chunks*."""
    m = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    endpoint_ns = types.SimpleNamespace(
        is_cli=True,
        session_id=session_id,
        to_dict=lambda: {"type": "fake_cli", "session_id": session_id},
    )
    m.endpoint = endpoint_ns
    m.streaming_process_func = None

    async def create_event(**kw):
        return object()

    m.create_event = create_event
    m.executor = types.SimpleNamespace(append=AsyncMock(), config={})

    async def stream(api_call=None):
        for chunk in chunks:
            yield chunk

    m.stream = stream
    return m


async def _drain(gen) -> list:
    """Collect all items from an async generator."""
    return [item async for item in gen]


# ---------------------------------------------------------------------------
# R0 — run() emits RunStart / RunEnd on the observer bus
# ---------------------------------------------------------------------------


async def test_run_generator_emits_run_start_and_run_end():
    """run() must emit RunStart before any yield and RunEnd in finally."""
    s = Session()
    model = _make_fake_cli_model([StreamChunk(type="text", content="hello")])
    s.default_branch.chat_model = model

    starts, ends = [], []
    s.observe(RunStart, lambda sig, _: starts.append(sig))
    s.observe(RunEnd, lambda sig, _: ends.append(sig))

    await _drain(run(s.default_branch, "go", RunParam()))

    assert len(starts) == 1, f"expected 1 RunStart, got {len(starts)}"
    assert len(ends) == 1, f"expected 1 RunEnd, got {len(ends)}"


async def test_run_generator_emits_run_failed_on_exception():
    """run() must emit RunFailed when the stream raises."""
    s = Session()
    model = _make_fake_cli_model([StreamChunk(type="error", content="fatal error")])
    s.default_branch.chat_model = model

    failures, ends = [], []
    s.observe(RunFailed, lambda sig, _: failures.append(sig.data))
    s.observe(RunEnd, lambda sig, _: ends.append(sig))

    with pytest.raises(RuntimeError, match="fatal error"):
        await _drain(run(s.default_branch, "go", RunParam()))

    assert len(failures) == 1, f"expected 1 RunFailed, got {len(failures)}"
    assert isinstance(failures[0], RuntimeError)
    assert len(ends) == 0, "RunEnd must NOT fire when run() raises"


async def test_run_generator_no_signals_without_observer():
    """run() must not raise when there is no observer (standalone branch)."""
    model = _make_fake_cli_model([StreamChunk(type="text", content="ok")])
    branch = Branch()
    branch.chat_model = model

    assert branch._observer is None
    # Should complete without error
    await _drain(run(branch, "go", RunParam()))


async def test_run_generator_run_start_before_first_yield():
    """RunStart must be emitted before any message is received by the consumer."""
    s = Session()
    model = _make_fake_cli_model([StreamChunk(type="text", content="hi")])
    s.default_branch.chat_model = model

    received_order: list[str] = []
    s.observe(RunStart, lambda sig, _: received_order.append("RunStart"))

    async for msg in run(s.default_branch, "go", RunParam()):
        received_order.append(type(msg).__name__)

    # RunStart should be first
    assert received_order[0] == "RunStart", f"order was: {received_order}"


# ---------------------------------------------------------------------------
# R1 — Branch.ReAct() emits exactly ONE RunStart (no double-wrap)
# ---------------------------------------------------------------------------


async def test_react_emits_exactly_one_run_start():
    """Branch.ReAct() must emit a single RunStart regardless of extension count."""
    s = Session()
    starts = []
    ends = []
    s.observe(RunStart, lambda sig, _: starts.append(sig))
    s.observe(RunEnd, lambda sig, _: ends.append(sig))

    from lionagi.operations.ReAct.utils import Analysis, ReActAnalysis

    call_count = 0

    async def mock_operate(*args, **kw):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return ReActAnalysis(analysis="thinking", extension_needed=False)
        return Analysis(answer="done")

    with patch(
        "lionagi.operations.operate.operate.operate",
        new=AsyncMock(side_effect=mock_operate),
    ):
        await s.default_branch.ReAct(
            instruct={"instruction": "solve it"},
            extension_allowed=False,
        )

    assert len(starts) == 1, f"expected 1 RunStart, got {len(starts)}: double-wrap present"
    assert len(ends) == 1, f"expected 1 RunEnd, got {len(ends)}"


async def test_react_emits_run_failed_on_exception():
    """Branch.ReAct() must emit RunFailed when the inner call raises."""
    s = Session()
    failures = []
    ends = []
    s.observe(RunFailed, lambda sig, _: failures.append(sig.data))
    s.observe(RunEnd, lambda sig, _: ends.append(sig))

    async def boom_operate(*args, **kw):
        raise ValueError("react-failed")

    with patch(
        "lionagi.operations.operate.operate.operate",
        new=AsyncMock(side_effect=boom_operate),
    ):
        with pytest.raises(ValueError, match="react-failed"):
            await s.default_branch.ReAct(
                instruct={"instruction": "fail"},
                extension_allowed=False,
            )

    assert len(failures) == 1
    assert isinstance(failures[0], ValueError)
    assert len(ends) == 0, "RunEnd must not fire when ReAct raises"


async def test_react_no_signals_without_observer():
    """Branch.ReAct() must not raise when there is no session observer."""
    branch = Branch()
    assert branch._observer is None

    from lionagi.operations.ReAct.utils import Analysis, ReActAnalysis

    call_count = 0

    async def mock_operate(*args, **kw):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            return ReActAnalysis(analysis="ok", extension_needed=False)
        return Analysis(answer="result")

    with patch(
        "lionagi.operations.operate.operate.operate",
        new=AsyncMock(side_effect=mock_operate),
    ):
        result = await branch.ReAct(
            instruct={"instruction": "standalone"},
            extension_allowed=False,
        )
    assert result == "result"


# ---------------------------------------------------------------------------
# R2 — Bug #1347: empty / empty-dict error chunk treated as end-of-stream
# ---------------------------------------------------------------------------


async def test_run_empty_error_chunk_treated_as_end_of_stream():
    """Empty-content error chunk must not raise; stream completes cleanly."""
    model = _make_fake_cli_model(
        [
            StreamChunk(type="text", content="partial result"),
            StreamChunk(type="error", content=""),  # resume end-of-stream
        ]
    )
    branch = Branch()
    branch.chat_model = model

    from lionagi.protocols.messages import AssistantResponse

    msgs = await _drain(run(branch, "resume", RunParam()))
    text_msgs = [m for m in msgs if isinstance(m, AssistantResponse)]

    # The partial result text must still be emitted; the empty error must not raise
    assert len(text_msgs) >= 1, "expected at least one AssistantResponse from partial result"
    assert text_msgs[0].response == "partial result"


async def test_run_empty_dict_error_chunk_treated_as_end_of_stream():
    """Error chunk with content='{}' (codex resume end-of-stream) must not raise."""
    model = _make_fake_cli_model(
        [
            StreamChunk(type="text", content="answer"),
            StreamChunk(type="error", content="{}"),
        ]
    )
    branch = Branch()
    branch.chat_model = model

    from lionagi.protocols.messages import AssistantResponse

    msgs = await _drain(run(branch, "resume", RunParam()))
    text_msgs = [m for m in msgs if isinstance(m, AssistantResponse)]

    assert len(text_msgs) >= 1
    assert text_msgs[0].response == "answer"


async def test_run_non_empty_error_chunk_still_raises():
    """Error chunk with real content must still raise RuntimeError."""
    model = _make_fake_cli_model([StreamChunk(type="error", content="connection refused")])
    branch = Branch()
    branch.chat_model = model

    with pytest.raises(RuntimeError, match="connection refused"):
        await _drain(run(branch, "go", RunParam()))


async def test_run_resume_session_produces_non_empty_stream():
    """Simulate a resumed session: text before empty-error gives non-empty output.

    This is the regression for issue #1347 where ``li agent -r`` returned an
    empty stream because the empty "error" sentinel raised before any content
    was yielded back to the caller.
    """
    from lionagi.protocols.messages import AssistantResponse

    # Simulate what a CLI provider sends on resume: system chunk with session_id,
    # then content, then an empty error sentinel marking session end.
    model = _make_fake_cli_model(
        [
            StreamChunk(type="system", metadata={"session_id": "resumed-sid"}),
            StreamChunk(type="text", content="Resumed response"),
            StreamChunk(type="error", content="{}"),  # end-of-stream sentinel
        ],
        session_id="prior-sid",
    )
    branch = Branch()
    branch.chat_model = model

    msgs = await _drain(run(branch, "", RunParam()))
    text_msgs = [m for m in msgs if isinstance(m, AssistantResponse)]

    assert len(text_msgs) >= 1, "resume must yield content, not an empty stream"
    assert "Resumed response" in text_msgs[0].response
