# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from pydantic import PrivateAttr

from lionagi.protocols.types import DataLogger, Event, EventStatus
from lionagi.service.hooks import HookEvent, HookEventTypes

global_hook_logger = DataLogger(
    persist_dir="./data/logs",
    subfolder="hooks",
    file_prefix="hook",
    capacity=100,
)


class HookedEvent(Event):
    """Event with pre/post invocation hooks using the template method pattern.

    HookedEvent sits between Event (lifecycle state machine) and concrete
    subclasses (e.g. APICalling).  It provides hook-orchestrating
    implementations of ``_invoke()`` and ``_stream()`` that are called by
    ``Event.invoke()`` / ``Event.stream()``.  Concrete subclasses override
    ``_core_invoke()`` / ``_core_stream()`` for their business logic.

    Lifecycle (non-streaming)::

        Event.invoke()          ← handles idempotency, status, timing, errors
          └─ HookedEvent._invoke()   ← pre-hook → _core_invoke() → post-hook
               └─ APICalling._core_invoke()  ← actual API call

    Lifecycle (streaming)::

        Event.stream()          ← handles idempotency, status, timing, errors
          └─ HookedEvent._stream()   ← pre-hook → _core_stream() → post-hook
               └─ APICalling._core_stream()  ← actual streaming call
    """

    _pre_invoke_hook_event: HookEvent = PrivateAttr(None)
    _post_invoke_hook_event: HookEvent = PrivateAttr(None)

    async def _core_invoke(self):
        """Business logic for non-streaming invocation.

        Override in subclasses (e.g. APICalling). Must return the response
        value that will be stored in ``self.execution.response``.

        Raises:
            NotImplementedError: If not overridden in a concrete subclass.
        """
        raise NotImplementedError("Override _core_invoke() in subclass.")

    async def _core_stream(self):
        """Business logic for streaming invocation.

        Override in subclasses (e.g. APICalling). Must be an async generator
        that yields chunks.

        Raises:
            NotImplementedError: If not overridden in a concrete subclass.
        """
        raise NotImplementedError("Override _core_stream() in subclass.")
        yield  # pragma: no cover -- makes this an async generator

    async def _invoke(self):
        """Hook-orchestrating invoke, called by ``Event.invoke()``.

        Runs the pre-invoke hook (if present), delegates to
        ``_core_invoke()``, then runs the post-invoke hook (if present).
        Hook failures or exit signals are raised as ``RuntimeError`` so
        that ``Event.invoke()`` records them as FAILED status.

        Returns:
            The value returned by ``_core_invoke()``.

        Raises:
            RuntimeError: If a hook fails, is cancelled, or signals exit.
        """
        # --- Pre-invoke hook ---
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

        # --- Core business logic ---
        core_error = None
        response = None
        try:
            response = await self._core_invoke()
        except BaseException as e:
            core_error = e

        # --- Post-invoke hook (runs even on _core_invoke failure) ---
        if h_ev := self._post_invoke_hook_event:
            try:
                await h_ev.invoke()
                if h_ev.execution.status in (EventStatus.FAILED, EventStatus.CANCELLED):
                    await global_hook_logger.alog(h_ev)
                    # Only raise hook error if core succeeded (don't shadow core error)
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
                    pass  # Don't shadow the original error
                else:
                    raise

        # Re-raise the original error from _core_invoke if any
        if core_error is not None:
            raise core_error

        return response

    async def _stream(self):
        """Hook-orchestrating stream, called by ``Event.stream()``.

        Runs the pre-invoke hook (if present), yields all chunks from
        ``_core_stream()``, then runs the post-invoke hook (if present).
        Pre-hook failures raise immediately; post-hook failures after data
        has been sent are only logged (to avoid corrupting a partial stream).

        Yields:
            Chunks produced by ``_core_stream()``.

        Raises:
            RuntimeError: If the pre-invoke hook fails or signals exit.
        """
        # --- Pre-invoke hook ---
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

        # --- Core streaming logic ---
        async for chunk in self._core_stream():
            yield chunk

        # --- Post-invoke hook (after stream completes) ---
        # Don't fail the stream after data was already sent — just log.
        if h_ev := self._post_invoke_hook_event:
            try:
                await h_ev.invoke()
            except BaseException:  # noqa: S110
                pass  # Don't fail after data already sent
            await global_hook_logger.alog(h_ev)

    def create_pre_invoke_hook(
        self,
        hook_registry,
        exit_hook: bool = None,
        hook_timeout: float = 30.0,
        hook_params: dict = None,
    ):
        """Attach a pre-invocation hook to this event.

        Args:
            hook_registry: Registry containing the pre-invocation hook.
            exit_hook: Whether hook failure should abort invocation.
            hook_timeout: Maximum seconds to wait for the hook.
            hook_params: Extra keyword arguments forwarded to the hook.
        """
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
        """Attach a post-invocation hook to this event.

        Args:
            hook_registry: Registry containing the post-invocation hook.
            exit_hook: Whether hook failure should abort completion.
            hook_timeout: Maximum seconds to wait for the hook.
            hook_params: Extra keyword arguments forwarded to the hook.
        """
        h_ev = HookEvent(
            hook_type=HookEventTypes.PostInvocation,
            event_like=self,
            registry=hook_registry,
            exit=exit_hook,
            timeout=hook_timeout,
            params=hook_params or {},
        )
        self._post_invoke_hook_event = h_ev
