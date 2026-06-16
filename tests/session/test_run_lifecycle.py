# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for run lifecycle signal emission and ReAct double-wrap fix."""

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
# empty / empty-dict error chunk treated as end-of-stream
# ---------------------------------------------------------------------------


async def test_run_benign_eos_error_chunk_treated_as_end_of_stream():
    """Error chunk with benign_eos=True must not raise; stream completes cleanly."""
    model = _make_fake_cli_model(
        [
            StreamChunk(type="text", content="partial result"),
            StreamChunk(type="error", content="", metadata={"benign_eos": True}),
        ]
    )
    branch = Branch()
    branch.chat_model = model

    from lionagi.protocols.messages import AssistantResponse

    msgs = await _drain(run(branch, "resume", RunParam()))
    text_msgs = [m for m in msgs if isinstance(m, AssistantResponse)]

    assert len(text_msgs) >= 1, "expected at least one AssistantResponse from partial result"
    assert text_msgs[0].response == "partial result"


async def test_run_benign_eos_empty_dict_error_chunk_treated_as_end_of_stream():
    """Error chunk with content='{}' and benign_eos=True must not raise."""
    model = _make_fake_cli_model(
        [
            StreamChunk(type="text", content="answer"),
            StreamChunk(type="error", content="{}", metadata={"benign_eos": True}),
        ]
    )
    branch = Branch()
    branch.chat_model = model

    from lionagi.protocols.messages import AssistantResponse

    msgs = await _drain(run(branch, "resume", RunParam()))
    text_msgs = [m for m in msgs if isinstance(m, AssistantResponse)]

    assert len(text_msgs) >= 1
    assert text_msgs[0].response == "answer"


async def test_run_empty_error_chunk_without_marker_raises():
    """Empty-content error chunk without benign_eos=True must surface as RunFailed."""
    model = _make_fake_cli_model(
        [
            StreamChunk(type="text", content="partial result"),
            StreamChunk(type="error", content=""),  # NO benign_eos marker
        ]
    )
    branch = Branch()
    branch.chat_model = model

    with pytest.raises(RuntimeError):
        await _drain(run(branch, "go", RunParam()))


async def test_run_non_empty_error_chunk_still_raises():
    """Error chunk with real content must still raise RuntimeError."""
    model = _make_fake_cli_model([StreamChunk(type="error", content="connection refused")])
    branch = Branch()
    branch.chat_model = model

    with pytest.raises(RuntimeError, match="connection refused"):
        await _drain(run(branch, "go", RunParam()))


async def test_run_resume_session_produces_non_empty_stream():
    """Resumed session: text before benign-eos gives non-empty output."""
    from lionagi.protocols.messages import AssistantResponse

    model = _make_fake_cli_model(
        [
            StreamChunk(type="system", metadata={"session_id": "resumed-sid"}),
            StreamChunk(type="text", content="Resumed response"),
            StreamChunk(type="error", content="{}", metadata={"benign_eos": True}),  # benign EOS
        ],
        session_id="prior-sid",
    )
    branch = Branch()
    branch.chat_model = model

    msgs = await _drain(run(branch, "", RunParam()))
    text_msgs = [m for m in msgs if isinstance(m, AssistantResponse)]

    assert len(text_msgs) >= 1, "resume must yield content, not an empty stream"
    assert "Resumed response" in text_msgs[0].response


# ---------------------------------------------------------------------------
# abandoned generators emit exactly one terminal signal
# ---------------------------------------------------------------------------


async def test_run_aclose_after_instruction_emits_run_end():
    """aclose() after the instruction must emit RunEnd."""
    s = Session()
    model = _make_fake_cli_model([StreamChunk(type="text", content="hello")])
    s.default_branch.chat_model = model

    starts, ends, failures = [], [], []
    s.observe(RunStart, lambda sig, _: starts.append(sig))
    s.observe(RunEnd, lambda sig, _: ends.append(sig))
    s.observe(RunFailed, lambda sig, _: failures.append(sig))

    gen = run(s.default_branch, "go", RunParam())
    await gen.__anext__()
    await gen.aclose()

    assert len(starts) == 1, f"expected 1 RunStart, got {len(starts)}"
    assert len(ends) == 1, f"expected 1 RunEnd on aclose, got {len(ends)}"
    assert len(failures) == 0, "aclose after instruction must not emit RunFailed"


