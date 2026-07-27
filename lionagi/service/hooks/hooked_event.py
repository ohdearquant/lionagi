# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import logging

import anyio
from pydantic import PrivateAttr

from lionagi.ln.concurrency import get_cancelled_exc_class, move_on_after
from lionagi.protocols.types import DataLogger, Event, EventStatus
from lionagi.service.hooks import HookEvent, HookEventTypes

from ._types import StreamTerminalState

_logger = logging.getLogger(__name__)

POST_STREAM_TEARDOWN_GRACE = 5.0
"""Seconds a post-stream hook may take when the stream is being closed or cancelled.

Teardown on those paths runs shielded so it is not cut short by the cancellation that
caused it, but it must not stall the unwind either, so it is bounded by this grace.
"""

global_hook_logger = DataLogger(
    persist_dir="./data/logs",
    subfolder="hooks",
    file_prefix="hook",
    capacity=100,
)


class HookedEvent(Event):
    """Template-method mixin adding pre/post hooks around ``_core_invoke()`` / ``_core_stream()``."""

    _pre_invoke_hook_event: HookEvent = PrivateAttr(None)
    _post_invoke_hook_event: HookEvent = PrivateAttr(None)
    _stream_terminal_state: StreamTerminalState | None = PrivateAttr(None)

    @property
    def stream_terminal_state(self) -> StreamTerminalState | None:
        """How the current ``_stream()`` run ended, or None while it is still running.

        Set before the post-invocation hook is invoked, so a hook can tell a stream that
        completed from one that failed, was closed by its consumer, or was cancelled.
        """
        return self._stream_terminal_state

    async def _core_invoke(self):
        """Override in subclasses; return value is stored in ``self.execution.response``."""
        raise NotImplementedError("Override _core_invoke() in subclass.")

    async def _core_stream(self):
        """Override in subclasses; must be an async generator yielding chunks."""
        raise NotImplementedError("Override _core_stream() in subclass.")
        yield  # pragma: no cover -- makes this an async generator

    async def _invoke(self):
        """Run pre-hook, delegate to ``_core_invoke()``, run post-hook; hook failures raise RuntimeError."""
        if h_ev := self._pre_invoke_hook_event:
            await h_ev.invoke()
            if h_ev.execution.status in (EventStatus.FAILED, EventStatus.CANCELLED):
                raise RuntimeError(
                    f"Pre-invoke hook {h_ev.execution.status.value}: {h_ev.execution.error}"
                )
            if h_ev._should_exit:
                raise h_ev._exit_cause or RuntimeError(
                    "Pre-invocation hook requested exit without a cause"
                )
            await global_hook_logger.alog(h_ev)

        core_error = None
        response = None
        try:
            response = await self._core_invoke()
        except BaseException as e:
            core_error = e

        if h_ev := self._post_invoke_hook_event:
            try:
                await h_ev.invoke()
                if h_ev.execution.status in (EventStatus.FAILED, EventStatus.CANCELLED):
                    await global_hook_logger.alog(h_ev)
                    if core_error is None:
                        raise RuntimeError(
                            f"Post-invoke hook {h_ev.execution.status.value}: {h_ev.execution.error}"
                        )
                elif h_ev._should_exit:
                    if core_error is None:
                        raise h_ev._exit_cause or RuntimeError(
                            "Post-invocation hook requested exit without a cause"
                        )
                else:
                    await global_hook_logger.alog(h_ev)
            except BaseException:
                if core_error is not None:
                    pass
                else:
                    raise

        if core_error is not None:
            raise core_error

        return response

    async def _stream(self):
        """Run pre-hook, yield chunks from ``_core_stream()``, run post-hook.

        The post-hook runs however the stream ends — exhaustion, a source error, an
        early-stopping consumer, or cancellation — and ``stream_terminal_state`` says
        which of those it was. Whatever ended the stream still propagates unchanged;
        post-hook failures are logged, never raised.

        A consumer that stops early is responsible for closing the stream, with
        ``aclose()`` or ``contextlib.aclosing``. A bare ``break`` does not close the
        generator it was iterating, so teardown is deferred to whenever the interpreter
        finalizes the abandoned generator — it still runs, and still reports the closed
        state, but not at a point the consumer picked, and during interpreter or loop
        shutdown the grace bound below can cut it short.
        """
        if h_ev := self._pre_invoke_hook_event:
            await h_ev.invoke()
            if h_ev.execution.status in (EventStatus.FAILED, EventStatus.CANCELLED):
                raise RuntimeError(
                    f"Pre-invoke hook {h_ev.execution.status.value}: {h_ev.execution.error}"
                )
            if h_ev._should_exit:
                raise h_ev._exit_cause or RuntimeError(
                    "Pre-invocation hook requested exit without a cause"
                )
            await global_hook_logger.alog(h_ev)

        self._stream_terminal_state = None
        state = StreamTerminalState.Completed
        try:
            async for chunk in self._core_stream():
                yield chunk
        except GeneratorExit:
            # Raised at the yield above when the consumer stops early and the generator
            # is closed. Teardown may await, but must not yield, and must not stall.
            state = StreamTerminalState.Closed
            raise
        except get_cancelled_exc_class():
            state = StreamTerminalState.Cancelled
            raise
        except BaseException:
            state = StreamTerminalState.Failed
            raise
        finally:
            self._stream_terminal_state = state
            await self._run_post_stream_hook(state)

    async def _run_post_stream_hook(self, state: StreamTerminalState) -> None:
        """Invoke and log the post-invocation hook for a stream that ended in ``state``.

        Post-stream hook failure: data already sent, must not reraise — log at WARNING
        only. HookRegistry.post_invocation() records a handler's raised exception on the
        HookEvent (status FAILED/CANCELLED/ABORTED) rather than re-raising it out of
        invoke(), so the failure must be detected via status, not a try/except.
        """
        h_ev = self._post_invoke_hook_event
        if not h_ev:
            return

        # On the close and cancel paths the teardown is running inside an unwind that is
        # already cancelling: an unshielded await would be cancelled before the hook
        # could run, and would replace the exception in flight with a fresh one. Shield
        # it so the hook actually runs, and bound it so a slow hook cannot hold the
        # unwind open.
        unwinding = state in (
            StreamTerminalState.Closed,
            StreamTerminalState.Cancelled,
        )
        if not unwinding:
            await self._invoke_post_stream_hook(h_ev)
            return

        with anyio.CancelScope(shield=True):
            with move_on_after(POST_STREAM_TEARDOWN_GRACE) as scope:
                await self._invoke_post_stream_hook(h_ev)
            if scope.cancelled_caught:
                _logger.warning(
                    "Post-stream hook did not finish within %ss while the stream was "
                    "being torn down (%s)",
                    POST_STREAM_TEARDOWN_GRACE,
                    state.value,
                )

    async def _invoke_post_stream_hook(self, h_ev: HookEvent) -> None:
        try:
            await h_ev.invoke()
            if h_ev.execution.status in (
                EventStatus.FAILED,
                EventStatus.CANCELLED,
                EventStatus.ABORTED,
            ):
                _logger.warning(
                    "Post-stream hook failed (data already sent): %s",
                    h_ev.execution.error,
                )
        except Exception as _hook_exc:
            _logger.warning(
                "Post-stream hook failed (data already sent): %s",
                _hook_exc,
                exc_info=True,
            )
        await global_hook_logger.alog(h_ev)

    def create_pre_invoke_hook(
        self,
        hook_registry,
        exit_hook: bool = None,
        hook_timeout: float = 30.0,
        hook_params: dict = None,
    ):
        """Attach a PreInvocation HookEvent; hook failure aborts invocation when exit_hook is True."""
        h_ev = HookEvent(
            hook_type=HookEventTypes.PreInvocation,
            event_like=self,
            registry=hook_registry,
            exit=exit_hook,
            timeout=hook_timeout,
            params=hook_params or {},
        )
        self._pre_invoke_hook_event = h_ev

    def create_post_invoke_hook(
        self,
        hook_registry,
        exit_hook: bool = None,
        hook_timeout: float = 30.0,
        hook_params: dict = None,
    ):
        """Attach a PostInvocation HookEvent; runs even on core failure (post-stream failures are logged, not raised)."""
        h_ev = HookEvent(
            hook_type=HookEventTypes.PostInvocation,
            event_like=self,
            registry=hook_registry,
            exit=exit_hook,
            timeout=hook_timeout,
            params=hook_params or {},
        )
        self._post_invoke_hook_event = h_ev
