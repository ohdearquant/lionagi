# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
import logging

import anyio
import sniffio
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

POST_STREAM_HOOK_STOP_GRACE = 1.0
"""Seconds a post-stream hook is given to stop after it has been cancelled.

A hook that swallows its own cancellation cannot be waited on forever, so once this
expires its task is abandoned: reported at WARNING and left running, rather than
dropped on the floor for the interpreter to complain about later.
"""

_abandoned_post_stream_hooks: set = set()
"""Post-stream hook tasks that did not stop when cancelled.

Held for the life of the process so a hook that outlived its cancellation is not
destroyed mid-await while the program is still running; each entry drops out on its own
if the hook ever finishes. At interpreter shutdown a still-pending one is released and
CPython reports it as destroyed while pending, which by then is an accurate description
of it.
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

        Guaranteed: the caller receives the very same exception object the stream ended
        with, not a replacement, no matter how the teardown fails — an ordinary
        exception, a cancellation the hook raises at itself, or a failure in the hook's
        own logging. A stream that ended normally still ends normally.

        Deliberately not guaranteed: a cancellation actually delivered to the consuming
        task while the teardown is running is not swallowed. It reaches the caller in
        place of whatever the stream ended with, because a task that was cancelled from
        outside must not come back believing it was not. On a stream that was itself
        ended by cancellation the source is re-raised instead, since the consumer stays
        cancelled either way. Off asyncio the two kinds of cancellation cannot be told
        apart at all and both propagate. ``KeyboardInterrupt`` and ``SystemExit`` raised
        inside the teardown also propagate — they are process-level directives rather
        than hook failures.

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
            try:
                await self._run_post_stream_hook(state)
            except (KeyboardInterrupt, SystemExit):
                raise
            except get_cancelled_exc_class():
                # A cancellation the hook raised at itself was already absorbed where it
                # could still be attributed. One reaching here was delivered to the
                # consuming task, so it is honoured -- except on a stream that ended in
                # cancellation, where re-raising the source leaves the consumer cancelled
                # anyway and keeps the exception it was handed.
                if state is not StreamTerminalState.Cancelled:
                    raise
                _logger.warning(
                    "Post-stream teardown was cancelled while the stream was ending "
                    "(%s); the stream's own ending is preserved",
                    state.value,
                )
            except BaseException as _teardown_exc:
                _logger.warning(
                    "Post-stream teardown failed while the stream was ending (%s): %s",
                    state.value,
                    _teardown_exc,
                    exc_info=True,
                )

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
            await self._invoke_post_stream_hook_isolated(h_ev)
            return

        with anyio.CancelScope(shield=True):
            with move_on_after(POST_STREAM_TEARDOWN_GRACE) as scope:
                await self._invoke_post_stream_hook_isolated(h_ev)
            if scope.cancelled_caught:
                _logger.warning(
                    "Post-stream hook did not finish within %ss while the stream was "
                    "being torn down (%s)",
                    POST_STREAM_TEARDOWN_GRACE,
                    state.value,
                )

    async def _invoke_post_stream_hook_isolated(self, h_ev: HookEvent) -> None:
        """Run the post-hook so a cancellation it raises cannot pass for the consumer's.

        Within one task a cancellation raised by the awaited code and one delivered to
        the task at that await are the same exception arriving at the same place.
        Shielding does not separate them, since a direct ``Task.cancel()`` reaches a
        shielded await anyway, and neither does asking afterwards whether the hook has
        finished: a hook finishing and a cancellation being delivered can be queued in
        the same loop turn, so that answer is a sample of a race, not a provenance.

        The collision is therefore removed rather than resolved. The hook runs in a child
        task that CAPTURES whatever ends it and RETURNS it instead of raising it, so that
        task can never end cancelled because of something the hook did. A cancellation
        surfacing at the await below then has exactly one possible origin -- delivery to
        this task -- and is honoured: the hook's task is cancelled, given
        ``POST_STREAM_HOOK_STOP_GRACE`` seconds to stop, and the cancellation propagates.
        A hook that will not stop within that grace is abandoned: it is reported at
        WARNING and left running, held so that it is not destroyed mid-await while the
        program is still going.

        A cancellation the hook raised at itself comes back as a returned value and is
        logged; anything else the hook ended with is re-raised for the caller to record,
        which keeps ``KeyboardInterrupt`` and ``SystemExit`` propagating.

        The backend is decided before any asyncio object exists, because an asyncio task
        or future is not awaitable on another backend and constructing one there fails at
        the await rather than at the construction. Off asyncio the hook runs inline: the
        two kinds of cancellation are genuinely indistinguishable there and both
        propagate.
        """
        try:
            backend = sniffio.current_async_library()
        except sniffio.AsyncLibraryNotFoundError:
            backend = None
        if backend != "asyncio":
            await self._invoke_post_stream_hook(h_ev)
            return

        child = asyncio.get_running_loop().create_task(self._capture_post_stream_hook(h_ev))
        try:
            hook_ended_with = await asyncio.shield(child)
        except get_cancelled_exc_class():
            await self._stop_post_stream_hook(child)
            raise

        if hook_ended_with is None:
            return
        if isinstance(hook_ended_with, get_cancelled_exc_class()):
            _logger.warning(
                "Post-stream hook cancelled itself (data already sent)",
            )
            return
        raise hook_ended_with

    async def _capture_post_stream_hook(self, h_ev: HookEvent) -> BaseException | None:
        """Run the post-hook and return whatever ended it instead of raising it.

        Returning the outcome is what makes this task's own ending unambiguous: it can
        complete, but it cannot end cancelled because of what the hook did.
        """
        try:
            await self._invoke_post_stream_hook(h_ev)
        except BaseException as e:
            return e
        return None

    async def _stop_post_stream_hook(self, child: asyncio.Task) -> None:
        """Cancel the hook's task and wait a bounded time for it to actually stop.

        Runs shielded, so the cancellation that got us here does not cut the wait short,
        and abandons the task with a warning if the hook outlasts the grace.
        """
        if child.done():
            return

        child.cancel()
        with anyio.CancelScope(shield=True):
            try:
                with move_on_after(POST_STREAM_HOOK_STOP_GRACE):
                    await asyncio.shield(child)
            finally:
                if not child.done():
                    _abandoned_post_stream_hooks.add(child)
                    child.add_done_callback(_abandoned_post_stream_hooks.discard)
                    _logger.warning(
                        "Post-stream hook did not stop within %ss of being cancelled; "
                        "it is left running and is not waited on again",
                        POST_STREAM_HOOK_STOP_GRACE,
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