async def test_run_break_after_instruction_emits_run_end():
    """break after instruction then explicit aclose() must emit RunEnd."""
    s = Session()
    model = _make_fake_cli_model([StreamChunk(type="text", content="hello")])
    s.default_branch.chat_model = model

    starts, ends, failures = [], [], []
    s.observe(RunStart, lambda sig, _: starts.append(sig))
    s.observe(RunEnd, lambda sig, _: ends.append(sig))
    s.observe(RunFailed, lambda sig, _: failures.append(sig))

    from lionagi.protocols.messages import Instruction as _Instruction

    gen = run(s.default_branch, "go", RunParam())
    async for msg in gen:
        if isinstance(msg, _Instruction):
            break
    await gen.aclose()

    assert len(starts) == 1
    assert len(ends) == 1, f"expected RunEnd after break+aclose, got {len(ends)}"
    assert len(failures) == 0


async def test_run_break_after_response_emits_run_end():
    """break after AssistantResponse + aclose() must emit RunEnd, not RunFailed."""
    s = Session()
    model = _make_fake_cli_model(
        [
            StreamChunk(type="text", content="answer"),
        ]
    )
    s.default_branch.chat_model = model

    starts, ends, failures = [], [], []
    s.observe(RunStart, lambda sig, _: starts.append(sig))
    s.observe(RunEnd, lambda sig, _: ends.append(sig))
    s.observe(RunFailed, lambda sig, _: failures.append(sig))

    from lionagi.protocols.messages import AssistantResponse as _AR

    gen = run(s.default_branch, "go", RunParam())
    async for msg in gen:
        if isinstance(msg, _AR):
            break
    await gen.aclose()

    assert len(starts) == 1
    assert len(ends) == 1, f"expected RunEnd after break+aclose, got {len(ends)}"
    assert len(failures) == 0, f"break+aclose must not produce RunFailed; got {failures}"


async def test_run_aclose_before_first_yield_no_signals():
    s = Session()
    model = _make_fake_cli_model([StreamChunk(type="text", content="hi")])
    s.default_branch.chat_model = model

    starts, ends = [], []
    s.observe(RunStart, lambda sig, _: starts.append(sig))
    s.observe(RunEnd, lambda sig, _: ends.append(sig))

    gen = run(s.default_branch, "go", RunParam())
    await gen.aclose()

    assert len(starts) == 0, "body not entered, no RunStart expected"
    assert len(ends) == 0


# ---------------------------------------------------------------------------
# CLI-backed ReAct emits exactly one RunStart total
# ---------------------------------------------------------------------------


async def test_react_cli_backed_emits_single_run_start():
    """CLI-backed Branch.ReAct() must emit exactly one RunStart across all rounds."""
    import asyncio

    from lionagi.operations.ReAct.utils import Analysis, ReActAnalysis
    from lionagi.operations.run.run import RunParam, run_and_collect

    s = Session()
    starts, ends, failures = [], [], []
    s.observe(RunStart, lambda sig, _: starts.append(sig))
    s.observe(RunEnd, lambda sig, _: ends.append(sig))
    s.observe(RunFailed, lambda sig, _: failures.append(sig))

    call_count = 0

    async def mock_operate_via_run_and_collect(*args, **kw):
        nonlocal call_count
        call_count += 1
        branch = args[0] if args else kw.get("branch")

        cli_model = _make_fake_cli_model([StreamChunk(type="text", content="intermediate answer")])
        if branch is not None:
            branch.chat_model = cli_model
            await run_and_collect(branch, "inner", RunParam(), skip_validation=True)

        # Final round: return a terminal Analysis so ReAct exits
        return Analysis(answer="done")

    with patch(
        "lionagi.operations.operate.operate.operate",
        new=AsyncMock(side_effect=mock_operate_via_run_and_collect),
    ):
        await s.default_branch.ReAct(
            instruct={"instruction": "solve it"},
            extension_allowed=False,
        )

    assert len(starts) == 1, (
        f"CLI-backed ReAct emitted {len(starts)} RunStart (expected 1); "
        "N+1 regression: nested run() calls emitted their own lifecycle signals"
    )
    assert len(ends) == 1, f"expected 1 RunEnd, got {len(ends)}"
    assert len(failures) == 0, f"unexpected RunFailed signals: {failures}"


