# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from lionagi.casts.capabilities import EscalationRequest, Finding
from lionagi.operations.run.run import _check_control, _StopStream
from lionagi.session.branch import Branch
from lionagi.session.control import LoopBreak, LoopControl, LoopDirective
from lionagi.session.session import Session
from lionagi.session.signal import StructuredOutput

# ---------------------------------------------------------------------------
# LoopDirective / LoopControl / LoopBreak construction
# ---------------------------------------------------------------------------


class TestControlTypes:
    def test_loop_directive_values(self):
        assert LoopDirective.CONTINUE == "continue"
        assert LoopDirective.CANCEL == "cancel"
        assert LoopDirective.BREAK == "break"

    def test_loop_directive_is_str(self):
        # str subclass so it can be used as a plain string
        assert isinstance(LoopDirective.BREAK, str)

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
        """_check_control calls poll_control, consuming the directive."""
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
        """
        Mirrors run()'s inner try/except _StopStream: pass with outer finally.
        CANCEL must not skip the finally block.
        """
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
                # mirrors `except _StopStream: pass` in run()
        finally:
            finally_ran = True

        assert stop_caught
        assert finally_ran

    async def test_break_propagates_through_inner_except(self):
        """
        BREAK raises LoopBreak, which is NOT caught by `except _StopStream`.
        It propagates out — but the finally still runs.
        """
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

    async def test_observer_queues_cancel_on_finding(self):
        """
        Observer firing on a Finding payload calls branch.control(CANCEL).
        The directive is then available to the run loop via poll_control.
        """
        s = Session()
        b = s.default_branch
        signals_seen: list = []

        @s.observe(Finding)
        def on_finding(payload, session):
            signals_seen.append(payload)
            b.control(LoopDirective.CANCEL, reason="finding received, stopping")

        await b.emit(StructuredOutput(data=Finding(description="critical bug found")))

        assert len(signals_seen) == 1
        ctrl = b.poll_control()
        assert ctrl is not None
        assert ctrl.directive is LoopDirective.CANCEL
        assert ctrl.reason == "finding received, stopping"

    async def test_cancel_after_finding_then_poll_is_one_shot(self):
        """poll_control() after observer sets CANCEL returns it once, then None."""
        s = Session()
        b = s.default_branch

        @s.observe(EscalationRequest)
        def on_escalation(payload, session):
            b.control(LoopDirective.CANCEL)

        await b.emit(StructuredOutput(data=EscalationRequest(reason="blocker")))

        ctrl = b.poll_control()
        assert ctrl is not None
        assert ctrl.directive is LoopDirective.CANCEL
        assert b.poll_control() is None  # one-shot
