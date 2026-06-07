# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import types
from unittest.mock import AsyncMock

import pytest

from lionagi.operations._observe import StopStream as _StopStream
from lionagi.operations._observe import check_control as _check_control
from lionagi.protocols.messages import AssistantResponse
from lionagi.service.imodel import iModel
from lionagi.service.types.stream_chunk import StreamChunk
from lionagi.session.branch import Branch
from lionagi.session.control import LoopBreak, LoopControl, LoopDirective
from lionagi.session.session import Session
from lionagi.session.signal import StructuredOutput

# ---------------------------------------------------------------------------
# LoopDirective / LoopControl / LoopBreak construction
# ---------------------------------------------------------------------------


class TestControlTypes:
    def test_loop_directive_values(self):
        assert LoopDirective.CONTINUE.value == "continue"
        assert LoopDirective.CANCEL.value == "cancel"
        assert LoopDirective.BREAK.value == "break"

    def test_loop_directive_members(self):
        assert set(LoopDirective.__members__) == {"CONTINUE", "CANCEL", "BREAK"}

    def test_loop_control_frozen(self):
        lc = LoopControl(directive=LoopDirective.CANCEL)
        with pytest.raises((AttributeError, TypeError)):
            lc.directive = LoopDirective.BREAK  # frozen dataclass

    def test_loop_control_reason_defaults_none(self):
        lc = LoopControl(LoopDirective.CONTINUE)
        assert lc.reason is None

    def test_loop_control_with_reason(self):
        lc = LoopControl(LoopDirective.CANCEL, reason="timeout")
        assert lc.reason == "timeout"

    def test_loop_break_carries_reason(self):
        exc = LoopBreak(reason="test stop")
        assert exc.reason == "test stop"
        assert "test stop" in str(exc)

    def test_loop_break_no_reason(self):
        exc = LoopBreak()
        assert exc.reason is None

    def test_loop_break_is_exception(self):
        assert issubclass(LoopBreak, Exception)


# ---------------------------------------------------------------------------
# Branch.control() / poll_control() — one-shot semantics
# ---------------------------------------------------------------------------


class TestBranchControlOneShot:
    def test_poll_before_any_control_returns_none(self):
        b = Branch()
        assert b.poll_control() is None

    def test_control_sets_directive(self):
        b = Branch()
        b.control(LoopDirective.CANCEL, reason="stop now")
        ctrl = b.poll_control()
        assert ctrl is not None
        assert ctrl.directive is LoopDirective.CANCEL
        assert ctrl.reason == "stop now"

    def test_one_shot_second_poll_returns_none(self):
        b = Branch()
        b.control(LoopDirective.BREAK)
        b.poll_control()  # consumes
        assert b.poll_control() is None  # cleared

    def test_overwrite_before_poll_last_write_wins(self):
        b = Branch()
        b.control(LoopDirective.CONTINUE)
        b.control(LoopDirective.BREAK)
        ctrl = b.poll_control()
        assert ctrl.directive is LoopDirective.BREAK

    def test_poll_clears_so_can_set_again(self):
        b = Branch()
        b.control(LoopDirective.CANCEL)
        b.poll_control()
        b.control(LoopDirective.CONTINUE)
        ctrl = b.poll_control()
        assert ctrl.directive is LoopDirective.CONTINUE

    def test_returns_loop_control_instance(self):
        b = Branch()
        b.control(LoopDirective.BREAK)
        ctrl = b.poll_control()
        assert isinstance(ctrl, LoopControl)

    def test_control_without_reason(self):
        b = Branch()
        b.control(LoopDirective.BREAK)
        ctrl = b.poll_control()
        assert ctrl.reason is None


# ---------------------------------------------------------------------------
# _check_control behavior
# ---------------------------------------------------------------------------


class TestCheckControl:
    def test_no_directive_is_noop(self):
        b = Branch()
        _check_control(b)  # no exception

    def test_continue_directive_is_noop(self):
        b = Branch()
        b.control(LoopDirective.CONTINUE)
        _check_control(b)  # no exception

    def test_break_directive_raises_loop_break(self):
        b = Branch()
        b.control(LoopDirective.BREAK, reason="halt immediately")
        with pytest.raises(LoopBreak) as exc_info:
            _check_control(b)
        assert exc_info.value.reason == "halt immediately"

    def test_cancel_directive_raises_stop_stream(self):
        b = Branch()
        b.control(LoopDirective.CANCEL, reason="clean stop")
        with pytest.raises(_StopStream) as exc_info:
            _check_control(b)
        assert exc_info.value.reason == "clean stop"

    def test_check_control_consumes_directive_on_break(self):
        b = Branch()
        b.control(LoopDirective.BREAK)
        with pytest.raises(LoopBreak):
            _check_control(b)
        assert b.poll_control() is None  # consumed

    def test_check_control_consumes_directive_on_cancel(self):
        b = Branch()
        b.control(LoopDirective.CANCEL)
        with pytest.raises(_StopStream):
            _check_control(b)
        assert b.poll_control() is None  # consumed


