# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import logging

from pydantic import PrivateAttr

from lionagi.protocols.types import DataLogger, Event, EventStatus
from lionagi.service.hooks import HookEvent, HookEventTypes

_logger = logging.getLogger(__name__)

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
                    # An ordinary post-hook exception is recorded as ABORTED, not
                    # FAILED, so it does not match the check above and does not
                    # request exit. The core call has already produced its result
                    # by this point, so the failure is not fatal here — but it
                    # must not be silent either.
                    if h_ev.execution.status in (
                        EventStatus.FAILED,
                        EventStatus.CANCELLED,
                        EventStatus.ABORTED,
                    ):
                        _logger.warning(
                            "Post-invoke hook %s (result already produced): %s",
                            h_ev.execution.status.value,
                            h_ev.execution.error,
                        )
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
        """Run pre-hook, yield chunks from ``_core_stream()``, run post-hook (post failures only logged)."""
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

        async for chunk in self._core_stream():
            yield chunk

        # Post-stream hook failure: data already sent, must not reraise — log at WARNING only.
        if h_ev := self._post_invoke_hook_event:
            try:
                await h_ev.invoke()
            except Exception as _hook_exc:
                _logger.warning(
                    "Post-stream hook failed (data already sent): %s",
                    _hook_exc,
                    exc_info=True,
                )
            else:
                # An ordinary hook exception never escapes invoke(): it is
                # captured into the hook event's status, so the status is the
                # only signal that the hook failed.
                if h_ev.execution.status in (
                    EventStatus.FAILED,
                    EventStatus.CANCELLED,
                    EventStatus.ABORTED,
                ):
                    _logger.warning(
                        "Post-stream hook %s (data already sent): %s",
                        h_ev.execution.status.value,
                        h_ev.execution.error,
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
