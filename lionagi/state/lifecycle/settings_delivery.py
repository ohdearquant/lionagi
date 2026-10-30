# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Build explicit settings-notification outcomes for durable delivery."""

from __future__ import annotations

import inspect
import logging
from contextvars import ContextVar
from typing import Any

from lionagi._settings import load_global_settings_layer

from .callbacks import RunTerminalEnvelope
from .notify_settings import (
    DurableRunRecorder,
    ResolvedNotifyHandler,
    build_handler,
    resolve_durable_global_config,
)
from .reconciler import DeliveryOutcome, PreparedDelivery

__all__ = ("prepare_current_global_delivery", "prepare_resolved_delivery")

logger = logging.getLogger(__name__)


def _record_global(
    recorder: DurableRunRecorder | None,
    *,
    ok: bool,
    exit_code: int | None,
    stderr_text: str | None,
    reason: str | None,
) -> str | None:
    if recorder is None:
        return None
    try:
        return recorder.record(
            "global", ok=ok, exit_code=exit_code, stderr_text=stderr_text, reason=reason
        )
    except Exception:  # noqa: BLE001 -- bookkeeping cannot recategorize delivery
        logger.warning("Could not record terminal notification outcome")
        return None


def prepare_resolved_delivery(
    resolved: ResolvedNotifyHandler,
    envelope: RunTerminalEnvelope,
    *,
    recorder: DurableRunRecorder | None = None,
    safe_logging: bool = False,
) -> PreparedDelivery | DeliveryOutcome:
    """Resolve a selected adapter without treating registry dispatch as success."""
    if resolved.filter_kinds is not None and envelope.entity.kind not in resolved.filter_kinds:
        return DeliveryOutcome.NOT_APPLICABLE
    if resolved.filter_ids is not None and envelope.entity.id not in resolved.filter_ids:
        return DeliveryOutcome.NOT_APPLICABLE

    if resolved.python_ref is not None:

        def invoke_python() -> Any:
            # Resolving a configured module can execute arbitrary import-time code;
            # the reconciler invokes this inside its tracked, bounded worker.
            def record_rejection(reason: str) -> None:
                _record_global(recorder, ok=False, exit_code=None, stderr_text=None, reason=reason)

            kwargs: dict[str, Any] = {}
            if recorder is not None:
                kwargs["on_build_failure"] = record_rejection
            if safe_logging:
                kwargs["safe_logging"] = True
            handler = build_handler(resolved, **kwargs)
            if handler is None:
                return DeliveryOutcome.FAILURE
            try:
                value = handler(envelope)
            except Exception:
                if not safe_logging:
                    raise
                logger.warning("notify.on_terminal global adapter: python_adapter_execution_failed")
                return DeliveryOutcome.FAILURE
            if inspect.isawaitable(value):

                async def finish() -> DeliveryOutcome:
                    try:
                        result = await value
                    except Exception:
                        if not safe_logging:
                            raise
                        logger.warning(
                            "notify.on_terminal global adapter: python_adapter_execution_failed"
                        )
                        return DeliveryOutcome.FAILURE
                    return DeliveryOutcome.FAILURE if result is False else DeliveryOutcome.SUCCESS

                return finish()
            return DeliveryOutcome.FAILURE if value is False else DeliveryOutcome.SUCCESS

        return PreparedDelivery(invoke_python)

    outcomes: ContextVar[list[bool] | None] = ContextVar("exec_delivery_outcomes", default=None)

    def record_exec(
        *, ok: bool, exit_code: int | None, stderr_text: str | None, reason: str | None = None
    ) -> str | None:
        current = outcomes.get()
        if current is not None:
            current.append(ok)
        return _record_global(
            recorder, ok=ok, exit_code=exit_code, stderr_text=stderr_text, reason=reason
        )

    def record_timeout() -> None:
        record_exec(ok=False, exit_code=None, stderr_text=None, reason="exec_adapter_timeout")

    def record_exec_failure(reason: str) -> None:
        record_exec(ok=False, exit_code=None, stderr_text=None, reason=reason)

    kwargs: dict[str, Any] = {"outcome_fn": record_exec}
    if safe_logging:
        kwargs["safe_logging"] = True
        kwargs["on_timeout"] = record_timeout
        kwargs["on_exec_failure"] = record_exec_failure
    handler = build_handler(resolved, **kwargs)
    if handler is None:
        return DeliveryOutcome.FAILURE

    async def invoke_exec() -> DeliveryOutcome:
        attempt_outcomes: list[bool] = []
        token = outcomes.set(attempt_outcomes)
        try:
            try:
                await handler(envelope)
            except Exception:
                if not safe_logging:
                    raise
                logger.warning("notify.on_terminal global adapter: exec_adapter_execution_failed")
                if not attempt_outcomes:
                    record_exec_failure("exec_adapter_execution_failed")
                return DeliveryOutcome.FAILURE
            if attempt_outcomes and attempt_outcomes[-1] is True:
                return DeliveryOutcome.SUCCESS
            return DeliveryOutcome.FAILURE
        finally:
            outcomes.reset(token)

    return PreparedDelivery(invoke_exec)


def prepare_current_global_delivery(
    envelope: RunTerminalEnvelope,
    *,
    recorder: DurableRunRecorder | None = None,
) -> PreparedDelivery:
    """Prepare a current-global attempt; the caller owns admission and ACKs.

    Settings I/O is deferred into the tracked delivery call. The caller must
    establish committed eligibility and bind any recorder to this entity.
    This function neither registers a consumer nor activates a delivery route.
    """
    if envelope.durable is not True:
        raise ValueError("global durable delivery requires a durable event")

    def deliver() -> Any:
        try:
            settings = load_global_settings_layer()
        except Exception:  # noqa: BLE001 -- settings failures leave delivery owed
            logger.warning("notify.on_terminal global configuration: settings_load_failed")
            _record_global(
                recorder, ok=False, exit_code=None, stderr_text=None, reason="settings_load_failed"
            )
            return DeliveryOutcome.FAILURE
        resolution = resolve_durable_global_config(settings)
        if resolution.reason is not None:
            _record_global(
                recorder, ok=False, exit_code=None, stderr_text=None, reason=resolution.reason
            )
            return DeliveryOutcome.FAILURE
        if resolution.handler is None:
            return DeliveryOutcome.NOT_APPLICABLE
        try:
            prepared = prepare_resolved_delivery(
                resolution.handler, envelope, recorder=recorder, safe_logging=True
            )
            return prepared.call() if isinstance(prepared, PreparedDelivery) else prepared
        except Exception:
            logger.warning("notify.on_terminal global adapter: adapter_preparation_failed")
            return DeliveryOutcome.FAILURE

    return PreparedDelivery(deliver)