# ---------------------------------------------------------------------------
# CANCEL stops the stream and finally still runs
# ---------------------------------------------------------------------------


class TestCancelFinallyBehavior:
    async def test_cancel_stops_stream_finally_runs(self):
        b = Branch()
        b.control(LoopDirective.CANCEL, reason="observer halt")

        finally_ran = False
        stop_caught = False

        try:
            try:
                _check_control(b)
                pytest.fail("_check_control should have raised _StopStream")
            except _StopStream:
                stop_caught = True
        finally:
            finally_ran = True

        assert stop_caught
        assert finally_ran

    async def test_break_propagates_through_inner_except(self):
        b = Branch()
        b.control(LoopDirective.BREAK, reason="hard stop")

        finally_ran = False

        with pytest.raises(LoopBreak) as exc_info:
            try:
                try:
                    _check_control(b)
                except _StopStream:
                    pytest.fail("BREAK must not be caught as _StopStream")
            finally:
                finally_ran = True

        assert exc_info.value.reason == "hard stop"
        assert finally_ran

    async def test_observer_queues_cancel_on_payload(self):
        from pydantic import BaseModel

        class _TestFinding(BaseModel):
            description: str

        s = Session()
        b = s.default_branch
        signals_seen: list = []

        @s.observe(_TestFinding)
        def on_finding(payload, session):
            signals_seen.append(payload)
            b.control(LoopDirective.CANCEL, reason="finding received, stopping")

        await b.emit(StructuredOutput(data=_TestFinding(description="critical bug found")))

        assert len(signals_seen) == 1
        ctrl = b.poll_control()
        assert ctrl is not None
        assert ctrl.directive is LoopDirective.CANCEL
        assert ctrl.reason == "finding received, stopping"

    async def test_cancel_after_payload_then_poll_is_one_shot(self):
        from pydantic import BaseModel

        class _TestEscalation(BaseModel):
            reason: str

        s = Session()
        b = s.default_branch

        @s.observe(_TestEscalation)
        def on_escalation(payload, session):
            b.control(LoopDirective.CANCEL)

        await b.emit(StructuredOutput(data=_TestEscalation(reason="blocker")))

        ctrl = b.poll_control()
        assert ctrl is not None
        assert ctrl.directive is LoopDirective.CANCEL
        assert b.poll_control() is None  # one-shot


# ---------------------------------------------------------------------------
# End-to-end integration — real run() loop with loop control
# ---------------------------------------------------------------------------


def _make_fake_cli_model_for_control(chunks: list[StreamChunk]):
    m = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    endpoint_ns = types.SimpleNamespace(
        is_cli=True,
        session_id=None,
        to_dict=lambda: {"type": "fake_cli", "session_id": None},
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


class TestRunLoopControlIntegration:
    async def test_cancel_stops_loop_cleanly_finally_runs(self):
        from lionagi.operations.run.run import RunParam, run

        sentinel = object()
        chunks = [
            StreamChunk(type="text", content="first"),
            StreamChunk(type="text", content="second"),
        ]
        model = _make_fake_cli_model_for_control(chunks)
        model.streaming_process_func = sentinel

        branch = Branch()
        branch.chat_model = model

        collected: list = []
        cancel_issued = False

        async for msg in run(branch, "go", RunParam()):
            collected.append(msg)
            if isinstance(msg, AssistantResponse) and not cancel_issued:
                cancel_issued = True
                branch.control(LoopDirective.CANCEL, reason="observer halt")

        assert model.streaming_process_func is sentinel

        assistant_msgs = [m for m in branch.messages if isinstance(m, AssistantResponse)]
        assert len(assistant_msgs) >= 1

    async def test_break_propagates_out_of_run_and_finally_still_runs(self):
        from lionagi.operations.run.run import RunParam, run

        sentinel = object()
        chunks = [
            StreamChunk(type="text", content="chunk-a"),
            StreamChunk(type="text", content="chunk-b"),
        ]
        model = _make_fake_cli_model_for_control(chunks)
        model.streaming_process_func = sentinel

        branch = Branch()
        branch.chat_model = model

        branch.control(LoopDirective.BREAK, reason="hard stop")

        loop_break_raised = False
        loop_break_reason = None
        try:
            async for _ in run(branch, "go", RunParam()):
                pass
        except LoopBreak as exc:
            loop_break_raised = True
            loop_break_reason = exc.reason

        assert loop_break_raised
        assert loop_break_reason == "hard stop"

        assert model.streaming_process_func is sentinel