async def test_concurrent_runs_on_same_branch_not_suppressed():
    """Concurrent run() calls on the same branch must each emit their own RunStart/RunEnd."""
    import asyncio

    from lionagi.session._lifecycle_ctx import suppress_lifecycle_var

    s = Session()
    starts = []
    s.observe(RunStart, lambda sig, _: starts.append(sig))

    async def suppressed_task():
        token = suppress_lifecycle_var.set(True)
        try:
            await asyncio.sleep(0)
        finally:
            suppress_lifecycle_var.reset(token)

    async def independent_run_task():
        model = _make_fake_cli_model([StreamChunk(type="text", content="hello")])
        s.default_branch.chat_model = model
        await _drain(run(s.default_branch, "go", RunParam()))

    await asyncio.gather(suppressed_task(), independent_run_task())

    assert len(starts) >= 1, (
        "Independent run() in a separate task must emit RunStart even when "
        "another task has suppress_lifecycle_var=True; ContextVar leak detected"
    )


async def test_run_start_observer_exception_does_not_abort_run():
    """A raising RunStart observer must not prevent run() from proceeding."""
    import asyncio

    s = Session()
    model = _make_fake_cli_model([StreamChunk(type="text", content="answer")])
    s.default_branch.chat_model = model

    boom_raised = False

    def boom_on_run_start(sig, _ctx):
        nonlocal boom_raised
        if isinstance(sig, RunStart):
            boom_raised = True
            raise RuntimeError("RunStart observer boom")

    s.observe(RunStart, boom_on_run_start)

    ends = []
    s.observe(RunEnd, lambda sig, _: ends.append(sig))

    from lionagi.protocols.messages import AssistantResponse

    msgs = await _drain(run(s.default_branch, "go", RunParam()))

    assert boom_raised, "RunStart boom observer was never invoked"
    text_msgs = [m for m in msgs if isinstance(m, AssistantResponse)]
    assert len(text_msgs) >= 1, (
        "run() must proceed and yield content even when RunStart observer raises"
    )


async def test_react_run_start_observer_exception_does_not_abort_react():
    """A raising RunStart observer must not abort Branch.ReAct()."""
    s = Session()
    boom_raised = False

    def boom_on_run_start(sig, _ctx):
        nonlocal boom_raised
        if isinstance(sig, RunStart):
            boom_raised = True
            raise RuntimeError("ReAct RunStart observer boom")

    s.observe(RunStart, boom_on_run_start)

    from lionagi.operations.ReAct.utils import Analysis

    async def mock_operate(*args, **kw):
        return Analysis(answer="despite observer boom")

    with patch(
        "lionagi.operations.operate.operate.operate",
        new=AsyncMock(side_effect=mock_operate),
    ):
        # Must NOT raise
        result = await s.default_branch.ReAct(
            instruct={"instruction": "test"},
            extension_allowed=False,
        )

    assert boom_raised, "RunStart boom observer was never invoked on ReAct"
    assert result is not None, "ReAct must return a result despite RunStart observer boom"


# ---------------------------------------------------------------------------
# observer exception during cleanup preserves streaming_process_func
# ---------------------------------------------------------------------------


async def test_run_observer_exception_on_run_end_restores_stream_func():
    """streaming_process_func must be restored even when a RunEnd observer raises."""
    s = Session()
    model = _make_fake_cli_model([StreamChunk(type="text", content="ok")])
    s.default_branch.chat_model = model

    sentinel = object()
    model.streaming_process_func = sentinel  # mark the original value

    boom_raised = False

    def boom_on_run_end(sig, _ctx):
        nonlocal boom_raised
        if isinstance(sig, RunEnd):
            boom_raised = True
            raise RuntimeError("observer boom")

    s.observe(RunEnd, boom_on_run_end)

    await _drain(run(s.default_branch, "go", RunParam()))

    assert boom_raised, "boom handler was never called"
    assert model.streaming_process_func is sentinel, (
        "streaming_process_func was not restored after observer exception on RunEnd"
    )


async def test_run_observer_exception_on_run_end_preserves_run_outcome():
    """Run outcome must be preserved when a RunEnd observer raises."""
    s = Session()
    model = _make_fake_cli_model([StreamChunk(type="text", content="result")])
    s.default_branch.chat_model = model

    def boom_on_run_end(sig, _ctx):
        if isinstance(sig, RunEnd):
            raise RuntimeError("observer boom")

    s.observe(RunEnd, boom_on_run_end)

    from lionagi.protocols.messages import AssistantResponse

    msgs = await _drain(run(s.default_branch, "go", RunParam()))
    text_msgs = [m for m in msgs if isinstance(m, AssistantResponse)]
    assert len(text_msgs) >= 1, "run outcome not preserved after observer exception"
